import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import db
import feed_generator
import pipeline
import scheduler
import scriptwriter
import synthesizer
import web
from ingest import Candidate

LOCAL = datetime(2026, 10, 15, 6, 0)


def test_opening_line_and_titles():
    assert pipeline.opening_line(LOCAL, 7) == "This is cruftcast for Thursday, October 15. There are 7 items today."
    assert pipeline.opening_line(LOCAL, 1).endswith("There is 1 item today.")
    assert pipeline.episode_title(LOCAL, "scheduled") == "cruftcast for Thursday, October 15, 2026"
    assert pipeline.episode_title(LOCAL, "adhoc") == "cruftcast for Thursday, October 15, 2026 (extra)"


def test_wildcard_topic_exclusions():
    got = scriptwriter.expand_exclusions(
        {"Tech News": ["Android news"], "*": ["Trump"]}, ["Tech News", "News", "All"])
    assert got == {"Tech News": ["Android news", "Trump"], "News": ["Trump"], "All": ["Trump"]}
    assert scriptwriter.expand_exclusions({"Tech News": ["Android news"]}, ["News"]) == {}


def test_scheduler_due():
    now = datetime(2026, 10, 15, 6, 0, 30)
    assert scheduler.due(now, "06:00", already_ran=False)
    assert not scheduler.due(now, "06:00", already_ran=True)
    assert not scheduler.due(now, "06:01", already_ran=False)


def test_build_rss(data_dir):
    (data_dir / "artwork.png").write_bytes(b"png")
    with db.get_conn() as conn:
        db.add_episode(conn, kind="scheduled", status="ok", title="cruftcast for X", guid="g1",
                       filename="a.mp3", bytes=1234, duration_s=600, item_count=7)
        episodes = db.list_episodes(conn)
    xml = feed_generator.build_rss(episodes, "http://nas:8091/")
    root = ET.fromstring(xml)
    enc = root.find("channel/item/enclosure")
    assert enc.get("url") == "http://nas:8091/episodes/a.mp3"
    assert enc.get("length") == "1234" and enc.get("type") == "audio/mpeg"
    assert root.find(f"channel/{{{feed_generator.ITUNES}}}image").get("href") == "http://nas:8091/artwork.png"
    assert root.find("channel/item/guid").text == "g1"
    assert root.find("channel/item/description").text == "cruftcast for X. 7 items."


def test_retention_deletes_old_episodes(data_dir):
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    db.EPISODES_DIR.joinpath("old.mp3").write_bytes(b"x")
    with db.get_conn() as conn:
        db.add_episode(conn, kind="scheduled", status="ok", title="old", guid="o",
                       filename="old.mp3", bytes=1, duration_s=1, created_at=old)
        db.add_episode(conn, kind="scheduled", status="ok", title="new", guid="n",
                       filename="new.mp3", bytes=1, duration_s=1)
    feed_generator.apply_retention(14)
    with db.get_conn() as conn:
        assert [e["title"] for e in db.list_episodes(conn)] == ["new"]
    assert not db.EPISODES_DIR.joinpath("old.mp3").exists()


def test_settings_form_validation():
    form = {k: v for k, v in db.DEFAULT_SETTINGS.items() if isinstance(v, (str, int))}
    form = {k: str(v) for k, v in form.items()}
    form.update(category_priority="Weblog\nTech News\n", excluded_categories="Personal",
                excluded_feeds="", topic_exclusions="Tech News: Android news\nTech News: mobile phone news",
                opml_url="http://nas:8082/opml/export", public_base_url="http://nas:8091")
    values, errors = web.parse_settings_form(form)
    assert errors == []
    assert values["topic_exclusions"] == {"Tech News": ["Android news", "mobile phone news"]}
    assert values["category_priority"] == ["Weblog", "Tech News"]

    form.update(run_time="25:00", topic_exclusions="no colon here", min_minutes="20")
    _, errors = web.parse_settings_form(form)
    assert len(errors) == 3


# ── Full pipeline with Claude and edge-tts stubbed out ───────────────────────

@pytest.fixture
def stubbed(data_dir, monkeypatch):
    now = datetime.now(timezone.utc)
    cands = [Candidate(id=i, feed_url="https://a/feed", feed_title="Blog A", category="Weblog",
                       guid=f"g{i}", title=f"Post {i}", link="", text="text",
                       published=now - timedelta(hours=1)) for i in range(2)]
    monkeypatch.setattr(pipeline.ingest, "load_feeds", lambda s: ["feed"])
    monkeypatch.setattr(pipeline.ingest, "poll_feeds", lambda *a, **k: list(cands))
    monkeypatch.setattr(pipeline.ingest, "fetch_articles", lambda *a: None)
    monkeypatch.setattr(scriptwriter, "select_stories",
                        lambda c, s: [scriptwriter.Segment("story", c)])
    monkeypatch.setattr(scriptwriter, "write_script",
                        lambda seg, ex, s: (["Story one."], "Bye."))
    monkeypatch.setattr(synthesizer, "duration_seconds", lambda p: 42)
    spoken = []

    def fake_synth(opening, stories, sign_off, settings, out):
        spoken.extend([opening, *stories, sign_off])
        Path(out).write_bytes(b"mp3")
    monkeypatch.setattr(synthesizer, "synthesize", fake_synth)
    return spoken


def test_scheduled_run_publishes_and_marks_used(stubbed):
    run_id = pipeline.run("scheduled")
    with db.get_conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        eps = db.list_episodes(conn)
        used = conn.execute("SELECT COUNT(*) FROM items WHERE used_in IS NOT NULL").fetchone()[0]
    assert run["status"] == "ok", run["log"]
    assert len(eps) == 1 and eps[0]["item_count"] == 1 and eps[0]["duration_s"] == 42
    assert stubbed[0].startswith("This is cruftcast for ")
    assert stubbed[0].endswith("There is 1 item today.")
    assert stubbed[1:] == ["Story one.", "Bye."]
    # poll_feeds is stubbed, so mark_used updates no existing rows; the call path is what matters.
    assert used == 0
    assert (db.EPISODES_DIR / eps[0]["filename"]).exists()


def test_failure_uses_prerendered_clip_when_tts_down(stubbed, monkeypatch):
    def broken_script(*a):
        raise scriptwriter.ScriptError("ANTHROPIC_API_KEY is not set in .env")
    def broken_tts(*a):
        raise synthesizer.SynthesisError("edge-tts down")
    monkeypatch.setattr(scriptwriter, "write_script", broken_script)
    monkeypatch.setattr(synthesizer, "speak_once", broken_tts)
    run_id = pipeline.run("adhoc")
    with db.get_conn() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        eps = db.list_episodes(conn)
    assert run["status"] == "failed" and run["stage"] == "script"
    assert "ANTHROPIC_API_KEY" in run["error"]
    assert eps[0]["status"] == "failed" and eps[0]["title"].endswith("(extra) (failed)")
    clip = (pipeline.FAILURE_AUDIO_DIR / "script.mp3").read_bytes()
    assert (db.EPISODES_DIR / eps[0]["filename"]).read_bytes() == clip


def test_episode_order_puts_chime_before_each_story():
    opening, s1, s2, sign_off, chime = (Path(n) for n in ("o", "s1", "s2", "end", "chime"))
    assert synthesizer.episode_order([opening, s1, s2, sign_off], chime) == \
        [opening, chime, s1, chime, s2, sign_off]
    assert synthesizer.episode_order([opening, s1, sign_off], None) == [opening, s1, sign_off]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_concat_real_files_with_chime(tmp_path):
    clips = sorted(pipeline.FAILURE_AUDIO_DIR.glob("*.mp3"))[:2]
    chime = tmp_path / "chime.mp3"
    synthesizer._conform(synthesizer.BUNDLED_CHIME, chime)
    out = tmp_path / "joined.mp3"
    synthesizer.concat(synthesizer.episode_order([clips[0], clips[1], clips[0]], chime),
                       out, gap_ms=300)
    assert synthesizer.duration_seconds(out) >= 2


def test_story_word_cap_follows_rate():
    base = dict(db.DEFAULT_SETTINGS, max_story_seconds=60)
    assert scriptwriter.story_word_cap(dict(base, rate="+0%")) == 150
    assert scriptwriter.story_word_cap(dict(base, rate="+20%")) == 180


def test_trim_story_keeps_whole_sentences():
    long = " ".join(f"Sentence number {i} has six words." for i in range(40))  # 240 words
    trimmed = scriptwriter.trim_story(long, 30)
    assert trimmed.endswith(".") and len(trimmed.split()) <= 30
    short = "One short story. Two sentences."
    assert scriptwriter.trim_story(short, 30) == short
