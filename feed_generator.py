"""
The podcast RSS feed and episode retention.

rss.xml is built from the episodes table on each request rather than kept as
a file, so it can never drift from the database, and a change to the public
base URL takes effect immediately.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import db

log = logging.getLogger("cruftcast.feed")

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)

ARTWORK_NAMES = ("artwork.jpg", "artwork.jpeg", "artwork.png")


def artwork_path():
    for name in ARTWORK_NAMES:
        path = db.DATA_DIR / name
        if path.exists():
            return path
    return None


def _it(tag: str) -> str:
    return f"{{{ITUNES}}}{tag}"


def build_rss(episodes, base_url: str) -> str:
    base = base_url.rstrip("/")
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "cruftcast"
    ET.SubElement(ch, "link").text = base + "/"
    ET.SubElement(ch, "description").text = "A private daily podcast generated from my RSS feeds."
    ET.SubElement(ch, "language").text = "en-us"
    ET.SubElement(ch, _it("author")).text = "cruftcast"
    ET.SubElement(ch, _it("explicit")).text = "false"
    ET.SubElement(ch, _it("block")).text = "Yes"
    art = artwork_path()
    if art:
        url = f"{base}/{art.name}"
        ET.SubElement(ch, _it("image"), href=url)
        image = ET.SubElement(ch, "image")
        ET.SubElement(image, "url").text = url
        ET.SubElement(image, "title").text = "cruftcast"
        ET.SubElement(image, "link").text = base + "/"

    for ep in episodes:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "guid", isPermaLink="false").text = ep["guid"]
        created = datetime.fromisoformat(ep["created_at"])
        ET.SubElement(item, "pubDate").text = format_datetime(created)
        count = ep["item_count"]
        ET.SubElement(item, "description").text = (
            "Run failed." if ep["status"] == "failed"
            else f"{count} item{'s' if count != 1 else ''}."
        )
        ET.SubElement(item, "enclosure", url=f"{base}/episodes/{ep['filename']}",
                      length=str(ep["bytes"]), type="audio/mpeg")
        ET.SubElement(item, _it("duration")).text = str(ep["duration_s"])

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


def apply_retention(days: int):
    """Delete episode files older than `days` and drop them from the feed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with db.get_conn() as conn:
        for ep in db.list_episodes(conn):
            if datetime.fromisoformat(ep["created_at"]) >= cutoff:
                continue
            path = db.EPISODES_DIR / ep["filename"]
            path.unlink(missing_ok=True)
            db.mark_episode_deleted(conn, ep["id"])
            log.info("Retention: removed %s", ep["filename"])
        # Seen-item records give undated items their time, so a record pruned
        # while the item is still in its feed would make it look new again.
        # A year is far longer than any feed keeps an item.
        item_cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        conn.execute("DELETE FROM items WHERE first_seen < ?", (item_cutoff,))
