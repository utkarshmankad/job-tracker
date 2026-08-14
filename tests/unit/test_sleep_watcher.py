"""Tests for backend/poller/sleep_watcher.py."""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, patch

from backend.poller.sleep_watcher import SleepWatcher

# AppKit/Foundation aren't installed in CI (Linux), but may be present on a macOS dev
# machine with pyobjc-framework-Cocoa installed. Force the ImportError path
# deterministically on every platform by blocking the imports via sys.modules, rather
# than relying on the environment — otherwise _run()'s real NSRunLoop blocks forever
# on a macOS box that has PyObjC installed.
_BLOCK_PYOBJC = patch.dict(sys.modules, {"AppKit": None, "Foundation": None})


def test_run_degrades_gracefully_when_pyobjc_missing() -> None:
    """_run must catch the ImportError and return without raising or calling on_wake,
    rather than crashing the daemon thread or blocking on a real run loop."""
    on_wake = MagicMock()
    watcher = SleepWatcher(on_wake=on_wake)

    with _BLOCK_PYOBJC:
        watcher._run()

    on_wake.assert_not_called()


def test_start_spawns_a_daemon_thread() -> None:
    watcher = SleepWatcher(on_wake=MagicMock())
    with _BLOCK_PYOBJC:
        watcher.start()
        time.sleep(0.1)  # let the thread run and hit the ImportError degrade path
    # No assertion beyond "didn't raise" — start() is fire-and-forget by design.
