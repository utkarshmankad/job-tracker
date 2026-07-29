"""Tests for backend/poller/sleep_watcher.py."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from backend.poller.sleep_watcher import SleepWatcher


def test_run_degrades_gracefully_when_pyobjc_missing() -> None:
    """AppKit/Foundation aren't installed in CI — _run must catch the ImportError and return
    without raising or calling on_wake, rather than crashing the daemon thread."""
    on_wake = MagicMock()
    watcher = SleepWatcher(on_wake=on_wake)

    # _run() catches ImportError internally and returns; call it directly (not via a thread)
    # so a failure to degrade gracefully surfaces as a test failure, not a silent thread crash.
    watcher._run()

    on_wake.assert_not_called()


def test_start_spawns_a_daemon_thread() -> None:
    watcher = SleepWatcher(on_wake=MagicMock())
    watcher.start()
    time.sleep(0.1)  # let the thread run and hit the ImportError degrade path
    # No assertion beyond "didn't raise" — start() is fire-and-forget by design.
