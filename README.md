# cruftcast

cruftcast turns a set of RSS feeds into a private daily podcast. Every morning it reads the feeds, has Claude choose the stories and write a script about them, reads it in one of Microsoft's neural voices through [edge-tts](https://github.com/rany2/edge-tts), and publishes the episode to a podcast RSS feed served from the same container.

It is built to run on a low-power NAS: no AI model runs locally. Claude does the writing over the Anthropic API, and edge-tts does the voices over the network. The NAS only fetches feeds, calls ffmpeg to join audio files, and serves files.

The feed list comes from the OPML export of [wireburst](https://github.com/cruftbox/wireburst), so feeds are managed in one place.

---

## How an episode is made

1. **Feeds.** Fetch the OPML (falling back to the last good copy if it can't be reached), drop excluded categories and feeds, and poll every feed in parallel.
2. **Window.** A scheduled episode covers everything published since the previous scheduled episode, minus anything already used. A **Generate now** episode covers the last 24 hours and does not affect the next scheduled one.
3. **Weblog cap.** Each feed in the weblog category contributes at most N items (newest first). The script mentions when a blog posted more.
4. **Selection (Claude).** Claude sees the title and a short excerpt of every candidate and picks the stories: higher-priority categories first, topic exclusions applied per category, duplicate coverage of one story merged into one segment, and no more stories than fit the target length.
5. **Articles.** For the chosen stories only, feeds that carry the full post are used as they are. Short feed text (link posts, teasers) gets the linked page fetched with trafilatura; the blogger's own words are kept in front of the article. If a fetch fails or hits a paywall, the feed text is used.
6. **Script (Claude).** Claude writes one self-contained section per story, plus a sign-off. The opening line ("This is cruftcast for Thursday, October 15. There are 7 items today.") is added by code.
7. **Voice.** One edge-tts request per section, retried with backoff. ffmpeg joins the clips without re-encoding, with a chime before each story. The bundled chime (`assets/chime.mp3`, made by `tools/render_chime.sh`) is replaced by `chime.mp3` in the data folder if you put one there.
8. **Publish.** The episode is added to the feed and episodes older than the retention period are deleted.

On a slow day the episode is shorter; nothing is padded. With no new items at all, a short "no new items today" episode is published.

### When a run fails

A failed run publishes a short **failure episode** so the problem shows up in your podcast app. It is spoken live with the stage and error ("Today's episode failed while writing the script with Claude. The error was: ..."). If edge-tts itself is unreachable, a pre-recorded clip for that stage from `failure_audio/` is used instead. Full logs for every run are in the web panel.

If the container or the NAS is down, no episode appears at all.

### A note on edge-tts

edge-tts is an unofficial client for the Read Aloud service in Microsoft Edge, not a documented API. It has broken before when Microsoft changed the service and was fixed by a library update. If voice generation starts failing every day, rebuild the image to pick up the newest edge-tts (`./update.sh` rebuilds; `requirements.txt` sets only a minimum version).

---

## Requirements

- Docker with Docker Compose (on a QNAP: Container Station)
- An Anthropic API key
- An OPML URL listing the feeds, with categories as OPML folders (wireburst's `/opml/export` works)
- A way for your phone to reach the server (same LAN, or Tailscale)

---

## Setup

### 1. Clone and configure

```bash
git clone <this repo> cruftcast
cd cruftcast
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key. Without it the app runs, but every run fails at the script stage. |
| `CRUFTCAST_PORT` | Port for the web panel, feed, and MP3s. Default `8091`. |
| `CRUFTCAST_OPML_URL` | Initial OPML URL. After first start this is a setting in the web panel. |
| `CRUFTCAST_PUBLIC_URL` | Initial public base URL: the address your phone uses to reach this server, e.g. `http://your-nas:8091`. Episode links in the feed are built from it. After first start this is a setting in the web panel. |
| `CRUFTCAST_UID`, `CRUFTCAST_GID` | Owner for files in `./data`. Use `id -u` and `id -g` on the host so you can manage the folder over SMB. |

The OPML URL is fetched from **inside the container**. A hostname that works on your desktop may not resolve there, and on some hosts (QNAP Container Station among them) a container cannot connect to ports published on the host at all. If wireburst runs on the same Docker host, join its network and use the container name instead. Put this in `docker-compose.override.yml` (gitignored; Compose loads it automatically) and set the OPML URL to `http://wireburst:8082/opml/export`:

```yaml
services:
  cruftcast:
    networks: [default, wireburst]
networks:
  wireburst:
    external: true
    name: wireburst_default
```

### 2. Artwork

Put a square image named `artwork.jpg` (or `artwork.png`) in `./data`. Apple's podcast spec asks for 1400 to 3000 pixels square. The data folder is not part of the repo, so the image stays private.

### 3. Start

```bash
docker compose up -d
```

Open `http://<server>:8091`. On first start the scheduler checks whether today's episode is due. **If it is already past the run time, the first episode is made right away.**

### 4. Subscribe

Add `http://<server>:8091/rss.xml` to your podcast app as a feed URL. The app has to fetch the feed itself rather than through its own servers, because a private address is not reachable from the internet. Downcast on iOS fetches feeds on the device.

---

## Updating

On the server:

```bash
./update.sh
```

It pulls, rebuilds the image, restarts the container, and waits for the health check. It works over a non-interactive SSH session and handles Container Station's `PATH` and `DOCKER_CONFIG` quirks.

`docker restart cruftcast` alone does **not** pick up code changes; the image has to be rebuilt.

---

## Web panel

The web panel is at `http://<server>:8091`.

- **Episodes**: every episode in the feed, playable in the browser, plus recent runs with their status and full log.
- **Generate now**: makes an extra episode from the last 24 hours, titled "(extra)". The page refreshes while it runs.
- **Settings**: every setting below. Changes apply to the next run; the scheduler picks up a new run time within 30 seconds.

> ⚠️ **Security note:** the web panel has no authentication, and anyone who can reach it can change settings and start runs (which cost API credits). Keep it on a trusted LAN or tailnet.

| Setting | Default | Notes |
|---|---|---|
| Run time, timezone | 06:00, America/Los_Angeles | One scheduled run per day. If the container was down at the run time, it runs when it comes back the same day. Failed runs are not retried. |
| Minimum, target, maximum minutes | 5, 10, 15 | Converted to a word budget at about 150 spoken words per minute. |
| Category priority | Weblog, Tech News | Highest first. Categories not listed come after, in no order. Priority decides what makes the cut; the stories are read in a mixed order. |
| Weblog category, max items per weblog | Weblog, 2 | |
| Topic exclusions | Tech News: mobile phone news, Android news | One per line as `Category: topic`; use `*` as the category to exclude a topic everywhere (`*: Trump`). Claude judges each item's topic; these are not keyword matches. |
| Excluded categories, excluded feed URLs | | Dropped before anything else happens. |
| Article characters per story | 6000 | Long posts are cut to this length before going to Claude. |
| Voice, rate, pitch | en-US-AvaNeural, +0%, +0Hz | Any edge-tts voice name works. |
| Chime between stories | on | |
| Pause around the chime and before the sign-off | 400 ms | |
| Model, effort | claude-opus-5-5, high | Both Claude calls use these. |
| OPML URL, public base URL | from `.env` | |
| Keep episodes (days) | 14 | Older MP3s are deleted and drop out of the feed. |

---

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entrypoint: database setup, scheduler thread, web server (waitress). |
| `pipeline.py` | One run end to end, run logging, failure episodes. |
| `ingest.py` | OPML, feed polling, window, weblog cap, article fetching. |
| `scriptwriter.py` | The two Claude calls (selection and script). |
| `synthesizer.py` | edge-tts and ffmpeg. |
| `feed_generator.py` | The RSS feed (built per request from the database) and retention. |
| `scheduler.py` | The daily trigger. |
| `web.py`, `templates/` | The web panel. |
| `db.py` | SQLite schema and settings defaults. |
| `opml.py` | OPML parsing (from wireburst). |
| `assets/chime.mp3` | The chime played before each story. Regenerate with `tools/render_chime.sh` (needs ffmpeg). |
| `failure_audio/` | Pre-recorded failure clips. Regenerate with `python tools/render_failure_audio.py`. |
| `data/` | Database, episodes, OPML cache, artwork. Not in git. |

---

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest   # Windows; use .venv/bin/pip elsewhere
.venv/Scripts/python -m pytest
```

The tests stub out Claude and edge-tts. The ffmpeg test is skipped when ffmpeg isn't installed; run the suite in the container to include it:

```bash
docker build -t cruftcast:dev .
docker run --rm -v "$PWD/tests:/app/tests" cruftcast:dev sh -c "pip install -q pytest && python -m pytest -q tests"
```

---

## License

MIT
