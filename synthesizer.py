"""
Text to speech with edge-tts, one request per speaker turn, then ffmpeg to
join the pieces into one MP3.

edge-tts is an unofficial client for Microsoft Edge's Read Aloud service, not
a documented API, and has broken before when Microsoft changed the service.
Every turn is retried with backoff; if a turn still fails, the whole step
fails and pipeline.py publishes a failure episode instead.

All edge-tts output uses one format (24 kHz mono MP3), so ffmpeg's concat
demuxer can join the files with `-c copy` and no re-encode. pydub is avoided
on purpose: it depends on audioop, which Python 3.13 removed.
"""

import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts

log = logging.getLogger("cruftcast.synthesizer")

RETRIES = 3
CONCURRENCY = 4
TURN_TIMEOUT = 60  # seconds per edge-tts request


class SynthesisError(Exception):
    pass


def _voice_for(speaker: str, settings: dict) -> dict:
    prefix = "lead" if speaker == "lead" else "second"
    return {
        "voice": settings[f"{prefix}_voice"],
        "rate": settings[f"{prefix}_rate"],
        "pitch": settings[f"{prefix}_pitch"],
    }


async def _speak(text: str, voice: dict, path: Path):
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            communicate = edge_tts.Communicate(text, **voice)
            await asyncio.wait_for(communicate.save(str(path)), TURN_TIMEOUT)
            if path.exists() and path.stat().st_size > 0:
                return
            raise SynthesisError("edge-tts returned no audio")
        except Exception as exc:
            last_exc = exc
            log.warning("edge-tts attempt %d failed for %s: %s", attempt + 1, voice["voice"], exc)
            await asyncio.sleep(2 ** (attempt + 1))
    raise SynthesisError(f"edge-tts failed after {RETRIES} attempts: {last_exc}")


async def _speak_all(turns: list[dict], settings: dict, workdir: Path) -> list[Path]:
    sem = asyncio.Semaphore(CONCURRENCY)
    paths = [workdir / f"turn_{i:04d}.mp3" for i in range(len(turns))]

    async def one(turn, path):
        async with sem:
            await _speak(turn["text"], _voice_for(turn["speaker"], settings), path)

    await asyncio.gather(*(one(t, p) for t, p in zip(turns, paths)))
    return paths


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SynthesisError(f"{cmd[0]} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def _make_silence(path: Path, ms: int):
    """Silence in the same format edge-tts produces, so concat can copy it."""
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
          "-i", "anullsrc=r=24000:cl=mono", "-t", f"{ms / 1000:.3f}",
          "-c:a", "libmp3lame", "-b:a", "48k", str(path)])


def concat(paths: list[Path], out: Path, gap_ms: int = 0):
    """Join MP3 files in order, with optional silence between them."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        silence = None
        if gap_ms > 0 and len(paths) > 1:
            silence = tmpdir / "gap.mp3"
            _make_silence(silence, gap_ms)
        listing = tmpdir / "list.txt"
        lines = []
        for i, p in enumerate(paths):
            if silence and i > 0:
                lines.append(f"file '{silence.as_posix()}'")
            lines.append(f"file '{Path(p).resolve().as_posix()}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", str(listing), "-c", "copy", str(out)])


def duration_seconds(path: Path) -> int:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return round(float(out.strip()))


def synthesize(turns: list[dict], settings: dict, out: Path):
    """Render all turns and write the joined episode to `out`."""
    if not shutil.which("ffmpeg"):
        raise SynthesisError("ffmpeg is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        paths = asyncio.run(_speak_all(turns, settings, Path(tmp)))
        concat(paths, out, settings["gap_ms"])
    log.info("Synthesized %d turns into %s", len(turns), out.name)


def speak_once(text: str, settings: dict, out: Path):
    """A single lead-voice clip; used for failure and no-news episodes."""
    asyncio.run(_speak(text, _voice_for("lead", settings), out))
