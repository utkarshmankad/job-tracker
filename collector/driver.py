"""Browser access, behind a small interface so adapters run identically against a real
browser and against sanitized fixtures.

The Playwright driver opens an explicitly configured persistent browser profile in a
visible window. It adds no stealth plugins, fingerprint spoofing, proxy rotation or
automation-hiding flags: sites see an ordinary automated browser, and if they object the
collector stops. It only navigates, reads page HTML, scrolls and clicks controls that an
adapter declares as read-only pagination ("Next", "Show more").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class DriverError(RuntimeError):
    """The browser could not load or interact with a page. ``reason`` is a fixed code
    (target_closed | timeout | network | browser_error) — never page content."""

    def __init__(self, message: str, reason: str = "browser_error") -> None:
        super().__init__(message)
        self.reason = reason


def classify_browser_error(exc: BaseException) -> str:
    text = str(exc)
    if "has been closed" in text or "Target closed" in text:
        return "target_closed"
    if "Timeout" in text or "timeout" in text:
        return "timeout"
    if "net::ERR_" in text:
        return "network"
    return "browser_error"


class PageDriver(Protocol):
    def goto(self, url: str) -> None: ...
    def current_url(self) -> str: ...
    def html(self) -> str: ...
    def click(self, selector: str) -> bool: ...
    def scroll_to_bottom(self) -> None: ...
    def wait_for_any(self, selectors: list[str], timeout_seconds: float) -> bool: ...
    def pause(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class PlaywrightDriver:
    """A visible, persistent-profile Chromium/Chrome session (headful by default)."""

    def __init__(
        self,
        user_data_dir: Path,
        *,
        channel: str | None = "chrome",
        executable_path: Path | None = None,
        headless: bool = False,
        navigation_timeout_ms: int = 30_000,
    ) -> None:
        from playwright.sync_api import sync_playwright  # local-only dependency

        self._pw = sync_playwright().start()
        launch_args: dict[str, Any] = {"headless": headless}
        if executable_path is not None:
            launch_args["executable_path"] = str(executable_path)
        elif channel:
            launch_args["channel"] = channel
        self._context = self._pw.chromium.launch_persistent_context(
            str(user_data_dir), **launch_args
        )
        self._context.set_default_navigation_timeout(navigation_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def goto(self, url: str) -> None:
        from playwright.sync_api import Error as PlaywrightError

        self._ensure_page()
        try:
            self._page.goto(url, wait_until="load")
        except PlaywrightError as exc:
            raise DriverError("navigation failed", classify_browser_error(exc)) from exc
        self._settle()

    def _ensure_page(self) -> None:
        """Keep working in a live tab: sign-in flows can close or replace the tab the
        collector opened. Reuse the newest open tab, or open one."""
        if not self._page.is_closed():
            return
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = open_pages[-1] if open_pages else self._context.new_page()

    def current_url(self) -> str:
        return str(self._page.url)

    def html(self) -> str:
        return str(self._page.content())

    def click(self, selector: str) -> bool:
        from playwright.sync_api import Error as PlaywrightError

        try:
            locator = self._page.locator(selector).first
            if locator.count() == 0 or not locator.is_visible() or not locator.is_enabled():
                return False
            locator.click()
        except PlaywrightError as exc:
            raise DriverError("click failed", classify_browser_error(exc)) from exc
        self._settle()
        return True

    def _settle(self) -> None:
        """Give client-rendered lists time to appear. Many sites never reach "network idle"
        (analytics, polling), so idle is best-effort, not a requirement."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            self._page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeout:
            pass
        self._page.wait_for_timeout(1_500)

    def scroll_to_bottom(self) -> None:
        self._page.mouse.wheel(0, 4000)
        self._page.wait_for_timeout(1500)

    def wait_for_any(self, selectors: list[str], timeout_seconds: float) -> bool:
        """Wait until one of the selectors is present (client-rendered history lists appear
        after "load"). Returns False on timeout; the caller then decides from what it sees."""
        from playwright.sync_api import Error as PlaywrightError

        joined = ", ".join(selectors)
        if not joined:
            return False
        try:
            self._page.wait_for_selector(joined, state="attached", timeout=timeout_seconds * 1000)
        except PlaywrightError:
            return False
        return True

    def pause(self, seconds: float) -> None:
        self._page.wait_for_timeout(int(seconds * 1000))

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()


class FixtureDriver:
    """Serves sanitized HTML fixtures as if they were pages (tests and dry demos).

    ``pages`` maps a URL to its HTML. ``clicks`` maps (url, selector) to the URL a click
    leads to; ``scrolls`` maps a URL to the HTML it shows after a scroll (lazy loading).
    """

    def __init__(
        self,
        pages: dict[str, str],
        clicks: dict[tuple[str, str], str] | None = None,
        scrolls: dict[str, list[str]] | None = None,
        redirects: dict[str, str] | None = None,
    ) -> None:
        self._pages = pages
        self._clicks = clicks or {}
        self._scrolls = {k: list(v) for k, v in (scrolls or {}).items()}
        self._redirects = redirects or {}
        self._url = "about:blank"
        self._html = ""
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.paused: float = 0.0

    def goto(self, url: str) -> None:
        target = self._redirects.get(url, url)
        if target not in self._pages:
            raise DriverError("fixture page missing", "network")
        self._url = target
        self._html = self._pages[target]
        self.visited.append(target)

    def current_url(self) -> str:
        return self._url

    def html(self) -> str:
        return self._html

    def click(self, selector: str) -> bool:
        target = self._clicks.get((self._url, selector))
        if target is None:
            return False
        self.clicked.append(selector)
        self.goto(target)
        return True

    def wait_for_any(self, selectors: list[str], timeout_seconds: float) -> bool:
        return True

    def scroll_to_bottom(self) -> None:
        pending = self._scrolls.get(self._url)
        if pending:
            self._html = pending.pop(0)

    def pause(self, seconds: float) -> None:
        self.paused += seconds

    def close(self) -> None:
        return None
