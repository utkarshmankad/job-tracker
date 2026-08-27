"""Tests for backend/db/models.py — schema defaults, enum storage, and the
UTCDateTime type's timezone round-trip behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from backend.db.models import (
    Application,
    ApplicationStatus,
    ApplicationThreadId,
    ProcessedMessage,
    StatusHistory,
    SuppressRule,
    utc_now,
)


def test_utc_now_is_timezone_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_application_defaults(db) -> None:
    app = Application(source_portal="Naukri", applied_date=utc_now())
    saved = db.upsert_application(app)

    assert saved.id is not None
    assert saved.current_status == ApplicationStatus.APPLIED
    assert saved.thread_ids == "[]"
    assert saved.is_false_positive is False
    assert saved.withdraw_reason is None
    assert saved.company is None
    assert saved.role is None


def test_current_status_stored_as_enum_value_not_name(db) -> None:
    """SAEnum is configured with values_callable so the DB stores 'Applied',
    not the Python member name 'APPLIED' — required for get_raw_status_values()
    (data_store.py) to read it back as a plain string without enum coercion."""
    app = Application(
        source_portal="Naukri",
        applied_date=utc_now(),
        current_status=ApplicationStatus.INTERVIEW_SCHEDULED,
    )
    db.upsert_application(app)

    raw_values = db.get_raw_status_values()
    assert "Interview Scheduled" in raw_values
    assert "INTERVIEW_SCHEDULED" not in raw_values


def test_utc_datetime_roundtrip_preserves_tz_aware_utc(db) -> None:
    applied = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    app = Application(source_portal="Naukri", applied_date=applied)
    saved = db.upsert_application(app)

    with Session(db._engine) as session:
        fetched = session.get(Application, saved.id)
        assert fetched is not None
        assert fetched.applied_date.tzinfo is not None
        assert fetched.applied_date == applied


def test_utc_datetime_accepts_naive_value_as_utc(db) -> None:
    """process_bind_param treats a naive input as already-UTC rather than rejecting it —
    callers that pass datetime.utcnow() (naive) don't corrupt applied_date on write."""
    naive_applied = datetime(2026, 1, 15, 10, 30)  # noqa: DTZ001 — deliberately naive input
    app = Application(source_portal="Naukri", applied_date=naive_applied)
    saved = db.upsert_application(app)

    with Session(db._engine) as session:
        fetched = session.get(Application, saved.id)
        assert fetched is not None
        assert fetched.applied_date == naive_applied.replace(tzinfo=UTC)


def test_status_history_requires_application_fk(db) -> None:
    app = Application(source_portal="Naukri", applied_date=utc_now())
    saved = db.upsert_application(app)

    with Session(db._engine) as session:
        history = StatusHistory(
            application_id=saved.id,
            to_status=ApplicationStatus.APPLIED.value,
            trigger="manual",
        )
        session.add(history)
        session.commit()
        session.refresh(history)
        assert history.id is not None
        assert history.from_status is None


def test_application_thread_id_indexed_lookup(db) -> None:
    app = Application(source_portal="Naukri", applied_date=utc_now())
    saved = db.upsert_application(app)

    with Session(db._engine) as session:
        link = ApplicationThreadId(application_id=saved.id, thread_id="thread-123")
        session.add(link)
        session.commit()

        found = session.exec(
            select(ApplicationThreadId).where(ApplicationThreadId.thread_id == "thread-123")
        ).first()
        assert found is not None
        assert found.application_id == saved.id


def test_suppress_rule_optional_subject_pattern(db) -> None:
    with Session(db._engine) as session:
        rule = SuppressRule(sender_pattern="noreply@spam.com")
        session.add(rule)
        session.commit()
        session.refresh(rule)
        assert rule.subject_pattern is None
        assert rule.created_at.tzinfo is not None


def test_poller_state_is_a_singleton_row(db) -> None:
    """PollerState.id defaults to 1 — DataStore relies on there being exactly one row."""
    state = db.get_poller_state()
    assert state.id == 1
    assert state.status == "SLEEPING"
    assert state.last_history_id is None


def test_processed_message_message_id_is_primary_key(db) -> None:
    with Session(db._engine) as session:
        msg = ProcessedMessage(message_id="msg-1", result="applied")
        session.add(msg)
        session.commit()

    assert db.is_processed("msg-1")
    assert not db.is_processed("msg-2")
