"""cruftcast entrypoint: database setup, daily scheduler, and the web server."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("cruftcast")


def main():
    # Imported after load_dotenv: db reads env vars for setting defaults.
    import db
    import scheduler
    from waitress import serve
    from web import app

    db.init_db()
    db.seed_settings()
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is not set; runs will fail until it is added to .env")
    scheduler.start()

    port = int(os.getenv("CRUFTCAST_PORT", "8091"))
    log.info("Web panel on port %d", port)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
