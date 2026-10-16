"""SQLite database layer for cruftcast: settings, seen items, episodes, runs."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("CRUFTCAST_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "cruftcast.db"
EPISODES_DIR = DATA_DIR / "episodes"

# Every setting the web panel can edit, with its default. Values are stored as
# JSON in the settings table; a key missing from the table falls back to here,
# so adding a new setting needs no migration.
DEFAULT_SETTINGS: dict = {
    "run_time": "06:00",
    "timezone": "America/Los_Angeles",
    "target_minutes": 10,
    "min_minutes": 5,
    "max_minutes": 15,
    "max_story_seconds": 60,
    "category_priority": ["Weblog", "Tech News"],
    "excluded_categories": ["Personal"],
    "excluded_feeds": [
        "https://cruftbox.com/feed/",
        "https://cruftbox.com/feed/?post_type=linkblog",
    ],
    "weblog_category": "Weblog",
    "weblog_cap": 2,
    "topic_exclusions": {"Tech News": ["mobile phone news", "Android news"]},
    "voice": "en-US-AvaNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "pronunciations": {},
    "chime": True,
    "gap_ms": 400,
    "retention_days": 14,
    "article_char_budget": 6000,
    "model": "claude-opus-5-5",
    "effort": "high",
    "opml_url": os.getenv("CRUFTCAST_OPML_URL", ""),
    "public_base_url": os.getenv("CRUFTCAST_PUBLIC_URL", "http://localhost:8091"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT    NOT NULL,          -- scheduled | adhoc
                status       TEXT    NOT NULL,          -- ok | failed
                title        TEXT    NOT NULL,
                guid         TEXT    NOT NULL UNIQUE,
                filename     TEXT    NOT NULL,
                bytes        INTEGER NOT NULL,
                duration_s   INTEGER NOT NULL,
                item_count   INTEGER NOT NULL DEFAULT 0,
                window_start TEXT,
                window_end   TEXT,
                created_at   TEXT    NOT NULL,
                deleted_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url   TEXT    NOT NULL,
                guid       TEXT    NOT NULL,
                first_seen TEXT    NOT NULL,
                used_in    INTEGER REFERENCES episodes(id),
                UNIQUE(feed_url, guid)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL,              -- scheduled | adhoc
                run_date   TEXT NOT NULL,              -- local date the run belongs to
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status     TEXT NOT NULL,              -- running | ok | failed
                stage      TEXT,
                error      TEXT,
                log        TEXT,
                episode_id INTEGER REFERENCES episodes(id)
            );
        """)
        # Runs left "running" by a container restart will never finish.
        conn.execute(
            "UPDATE runs SET status = 'failed', error = 'interrupted by restart', "
            "finished_at = ? WHERE status = 'running'",
            (now_iso(),),
        )


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings(conn) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in settings:
            settings[row["key"]] = json.loads(row["value"])
    return settings


def save_settings(conn, values: dict):
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            raise KeyError(f"unknown setting: {key}")
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def seed_settings():
    """Store the env-derived settings on first start so later edits stick."""
    with get_conn() as conn:
        for key in ("opml_url", "public_base_url"):
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(DEFAULT_SETTINGS[key])),
            )


# ── Items ─────────────────────────────────────────────────────────────────────

def record_seen(conn, feed_url: str, guid: str) -> sqlite3.Row:
    """Insert the item if new and return its row (first_seen, used_in)."""
    conn.execute(
        "INSERT OR IGNORE INTO items (feed_url, guid, first_seen) VALUES (?, ?, ?)",
        (feed_url, guid, now_iso()),
    )
    return conn.execute(
        "SELECT * FROM items WHERE feed_url = ? AND guid = ?", (feed_url, guid)
    ).fetchone()


def mark_used(conn, keys: list[tuple[str, str]], episode_id: int):
    for feed_url, guid in keys:
        conn.execute(
            "UPDATE items SET used_in = ? WHERE feed_url = ? AND guid = ?",
            (episode_id, feed_url, guid),
        )


# ── Episodes ──────────────────────────────────────────────────────────────────

def add_episode(conn, **fields) -> int:
    fields.setdefault("created_at", now_iso())
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO episodes ({cols}) VALUES ({marks})", tuple(fields.values())
    )
    return cur.lastrowid


def list_episodes(conn, include_deleted: bool = False) -> list[sqlite3.Row]:
    where = "" if include_deleted else "WHERE deleted_at IS NULL"
    return conn.execute(
        f"SELECT * FROM episodes {where} ORDER BY created_at DESC"
    ).fetchall()


def last_scheduled_window_end(conn) -> str | None:
    """End of the window covered by the last successful scheduled episode."""
    row = conn.execute(
        "SELECT window_end FROM episodes WHERE kind = 'scheduled' AND status = 'ok' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["window_end"] if row else None


def mark_episode_deleted(conn, episode_id: int):
    conn.execute(
        "UPDATE episodes SET deleted_at = ? WHERE id = ?", (now_iso(), episode_id)
    )


# ── Runs ──────────────────────────────────────────────────────────────────────

def start_run(conn, kind: str, run_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, run_date, started_at, status) VALUES (?, ?, ?, 'running')",
        (kind, run_date, now_iso()),
    )
    return cur.lastrowid


def update_run(conn, run_id: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*fields.values(), run_id))


def scheduled_run_exists(conn, run_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs WHERE kind = 'scheduled' AND run_date = ?", (run_date,)
    ).fetchone()
    return row is not None


def recent_runs(conn, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
