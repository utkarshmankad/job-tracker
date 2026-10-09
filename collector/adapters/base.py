"""Versioned adapter interface.

An adapter knows one job site's application-history page: where it is, how to recognise
signed-out, challenge, consent, rate-limit and empty states, which selectors read a row,
how to page through history, and how to map the site's status labels.

Honesty about selectors:
- The ``"primary"`` selector set yields ``extraction="verified"`` only after a person has
  confirmed it against the live site and recorded the date in ``LIVE_VERIFIED``. Until
  then it yields ``extraction="unverified"`` — it has been checked against sanitized
  fixtures only — and the tracker never creates applications from it automatically.
- ``"fallback"`` sets are alternative structures tried only when the primary set finds no
  rows; observations extracted with them are marked ``extraction="fallback"``.
- There is no free-form heuristic scraping. If no selector set finds rows on a page that is
  neither the explicit empty state nor a known stop state, the adapter raises
  ``AdapterError("selector_drift")`` — an empty result is never reported as success.
"""

from __future__ import annotations

import re
from abc import ABC
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from collector.core import AdapterError, ExtractedItem, PageResult, PageState

_WS = re.compile(r"\s+")
_RELATIVE = re.compile(r"(\d+)\s*(minute|min|hour|hr|day|d|week|wk|w|month|mo)s?\b", re.IGNORECASE)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_ABSOLUTE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*,?\s+(\d{4})")
_ABSOLUTE_US = re.compile(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),?\s+(\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2})\b")


def text_of(node: Tag | None) -> str | None:
    if node is None:
        return None
    value = _WS.sub(" ", node.get_text(" ", strip=True)).strip()
    return value or None


def parse_applied_date(text: str | None, today: date) -> str | None:
    """ISO date from the formats job sites use ("Applied 3 days ago", "Applied on 12 Sep
    2026", "Sep 12, 2026", "2026-09-12"). Unrecognised text yields None, never a guess."""
    if not text:
        return None
    if re.search(r"\btoday\b|just now", text, re.IGNORECASE):
        return today.isoformat()
    if re.search(r"\byesterday\b", text, re.IGNORECASE):
        return (today - timedelta(days=1)).isoformat()
    if m := _ISO.search(text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    for pattern, order in ((_ABSOLUTE, "dmy"), (_ABSOLUTE_US, "mdy")):
        if m := pattern.search(text):
            day, mon, year = (m[1], m[2], m[3]) if order == "dmy" else (m[2], m[1], m[3])
            month = _MONTHS.get(mon.lower()[:3])
            if month:
                try:
                    return date(int(year), month, int(day)).isoformat()
                except ValueError:
                    return None
    if m := _RELATIVE.search(text):
        return _relative(m, today)
    # Day and month without a year ("Applied on 2 Oct"): the most recent such date that is
    # not in the future.
    for pattern, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        for m in pattern.finditer(text):
            day, mon = (m[1], m[2]) if order == "dm" else (m[2], m[1])
            month = _MONTHS.get(mon.lower()[:3])
            if not month:
                continue
            for year in (today.year, today.year - 1):
                try:
                    candidate = date(year, month, int(day))
                except ValueError:
                    return None
                if candidate <= today:
                    return candidate.isoformat()
    return None


_DAYS_PER_UNIT = {
    "minute": 0,
    "min": 0,
    "hour": 0,
    "hr": 0,
    "day": 1,
    "d": 1,
    "week": 7,
    "wk": 7,
    "w": 7,
    "month": 30,
    "mo": 30,
}


def _relative(m: re.Match[str], today: date) -> str:
    """ISO date for "<n> <unit> ago" text."""
    return (today - timedelta(days=_DAYS_PER_UNIT[m[2].lower()] * int(m[1]))).isoformat()


@dataclass(frozen=True)
class SelectorSet:
    tier: str  # "primary" | "fallback"
    row: str
    company: str
    role: str
    status: str | None = None
    applied: str | None = None
    link: str | None = None
    item_id_attr: str | None = None  # attribute on the row holding the site's item ID


@dataclass(frozen=True)
class StateMarkers:
    """CSS selectors and URL fragments identifying non-list page states."""

    signed_out: tuple[str, ...] = ()
    signed_out_urls: tuple[str, ...] = ()
    challenge: tuple[str, ...] = (
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "#captcha",
        "[data-captcha]",
        "#challenge-form",
        "#cf-challenge-running",
    )
    challenge_urls: tuple[str, ...] = ("/checkpoint/", "/challenge", "captcha")
    challenge_text: tuple[str, ...] = (
        "verify you are human",
        "are you a robot",
        "unusual activity",
        "security verification",
        "enter the code we sent",
    )
    consent: tuple[str, ...] = ()
    rate_limited_text: tuple[str, ...] = ("too many requests", "rate limit", "try again later")
    empty: tuple[str, ...] = ()
    list_container: tuple[str, ...] = ()


class Adapter(ABC):
    """Base class for one source's application-history adapter."""

    SOURCE_KEY: ClassVar[str]
    VERSION: ClassVar[str]
    HISTORY_URL: ClassVar[str]
    ALLOWED_HOSTS: ClassVar[tuple[str, ...]]
    LIVE_VERIFIED: ClassVar[str | None] = None  # date a person confirmed selectors live
    PAGINATION: ClassVar[str] = "next"  # next | load_more | scroll | none
    NEXT_SELECTOR: ClassVar[str | None] = None
    LOAD_MORE_SELECTOR: ClassVar[str | None] = None
    SELECTORS: ClassVar[tuple[SelectorSet, ...]]
    MARKERS: ClassVar[StateMarkers]
    STATUS_MAP: ClassVar[dict[str, str]] = {}
    # Rows on these history pages are, by definition, applications the user submitted.
    PROVES_SUBMISSION: ClassVar[bool] = True
    ITEM_ID_FROM_URL: ClassVar[re.Pattern[str] | None] = None
    # False when real-session validation showed the adapter cannot safely collect (the
    # page moved, has no application history, or needs new selectors). Such a source is
    # reported "unsupported" and never scraped. UNSUPPORTED_REASON says why.
    SUPPORTED: ClassVar[bool] = True
    UNSUPPORTED_REASON: ClassVar[str] = ""
    # Regexes removed from extracted roles (e.g. visually hidden screen-reader suffixes).
    ROLE_NOISE: ClassVar[tuple[str, ...]] = ()
    # The site's own count of applications (e.g. a tab label "4 Applied"). When present,
    # a run that read fewer unique items ends "partial" (incomplete_history), and a list
    # that is absent while the count is 0 is the site's verified empty state.
    EXPECTED_COUNT_SELECTOR: ClassVar[str | None] = None
    EXPECTED_COUNT_PATTERN: ClassVar[re.Pattern[str] | None] = None

    @property
    def adapter_version(self) -> str:
        return f"{self.SOURCE_KEY}/{self.VERSION}"

    # -- page states ---------------------------------------------------------------

    def detect_state(self, soup: BeautifulSoup, url: str) -> PageState:
        markers = self.MARKERS
        lowered_url = url.lower()
        body_text = (text_of(soup.body) or "").lower() if soup.body else ""
        if (
            any(soup.select_one(s) for s in markers.challenge)
            or any(f in lowered_url for f in markers.challenge_urls)
            or any(t in body_text for t in markers.challenge_text)
        ):
            return PageState.CHALLENGE
        if any(t in body_text for t in markers.rate_limited_text) and not self._list_present(soup):
            return PageState.RATE_LIMITED
        if any(soup.select_one(s) for s in markers.consent):
            return PageState.CONSENT
        if any(f in lowered_url for f in markers.signed_out_urls) or any(
            soup.select_one(s) for s in markers.signed_out
        ):
            return PageState.SIGNED_OUT
        if not self._on_allowed_host(url):
            return PageState.UNKNOWN
        if any(soup.select_one(s) for s in markers.empty):
            return PageState.EMPTY
        if self._list_present(soup):
            return PageState.AUTHENTICATED
        if self.expected_count(soup) == 0:
            return PageState.EMPTY
        return PageState.UNKNOWN

    def expected_count(self, soup: BeautifulSoup) -> int | None:
        if not (self.EXPECTED_COUNT_SELECTOR and self.EXPECTED_COUNT_PATTERN):
            return None
        label = text_of(soup.select_one(self.EXPECTED_COUNT_SELECTOR))
        match = self.EXPECTED_COUNT_PATTERN.search(label or "")
        return int(match.group(1)) if match else None

    def ready_selectors(self) -> list[str]:
        """Selectors whose presence means the page has rendered enough to decide its state."""
        m = self.MARKERS
        out = [*m.list_container, *m.empty, *m.signed_out, *m.consent, *m.challenge]
        if self.EXPECTED_COUNT_SELECTOR:
            out.append(self.EXPECTED_COUNT_SELECTOR)
        return out

    def _list_present(self, soup: BeautifulSoup) -> bool:
        return any(soup.select_one(s) for s in self.MARKERS.list_container)

    def _on_allowed_host(self, url: str) -> bool:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.ALLOWED_HOSTS)

    # -- extraction ----------------------------------------------------------------

    def parse_page(self, html: str, url: str, today: date) -> PageResult:
        soup = BeautifulSoup(html, "html.parser")
        state = self.detect_state(soup, url)
        if state is not PageState.AUTHENTICATED:
            return PageResult(state)
        for selectors in self.SELECTORS:
            rows = soup.select(selectors.row)
            if not rows:
                continue
            items = [self._extract(row, selectors, today) for row in rows]
            usable = [i for i in items if i is not None]
            if not usable:
                # Rows exist but none has the required fields: the layout changed.
                raise AdapterError("selector_drift", f"{selectors.tier}_fields")
            return PageResult(
                PageState.AUTHENTICATED,
                usable,
                selector_tier=selectors.tier,
                has_more=self.has_more(soup),
                expected_count=self.expected_count(soup),
            )
        # The history list container is present but no known row structure matched.
        raise AdapterError("selector_drift", "no_rows")

    def _extract(self, row: Tag, sel: SelectorSet, today: date) -> ExtractedItem | None:
        company = text_of(row if sel.company == ":scope" else row.select_one(sel.company))
        if not company:
            return None
        role = text_of(row.select_one(sel.role))
        for noise in self.ROLE_NOISE:
            role = re.sub(noise, "", role or "", flags=re.IGNORECASE).strip() or None
        raw_status = text_of(row.select_one(sel.status)) if sel.status else None
        applied_text = text_of(row.select_one(sel.applied)) if sel.applied else None
        link = row.select_one(sel.link) if sel.link else None
        href = link.get("href") if link is not None else None
        job_url = self.absolute_url(href) if isinstance(href, str) else None
        item_id = None
        if sel.item_id_attr:
            raw = row.get(sel.item_id_attr)
            item_id = raw if isinstance(raw, str) and raw.strip() else None
        if item_id is None and job_url and self.ITEM_ID_FROM_URL is not None:
            if m := self.ITEM_ID_FROM_URL.search(job_url):
                item_id = m.group(1)
        status = self.map_status(raw_status)
        return ExtractedItem(
            company=company,
            role=role,
            source_item_id=item_id,
            applied_on=parse_applied_date(applied_text, today),
            raw_status=raw_status,
            status=status,
            job_url=job_url,
            proves_submission=self.PROVES_SUBMISSION and status != "unknown",
            extraction=self.extraction_label(sel.tier),
        )

    def extraction_label(self, tier: str) -> str:
        if tier != "primary":
            return "fallback"
        return "verified" if self.LIVE_VERIFIED else "unverified"

    def map_status(self, raw: str | None) -> str:
        """Site label → contract status. Matched on lowercase substrings, longest first, so
        "not selected" wins over "selected". Unknown labels map to "unknown"."""
        if not raw:
            return "applied"
        label = raw.lower()
        for key in sorted(self.STATUS_MAP, key=len, reverse=True):
            if key in label:
                return self.STATUS_MAP[key]
        return "unknown"

    def has_more(self, soup: BeautifulSoup) -> bool:
        for selector in (self.NEXT_SELECTOR, self.LOAD_MORE_SELECTOR):
            if selector:
                node = soup.select_one(selector)
                if (
                    node is not None
                    and not node.has_attr("disabled")
                    and node.get("aria-disabled") != "true"
                ):
                    return True
        return False

    def absolute_url(self, href: str) -> str | None:
        from urllib.parse import urljoin, urlsplit

        url = urljoin(self.HISTORY_URL, href.strip())
        return url if urlsplit(url).scheme == "https" and self._on_allowed_host(url) else None


@dataclass(frozen=True)
class AdapterInfo:
    source_key: str
    version: str
    history_url: str
    pagination: str
    live_verified: str | None
    selector_tiers: list[str] = field(default_factory=list)
    notes: str = ""
