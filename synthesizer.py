"""
Text to speech with edge-tts, one request per section (the opening, each
story, the sign-off), then ffmpeg to join the pieces into one MP3 with a
chime before each story.

edge-tts is an unofficial client for Microsoft Edge's Read Aloud service, not
a documented API, and has broken before when Microsoft changed the service.
Every section is retried with backoff; if one still fails, the whole step
fails and pipeline.py publishes a failure episode instead.

All edge-tts output uses one format (24 kHz mono MP3), so ffmpeg's concat
demuxer can join the files with `-c copy` and no re-encode. The chime and
the silence gaps are converted to that format first. pydub is avoided on
purpose: it depends on audioop, which Python 3.13 removed.
"""

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts

import db

log = logging.getLogger("cruftcast.synthesizer")

RETRIES = 3
CONCURRENCY = 4
SECTION_TIMEOUT = 120  # seconds per edge-tts request
BUNDLED_CHIME = Path(__file__).parent / "assets" / "chime.mp3"


class SynthesisError(Exception):
    pass


def _voice(settings: dict) -> dict:
    return {"voice": settings["voice"], "rate": settings["rate"], "pitch": settings["pitch"]}


def respell(text: str, pronunciations: dict) -> str:
    """
    Replace whole words the voice mispronounces with spellings it reads
    correctly. edge-tts escapes its input, so SSML phoneme tags are not an
    option. Matching ignores case; longer entries win over shorter ones.
    """
    words = sorted((w for w, spoken in pronunciations.items() if spoken), key=len, reverse=True)
    if not words:
        return text
    lookup = {w.lower(): pronunciations[w] for w in words}
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def chime_source() -> Path:
    """data/chime.mp3 replaces the bundled chime when present."""
    custom = db.DATA_DIR / "chime.mp3"
    return custom if custom.exists() else BUNDLED_CHIME


async def _speak(text: str, voice: dict, path: Path):
    last_exc: Exception | None = None
    for attempt in range(RETRIES):
        try:
            communicate = edge_tts.Communicate(text, **voice)
            await asyncio.wait_for(communicate.save(str(path)), SECTION_TIMEOUT)
            if path.exists() and path.stat().st_size > 0:
                return
            raise SynthesisError("edge-tts returned no audio")
        except Exception as exc:
            last_exc = exc
            log.warning("edge-tts attempt %d failed for %s: %s", attempt + 1, voice["voice"], exc)
            await asyncio.sleep(2 ** (attempt + 1))
    raise SynthesisError(f"edge-tts failed after {RETRIES} attempts: {last_exc}")


async def _speak_all(texts: list[str], settings: dict, workdir: Path) -> list[Path]:
    sem = asyncio.Semaphore(CONCURRENCY)
    paths = [workdir / f"section_{i:04d}.mp3" for i in range(len(texts))]
    voice = _voice(settings)

    async def one(text, path):
        async with sem:
            await _speak(text, voice, path)

    texts = [respell(t, settings["pronunciations"]) for t in texts]
    await asyncio.gather(*(one(t, p) for t, p in zip(texts, paths)))
    return paths


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SynthesisError(f"{cmd[0]} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


# Matches edge-tts output so concat can copy streams without re-encoding.
_EDGE_FORMAT = ["-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k"]


def _make_silence(path: Path, ms: int):
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
          "-i", "anullsrc=r=24000:cl=mono", "-t", f"{ms / 1000:.3f}", *_EDGE_FORMAT, str(path)])


def _conform(src: Path, dest: Path):
    """Re-encode any audio file to the edge-tts format."""
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *_EDGE_FORMAT, str(dest)])


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


def episode_order(sections: list[Path], chime: Path | None) -> list[Path]:
    """
    Opening, then a chime before each story, then the sign-off with no chime.
    `sections` is [opening, story 1 .. story n, sign-off].
    """
    if chime is None:
        return list(sections)
    order = [sections[0]]
    for story in sections[1:-1]:
        order += [chime, story]
    order.append(sections[-1])
    return order


def synthesize(opening: str, stories: list[str], sign_off: str, settings: dict, out: Path):
    """Render the episode and write the joined MP3 to `out`."""
    if not shutil.which("ffmpeg"):
        raise SynthesisError("ffmpeg is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        sections = asyncio.run(_speak_all([opening, *stories, sign_off], settings, tmpdir))
        chime = None
        if settings["chime"]:
            chime = tmpdir / "chime.mp3"
            _conform(chime_source(), chime)
        concat(episode_order(sections, chime), out, settings["gap_ms"])
    log.info("Synthesized %d stories into %s", len(stories), out.name)


def speak_once(text: str, settings: dict, out: Path):
    """A single clip; used for failure and no-news episodes."""
    asyncio.run(_speak(respell(text, settings["pronunciations"]), _voice(settings), out))
