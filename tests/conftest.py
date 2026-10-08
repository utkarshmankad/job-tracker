"""Shared pytest fixtures."""

from datetime import timedelta

import pytest
from sqlmodel import Session

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now


@pytest.fixture
def test_auth_user():
    """Explicit test authentication for route tests.

    Overrides the router-level `require_user` dependency on the real FastAPI app with a
    fixed signed-in owner, so tests exercise endpoint behaviour without minting Google
    tokens. Auth itself is covered without this override in tests/integration/test_auth.py.
    Sessions and CSRF are bypassed only while this fixture is active.
    """
    from backend.api.auth import AuthenticatedUser, require_user
    from backend.main import app

    user = AuthenticatedUser(
        email="owner@example.com",
        session_id="test-session",
        expires_at=int(utc_now().timestamp()) + 3600,
    )
    app.dependency_overrides[require_user] = lambda: user
    yield user
    app.dependency_overrides.pop(require_user, None)


@pytest.fixture
def db(tmp_path):
    return DataStore(db_path=tmp_path / "test.db")


@pytest.fixture
def seeded_db(db):
    """DB with 15 applications across 3 portals for analytics tests."""
    portals = ["Naukri", "LinkedIn", "Direct/Consultancy"]
    statuses = [
        ApplicationStatus.APPLIED,
        ApplicationStatus.RESUME_SHORTLISTED,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
    ]
    for i in range(15):
        app = Application(
            company=f"Company{i}",
            role="Software Engineer",
            source_portal=portals[i % 3],
            applied_date=utc_now() - timedelta(days=i),
            current_status=statuses[i % 5],
        )
        db.upsert_application(app)
    return db


@pytest.fixture
def stale_app(db):
    """Application 16 days old in Applied status."""
    app = Application(
        company="StaleCompany",
        role="Engineer",
        source_portal="Naukri",
        applied_date=utc_now() - timedelta(days=16),
        current_status=ApplicationStatus.APPLIED,
    )
    saved = db.upsert_application(app)
    with Session(db._engine) as session:
        record = session.get(Application, saved.id)
        record.updated_at = utc_now() - timedelta(days=16)
        session.add(record)
        session.commit()
    return saved
