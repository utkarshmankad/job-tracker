"""Tests for backend/engine/duplicate_detector.py."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.parser.email_parser import ParsedApplication

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "test.db")


def _seed_app(
    db: DataStore,
    *,
    company: str = "Acme",
    role: str = "Engineer",
    source_portal: str = "LinkedIn",
    days_ago: int = 10,
) -> Application:
    app = Application(
        company=company,
        role=role,
        source_portal=source_portal,
        applied_date=utc_now() - timedelta(days=days_ago),
        current_status=ApplicationStatus.APPLIED,
    )
    return db.upsert_application(app)


def _make_parsed(
    *,
    company: str = "Acme",
    role: str = "Engineer",
    source_portal: str = "LinkedIn",
    thread_id: str = "t-new",
) -> ParsedApplication:
    return ParsedApplication(
        message_id="msg-new",
        thread_id=thread_id,
        company=company,
        role=role,
        source_portal=source_portal,
        job_url=None,
        applied_date=utc_now(),
        status_signal=None,
        raw_sender="hr@acme.com",
        raw_subject=f"Your application at {company}",
        is_classification_confident=True,
    )


# ---------------------------------------------------------------------------
# find_duplicate tests
# ---------------------------------------------------------------------------


def test_find_duplicate_exact_match(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db, company="Google", role="SWE")
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(_make_parsed(company="Google", role="SWE"))
    assert result is not None
    assert result.id == existing.id


def test_find_duplicate_high_fuzzy_score(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db, company="Google LLC", role="Software Engineer")
    detector = DuplicateDetector(db, threshold=80)

    result = detector.find_duplicate(_make_parsed(company="Google LLC", role="Software Engineer I"))
    assert result is not None
    assert result.id == existing.id


def test_find_duplicate_low_score_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, company="Amazon", role="Developer")
    detector = DuplicateDetector(db, threshold=85)

    result = detector.find_duplicate(_make_parsed(company="Microsoft", role="Designer"))
    assert result is None


def test_find_duplicate_no_candidates_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(_make_parsed())
    assert result is None


def test_find_duplicate_empty_query_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, company="Meta", role="PM")
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(_make_parsed(company="", role=""))
    assert result is None


def test_find_duplicate_outside_lookup_window_not_matched(tmp_path: Path) -> None:
    """Applications older than 90 days should not be considered for deduplication."""
    db = _make_db(tmp_path)
    _seed_app(db, company="OldCo", role="Engineer", days_ago=95)
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(_make_parsed(company="OldCo", role="Engineer"))
    assert result is None


def test_find_duplicate_filters_by_source_portal(tmp_path: Path) -> None:
    """Candidates from a different source_portal are not considered."""
    db = _make_db(tmp_path)
    _seed_app(db, company="Acme", role="Engineer", source_portal="Naukri")
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(
        _make_parsed(company="Acme", role="Engineer", source_portal="LinkedIn")
    )
    assert result is None


def test_find_duplicate_same_portal_matched(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db, company="Acme", role="Engineer", source_portal="LinkedIn")
    detector = DuplicateDetector(db)

    result = detector.find_duplicate(
        _make_parsed(company="Acme", role="Engineer", source_portal="LinkedIn")
    )
    assert result is not None
    assert result.id == existing.id


# ---------------------------------------------------------------------------
# merge tests
# ---------------------------------------------------------------------------


def test_merge_adds_new_thread_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db)
    existing.thread_ids = json.dumps(["old-thread"])
    existing = db.upsert_application(existing)

    detector = DuplicateDetector(db)
    parsed = _make_parsed(thread_id="new-thread")

    merged = detector.merge(existing, parsed)
    thread_ids = json.loads(merged.thread_ids)
    assert "old-thread" in thread_ids
    assert "new-thread" in thread_ids


def test_merge_does_not_duplicate_thread_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db)
    existing.thread_ids = json.dumps(["same-thread"])
    existing = db.upsert_application(existing)

    detector = DuplicateDetector(db)
    parsed = _make_parsed(thread_id="same-thread")

    merged = detector.merge(existing, parsed)
    thread_ids = json.loads(merged.thread_ids)
    assert thread_ids.count("same-thread") == 1


def test_merge_updates_applied_date_to_earlier(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db, days_ago=5)
    original_date = existing.applied_date

    detector = DuplicateDetector(db)
    earlier_date = utc_now() - timedelta(days=10)
    parsed = ParsedApplication(
        message_id="m",
        thread_id="t",
        company="Acme",
        role="Engineer",
        source_portal="LinkedIn",
        job_url=None,
        applied_date=earlier_date,
        status_signal=None,
        raw_sender="hr@acme.com",
        raw_subject="Your application",
        is_classification_confident=True,
    )

    merged = detector.merge(existing, parsed)
    assert merged.applied_date < original_date


def test_merge_keeps_applied_date_if_new_is_later(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    existing = _seed_app(db, days_ago=10)
    original_date = existing.applied_date

    detector = DuplicateDetector(db)
    later_parsed = _make_parsed()  # applied_date = now (more recent)
    later_parsed = ParsedApplication(
        message_id="m",
        thread_id="t",
        company="Acme",
        role="Engineer",
        source_portal="LinkedIn",
        job_url=None,
        applied_date=utc_now(),  # more recent
        status_signal=None,
        raw_sender="hr@acme.com",
        raw_subject="Your application",
        is_classification_confident=True,
    )

    merged = detector.merge(existing, later_parsed)
    # applied_date should not change when new date is later
    assert abs((merged.applied_date - original_date).total_seconds()) < 2
