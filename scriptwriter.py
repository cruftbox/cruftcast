"""
Claude calls: choosing the stories, then writing the script.

Selection sees only titles and short excerpts for every candidate, because the
news feeds alone produce hundreds of items a day. Full articles are fetched
only for the chosen stories and go to the second call, which writes the script.
Both calls return JSON constrained by a schema (structured outputs).
"""

import json
import logging
import os
from dataclasses import dataclass

import anthropic

from ingest import Candidate, truncate

log = logging.getLogger("cruftcast.scriptwriter")

WORDS_PER_MINUTE = 150
WORDS_PER_STORY = 180       # rough spoken length of one story segment
EXCERPT_CHARS = 400
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ScriptError(Exception):
    pass


@dataclass
class Segment:
    topic: str
    items: list[Candidate]


# ── Claude helper ────────────────────────────────────────────────────────────

def _client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ScriptError("ANTHROPIC_API_KEY is not set in .env")
    return anthropic.Anthropic()


def _call_json(settings: dict, system: str, user: str, schema: dict) -> dict:
    """
    One structured-output request. `fallbacks: "default"` lets the API re-run
    a policy-declined request on Anthropic's recommended fallback model; news
    about violence or security can trip a classifier.
    """
    client = _client()
    try:
        response = client.beta.messages.create(
            model=settings["model"],
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={
                "effort": settings["effort"],
                "format": {"type": "json_schema", "schema": schema},
            },
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError as exc:
        raise ScriptError(f"Anthropic rejected the API key: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise ScriptError(f"Anthropic rate limit: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise ScriptError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ScriptError(f"Could not reach the Anthropic API: {exc}") from exc

    usage = response.usage
    log.info("Claude %s: %s in / %s out tokens, stop=%s, request=%s",
             response.model, usage.input_tokens, usage.output_tokens,
             response.stop_reason, response._request_id)

    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise ScriptError(f"Claude declined the request (category: {category})")
    if response.stop_reason == "max_tokens":
        raise ScriptError("Claude's response was cut off at max_tokens")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Claude returned invalid JSON: {exc}") from exc


# ── Budget ───────────────────────────────────────────────────────────────────

def word_budget(settings: dict) -> tuple[int, int]:
    """Target and maximum spoken words for the episode."""
    return (settings["target_minutes"] * WORDS_PER_MINUTE,
            settings["max_minutes"] * WORDS_PER_MINUTE)


# ── Selection ────────────────────────────────────────────────────────────────

SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["topic", "item_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["segments"],
    "additionalProperties": False,
}

SELECTION_SYSTEM = """\
You choose the stories for cruftcast, a short private daily news podcast for one listener.
You receive candidate items from the listener's RSS feeds, grouped by category, and return
the stories to cover.

How to choose:
- Category priority decides what makes the cut. Fill from the highest-priority category first,
  then the next. Categories not listed in the priority order are equal to each other and come last.
- Drop any item that matches a topic exclusion for its category. Exclusions are topics, not
  keywords: judge what the item is about.
- When several items cover the same story, put them in one segment (list all their ids).
- Skip items with no news or informational value: sponsorships, deals and sales posts,
  housekeeping posts, podcast or video announcements with no content.
- Return no more than the maximum number of stories. On a slow day return fewer; never add
  weak items to reach a length.
- `topic` is a few words naming the story, for logs only."""


WILDCARD = "*"


def expand_exclusions(topic_exclusions: dict[str, list[str]],
                      categories) -> dict[str, list[str]]:
    """
    Per-category topic exclusions, with the "*" entry added to every
    category. Done here rather than explained to Claude so the rule is exact;
    "*" is used because wireburst has a real category named "All".
    """
    everywhere = topic_exclusions.get(WILDCARD, [])
    result = {}
    for category in categories:
        topics = topic_exclusions.get(category, []) + everywhere
        if topics:
            result[category] = topics
    return result


def select_stories(candidates: list[Candidate], settings: dict) -> list[Segment]:
    if not candidates:
        return []
    target_words, _ = word_budget(settings)
    max_stories = max(1, round(target_words / WORDS_PER_STORY))

    by_category: dict[str, list[dict]] = {}
    for c in candidates:
        by_category.setdefault(c.category, []).append({
            "id": c.id,
            "source": c.feed_title,
            "title": c.title,
            "excerpt": truncate(c.text, EXCERPT_CHARS),
        })

    exclusions = expand_exclusions(settings["topic_exclusions"], by_category)
    user = (
        f"Maximum number of stories: {max_stories}\n"
        f"Category priority (highest first): {json.dumps(settings['category_priority'])}\n"
        f"Topic exclusions by category: {json.dumps(exclusions)}\n\n"
        f"Candidates by category:\n{json.dumps(by_category, ensure_ascii=False, indent=1)}"
    )
    data = _call_json(settings, SELECTION_SYSTEM, user, SELECTION_SCHEMA)

    by_id = {c.id: c for c in candidates}
    segments: list[Segment] = []
    used: set[int] = set()
    for seg in data["segments"][:max_stories]:
        items = [by_id[i] for i in seg["item_ids"] if i in by_id and i not in used]
        if items:
            used.update(c.id for c in items)
            segments.append(Segment(topic=seg["topic"], items=items))
    log.info("Selected %d stories from %d candidates: %s",
             len(segments), len(candidates), "; ".join(s.topic for s in segments))
    return segments


# ── Script ───────────────────────────────────────────────────────────────────

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {"type": "array", "items": {"type": "string"}},
        "sign_off": {"type": "string"},
    },
    "required": ["stories", "sign_off"],
    "additionalProperties": False,
}

SCRIPT_SYSTEM = """\
You write the script for cruftcast, a short private daily news podcast for one listener, read
by a single text-to-speech voice. There is one host and no dialogue.

Return one entry in `stories` per story, in the order they should be read, and a one-line
`sign_off`. A chime plays between stories, so each story stands on its own: no transitions
like "next up" or "moving on", and no references to other stories.

Content:
- Mainly summaries. Add a brief opinion only when it gives the listener information they
  would not otherwise get. No filler enthusiasm.
- Name the source of each story: the site, or the author for personal weblogs.
- When a story has several sources covering it, combine them into one telling and mention
  each source.
- Some weblogs had more posts than are covered. When covering one of those blogs, say briefly
  that it posted more (for example, "Simon Willison posted four more times since yesterday").
- Mix the stories up; do not group them by category.
- The opening line ("This is cruftcast for ...") is added separately. Start directly with the
  first story.

Writing for the ear:
- Short, clear sentences in plain paragraphs.
- No URLs, markdown, lists, emoji, or stage directions. Plain spoken English only.
- Write numbers, dates, and abbreviations the way a person would say them when a
  speech engine might misread them.

Length: aim for the target word count across all stories. If the material is thin, a shorter
script is fine; do not pad. Never exceed the maximum."""


def write_script(segments: list[Segment], extras: dict[str, int],
                 settings: dict) -> tuple[list[str], str]:
    """Returns the story texts in reading order and the sign-off line."""
    target_words, max_words = word_budget(settings)
    stories = []
    for seg in segments:
        stories.append({
            "sources": [
                {"source": c.feed_title, "category": c.category,
                 "title": c.title, "text": c.article or c.text}
                for c in seg.items
            ],
        })
    extra_note = {name: n for name, n in extras.items()
                  if any(c.feed_title == name for s in segments for c in s.items)}
    user = (
        f"Target words: {target_words}. Maximum words: {max_words}.\n"
        f"Weblogs with more posts than covered (blog: extra posts): {json.dumps(extra_note)}\n\n"
        f"Stories:\n{json.dumps(stories, ensure_ascii=False, indent=1)}"
    )
    data = _call_json(settings, SCRIPT_SYSTEM, user, SCRIPT_SCHEMA)
    stories = [s.strip() for s in data["stories"] if s.strip()]
    if not stories:
        raise ScriptError("Claude returned an empty script")
    sign_off = data["sign_off"].strip() or "That's cruftcast for today."
    words = sum(len(s.split()) for s in stories) + len(sign_off.split())
    log.info("Script: %d stories, %d words", len(stories), words)
    return stories, sign_off
