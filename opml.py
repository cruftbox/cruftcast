"""OPML parsing, copied from wireburst. Folders become categories."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class OpmlFeed:
    url: str
    title: str
    category: str | None = None  # folder name, None = uncategorized


def parse_opml(content: bytes | str) -> list[OpmlFeed]:
    """Parse OPML bytes/string and return a flat list of OpmlFeed entries."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    root = ET.fromstring(content)
    body = root.find("body")
    if body is None:
        raise ValueError("OPML has no <body> element")

    feeds: list[OpmlFeed] = []

    for child in body:
        if child.tag != "outline":
            continue

        xml_url = child.get("xmlUrl") or child.get("xmlurl")
        if xml_url:
            # Top-level feed (no folder)
            title = child.get("title") or child.get("text") or xml_url
            feeds.append(OpmlFeed(url=xml_url, title=title, category=None))
        else:
            # Folder — feeds are children
            folder_name = child.get("title") or child.get("text")
            for sub in child:
                if sub.tag != "outline":
                    continue
                sub_url = sub.get("xmlUrl") or sub.get("xmlurl")
                if not sub_url:
                    continue
                sub_title = sub.get("title") or sub.get("text") or sub_url
                feeds.append(OpmlFeed(url=sub_url, title=sub_title, category=folder_name))

    return feeds
