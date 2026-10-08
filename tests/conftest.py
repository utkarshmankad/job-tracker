"""Shared pytest fixtures.

Isolation comes first: tests/isolation.py configures the environment (temporary
JOB_TRACKER_DIR, poller/Redis/LLM disabled, no secrets, no .env) and installs keyring and
socket guards *before* any backend module is imported, because backend/config.py reads the
environment at import time. Do not import backend modules above this block.
"""

from tests import isolation

isolation.install()

# ruff: noqa: E402 — backend imports must follow isolation.install()
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove the per-run temporary JOB_TRACKER_DIR created by tests/isolation.py."""
    import shutil

    if isolation.TEST_JOB_TRACKER_DIR.name.startswith("job-tracker-tests-"):
        shutil.rmtree(isolation.TEST_JOB_TRACKER_DIR, ignore_errors=True)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: the test deliberately performs (and asserts) external network access",
    )


@pytest.fixture(autouse=True)
def _forbid_external_access(request: pytest.FixtureRequest):
    """Fail any test that reached the keychain or a non-loopback network address, even if
    the code under test swallowed the resulting error."""
    isolation.network_attempts.clear()
    isolation.keyring_attempts.clear()
    yield
    network = list(isolation.network_attempts)
    keychain = list(isolation.keyring_attempts)
    isolation.network_attempts.clear()
    isolation.keyring_attempts.clear()
    if request.node.get_closest_marker("allow_network"):
        return
    if network or keychain:
        pytest.fail(
            f"Test isolation violated — network: {network or 'none'}; "
            f"keychain: {keychain or 'none'}. Mock the external boundary.",
            pytrace=False,
        )


class FakePollerScheduler:
    """Stands in for PollerScheduler in route tests. The poller is a MagicMock, so nothing
    can reach Gmail; tests patch the methods they exercise."""

    def __init__(self) -> None:
        self.poller = MagicMock(name="GmailPoller")
        self.poller.service = None
        self.poller.is_polling = False
        self.triggered = 0
        self.stopped = False

    def trigger(self) -> None:
        self.triggered += 1

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_poller_scheduler() -> FakePollerScheduler:
    return FakePollerScheduler()


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


SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schema"


def build_legacy_database(path: Path, fixture: str = "pre_phase1_schema.sql") -> Path:
    """Create an unversioned database from a verbatim schema dump (tests only).

    Test-fixture exception to the "no raw sqlite3 / raw SQL" rule: the point is to reproduce
    the exact DDL older releases left on disk, which the ORM cannot express.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.executescript((SCHEMA_FIXTURES / fixture).read_text())
        conn.commit()
    finally:
        conn.close()
    return path


def seed_legacy_rows(path: Path, *, with_phase1_tables: bool = True) -> dict[str, int]:
    """Insert representative rows the way origin/main stored them. Returns row counts."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        app_cols = (
            "company, role, source_portal, applied_date, current_status, thread_ids, "
            "is_false_positive, created_at, updated_at"
        )
        if with_phase1_tables:
            app_cols += ", application_method"
        rows = [
            ("Acme", "Engineer", "LinkedIn", "2026-05-01 10:00:00", "Applied", '["t-1"]'),
            ("Globex", "Analyst", "Instahire", "2026-05-02 10:00:00", "Rejected", '["t-2","t-3"]'),
            ("Initech", "SRE", "Naukri", "2026-05-03 10:00:00", "Interview Scheduled", "[]"),
        ]
        for company, role, portal, applied, status, threads in rows:
            values = [company, role, portal, applied, status, threads, 0, applied, applied]
            if with_phase1_tables:
                values.append("Easy Apply")
            conn.execute(
                f"INSERT INTO application ({app_cols}) VALUES ({','.join('?' * len(values))})",
                values,
            )
        conn.execute(
            'INSERT INTO statushistory (application_id, from_status, to_status, "trigger", '
            "changed_at, message_id) VALUES (2, 'Applied', 'Rejected', 'email', "
            "'2026-05-09 10:00:00', 'm-2')"
        )
        conn.execute(
            "INSERT INTO processedmessage (message_id, processed_at, result) "
            "VALUES ('m-2', '2026-05-09 10:00:00', 'status_update')"
        )
        conn.execute("INSERT INTO pollerstate (id, status) VALUES (1, 'SLEEPING')")
        counts = {"application": 3, "statushistory": 1}
        if with_phase1_tables:
            conn.execute(
                "INSERT INTO applicationevent (application_id, event_type, occurred_at, source, "
                "status_history_id, created_at) VALUES (2, 'Rejected', '2026-05-09 10:00:00', "
                "'email', 1, '2026-05-09 10:00:00')"
            )
            conn.execute(
                "INSERT INTO applicationthreadid (application_id, thread_id) VALUES "
                "(1, 't-1'), (2, 't-2'), (2, 't-3')"
            )
            conn.execute(
                "INSERT INTO prospect (source_portal, category, title, sender, received_at, "
                "gmail_message_id, gmail_thread_id, application_id, status, "
                "classification_reason, created_at, updated_at) VALUES ('LinkedIn', "
                "'recruiter_outreach', 'Role at Acme', 'Recruiter', '2026-05-04 10:00:00', "
                "'p-1', 'pt-1', 1, 'New', 'outreach', '2026-05-04 10:00:00', "
                "'2026-05-04 10:00:00')"
            )
            counts.update(applicationevent=1, applicationthreadid=3, prospect=1)
        conn.commit()
        return counts
    finally:
        conn.close()
