"""
Render the pre-recorded failure clips in failure_audio/.

pipeline.py plays one of these when a run fails and edge-tts is also
unavailable, so the clips are committed to the repo rather than generated at
deploy time. Re-run this after changing a message or the lead voice:

    python tools/render_failure_audio.py
"""

import asyncio
import sys
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import DEFAULT_SETTINGS  # noqa: E402
from pipeline import FAILURE_AUDIO_DIR, STAGE_PHRASES  # noqa: E402


async def main():
    FAILURE_AUDIO_DIR.mkdir(exist_ok=True)
    voice = DEFAULT_SETTINGS["voice"]
    for stage, phrase in STAGE_PHRASES.items():
        text = (f"This is cruftcast. Today's episode failed while {phrase}, "
                "and the voice service could not be reached to say more. "
                "Check the run log in the cruftcast web panel.")
        out = FAILURE_AUDIO_DIR / f"{stage}.mp3"
        await edge_tts.Communicate(text, voice).save(str(out))
        print(f"{out.relative_to(ROOT)}: {out.stat().st_size} bytes")


if __name__ == "__main__":
    asyncio.run(main())
