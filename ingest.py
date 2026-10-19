"""
Gathering stories: the feed list from wireburst's OPML export, feed polling,
the time window, the per-weblog cap, and full-article fetching.

Nothing here talks to Claude. The output is a list of Candidate objects that
scriptwriter.py chooses from.
"""

import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape

import feedparser
import trafilatura

import db
from opml import parse_opml

log = logging.getLogger("cruftcast.ingest")

FETCH_TIMEOUT = 30          # seconds per socket operation
USER_AGENT = "cruftcast/1.0 (+private podcast generator)"
FULL_TEXT_THRESHOLD = 2000  # feed text shorter than this gets the linked article fetched
POLL_WORKERS = 8


class _TimeoutHandler(urllib.request.BaseHandler):
    """
    Applies FETCH_TIMEOUT to every request feedparser makes. Copied from
    wireburst, where a single stalled feed once hung polling for days:
    feedparser.parse() has no timeout parameter and its own urllib opener
    otherwise waits forever.
    """

    def http_request(self, req):
        req.timeout = FETCH_TIMEOUT
        return req

    https_request = http_request


@dataclass
class Feed:
    url: str
    title: str
    category: str


@dataclass
class Candidate:
    """One feed item that falls inside the run's window."""
    id: int                 # index in the run's candidate list; Claude refers to items by it
    feed_url: str
    feed_title: str
    category: str
    guid: str
    title: str
    link: str
    published: datetime     # publish time, or first-seen time for undated items
    text: str               # plain text from the feed
    article: str = ""       # filled in by fetch_articles for selected items only

    @property
    def key(self) -> tuple[str, str]:
        return (self.feed_url, self.guid)


class IngestError(Exception):
    pass


# ── Feed list ────────────────────────────────────────────────────────────────

def feed_list_path():
    """
    The saved feed list: the last good download from the OPML URL, or an
    uploaded file. With no URL set it is the only feed list, and nothing
    overwrites it.
    """
    return db.DATA_DIR / "feeds-cache.opml"


def save_feed_list(content: bytes, source: str, name: str = "") -> list:
    """
    Validate OPML and store it as the feed list. Raises ValueError, without
    touching the saved copy, if it doesn't parse or lists no feeds.
    `source` is "url" or "upload"; the settings page shows it.
    """
    try:
        feeds = parse_opml(content)
    except ET.ParseError as exc:
        raise ValueError(f"not valid XML ({exc})") from exc
    if not feeds:
        raise ValueError("it lists no feeds")
    feed_list_path().write_bytes(content)
    with db.get_conn() as conn:
        db.set_state(conn, "feed_list", {"source": source, "name": name, "at": db.now_iso()})
    return feeds


def feed_list_summary() -> dict | None:
    """Where the saved feed list came from, its feed count, and feeds per category."""
    cache = feed_list_path()
    if not cache.exists():
        return None
    with db.get_conn() as conn:
        origin = db.get_state(conn, "feed_list") or {}
    try:
        feeds = parse_opml(cache.read_bytes())
    except Exception as exc:
        return {**origin, "error": str(exc), "count": 0, "categories": {}}
    categories: dict[str, int] = {}
    for f in feeds:
        name = f.category or "Uncategorized"
        categories[name] = categories.get(name, 0) + 1
    return {**origin, "count": len(feeds), "categories": categories}


def load_feeds(settings: dict) -> list[Feed]:
    """
    Fetch the OPML, falling back to the last good copy, and apply exclusions.
    The cache lives next to the database so a wireburst outage doesn't stop
    the episode. With no URL, the saved copy (usually an upload) is used.
    """
    cache = feed_list_path()
    url = settings["opml_url"]
    content = None
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                content = resp.read()
            save_feed_list(content, "url", url)
        except Exception as exc:
            log.warning("OPML fetch from %s failed (%s); using cached copy", url, exc)
            content = None
    if content is None:
        if not cache.exists():
            if url:
                raise IngestError(f"Could not fetch the feed list from {url} and no cached copy exists")
            raise IngestError("No feed list. Set an OPML URL or upload an OPML file in Settings.")
        content = cache.read_bytes()

    excluded_cats = {c.lower() for c in settings["excluded_categories"]}
    excluded_feeds = {u.strip() for u in settings["excluded_feeds"]}
    feeds = []
    for f in parse_opml(content):
        category = f.category or "Uncategorized"
        if category.lower() in excluded_cats or f.url in excluded_feeds:
            continue
        feeds.append(Feed(url=f.url, title=f.title, category=category))
    log.info("Feed list: %d feeds after exclusions", len(feeds))
    return feeds


# ── Polling ──────────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """Strip HTML, keeping paragraph breaks. Adapted from wireburst's _truncate."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</div>|</li>|</blockquote>|</h\d>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _entry_text(entry) -> str:
    """Prefer full content over the summary, as wireburst does."""
    content = entry.get("content")
    body = content[0].get("value", "") if content else ""
    return html_to_text(body or entry.get("summary", ""))


def _entry_time(entry) -> datetime | None:
    # feedparser normalizes these to UTC struct_time.
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _fetch_feed(feed: Feed):
    try:
        parsed = feedparser.parse(
            feed.url, agent=USER_AGENT, handlers=[_TimeoutHandler()]
        )
    except Exception as exc:
        log.warning("Error fetching %s: %s", feed.url, exc)
        return feed, []
    if parsed.get("bozo") and not parsed.entries:
        log.warning("Unreadable feed %s: %s", feed.url, parsed.get("bozo_exception"))
        return feed, []
    return feed, parsed.entries


def poll_feeds(feeds: list[Feed], window_start: datetime, window_end: datetime,
               skip_used: bool) -> list[Candidate]:
    """
    Poll every feed in parallel and return items inside the window.

    Every item is recorded in the items table the first time it is seen, which
    gives undated items a usable time. Scheduled runs skip items already used
    in an earlier episode; ad hoc runs don't.
    """
    with ThreadPoolExecutor(max_workers=POLL_WORKERS) as pool:
        results = list(pool.map(_fetch_feed, feeds))

    failed = sum(1 for _, entries in results if not entries)
    if feeds and failed == len(feeds):
        raise IngestError(f"All {len(feeds)} feeds failed to load")

    candidates: list[Candidate] = []
    with db.get_conn() as conn:
        for feed, entries in results:
            for entry in entries:
                guid = entry.get("id") or entry.get("link") or ""
                if not guid:
                    continue
                row = db.record_seen(conn, feed.url, guid)
                if skip_used and row["used_in"] is not None:
                    continue
                published = _entry_time(entry) or datetime.fromisoformat(row["first_seen"])
                if not (window_start < published <= window_end):
                    continue
                candidates.append(Candidate(
                    id=len(candidates),
                    feed_url=feed.url,
                    feed_title=feed.title,
                    category=feed.category,
                    guid=guid,
                    title=html_to_text(entry.get("title", "")) or "(untitled)",
                    link=entry.get("link", ""),
                    published=published,
                    text=_entry_text(entry),
                ))
    log.info("Polled %d feeds (%d empty or failed); %d items in window",
             len(feeds), failed, len(candidates))
    return candidates


# ── Weblog cap ───────────────────────────────────────────────────────────────

def apply_weblog_cap(candidates: list[Candidate], weblog_category: str,
                     cap: int) -> tuple[list[Candidate], dict[str, int]]:
    """
    Keep each weblog's `cap` newest items. Returns the kept candidates (ids
    renumbered) and, per weblog, how many posts were left out so the hosts
    can mention them.
    """
    by_feed: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.category == weblog_category:
            by_feed.setdefault(c.feed_url, []).append(c)

    dropped: set[int] = set()
    extras: dict[str, int] = {}
    for items in by_feed.values():
        items.sort(key=lambda c: c.published, reverse=True)
        for c in items[cap:]:
            dropped.add(c.id)
        if len(items) > cap:
            extras[items[0].feed_title] = len(items) - cap

    kept = [c for c in candidates if c.id not in dropped]
    for i, c in enumerate(kept):
        c.id = i
    return kept, extras


# ── Full articles ────────────────────────────────────────────────────────────

def _fetch_article(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            html = resp.read(5_000_000).decode("utf-8", errors="replace")
        return trafilatura.extract(html, include_comments=False) or ""
    except Exception as exc:
        log.info("Article fetch failed for %s: %s", url, exc)
        return ""


def fetch_articles(candidates: list[Candidate], budget: int):
    """
    Fill in `article` for each candidate. Feeds that already carry the full
    post (most weblogs) are used as is. Short feed text (link posts, teasers)
    gets the linked page fetched; the blogger's own words are kept in front of
    the fetched article because they are often the point of the post. Falls
    back to the feed text when the fetch fails or the page is paywalled.
    """
    def work(c: Candidate):
        if len(c.text) >= FULL_TEXT_THRESHOLD:
            c.article = truncate(c.text, budget)
            return
        fetched = _fetch_article(c.link)
        if fetched and len(fetched) > len(c.text):
            combined = f"{c.text}\n\nLinked article:\n{fetched}" if c.text else fetched
            c.article = truncate(combined, budget)
        else:
            c.article = truncate(c.text, budget)

    with ThreadPoolExecutor(max_workers=POLL_WORKERS) as pool:
        list(pool.map(work, candidates))
