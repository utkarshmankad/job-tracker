"""Tests for backend/engine/identity_resolver.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.engine.identity_resolver import (
    IdentityResolver,
    MessageKind,
    classify_message_kind,
)
from backend.parser.email_parser import ParsedApplication


def _parsed(
    *,
    thread_id: str = "new-thread",
    company: str | None = "Acme",
    role: str | None = "Engineer",
    subject: str = "Your application to Acme",
    status: ApplicationStatus | None = None,
    job_url: str | None = None,
) -> ParsedApplication:
    return ParsedApplication(
        message_id="m",
        thread_id=thread_id,
        company=company,
        role=role,
        source_portal="LinkedIn",
        job_url=job_url,
        applied_date=utc_now(),
        status_signal=status,
        raw_sender="jobs@example.com",
        raw_subject=subject,
        is_classification_confident=True,
    )


@pytest.fixture
def db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "resolver.db")


def _app(db: DataStore, company: str, role: str | None = "Engineer", **extra) -> Application:
    return db.upsert_application(
        Application(
            company=company,
            role=role,
            source_portal="LinkedIn",
            applied_date=extra.pop("applied_date", utc_now()),
            current_status=extra.pop("status", ApplicationStatus.APPLIED),
            thread_ids=extra.pop("thread_ids", "[]"),
            **extra,
        )
    )


# ------------------------------------------------------------------ #
# Message kind                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("subject", "status", "expected"),
    [
        ("Your application to Acme", None, MessageKind.ACKNOWLEDGEMENT),
        ("Application received - Engineer", None, MessageKind.ACKNOWLEDGEMENT),
        ("Thank you for applying", None, MessageKind.ACKNOWLEDGEMENT),
        ("Re: Your application to Acme", None, MessageKind.FOLLOW_UP),
        ("Fwd: Application", None, MessageKind.FOLLOW_UP),
        ("Interview with Acme", None, MessageKind.FOLLOW_UP),
        ("Please share your availability", None, MessageKind.FOLLOW_UP),
        ("Scheduling your call", None, MessageKind.FOLLOW_UP),
        ("Online assessment for Engineer", None, MessageKind.FOLLOW_UP),
        ("Reminder: complete your coding test", None, MessageKind.FOLLOW_UP),
        ("Next steps", None, MessageKind.FOLLOW_UP),
        ("An update on your application", None, MessageKind.FOLLOW_UP),
        ("Your application to Acme", ApplicationStatus.REJECTED, MessageKind.STATUS_UPDATE),
    ],
)
def test_classify_message_kind(subject, status, expected) -> None:
    assert classify_message_kind(_parsed(subject=subject, status=status)) is expected


# ------------------------------------------------------------------ #
# Resolution rules                                                     #
# ------------------------------------------------------------------ #


def test_thread_match_wins(db: DataStore) -> None:
    owner = _app(db, "Acme", thread_ids='["t-1"]')
    _app(db, "Acme Corp")  # a better fuzzy candidate must not override the thread
    match = IdentityResolver(db).resolve(_parsed(thread_id="t-1", company="Other Co"))
    assert (match.application.id, match.method, match.confidence) == (owner.id, "thread", 1.0)


def test_canonical_job_url_match(db: DataStore) -> None:
    target = _app(db, "Acme", job_url="https://jobs.acme.com/42/")
    match = IdentityResolver(db).resolve(
        _parsed(company="Unparseable", role=None, job_url="https://JOBS.acme.com/42?utm_source=x")
    )
    assert (match.application.id, match.method, match.confidence) == (target.id, "job_url", 0.95)


def test_shared_job_url_is_ambiguous(db: DataStore) -> None:
    _app(db, "Acme", job_url="https://jobs.acme.com/42")
    _app(db, "Acme", role="Analyst", job_url="https://jobs.acme.com/42")
    match = IdentityResolver(db).resolve(_parsed(job_url="https://jobs.acme.com/42"))
    assert match.application is None
    assert match.ambiguity == "ambiguous_job_url"


def test_status_mail_links_to_single_application_at_company(db: DataStore) -> None:
    only = _app(db, "Globex Technologies")
    match = IdentityResolver(db).resolve(
        _parsed(company="Globex", role=None, status=ApplicationStatus.REJECTED)
    )
    assert (match.application.id, match.method, match.confidence) == (only.id, "company_only", 0.6)


def test_fuzzy_company_role_match(db: DataStore) -> None:
    target = _app(db, "Initech", role="Software Engineer")
    match = IdentityResolver(db).resolve(_parsed(company="Initech", role="Software Engineer"))
    assert match.application.id == target.id
    assert match.method == "company_role"
    assert 0.85 <= match.confidence <= 1.0


def test_identical_fuzzy_candidates_are_ambiguous(db: DataStore) -> None:
    _app(db, "Initech", role="Engineer")
    _app(db, "Initech", role="Engineer")
    match = IdentityResolver(db).resolve(_parsed(company="Initech", role="Engineer"))
    assert match.application is None
    assert match.ambiguity == "ambiguous_company_role"


def test_status_mail_with_several_applications_at_company_is_ambiguous(db: DataStore) -> None:
    _app(db, "Umbrella", role="Data Engineer")
    _app(db, "Umbrella", role="Platform Engineer")
    match = IdentityResolver(db).resolve(
        _parsed(company="Umbrella", role=None, status=ApplicationStatus.INTERVIEW_SCHEDULED)
    )
    assert match.application is None
    assert match.ambiguity == "ambiguous_company"


def test_no_match(db: DataStore) -> None:
    _app(db, "Acme")
    match = IdentityResolver(db).resolve(_parsed(company="Completely Different", role="Chef"))
    assert match.application is None and match.ambiguity is None


def test_applications_outside_lookback_are_ignored(db: DataStore) -> None:
    from datetime import timedelta

    _app(db, "Acme", applied_date=utc_now() - timedelta(days=400))
    match = IdentityResolver(db, lookback_days=180).resolve(_parsed())
    assert match.application is None
