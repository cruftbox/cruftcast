"""
Flask web panel for cruftcast, plus the podcast feed and episode files.
LAN and tailnet only; no authentication, by design (same as wireburst).
"""

import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, url_for

import db
import feed_generator
import pipeline
import scriptwriter
import synthesizer

log = logging.getLogger("cruftcast.web")

app = Flask(__name__)

# The voice dropdown: American edge-tts voices with lively or conversational
# tags (labels are Microsoft's own descriptions). Left out on purpose: the
# child voice (Ana), the "Rational" and "Authority" voices (Eric, Steffan,
# Christopher), the Multilingual duplicates, and all non-US voices.
VOICES = [
    ("en-US-AvaNeural", "Ava (female: expressive, caring, pleasant, friendly)"),
    ("en-US-EmmaNeural", "Emma (female: cheerful, clear, conversational)"),
    ("en-US-AriaNeural", "Aria (female: positive, confident)"),
    ("en-US-JennyNeural", "Jenny (female: friendly, considerate, comfort)"),
    ("en-US-MichelleNeural", "Michelle (female: friendly, pleasant)"),
    ("en-US-AndrewNeural", "Andrew (male: warm, confident, authentic, honest)"),
    ("en-US-BrianNeural", "Brian (male: approachable, casual, sincere)"),
    ("en-US-GuyNeural", "Guy (male: passion)"),
    ("en-US-RogerNeural", "Roger (male: lively)"),
]


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
        episodes = db.list_episodes(conn)
        runs = db.recent_runs(conn, 10)
    feed_url = settings["public_base_url"].rstrip("/") + "/rss.xml"
    return render_template(
        "index.html", episodes=episodes, runs=runs, status=pipeline.status,
        running=pipeline.is_running(), feed_url=feed_url, settings=settings,
        has_artwork=feed_generator.artwork_path() is not None,
        message=request.args.get("message"),
    )


@app.route("/generate", methods=["POST"])
def generate():
    started = pipeline.start_in_background("adhoc")
    message = "Started a new episode (last 24 hours)." if started else "A run is already in progress."
    return redirect(url_for("index", message=message))


@app.route("/runs/<int:run_id>")
def run_detail(run_id: int):
    with db.get_conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        abort(404)
    return render_template("run.html", run=run)


@app.route("/health")
def health():
    with db.get_conn() as conn:
        conn.execute("SELECT 1")
    return "ok"


# ── Settings ─────────────────────────────────────────────────────────────────

LIST_FIELDS = ("category_priority", "excluded_categories", "excluded_feeds")
INT_FIELDS = {
    "target_minutes": (1, 60), "min_minutes": (1, 60), "max_minutes": (1, 60),
    "max_story_seconds": (15, 600),
    "weblog_cap": (0, 20), "gap_ms": (0, 3000), "retention_days": (1, 365),
    "article_char_budget": (500, 50000),
}
TEXT_FIELDS = ("weblog_category", "voice", "rate", "pitch", "model", "effort",
               "opml_url", "public_base_url")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_settings_form(form) -> tuple[dict, list[str]]:
    """Validate the settings form. Returns (values, errors)."""
    values: dict = {}
    errors: list[str] = []

    run_time = form.get("run_time", "").strip()
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", run_time):
        values["run_time"] = run_time
    else:
        errors.append("Run time must be HH:MM (24-hour).")

    tz = form.get("timezone", "").strip()
    try:
        ZoneInfo(tz)
        values["timezone"] = tz
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone: {tz!r}")

    for name, (lo, hi) in INT_FIELDS.items():
        try:
            n = int(form.get(name, ""))
            if not lo <= n <= hi:
                raise ValueError
            values[name] = n
        except ValueError:
            errors.append(f"{name.replace('_', ' ').capitalize()} must be a whole number from {lo} to {hi}.")
    minutes = [values.get(k) for k in ("min_minutes", "target_minutes", "max_minutes")]
    if None not in minutes and not minutes[0] <= minutes[1] <= minutes[2]:
        errors.append("Minutes must satisfy minimum ≤ target ≤ maximum.")

    for name in LIST_FIELDS:
        values[name] = _lines(form.get(name, ""))

    exclusions: dict[str, list[str]] = {}
    for line in _lines(form.get("topic_exclusions", "")):
        category, sep, topic = line.partition(":")
        if not sep or not category.strip() or not topic.strip():
            errors.append(f"Topic exclusion lines must look like 'Category: topic' (got {line!r}).")
            continue
        exclusions.setdefault(category.strip(), []).append(topic.strip())
    values["topic_exclusions"] = exclusions

    # "word" alone gets a respelling from Claude on save; "word = respelling" sets one by hand.
    pronunciations: dict[str, str] = {}
    for line in _lines(form.get("pronunciations", "")):
        word, _, spoken = line.partition("=")
        if not word.strip():
            errors.append(f"Pronunciation lines must start with a word (got {line!r}).")
            continue
        pronunciations[word.strip()] = spoken.strip()
    values["pronunciations"] = pronunciations

    for name in TEXT_FIELDS:
        values[name] = form.get(name, "").strip()
    values["chime"] = form.get("chime") == "on"
    if not values["voice"]:
        errors.append("Voice is required.")
    if not re.fullmatch(r"[+-]\d{1,3}%", values["rate"]):
        errors.append("Rate must look like +0% or -10%.")
    if not re.fullmatch(r"[+-]\d{1,3}Hz", values["pitch"]):
        errors.append("Pitch must look like +0Hz or -5Hz.")
    if values["effort"] not in EFFORTS:
        errors.append(f"Effort must be one of {', '.join(EFFORTS)}.")
    for name, label in (("opml_url", "OPML URL"), ("public_base_url", "Public base URL")):
        if not values[name].startswith(("http://", "https://")):
            errors.append(f"{label} must start with http:// or https://.")
    return values, errors


def fill_respellings(new: dict[str, str], old: dict[str, str], settings: dict) -> tuple[dict, list[str]]:
    """
    Give every word without a respelling one: the saved one if the word was
    already listed, else Claude's. A Claude failure leaves the word blank
    (spoken as before) and returns a warning instead of blocking the save.
    """
    old_by_lower = {w.lower(): spoken for w, spoken in old.items()}
    filled, warnings = {}, []
    for word, spoken in new.items():
        spoken = spoken or old_by_lower.get(word.lower(), "")
        if not spoken:
            try:
                spoken = scriptwriter.respell_word(word, settings)
            except scriptwriter.ScriptError as exc:
                warnings.append(f"No respelling for {word!r} yet: {exc}")
        filled[word] = spoken
    return filled, warnings


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    errors: list[str] = []
    warnings: list[str] = []
    saved = False
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    if request.method == "POST":
        values, errors = parse_settings_form(request.form)
        if errors:
            settings.update(values)  # redisplay what was typed
        else:
            values["pronunciations"], warnings = fill_respellings(
                values["pronunciations"], settings["pronunciations"], {**settings, **values})
            with db.get_conn() as conn:
                db.save_settings(conn, values)
                settings = db.get_settings(conn)
            saved = True
    if request.args.get("error"):
        warnings.append(request.args["error"])
    exclusion_text = "\n".join(f"{cat}: {topic}" for cat, topics in settings["topic_exclusions"].items()
                               for topic in topics)
    now_local = datetime.now(ZoneInfo(settings["timezone"])) if not errors else None
    return render_template("settings.html", s=settings, errors=errors, warnings=warnings, saved=saved,
                           exclusion_text=exclusion_text, voices=VOICES,
                           efforts=EFFORTS, now_local=now_local)


@app.route("/pronunciations/retry", methods=["POST"])
def retry_pronunciation():
    """Replace one word's respelling with a different one from Claude."""
    word = request.form.get("word", "")
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    pronunciations = dict(settings["pronunciations"])
    if word not in pronunciations:
        abort(404)
    try:
        pronunciations[word] = scriptwriter.respell_word(word, settings, rejected=pronunciations[word])
    except scriptwriter.ScriptError as exc:
        return redirect(url_for("settings_page", error=f"Retry for {word!r} failed: {exc}") + "#pronunciations")
    with db.get_conn() as conn:
        db.save_settings(conn, {"pronunciations": pronunciations})
    return redirect(url_for("settings_page") + "#pronunciations")


PREVIEW_TEXT = "{w}. Today, {w} made the news, and {w}'s announcement was widely read."


@app.route("/pronunciations/preview.mp3")
def preview_pronunciation():
    """The word in a short sentence, in the current voice with respellings applied."""
    word = request.args.get("word", "").strip()
    if not word:
        abort(400)
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "preview.mp3"
        try:
            synthesizer.speak_once(PREVIEW_TEXT.format(w=word), settings, out)
        except synthesizer.SynthesisError as exc:
            return Response(str(exc), status=502, mimetype="text/plain")
        return Response(out.read_bytes(), mimetype="audio/mpeg")


# ── Feed and files ───────────────────────────────────────────────────────────

@app.route("/rss.xml")
def rss():
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
        episodes = db.list_episodes(conn)
    xml = feed_generator.build_rss(episodes, settings["public_base_url"])
    return Response(xml, mimetype="application/rss+xml")


@app.route("/episodes/<path:filename>")
def episode_file(filename: str):
    return send_from_directory(db.EPISODES_DIR.resolve(), filename, mimetype="audio/mpeg")


@app.route("/<any('artwork.jpg', 'artwork.jpeg', 'artwork.png'):name>")
def artwork(name: str):
    return send_from_directory(db.DATA_DIR.resolve(), name)
