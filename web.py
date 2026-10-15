"""
Flask web panel for cruftcast, plus the podcast feed and episode files.
LAN and tailnet only; no authentication, by design (same as wireburst).
"""

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import edge_tts
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, url_for

import db
import feed_generator
import pipeline

log = logging.getLogger("cruftcast.web")

app = Flask(__name__)

_voice_cache: list[str] = []


def _english_voices() -> list[str]:
    """Voice names for the settings form's suggestions; empty if edge-tts is unreachable."""
    if not _voice_cache:
        try:
            voices = asyncio.run(edge_tts.list_voices())
            _voice_cache.extend(sorted(v["ShortName"] for v in voices
                                       if v["Locale"].startswith("en-")))
        except Exception as exc:
            log.warning("Could not list edge-tts voices: %s", exc)
    return _voice_cache


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
    "weblog_cap": (0, 20), "gap_ms": (0, 3000), "retention_days": (1, 365),
    "article_char_budget": (500, 50000),
}
TEXT_FIELDS = ("weblog_category", "lead_voice", "second_voice", "lead_rate", "lead_pitch",
               "second_rate", "second_pitch", "model", "effort", "opml_url", "public_base_url")
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

    for name in TEXT_FIELDS:
        values[name] = form.get(name, "").strip()
    for name in ("lead_rate", "second_rate"):
        if not re.fullmatch(r"[+-]\d{1,3}%", values[name]):
            errors.append(f"{name.replace('_', ' ').capitalize()} must look like +0% or -10%.")
    for name in ("lead_pitch", "second_pitch"):
        if not re.fullmatch(r"[+-]\d{1,3}Hz", values[name]):
            errors.append(f"{name.replace('_', ' ').capitalize()} must look like +0Hz or -5Hz.")
    if values["effort"] not in EFFORTS:
        errors.append(f"Effort must be one of {', '.join(EFFORTS)}.")
    for name, label in (("opml_url", "OPML URL"), ("public_base_url", "Public base URL")):
        if not values[name].startswith(("http://", "https://")):
            errors.append(f"{label} must start with http:// or https://.")
    return values, errors


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    errors: list[str] = []
    saved = False
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    if request.method == "POST":
        values, errors = parse_settings_form(request.form)
        if errors:
            settings.update(values)  # redisplay what was typed
        else:
            with db.get_conn() as conn:
                db.save_settings(conn, values)
                settings = db.get_settings(conn)
            saved = True
    exclusion_text = "\n".join(f"{cat}: {topic}" for cat, topics in settings["topic_exclusions"].items()
                               for topic in topics)
    now_local = datetime.now(ZoneInfo(settings["timezone"])) if not errors else None
    return render_template("settings.html", s=settings, errors=errors, saved=saved,
                           exclusion_text=exclusion_text, voices=_english_voices(),
                           efforts=EFFORTS, now_local=now_local)


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
