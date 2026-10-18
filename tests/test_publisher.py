import posixpath
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import db
import publisher


class FakeSFTP:
    """An in-memory SFTP server with the calls publisher uses."""

    def __init__(self, files=None, dirs=("/",)):
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs = set(dirs)
        self.closed = False

    def stat(self, path):
        if path in self.files:
            return SimpleNamespace(st_size=len(self.files[path]))
        if path in self.dirs:
            return SimpleNamespace(st_size=0)
        raise FileNotFoundError(path)

    def mkdir(self, path):
        self.dirs.add(path)

    def listdir_attr(self, path):
        return [SimpleNamespace(filename=posixpath.basename(p), st_size=len(b))
                for p, b in self.files.items() if posixpath.dirname(p) == path]

    def put(self, local, remote, confirm=True):
        with open(local, "rb") as f:
            self.files[remote] = f.read()

    def posix_rename(self, old, new):
        self.files[new] = self.files.pop(old)

    def rename(self, old, new):
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def close(self):
        self.closed = True


@pytest.fixture
def web_on(data_dir, monkeypatch):
    with db.get_conn() as conn:
        db.save_settings(conn, {
            "web_hosting": True, "web_base_url": "https://example.com/cruftcast",
            "sftp_host": "example.com", "sftp_user": "cruftcast", "sftp_path": "/"})
    monkeypatch.setattr(publisher, "RETRY_DELAYS", (0, 0))
    return data_dir


def _episode(name: str, data: bytes):
    (db.EPISODES_DIR / name).write_bytes(data)
    with db.get_conn() as conn:
        db.add_episode(conn, kind="scheduled", status="ok", title=name, guid=name,
                       filename=name, bytes=len(data), duration_s=1)


def _use(monkeypatch, server):
    monkeypatch.setattr(publisher, "_connect",
                        lambda s: (SimpleNamespace(close=lambda: None), server))


def test_sync_uploads_feed_and_episodes_and_prunes(web_on, monkeypatch):
    (web_on / "artwork.jpg").write_bytes(b"art")
    _episode("cruftcast-2026-10-16-060000.mp3", b"new episode")
    _episode("cruftcast-2026-10-15-060000.mp3", b"kept")
    server = FakeSFTP({
        "/episodes/cruftcast-2026-10-15-060000.mp3": b"kept",       # already there: not re-sent
        "/episodes/cruftcast-2026-10-01-060000.mp3": b"expired",    # left the feed: removed
        "/episodes/notes.txt": b"not ours",                          # not a cruftcast file: kept
    })
    _use(monkeypatch, server)

    assert publisher.sync("test")
    assert sorted(server.files) == [
        "/artwork.jpg",
        "/episodes/cruftcast-2026-10-15-060000.mp3",
        "/episodes/cruftcast-2026-10-16-060000.mp3",
        "/episodes/notes.txt",
        "/rss.xml",
    ]
    feed = ET.fromstring(server.files["/rss.xml"])
    urls = [e.get("url") for e in feed.iter("enclosure")]
    assert "https://example.com/cruftcast/episodes/cruftcast-2026-10-16-060000.mp3" in urls
    with db.get_conn() as conn:
        state = db.get_state(conn, "web_sync")
    assert state["ok"] and "1 uploaded, 1 removed" in state["message"]


def test_sync_creates_episodes_folder_and_replaces_feed(web_on, monkeypatch):
    _episode("cruftcast-2026-10-16-060000.mp3", b"ep")
    server = FakeSFTP({"/rss.xml": b"old feed"}, dirs={"/"})

    def no_posix_rename(old, new):
        raise OSError("unsupported")

    server.posix_rename = no_posix_rename
    _use(monkeypatch, server)
    assert publisher.sync("test")
    assert "/episodes" in server.dirs

    with db.get_conn() as conn:
        db.save_settings(conn, {"sftp_path": "/new/podcast"})
    assert publisher.sync("test")
    assert {"/new", "/new/podcast", "/new/podcast/episodes"} <= server.dirs
    assert server.files["/rss.xml"] != b"old feed"
    assert not any(p.endswith(".part") for p in server.files)


def test_sync_failure_is_recorded_not_raised(web_on, monkeypatch):
    attempts = []

    def broken(settings):
        attempts.append(1)
        raise publisher.PublishError("Could not reach example.com:22: timed out")

    monkeypatch.setattr(publisher, "_connect", broken)
    assert publisher.sync("test") is False
    assert len(attempts) == 3
    with db.get_conn() as conn:
        state = db.get_state(conn, "web_sync")
    assert not state["ok"] and "timed out" in state["message"]


def test_changed_host_key_is_not_retried(web_on, monkeypatch):
    attempts = []

    def changed(settings):
        attempts.append(1)
        raise publisher.PublishError("The host key of example.com:22 has changed. ...")

    monkeypatch.setattr(publisher, "_connect", changed)
    assert publisher.sync("test") is False
    assert len(attempts) == 1


def test_sync_does_nothing_when_web_hosting_off(data_dir, monkeypatch):
    monkeypatch.setattr(publisher, "_connect", lambda s: pytest.fail("should not connect"))
    assert publisher.sync("test")
