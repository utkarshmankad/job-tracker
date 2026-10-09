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
    job_url: str | None = None,
    status_signal: ApplicationStatus | None = None,
) -> ParsedApplication:
    return ParsedApplication(
        message_id="msg-new",
        thread_id=thread_id,
        company=company,
        role=role,
        source_portal=source_portal,
        job_url=job_url,
        applied_date=utc_now(),
        status_signal=status_signal,
        raw_sender="hr@acme.com",
        raw_subject=f"Your application at {company}",
        is_classification_confident=True,
    )


# ---------------------------------------------------------------------------
# Duplicate suggestions (review only; scoring shared with the identity resolver)
# ---------------------------------------------------------------------------


def test_duplicate_candidate_pairs_are_review_only(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    first = _seed_app(db, company="Acme Pvt Ltd", role="Engineer")
    second = _seed_app(db, company="Acme", role="Engineer", source_portal="Naukri")

    pairs = DuplicateDetector(db).find_candidate_pairs()

    assert len(pairs) == 1
    assert {pairs[0]["primary"].id, pairs[0]["duplicate"].id} == {first.id, second.id}
    assert "Same normalized company" in pairs[0]["reasons"]
    assert db.get_application(first.id) is not None
    assert db.get_application(second.id) is not None


def test_same_company_different_role_is_not_suggested(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, company="Acme", role="Data Engineer")
    _seed_app(db, company="Acme", role="Sales Manager")
    assert DuplicateDetector(db).find_candidate_pairs() == []


def test_same_role_different_company_is_not_suggested(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, company="Acme", role="Software Engineer")
    _seed_app(db, company="Globex", role="Software Engineer")
    assert DuplicateDetector(db).find_candidate_pairs() == []


def test_same_canonical_url_is_suggested_despite_text_differences(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    a = _seed_app(db, company="Acme", role="Engineer")
    a.job_url = "https://jobs.example.com/123?utm_source=linkedin"
    db.upsert_application(a)
    b = _seed_app(db, company="Acme India", role="Software Engineer")
    b.job_url = "https://jobs.example.com/123"
    db.upsert_application(b)
    [pair] = DuplicateDetector(db).find_candidate_pairs()
    assert "Same canonical job URL" in pair["reasons"]


def test_find_duplicate_was_removed_in_favour_of_the_resolver() -> None:
    assert not hasattr(DuplicateDetector, "find_duplicate")


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
