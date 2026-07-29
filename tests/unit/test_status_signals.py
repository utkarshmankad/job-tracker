"""Tests for backend/parser/status_signals.py."""

from __future__ import annotations

from backend.db.models import ApplicationStatus
from backend.parser.status_signals import GLOBAL_STATUS_KEYWORDS


def test_all_keys_are_valid_application_statuses() -> None:
    for status in GLOBAL_STATUS_KEYWORDS:
        assert isinstance(status, ApplicationStatus)


def test_no_status_has_empty_keyword_list() -> None:
    for status, keywords in GLOBAL_STATUS_KEYWORDS.items():
        assert len(keywords) > 0, f"{status} has no keywords"


def test_all_keywords_are_lowercase() -> None:
    for keywords in GLOBAL_STATUS_KEYWORDS.values():
        for kw in keywords:
            assert kw == kw.lower(), f"{kw!r} is not lowercase"


def test_no_duplicate_keywords_within_a_status() -> None:
    for status, keywords in GLOBAL_STATUS_KEYWORDS.items():
        assert len(keywords) == len(set(keywords)), f"{status} has duplicate keywords"


def test_no_keyword_shared_across_statuses() -> None:
    """A single keyword mapping to two different statuses would make detection ambiguous."""
    seen: dict[str, ApplicationStatus] = {}
    for status, keywords in GLOBAL_STATUS_KEYWORDS.items():
        for kw in keywords:
            assert kw not in seen, f"{kw!r} appears under both {seen.get(kw)} and {status}"
            seen[kw] = status


def test_applied_and_joined_have_no_global_keywords() -> None:
    """APPLIED and JOINED are never inferred from generic keyword scanning."""
    assert ApplicationStatus.APPLIED not in GLOBAL_STATUS_KEYWORDS
    assert ApplicationStatus.JOINED not in GLOBAL_STATUS_KEYWORDS
