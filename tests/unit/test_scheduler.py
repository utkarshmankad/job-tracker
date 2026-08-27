"""Tests for backend/poller/scheduler.py."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from backend.poller.error_retry import AuthError
from backend.poller.scheduler import PollerScheduler


def _make_scheduler() -> tuple[PollerScheduler, MagicMock]:
    poller = MagicMock()
    scheduler = PollerScheduler(poller)
    return scheduler, poller


def test_start_polls_immediately_and_stop_joins_thread() -> None:
    scheduler, poller = _make_scheduler()
    poller.poll_once.return_value = 0

    with patch("backend.poller.scheduler.SleepWatcher") as mock_watcher_cls:
        mock_watcher_cls.return_value = MagicMock()
        scheduler.start()
        time.sleep(0.2)
        scheduler.stop()

    assert poller.poll_once.call_count >= 1
    assert not scheduler._thread.is_alive()


def test_trigger_wakes_loop_before_interval_elapses() -> None:
    scheduler, poller = _make_scheduler()
    poll_calls = threading.Event()
    poller.poll_once.side_effect = lambda: poll_calls.set() or 0

    with (
        patch("backend.poller.scheduler.SleepWatcher") as mock_watcher_cls,
        patch("backend.poller.scheduler.POLL_INTERVAL_SECONDS", 300),
    ):
        mock_watcher_cls.return_value = MagicMock()
        scheduler.start()
        assert poll_calls.wait(timeout=2), "initial poll on start() did not fire"
        poll_calls.clear()

        scheduler.trigger()
        assert poll_calls.wait(timeout=2), "trigger() did not wake the loop"
        scheduler.stop()


def test_auth_error_halts_the_loop() -> None:
    scheduler, poller = _make_scheduler()
    poller.poll_once.side_effect = AuthError("401")

    with patch("backend.poller.scheduler.SleepWatcher") as mock_watcher_cls:
        mock_watcher_cls.return_value = MagicMock()
        scheduler.start()
        scheduler._thread.join(timeout=2)

    assert not scheduler._thread.is_alive(), "thread should exit after AuthError"


def test_unexpected_exception_backs_off_and_continues() -> None:
    scheduler, poller = _make_scheduler()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return 0

    poller.poll_once.side_effect = flaky

    with (
        patch("backend.poller.scheduler.SleepWatcher") as mock_watcher_cls,
        patch.object(threading.Event, "wait", return_value=True),
    ):
        mock_watcher_cls.return_value = MagicMock()
        scheduler.start()
        time.sleep(0.2)
        scheduler.stop()

    assert calls["n"] >= 1


def test_stop_before_start_does_not_raise() -> None:
    scheduler, _ = _make_scheduler()
    scheduler.stop()  # no thread started — must not raise
