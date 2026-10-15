"""
Daily trigger. Runs inside the container (not QNAP cron) because the run time
is edited in the web panel; the time and timezone are re-read every check, so
a change applies without a restart.

A scheduled run happens once per local date, at or after the configured time.
If the container was down at that time, the run happens when it comes back,
as long as it is still the same day. A failed run is not retried; Generate
now covers that.
"""

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import pipeline

log = logging.getLogger("cruftcast.scheduler")

CHECK_SECONDS = 30


def due(now_local: datetime, run_time: str, already_ran: bool) -> bool:
    hour, minute = (int(x) for x in run_time.split(":"))
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now_local >= target and not already_ran


def _check():
    with db.get_conn() as conn:
        settings = db.get_settings(conn)
        now_local = datetime.now(ZoneInfo(settings["timezone"]))
        already = db.scheduled_run_exists(conn, now_local.date().isoformat())
    if due(now_local, settings["run_time"], already) and not pipeline.is_running():
        log.info("Scheduled run due (%s %s)", settings["run_time"], settings["timezone"])
        pipeline.run("scheduled")


def _loop():
    while True:
        try:
            _check()
        except Exception:
            log.exception("Scheduler check failed")
        time.sleep(CHECK_SECONDS)


def start():
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
    log.info("Scheduler started")
