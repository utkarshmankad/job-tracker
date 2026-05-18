"""Poller scheduler — runs as a managed background thread inside the FastAPI process."""

import threading

import structlog

from backend.config import POLL_INTERVAL_SECONDS
from backend.db.data_store import DataStore
from backend.parser.email_parser import EmailParser
from backend.engine.status_updater import StatusUpdater
from backend.engine.duplicate_detector import DuplicateDetector
from backend.poller.gmail_poller import GmailPoller
from backend.poller.sleep_watcher import SleepWatcher
from backend.poller.error_retry import AuthError

log = structlog.get_logger()


class PollerScheduler:
    """Runs GmailPoller on a fixed interval in a daemon thread.

    Responsibilities:
    - Fires immediately on start, then every POLL_INTERVAL_SECONDS
    - Wakes early when trigger() is called (e.g. on macOS wake-from-sleep)
    - On unexpected failures: logs + backs off exponentially, then retries
    - On AuthError: halts and marks the DB state; operator must reauth
    """

    def __init__(self, poller: GmailPoller) -> None:
        self._poller = poller
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def poller(self) -> GmailPoller:
        return self._poller

    def start(self) -> None:
        self._stop_event.clear()
        self._wake_event.set()  # immediate first poll
        self._thread = threading.Thread(target=self._run, daemon=True, name="poller")
        self._thread.start()
        SleepWatcher(on_wake=self.trigger).start()
        log.info("poller_scheduler_started", interval_seconds=POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()  # unblock any sleeping wait
        if self._thread:
            self._thread.join(timeout=5)

    def trigger(self) -> None:
        """Interrupt the current sleep and run a poll immediately."""
        self._wake_event.set()

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=POLL_INTERVAL_SECONDS)
            if self._stop_event.is_set():
                break
            self._wake_event.clear()

            try:
                self._poller.poll_once()
                consecutive_failures = 0
            except AuthError:
                log.error("poller_auth_error_halted")
                break
            except Exception:
                consecutive_failures += 1
                backoff = min(60 * (2 ** (consecutive_failures - 1)), 600)
                log.exception(
                    "poller_error_will_retry",
                    consecutive_failures=consecutive_failures,
                    backoff_seconds=backoff,
                )
                self._stop_event.wait(backoff)

        log.info("poller_scheduler_stopped")


def build_poller() -> GmailPoller:
    db = DataStore()
    parser = EmailParser()
    detector = DuplicateDetector(db=db)
    updater = StatusUpdater(db=db, detector=detector)
    return GmailPoller(db=db, parser=parser, updater=updater)


def run_scheduler() -> None:
    """Legacy entry point — kept for standalone use outside uvicorn."""
    poller = build_poller()
    poller.authenticate()
    scheduler = PollerScheduler(poller)
    scheduler.start()
    threading.Event().wait()  # block forever


if __name__ == "__main__":
    run_scheduler()
