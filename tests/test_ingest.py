from datetime import datetime, timedelta, timezone

import feedparser
import pytest

import db
import ingest
from ingest import Candidate, Feed

OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><body>
  <outline text="Weblog"><outline type="rss" text="Blog A" xmlUrl="https://a.example/feed"/>
    <outline type="rss" text="Cruftbox" xmlUrl="https://cruftbox.com/feed/"/></outline>
  <outline text="Personal"><outline type="rss" text="Me" xmlUrl="https://yt.example/feed"/></outline>
  <outline text="Tech News"><outline type="rss" text="Tech" xmlUrl="https://t.example/feed"/></outline>
</body></opml>"""

NOW = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)


def _cand(i, feed, category, hours_ago, text="x"):
    return Candidate(id=i, feed_url=feed, feed_title=feed.upper(), category=category,
                     guid=f"{feed}-{i}", title=f"t{i}", link="", text=text,
                     published=NOW - timedelta(hours=hours_ago))


def test_load_feeds_applies_exclusions_and_caches(data_dir, monkeypatch):
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return OPML
    monkeypatch.setattr(ingest.urllib.request, "urlopen", lambda *a, **k: Resp())
    settings = dict(db.DEFAULT_SETTINGS, opml_url="http://nas:8082/opml/export")
    feeds = ingest.load_feeds(settings)
    assert [(f.title, f.category) for f in feeds] == [("Blog A", "Weblog"), ("Tech", "Tech News")]
    assert (data_dir / "feeds-cache.opml").read_bytes() == OPML


def test_load_feeds_falls_back_to_cache(data_dir, monkeypatch):
    (data_dir / "feeds-cache.opml").write_bytes(OPML)
    def boom(*a, **k): raise OSError("down")
    monkeypatch.setattr(ingest.urllib.request, "urlopen", boom)
    feeds = ingest.load_feeds(dict(db.DEFAULT_SETTINGS, opml_url="http://nas/opml"))
    assert len(feeds) == 2


def test_load_feeds_without_cache_raises(data_dir, monkeypatch):
    def boom(*a, **k): raise OSError("down")
    monkeypatch.setattr(ingest.urllib.request, "urlopen", boom)
    with pytest.raises(ingest.IngestError):
        ingest.load_feeds(dict(db.DEFAULT_SETTINGS, opml_url="http://nas/opml"))


def test_save_feed_list_rejects_bad_files_and_keeps_the_old_one(data_dir):
    ingest.save_feed_list(OPML, "upload", "mine.opml")
    for bad in (b"not xml at all", b"<opml><head/></opml>",
                b"<opml><body><outline text='Empty folder'/></body></opml>"):
        with pytest.raises(ValueError):
            ingest.save_feed_list(bad, "upload", "bad.opml")
    assert ingest.feed_list_path().read_bytes() == OPML
    summary = ingest.feed_list_summary()
    assert summary["source"] == "upload" and summary["name"] == "mine.opml"
    assert summary["count"] == 4
    assert summary["categories"] == {"Weblog": 2, "Personal": 1, "Tech News": 1}


def test_load_feeds_without_url_reads_upload_offline(data_dir, monkeypatch):
    def no_network(*a, **k): raise AssertionError("no fetch expected")
    monkeypatch.setattr(ingest.urllib.request, "urlopen", no_network)
    with pytest.raises(ingest.IngestError, match="upload an OPML file"):
        ingest.load_feeds(dict(db.DEFAULT_SETTINGS, opml_url=""))
    ingest.save_feed_list(OPML, "upload", "mine.opml")
    feeds = ingest.load_feeds(dict(db.DEFAULT_SETTINGS, opml_url=""))
    assert [f.title for f in feeds] == ["Blog A", "Tech"]


def test_url_download_replaces_upload(data_dir, monkeypatch):
    ingest.save_feed_list(b"<opml><body><outline xmlUrl='https://u/feed' text='U'/></body></opml>",
                          "upload", "mine.opml")

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return OPML
    monkeypatch.setattr(ingest.urllib.request, "urlopen", lambda *a, **k: Resp())
    ingest.load_feeds(dict(db.DEFAULT_SETTINGS, opml_url="http://nas/opml"))
    assert ingest.feed_list_path().read_bytes() == OPML
    assert ingest.feed_list_summary()["source"] == "url"


def test_weblog_cap_keeps_newest_and_counts_extras():
    cands = [_cand(0, "a", "Weblog", 5), _cand(1, "a", "Weblog", 1), _cand(2, "a", "Weblog", 3),
             _cand(3, "t", "Tech News", 2), _cand(4, "t", "Tech News", 4)]
    kept, extras = ingest.apply_weblog_cap(cands, "Weblog", 2)
    assert [c.guid for c in kept] == ["a-1", "a-2", "t-3", "t-4"]
    assert [c.id for c in kept] == [0, 1, 2, 3]
    assert extras == {"A": 1}


def _entry(guid, hours_ago, title="T"):
    t = (NOW - timedelta(hours=hours_ago)).timetuple()
    return feedparser.FeedParserDict(id=guid, title=title, link="https://x/" + guid,
                                     summary="<p>Hello <b>world</b></p>", published_parsed=t)


def test_poll_window_and_used_items(data_dir, monkeypatch):
    feed = Feed(url="https://a.example/feed", title="Blog A", category="Weblog")
    entries = [_entry("old", 30), _entry("new", 2), _entry("used", 3)]
    monkeypatch.setattr(ingest, "_fetch_feed", lambda f: (f, entries))
    with db.get_conn() as conn:
        db.record_seen(conn, feed.url, "used")
        ep = db.add_episode(conn, kind="scheduled", status="ok", title="x", guid="g",
                            filename="f.mp3", bytes=1, duration_s=1)
        db.mark_used(conn, [(feed.url, "used")], ep)

    got = ingest.poll_feeds([feed], NOW - timedelta(hours=24), NOW, skip_used=True)
    assert [c.guid for c in got] == ["new"]
    assert got[0].text == "Hello world"

    got = ingest.poll_feeds([feed], NOW - timedelta(hours=24), NOW, skip_used=False)
    assert sorted(c.guid for c in got) == ["new", "used"]


def test_poll_all_feeds_failing_raises(data_dir, monkeypatch):
    monkeypatch.setattr(ingest, "_fetch_feed", lambda f: (f, []))
    with pytest.raises(ingest.IngestError):
        ingest.poll_feeds([Feed("u", "t", "c")], NOW - timedelta(hours=24), NOW, True)


def test_fetch_articles_keeps_full_feed_text_and_fetches_short(monkeypatch):
    full = _cand(0, "a", "Weblog", 1, text="word " * 600)
    short = _cand(1, "b", "Weblog", 1, text="Worth reading.")
    short.link = "https://example.com/post"
    monkeypatch.setattr(ingest, "_fetch_article", lambda url: "The linked article body. " * 20)
    ingest.fetch_articles([full, short], budget=5000)
    assert full.article.startswith("word word")
    assert short.article.startswith("Worth reading.\n\nLinked article:\nThe linked article body.")
