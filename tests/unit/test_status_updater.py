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
