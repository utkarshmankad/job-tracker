"""APScheduler job definitions."""

from apscheduler.schedulers.blocking import BlockingScheduler
import structlog

from backend.config import POLL_INTERVAL_SECONDS
from backend.db.data_store import DataStore
from backend.parser.email_parser import EmailParser
from backend.engine.status_updater import StatusUpdater
from backend.engine.duplicate_detector import DuplicateDetector
from backend.poller.gmail_poller import GmailPoller
from backend.poller.sleep_watcher import SleepWatcher

log = structlog.get_logger()


def build_poller() -> GmailPoller:
    db = DataStore()
    parser = EmailParser()
    detector = DuplicateDetector(db=db)
    updater = StatusUpdater(db=db, detector=detector)
    return GmailPoller(db=db, parser=parser, updater=updater)


def run_scheduler() -> None:
    poller = build_poller()
    poller.authenticate()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        poller.poll_once,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        misfire_grace_time=None,  # no cutoff — always fires once on wake regardless of sleep length
        coalesce=True,            # collapse multiple missed fires into one
        id="gmail_poll",
    )

    # Fire an immediate poll whenever macOS wakes from sleep
    SleepWatcher(on_wake=poller.poll_once).start()

    log.info("scheduler_starting", interval_seconds=POLL_INTERVAL_SECONDS)
    poller.poll_once()  # immediate first run
    scheduler.start()


if __name__ == "__main__":
    run_scheduler()
