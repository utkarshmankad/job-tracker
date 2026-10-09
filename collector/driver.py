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
    """The browser could not load or interact with a page (network, timeout, closed)."""


class PageDriver(Protocol):
    def goto(self, url: str) -> None: ...
    def current_url(self) -> str: ...
    def html(self) -> str: ...
    def click(self, selector: str) -> bool: ...
    def scroll_to_bottom(self) -> None: ...
    def pause(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class PlaywrightDriver:
    """A visible, persistent-profile Chromium/Chrome session (headful by default)."""

    def __init__(
        self,
        user_data_dir: Path,
        *,
        channel: str | None = "chrome",
        headless: bool = False,
        navigation_timeout_ms: int = 30_000,
    ) -> None:
        from playwright.sync_api import sync_playwright  # local-only dependency

        self._pw = sync_playwright().start()
        launch_args: dict[str, Any] = {"headless": headless}
        if channel:
            launch_args["channel"] = channel
        self._context = self._pw.chromium.launch_persistent_context(
            str(user_data_dir), **launch_args
        )
        self._context.set_default_navigation_timeout(navigation_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def goto(self, url: str) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            self._page.goto(url, wait_until="domcontentloaded")
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError as exc:
            raise DriverError("navigation failed") from exc

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
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError as exc:
            raise DriverError("click failed") from exc
        return True

    def scroll_to_bottom(self) -> None:
        self._page.mouse.wheel(0, 4000)
        self._page.wait_for_timeout(1500)

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
            raise DriverError("fixture page missing")
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

    def scroll_to_bottom(self) -> None:
        pending = self._scrolls.get(self._url)
        if pending:
            self._html = pending.pop(0)

    def pause(self, seconds: float) -> None:
        self.paused += seconds

    def close(self) -> None:
        return None
