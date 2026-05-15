"""macOS sleep/wake notification listener.

Triggers a callback immediately when the machine wakes from sleep so the
poller runs without waiting for the next scheduled interval.

Requires pyobjc-framework-Cocoa on macOS. Degrades gracefully if absent.
"""

from __future__ import annotations

import threading
from typing import Callable

import structlog

log = structlog.get_logger(__name__)


class SleepWatcher:
    def __init__(self, on_wake: Callable[[], None]) -> None:
        self._on_wake = on_wake

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True, name="sleep-watcher")
        t.start()

    def _run(self) -> None:
        try:
            from AppKit import NSWorkspace, NSWorkspaceDidWakeNotification  # type: ignore[import]
            from Foundation import NSObject, NSRunLoop, NSDate  # type: ignore[import]

            on_wake = self._on_wake

            class _WakeDelegate(NSObject):  # type: ignore[misc]
                def handleWake_(self_, _notification: object) -> None:  # noqa: N805
                    log.info("mac_wake_detected")
                    try:
                        on_wake()
                    except Exception as exc:
                        log.warning("wake_callback_error", error=str(exc))

            delegate = _WakeDelegate.alloc().init()
            NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
                delegate, "handleWake:", NSWorkspaceDidWakeNotification, None
            )
            log.info("sleep_watcher_active")
            loop = NSRunLoop.currentRunLoop()
            while True:
                loop.runMode_beforeDate_(
                    "NSDefaultRunLoopMode", NSDate.dateWithTimeIntervalSinceNow_(1.0)
                )
        except ImportError:
            log.warning(
                "sleep_watcher_disabled",
                reason="pyobjc-framework-Cocoa not installed; install it for instant wake polling",
            )
        except Exception as exc:
            log.error("sleep_watcher_error", error=str(exc))
