"""Shared types for adapters, the runner and the CLI."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class PageState(enum.StrEnum):
    """What the collector believes the current page is. Anything other than AUTHENTICATED
    or EMPTY stops the adapter: the collector never guesses its way past an unknown page."""

    AUTHENTICATED = "authenticated"  # the application-history list is visible
    EMPTY = "empty"  # the site's explicit "no applications" state is visible
    SIGNED_OUT = "signed_out"
    CHALLENGE = "challenge"  # CAPTCHA, verification, "are you a robot", 2FA prompt
    CONSENT = "consent"  # cookie/terms wall blocking the page
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


# PageState -> (run status, error code) for states that end a run.
STOP_STATES: dict[PageState, tuple[str, str]] = {
    PageState.SIGNED_OUT: ("signed_out", "signed_out"),
    PageState.CHALLENGE: ("challenged", "challenge"),
    PageState.CONSENT: ("challenged", "consent_required"),
    PageState.RATE_LIMITED: ("challenged", "rate_limited"),
    PageState.UNKNOWN: ("failed", "unexpected_page"),
}


class AdapterError(Exception):
    """An adapter stopped safely. `code` is one of contract.ERROR_MESSAGES' keys; `detail`
    is a fixed internal code (e.g. which selector group failed) — never page content."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ExtractedItem:
    """One row an adapter read from a page, before contract validation."""

    company: str | None
    role: str | None
    source_item_id: str | None
    applied_on: str | None  # ISO date, or None
    raw_status: str | None
    status: str  # a contract.CollectorStatus value
    job_url: str | None
    proves_submission: bool
    extraction: str  # verified | unverified | fallback (see adapters/base.py)


@dataclass
class PageResult:
    state: PageState
    items: list[ExtractedItem] = field(default_factory=list)
    selector_tier: str = "primary"  # which selector set matched: primary | fallback
    has_more: bool = False
    expected_count: int | None = None  # the site's own count, when it shows one


@dataclass
class Diagnostics:
    """Aggregate, content-free run diagnostics (counts and fixed codes only)."""

    pages: int = 0
    items_extracted: int = 0
    items_valid: int = 0
    items_invalid: int = 0
    duplicates_in_run: int = 0
    fallback_pages: int = 0
    batches_sent: int = 0
    stopped_state: str | None = None
    stopped_detail: str | None = None
    expected_count: int | None = None

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pages": self.pages,
            "items_extracted": self.items_extracted,
            "items_valid": self.items_valid,
            "items_invalid": self.items_invalid,
            "duplicates_in_run": self.duplicates_in_run,
            "fallback_pages": self.fallback_pages,
            "batches_sent": self.batches_sent,
        }
        if self.expected_count is not None:
            out["expected_count"] = self.expected_count
        if self.stopped_state:
            out["stopped_state"] = self.stopped_state
        if self.stopped_detail:
            out["stopped_detail"] = self.stopped_detail
        return out
