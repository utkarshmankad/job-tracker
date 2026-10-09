"""Tests for backend/engine/status_updater.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import ApplicationStatus, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import signals_from_parsed
from backend.engine.status_updater import StatusUpdater
from backend.parser.email_parser import ParsedApplication

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_parsed(
    *,
    message_id: str = "msg-001",
    thread_id: str = "thread-001",
    company: str = "Acme",
    role: str = "Engineer",
    source_portal: str = "LinkedIn",
    status_signal: ApplicationStatus | None = None,
) -> ParsedApplication:
    return ParsedApplication(
        message_id=message_id,
        thread_id=thread_id,
        company=company,
        role=role,
        source_portal=source_portal,
        job_url=None,
        applied_date=utc_now(),
        status_signal=status_signal,
        raw_sender="noreply@linkedin.com",
        raw_subject=f"Your application to {company}",
        is_classification_confident=True,
    )


def _make_db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "test.db")


def _make_updater(db: DataStore) -> StatusUpdater:
    return StatusUpdater(db, DuplicateDetector(db))


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_new_application_created_when_no_existing(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    app, is_new = updater.process(_make_parsed())

    assert is_new is True
    assert app.id is not None
    assert app.company == "Acme"
    assert app.current_status == ApplicationStatus.APPLIED


def test_applied_to_shortlisted_valid(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed(message_id="msg-001"))

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")

    updated = db.get_application(app.id)
    assert updated is not None
    assert updated.current_status == ApplicationStatus.RESUME_SHORTLISTED


def test_shortlisted_to_interview_valid(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed())

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")
    app = db.get_application(app.id)

    updater._advance_status(app, ApplicationStatus.INTERVIEW_SCHEDULED, "msg-003")
    app = db.get_application(app.id)

    assert app.current_status == ApplicationStatus.INTERVIEW_SCHEDULED


def test_offer_to_applied_blocked(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed())

    # Advance to OFFER through valid path
    for signal, msg in [
        (ApplicationStatus.RESUME_SHORTLISTED, "msg-2"),
        (ApplicationStatus.INTERVIEW_SCHEDULED, "msg-3"),
        (ApplicationStatus.INTERVIEW_IN_PROGRESS, "msg-4"),
        (ApplicationStatus.OFFER, "msg-5"),
    ]:
        updater._advance_status(app, signal, msg)
        app = db.get_application(app.id)

    assert app.current_status == ApplicationStatus.OFFER

    # Attempt invalid regression: OFFER → APPLIED
    updater._advance_status(app, ApplicationStatus.APPLIED, "msg-6")

    app = db.get_application(app.id)
    assert app.current_status == ApplicationStatus.OFFER  # unchanged


def test_manual_override_bypasses_state_machine(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed())
    assert app.current_status == ApplicationStatus.APPLIED

    updated = updater.manual_update(app.id, ApplicationStatus.JOINED)

    assert updated.current_status == ApplicationStatus.JOINED

    history = db.get_status_history(app.id)
    manual_entry = next(h for h in history if h.trigger == "manual")
    assert manual_entry.to_status == ApplicationStatus.JOINED.value


def test_existing_found_by_thread_id(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    # Create an application that owns thread-001
    app, _ = updater.process(_make_parsed(thread_id="thread-001", message_id="msg-001"))
    assert app is not None and app.id is not None

    # The resolver finds it by thread, before any fuzzy matching.
    match = updater._resolver.resolve(
        signals_from_parsed(_make_parsed(thread_id="thread-001", message_id="msg-002"))
    )

    assert match.outcome == "linked"
    assert match.application_id == app.id
    assert match.link_method == "thread"
    assert match.confidence == 1.0


def test_status_history_written_on_transition(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed(message_id="msg-001"))

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")

    history = db.get_status_history(app.id)
    # creation entry + advance entry
    assert len(history) >= 2

    advance = next(h for h in history if h.to_status == ApplicationStatus.RESUME_SHORTLISTED.value)
    assert advance.trigger == "email"
    assert advance.message_id == "msg-002"
    assert advance.from_status == ApplicationStatus.APPLIED.value


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_find_existing_uses_thread_id_lookup(tmp_path: Path) -> None:
    """Thread resolution uses the indexed DB lookup, not a table scan."""
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    created, _ = updater.process(_make_parsed(thread_id="t-lookup", message_id="msg-1"))
    assert created is not None and created.id is not None

    with patch.object(db, "get_applications", wraps=db.get_applications) as scan:
        match = updater._resolver.resolve(
            signals_from_parsed(_make_parsed(thread_id="t-lookup", message_id="msg-2"))
        )
    assert match.application_id == created.id
    scan.assert_not_called()


def test_resolution_falls_back_to_company_role_when_no_thread_match(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    created, _ = updater.process(_make_parsed(thread_id="t-original", message_id="msg-1"))
    assert created is not None

    match = updater._resolver.resolve(
        signals_from_parsed(_make_parsed(thread_id="brand-new-thread"))
    )
    assert match.application_id == created.id
    assert match.link_method == "company_role"


def test_status_email_without_application_needs_review_instead_of_creating(
    tmp_path: Path,
) -> None:
    """Phase 2 behaviour change: a status email that matches no application no longer
    creates (and advances) a new one — that was the main duplicate source."""
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    parsed = _make_parsed(
        message_id="msg-001",
        status_signal=ApplicationStatus.RESUME_SHORTLISTED,
    )

    app, is_new = updater.process(parsed)
    assert app is None
    assert is_new is False
    assert db.get_applications(ApplicationFilter())[1] == 0
    assert db.is_processed("msg-001")


def test_process_existing_with_status_signal_advances(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    # First email creates the app on thread-001
    app, is_new = updater.process(_make_parsed(thread_id="thread-001", message_id="msg-001"))
    assert is_new is True
    assert app.current_status == ApplicationStatus.APPLIED

    # Second email on same thread carries a status signal
    updater2 = _make_updater(db)
    updated, is_new2 = updater2.process(
        _make_parsed(
            thread_id="thread-001",
            message_id="msg-002",
            status_signal=ApplicationStatus.RESUME_SHORTLISTED,
        )
    )
    assert is_new2 is False
    assert updated.current_status == ApplicationStatus.RESUME_SHORTLISTED


def test_manual_update_nonexistent_raises_value_error(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    with pytest.raises(ValueError, match="not found"):
        updater.manual_update(99999, ApplicationStatus.REJECTED)


def test_invalid_transition_is_silently_ignored(tmp_path: Path) -> None:
    """Backward transitions must not raise — they just log a warning and skip."""
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app, _ = updater.process(_make_parsed())

    # Force-advance to OFFER via valid chain
    for signal, msg in [
        (ApplicationStatus.RESUME_SHORTLISTED, "m2"),
        (ApplicationStatus.INTERVIEW_SCHEDULED, "m3"),
        (ApplicationStatus.INTERVIEW_IN_PROGRESS, "m4"),
        (ApplicationStatus.OFFER, "m5"),
    ]:
        updater._advance_status(app, signal, msg)
        app = db.get_application(app.id)

    # Attempt illegal regression
    updater._advance_status(app, ApplicationStatus.APPLIED, "m6")
    app = db.get_application(app.id)
    assert app.current_status == ApplicationStatus.OFFER
