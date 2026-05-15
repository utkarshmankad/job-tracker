"""Tests for backend/engine/status_updater.py."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus
from backend.engine.duplicate_detector import DuplicateDetector
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
        applied_date=datetime.utcnow(),
        status_signal=status_signal,
        raw_sender="noreply@linkedin.com",
        raw_subject=f"Your application to {company}",
        is_classification_confident=True,
    )


def _make_db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "test.db")


def _mock_detector(existing: Application | None = None) -> MagicMock:
    detector = MagicMock(spec=DuplicateDetector)
    detector.find_duplicate.return_value = existing
    detector.merge.side_effect = lambda e, p: e
    return detector


def _make_updater(db: DataStore, existing: Application | None = None) -> StatusUpdater:
    return StatusUpdater(db, _mock_detector(existing))


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_new_application_created_when_no_existing(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    app = updater.process(_make_parsed())

    assert app.id is not None
    assert app.company == "Acme"
    assert app.current_status == ApplicationStatus.APPLIED


def test_applied_to_shortlisted_valid(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app = updater.process(_make_parsed(message_id="msg-001"))

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")

    updated = db.get_application(app.id)
    assert updated is not None
    assert updated.current_status == ApplicationStatus.RESUME_SHORTLISTED


def test_shortlisted_to_interview_valid(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app = updater.process(_make_parsed())

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")
    app = db.get_application(app.id)

    updater._advance_status(app, ApplicationStatus.INTERVIEW_SCHEDULED, "msg-003")
    app = db.get_application(app.id)

    assert app.current_status == ApplicationStatus.INTERVIEW_SCHEDULED


def test_offer_to_applied_blocked(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app = updater.process(_make_parsed())

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
    app = updater.process(_make_parsed())
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
    app = updater.process(_make_parsed(thread_id="thread-001", message_id="msg-001"))
    assert app.id is not None

    # _find_existing with the same thread_id should return the same record
    # (bypassing the fuzzy detector entirely)
    found = updater._find_existing(_make_parsed(thread_id="thread-001", message_id="msg-002"))

    assert found is not None
    assert found.id == app.id


def test_status_history_written_on_transition(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    app = updater.process(_make_parsed(message_id="msg-001"))

    updater._advance_status(app, ApplicationStatus.RESUME_SHORTLISTED, "msg-002")

    history = db.get_status_history(app.id)
    # creation entry + advance entry
    assert len(history) >= 2

    advance = next(
        h for h in history if h.to_status == ApplicationStatus.RESUME_SHORTLISTED.value
    )
    assert advance.trigger == "email"
    assert advance.message_id == "msg-002"
    assert advance.from_status == ApplicationStatus.APPLIED.value


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_find_existing_uses_thread_id_lookup(tmp_path: Path) -> None:
    """_find_existing uses DB lookup, not a full table scan."""
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    # Create app that owns the thread
    created = updater.process(_make_parsed(thread_id="t-lookup", message_id="msg-1"))
    assert created.id is not None

    # _find_existing should find it via find_application_by_thread_id
    found = updater._find_existing(_make_parsed(thread_id="t-lookup", message_id="msg-2"))
    assert found is not None
    assert found.id == created.id


def test_find_existing_falls_back_to_detector_when_no_thread_match(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    # Detector reports an existing match for a different thread
    from backend.db.models import Application
    from datetime import datetime as dt
    existing_app = Application(
        id=1,
        company="Acme",
        role="Engineer",
        source_portal="LinkedIn",
        applied_date=dt.utcnow(),
        thread_ids='["different-thread"]',
    )
    detector = MagicMock(spec=DuplicateDetector)
    detector.find_duplicate.return_value = existing_app
    updater = StatusUpdater(db, detector)

    result = updater._find_existing(_make_parsed(thread_id="brand-new-thread"))
    # No DB thread match, so falls back to detector
    detector.find_duplicate.assert_called_once()


def test_process_with_status_signal_creates_and_advances(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)
    parsed = _make_parsed(
        message_id="msg-001",
        status_signal=ApplicationStatus.RESUME_SHORTLISTED,
    )

    app = updater.process(parsed)
    assert app.current_status == ApplicationStatus.RESUME_SHORTLISTED


def test_process_existing_with_status_signal_advances(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    updater = _make_updater(db)

    # First email creates the app on thread-001
    app = updater.process(_make_parsed(thread_id="thread-001", message_id="msg-001"))
    assert app.current_status == ApplicationStatus.APPLIED

    # Second email on same thread carries a status signal
    updater2 = _make_updater(db)
    updated = updater2.process(
        _make_parsed(
            thread_id="thread-001",
            message_id="msg-002",
            status_signal=ApplicationStatus.RESUME_SHORTLISTED,
        )
    )
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
    app = updater.process(_make_parsed())

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
