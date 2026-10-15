"""
One cruftcast run, end to end: feeds → selection → articles → script → audio
→ episode. Used by the daily scheduler and by the web panel's Generate now.

Only one run happens at a time. If any stage fails, a short failure episode
is published so the problem shows up in the podcast app: first a spoken
message naming the stage and error, and if edge-tts itself is down, the
pre-rendered clip for that stage from failure_audio/.
"""

import logging
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import db
import feed_generator
import ingest
import scriptwriter
import synthesizer

log = logging.getLogger("cruftcast.pipeline")

FAILURE_AUDIO_DIR = Path(__file__).parent / "failure_audio"

# What the failure message says for each stage. tools/render_failure_audio.py
# renders the same text into failure_audio/<stage>.mp3.
STAGE_PHRASES = {
    "feeds": "loading the feeds",
    "script": "writing the script with Claude",
    "articles": "fetching the articles",
    "voice": "generating the voices",
    "publish": "publishing the episode",
    "unknown": "running",
}

_lock = threading.Lock()
status = {"running": False, "kind": None, "stage": None, "started_at": None}


class _RunLog(logging.Handler):
    """Collects this run's log lines for the runs table and the web panel."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.lines.append(self.format(record))


def is_running() -> bool:
    return _lock.locked()


def start_in_background(kind: str) -> bool:
    """Start a run on a thread. Returns False if one is already running."""
    if _lock.locked():
        return False
    threading.Thread(target=run, args=(kind,), daemon=True, name=f"run-{kind}").start()
    return True


def run(kind: str) -> int | None:
    """Run the pipeline. `kind` is "scheduled" or "adhoc". Returns the run id."""
    if not _lock.acquire(blocking=False):
        log.info("A run is already in progress; skipping %s run", kind)
        return None
    try:
        return _run(kind)
    finally:
        status.update(running=False, kind=None, stage=None, started_at=None)
        _lock.release()


def spoken_date(local: datetime) -> str:
    return f"{local:%A}, {local:%B} {local.day}"


def opening_line(local: datetime, count: int) -> str:
    if count == 1:
        return f"This is cruftcast for {spoken_date(local)}. There is 1 item today."
    return f"This is cruftcast for {spoken_date(local)}. There are {count} items today."


def episode_title(local: datetime, kind: str, failed: bool = False) -> str:
    title = f"cruftcast for {spoken_date(local)}, {local.year}"
    if kind == "adhoc":
        title += " (extra)"
    if failed:
        title += " (failed)"
    return title


def _run(kind: str) -> int:
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings["timezone"]))
    with db.get_conn() as conn:
        run_id = db.start_run(conn, kind, local.date().isoformat())

    handler = _RunLog()
    root = logging.getLogger("cruftcast")
    root.addHandler(handler)
    status.update(running=True, kind=kind, stage="feeds", started_at=now.isoformat())
    stage = "feeds"

    def enter(name: str):
        nonlocal stage
        stage = name
        status["stage"] = name
        with db.get_conn() as conn:
            db.update_run(conn, run_id, stage=name)

    stem = f"cruftcast-{local:%Y-%m-%d-%H%M%S}" + ("-extra" if kind == "adhoc" else "")
    try:
        log.info("Starting %s run", kind)
        enter("feeds")
        if kind == "scheduled":
            with db.get_conn() as conn:
                last_end = db.last_scheduled_window_end(conn)
            window_start = datetime.fromisoformat(last_end) if last_end else now - timedelta(hours=24)
        else:
            window_start = now - timedelta(hours=24)
        log.info("Window: %s to %s", window_start.isoformat(timespec="minutes"),
                 now.isoformat(timespec="minutes"))
        feeds = ingest.load_feeds(settings)
        candidates = ingest.poll_feeds(feeds, window_start, now, skip_used=(kind == "scheduled"))
        candidates, extras = ingest.apply_weblog_cap(
            candidates, settings["weblog_category"], settings["weblog_cap"])

        out = db.EPISODES_DIR / f"{stem}.mp3"
        part = out.with_suffix(".part.mp3")
        if candidates:
            enter("script")
            segments = scriptwriter.select_stories(candidates, settings)
        else:
            segments = []

        if segments:
            enter("articles")
            ingest.fetch_articles([c for s in segments for c in s.items],
                                  settings["article_char_budget"])
            enter("script")
            stories, sign_off = scriptwriter.write_script(segments, extras, settings)
            enter("voice")
            synthesizer.synthesize(opening_line(local, len(stories)), stories, sign_off,
                                   settings, part)
        else:
            log.info("No stories today; publishing a short no-news episode")
            enter("voice")
            synthesizer.speak_once(
                f"This is cruftcast for {spoken_date(local)}. There are no new items today.",
                settings, part)

        enter("publish")
        part.replace(out)
        with db.get_conn() as conn:
            episode_id = db.add_episode(
                conn, kind=kind, status="ok", title=episode_title(local, kind),
                guid=f"{stem}-{now:%Y%m%dT%H%M%SZ}", filename=out.name,
                bytes=out.stat().st_size, duration_s=synthesizer.duration_seconds(out),
                item_count=len(segments), window_start=window_start.isoformat(),
                window_end=now.isoformat(),
            )
            if kind == "scheduled":
                db.mark_used(conn, [c.key for s in segments for c in s.items], episode_id)
        feed_generator.apply_retention(settings["retention_days"])
        log.info("Published %s", out.name)
        _finish(run_id, handler, "ok", episode_id=episode_id)
    except Exception as exc:
        log.exception("Run failed during %s", stage)
        episode_id = _publish_failure(stage, exc, settings, local, kind, stem, now)
        _finish(run_id, handler, "failed", error=f"{type(exc).__name__}: {exc}",
                episode_id=episode_id)
    finally:
        root.removeHandler(handler)
        for leftover in db.EPISODES_DIR.glob("*.part.mp3"):
            leftover.unlink(missing_ok=True)
    return run_id


def _finish(run_id: int, handler: _RunLog, status_: str, **fields):
    with db.get_conn() as conn:
        db.update_run(conn, run_id, status=status_, finished_at=db.now_iso(),
                      log="\n".join(handler.lines), **fields)


def _speakable(error: Exception) -> str:
    text = re.sub(r"https?://\S+", "a web address", str(error))
    text = re.sub(r"[_{}\[\]<>]", " ", text)
    return ingest.truncate(text, 200)


def _publish_failure(stage: str, exc: Exception, settings: dict, local: datetime,
                     kind: str, stem: str, now: datetime) -> int | None:
    """Publish a failure episode. Never raises; returns the episode id or None."""
    out = db.EPISODES_DIR / f"{stem}-failed.mp3"
    try:
        message = (f"This is cruftcast for {spoken_date(local)}. Today's episode failed while "
                   f"{STAGE_PHRASES.get(stage, 'running')}. The error was: {_speakable(exc)}")
        try:
            synthesizer.speak_once(message, settings, out)
        except Exception as speak_exc:
            log.warning("Could not speak the failure message (%s); using pre-rendered clip", speak_exc)
            clip = FAILURE_AUDIO_DIR / f"{stage}.mp3"
            if not clip.exists():
                clip = FAILURE_AUDIO_DIR / "unknown.mp3"
            shutil.copyfile(clip, out)
        try:
            duration = synthesizer.duration_seconds(out)
        except Exception:
            duration = 0
        with db.get_conn() as conn:
            return db.add_episode(
                conn, kind=kind, status="failed", title=episode_title(local, kind, failed=True),
                guid=f"{stem}-failed-{now:%Y%m%dT%H%M%SZ}", filename=out.name,
                bytes=out.stat().st_size, duration_s=duration, item_count=0,
            )
    except Exception:
        log.exception("Could not publish the failure episode")
        return None
