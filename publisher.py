"""
Publishing to a web server over SFTP, alongside or instead of serving the
feed from this machine.

Each sync makes the remote folder match the database: upload the MP3s it
lacks and the artwork, then rss.xml (built with the web base URL), then
delete episode files that have left the feed. Every upload goes to a
temporary name and is renamed into place, so a podcast app never fetches half
a file, and the feed goes up only after every MP3 it lists. Because a sync
compares rather than replays, a failed one is repaired by the next.

The server's host key is pinned in the sftp_host_key setting: filled in on
the first connection, and after that a different key stops the sync, the
same check OpenSSH makes with known_hosts. The password comes from .env,
because the web panel has no login.
"""

import logging
import os
import posixpath
import re
import socket
import tempfile
import threading
import time
from pathlib import Path

import paramiko

import db
import feed_generator

log = logging.getLogger("cruftcast.publisher")

PASSWORD_ENV = "CRUFTCAST_SFTP_PASSWORD"
CONNECT_TIMEOUT = 30
IO_TIMEOUT = 120
RETRY_DELAYS = (5, 30)  # seconds before the second and third attempts

# Only files cruftcast names this way are ever deleted on the server.
EPISODE_FILE = re.compile(r"^cruftcast-[\w-]+\.mp3(\.part)?$")

_lock = threading.Lock()


class PublishError(Exception):
    pass


def is_syncing() -> bool:
    return _lock.locked()


def host_key_string(key: paramiko.PKey) -> str:
    """The key as known_hosts writes it, e.g. "ssh-rsa AAAA..."."""
    return f"{key.get_name()} {key.get_base64()}"


def _connect(settings: dict) -> tuple[paramiko.Transport, paramiko.SFTPClient]:
    password = os.getenv(PASSWORD_ENV)
    if not password:
        raise PublishError(f"{PASSWORD_ENV} is not set in .env")
    host, port = settings["sftp_host"], settings["sftp_port"]
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        raise PublishError(f"Could not reach {host}:{port}: {exc}") from exc
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=CONNECT_TIMEOUT)
        offered = host_key_string(transport.get_remote_server_key())
        pinned = settings["sftp_host_key"]
        if not pinned:
            with db.get_conn() as conn:
                db.save_settings(conn, {"sftp_host_key": offered})
            log.info("Pinned the host key of %s:%s", host, port)
        elif offered != pinned:
            raise PublishError(
                f"The host key of {host}:{port} has changed. If you expected that, clear "
                "the host key field on the settings page to accept the new key.")
        transport.auth_password(settings["sftp_user"], password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.get_channel().settimeout(IO_TIMEOUT)
        return transport, sftp
    except paramiko.AuthenticationException as exc:
        transport.close()
        raise PublishError(f"The server rejected the login for {settings['sftp_user']}") from exc
    except Exception:
        transport.close()
        raise


def _put(sftp: paramiko.SFTPClient, local, remote: str):
    """Upload under a temporary name, then rename over the real one."""
    part = remote + ".part"
    sftp.put(str(local), part, confirm=True)
    try:
        sftp.posix_rename(part, remote)
    except OSError:
        # Plain SFTP rename refuses to overwrite, and not every server
        # has the posix-rename extension.
        try:
            sftp.remove(remote)
        except FileNotFoundError:
            pass
        sftp.rename(part, remote)


def _ensure_dir(sftp: paramiko.SFTPClient, path: str):
    """Create `path` and any missing parents."""
    try:
        sftp.stat(path)
    except FileNotFoundError:
        parent = posixpath.dirname(path.rstrip("/"))
        if parent and parent != path:
            _ensure_dir(sftp, parent)
        sftp.mkdir(path)


def _sync_once(settings: dict, episodes) -> str:
    transport, sftp = _connect(settings)
    try:
        root = settings["sftp_path"] or "."
        ep_dir = posixpath.join(root, "episodes")
        _ensure_dir(sftp, ep_dir)
        remote = {a.filename: a.st_size for a in sftp.listdir_attr(ep_dir)}

        uploaded = 0
        for ep in episodes:
            if remote.get(ep["filename"]) == ep["bytes"]:
                continue
            local = db.EPISODES_DIR / ep["filename"]
            if not local.exists():
                log.warning("Not uploading %s: the local file is missing", ep["filename"])
                continue
            _put(sftp, local, posixpath.join(ep_dir, ep["filename"]))
            log.info("Uploaded %s", ep["filename"])
            uploaded += 1

        art = feed_generator.artwork_path()
        if art:
            art_remote = posixpath.join(root, art.name)
            try:
                same = sftp.stat(art_remote).st_size == art.stat().st_size
            except FileNotFoundError:
                same = False
            if not same:
                _put(sftp, art, art_remote)
                log.info("Uploaded %s", art.name)

        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "rss.xml"
            feed.write_text(feed_generator.build_rss(episodes, settings["web_base_url"]), encoding="utf-8")
            _put(sftp, feed, posixpath.join(root, "rss.xml"))

        keep = {ep["filename"] for ep in episodes}
        removed = 0
        for name in remote:
            if EPISODE_FILE.match(name) and name not in keep:
                sftp.remove(posixpath.join(ep_dir, name))
                log.info("Removed %s from the server", name)
                removed += 1
        count = len(episodes)
        return (f"{count} episode{'s' if count != 1 else ''} on the server; "
                f"{uploaded} uploaded, {removed} removed")
    finally:
        sftp.close()
        transport.close()


def sync(reason: str) -> bool:
    """
    Bring the web server up to date, with retries. Never raises: the result
    goes to the log and the dashboard. Returns True on success, and also when
    web hosting is off or another sync holds the lock.
    """
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
    if not settings["web_hosting"]:
        return True
    if not _lock.acquire(blocking=False):
        log.info("A web sync is already running; skipping (%s)", reason)
        return True
    try:
        log.info("Syncing to %s (%s)", settings["sftp_host"], reason)
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            # Re-read each time: the first attempt may have pinned the host key.
            with db.get_conn() as conn:
                settings = db.get_settings(conn)
                episodes = db.list_episodes(conn)
            try:
                summary = _sync_once(settings, episodes)
                log.info("Web sync done: %s", summary)
                _record(True, summary)
                return True
            except Exception as exc:
                message = str(exc) if isinstance(exc, PublishError) else f"{type(exc).__name__}: {exc}"
                log.warning("Web sync attempt %d of %d failed: %s", attempt + 1, attempts, message)
                if isinstance(exc, PublishError) and "host key" in message:
                    break  # retrying cannot fix a changed key
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
        _record(False, message)
        return False
    finally:
        _lock.release()


def sync_in_background(reason: str) -> bool:
    """Start a sync on a thread. Returns False if one is already running."""
    if _lock.locked():
        return False
    threading.Thread(target=sync, args=(reason,), daemon=True, name="web-sync").start()
    return True


def _record(ok: bool, message: str):
    with db.get_conn() as conn:
        db.set_state(conn, "web_sync", {"at": db.now_iso(), "ok": ok, "message": message})
