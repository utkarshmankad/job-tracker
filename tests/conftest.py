"""Shared pytest fixtures."""

import pytest
from datetime import datetime, timedelta
from sqlmodel import Session

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus


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
            applied_date=datetime.utcnow() - timedelta(days=i),
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
        applied_date=datetime.utcnow() - timedelta(days=16),
        current_status=ApplicationStatus.APPLIED,
    )
    saved = db.upsert_application(app)
    with Session(db._engine) as session:
        record = session.get(Application, saved.id)
        record.updated_at = datetime.utcnow() - timedelta(days=16)
        session.add(record)
        session.commit()
    return saved
