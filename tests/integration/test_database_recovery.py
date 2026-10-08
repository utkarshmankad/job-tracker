"""End-to-end database recovery: a full backup → verify → restore drill on representative
data, and the API's maintenance mode (quiesced writers) during migrations and restores.

Everything runs on temporary files; nothing here can reach a Fly.io volume.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from backend.db.backup import create_backup, restore_backup, validate_backup, verify_backup
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    InterviewRound,
    Prospect,
    utc_now,
)
from backend.db.recovery_cli import migrate_group
from backend.db.schema import read_status
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.conftest import build_legacy_database

_TABLES = (
    "application",
    "statushistory",
    "applicationevent",
    "applicationthreadid",
    "prospect",
    "processedmessage",
    "suppressrule",
    "pollerstate",
)


def _dump(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table, for exact before/after comparison (test-only raw read)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in _TABLES}
    finally:
        conn.close()


def _populate(path: Path) -> None:
    """Representative data through the application's own write paths."""
    store = DataStore(path)
    updater = StatusUpdater(store, DuplicateDetector(store))
    now = utc_now()
    apps = []
    for i, (company, portal) in enumerate(
        [
            ("Acme", "LinkedIn"),
            ("Globex", "Naukri"),
            ("Initech", "Direct/Unknown"),
            ("Umbrella", "Instahyre"),
        ]
    ):
        apps.append(
            store.upsert_application(
                Application(
                    company=company,
                    role=f"Engineer {i}",
                    source_portal=portal,
                    application_method="Easy Apply" if i % 2 else "Company Site",
                    applied_date=now - timedelta(days=30 - i),
                    current_status=ApplicationStatus.APPLIED,
                    thread_ids=f'["thread-{i}a", "thread-{i}b"]',
                )
            )
        )
        store.append_status_history(apps[-1].id, None, "Applied", "email", f"msg-{i}")
    updater.manual_update(apps[0].id, ApplicationStatus.INTERVIEW_SCHEDULED)
    updater.manual_update(apps[1].id, ApplicationStatus.REJECTED)
    store.add_application_event(
        ApplicationEvent(
            application_id=apps[0].id,
            event_type=ApplicationEventType.INTERVIEW_ATTENDED,
            occurred_at=now - timedelta(days=2),
            interview_round=InterviewRound.HIRING_MANAGER,
            source="manual",
            notes="Panel went well",
        )
    )
    for j in range(3):
        store.upsert_prospect(
            Prospect(
                category="recruiter_outreach",
                title=f"Opportunity {j}",
                sender="Recruiter",
                snippet="We'd like to talk",
                received_at=now - timedelta(days=j),
                gmail_message_id=f"prospect-{j}",
                gmail_thread_id=f"prospect-thread-{j}",
                classification_reason="outreach",
                application_id=apps[2].id if j == 0 else None,
            )
        )
    store.add_suppress_rule("noreply@spam.example")
    store.mark_processed("msg-0", "applied")
    store.update_poller_state(status="RUNNING", last_history_id="12345")
    store.close()


def test_full_backup_and_restore_drill(tmp_path: Path) -> None:
    live = tmp_path / "live" / "applications.db"
    _populate(live)
    original = _dump(live)
    assert len(original["application"]) == 4
    assert len(original["applicationthreadid"]) == 8
    assert original["applicationevent"] and original["prospect"] and original["statushistory"]

    # 1. Back up the live database while a connection holds it open (as the API would).
    holder = sqlite3.connect(live)
    try:
        backup = create_backup(live, tmp_path / "backups", label="drill")
    finally:
        holder.close()
    manifest = validate_backup(backup.path)
    assert manifest.application_count == 4
    assert manifest.table_counts["applicationthreadid"] == 8

    # 2. Verify: temp restore + open through DataStore.
    verified = verify_backup(backup.path)
    assert verified.application_count == 4
    assert verified.prospect_count == 3

    # 3. Simulate data loss on the live database.
    store = DataStore(live)
    for app_row in store.get_applications(ApplicationFilter())[0]:
        store.delete_application(app_row.id)
    assert store.get_applications(ApplicationFilter())[1] == 0
    store.close()

    # 4. Restore over it (forced: the damaged file is kept aside, not deleted).
    result = restore_backup(backup.path, live, force=True)
    assert result.moved_aside
    assert all(p.exists() for p in result.moved_aside)

    # 5. Every row of every table is back exactly.
    assert _dump(live) == original
    store = DataStore(live)
    assert store.find_application_by_thread_id("thread-3b").company == "Umbrella"
    acme = store.find_application_by_thread_id("thread-0a")
    assert acme.current_status is ApplicationStatus.INTERVIEW_SCHEDULED
    assert [e.event_type for e in store.get_application_events(acme.id)].count(
        ApplicationEventType.INTERVIEW_ATTENDED
    ) == 1
    assert store.get_poller_state().last_history_id == "12345"
    store.close()


# ------------------------------------------------------------------ #
# Maintenance mode                                                     #
# ------------------------------------------------------------------ #


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    paths = {
        "db": tmp_path / "applications.db",
        "flag": tmp_path / "MAINTENANCE",
        "backups": tmp_path / "backups",
    }
    monkeypatch.setattr("backend.config.DB_PATH", paths["db"])
    monkeypatch.setattr("backend.config.MAINTENANCE_FLAG_PATH", paths["flag"])
    monkeypatch.setattr("backend.config.BACKUP_DIR", paths["backups"])
    return paths


def test_maintenance_flag_quiesces_the_api(isolated_paths, test_auth_user) -> None:
    DataStore(isolated_paths["db"]).close()
    isolated_paths["flag"].touch()
    with TestClient(app) as client:
        assert app.state.db is None
        assert app.state.poller_scheduler is None  # no writers
        assert client.get("/api/v1/health").json() == {"status": "ok"}

        resp = client.get("/api/v1/applications")
        assert resp.status_code == 503
        assert "maintenance" in resp.json()["detail"]
        assert client.post("/api/v1/poller/trigger").status_code == 503

        status = client.get("/api/v1/status").json()
        assert status["overall"] == "outage"
        assert "maintenance flag present" in status["components"][0]["description"]
        assert client.get("/api/v1/diagnostics").status_code == 200


def test_maintenance_mode_still_requires_authentication(isolated_paths) -> None:
    isolated_paths["flag"].touch()
    with TestClient(app) as client:
        assert client.get("/api/v1/applications").status_code == 401
        assert client.get("/api/v1/status").status_code == 401
        assert client.get("/api/v1/health").status_code == 200


def test_outdated_schema_starts_in_maintenance_instead_of_migrating(
    isolated_paths, test_auth_user, monkeypatch
) -> None:
    monkeypatch.setattr("backend.config.DB_AUTO_MIGRATE", False)  # production-equivalent
    build_legacy_database(isolated_paths["db"])
    with TestClient(app) as client:
        assert app.state.db is None
        assert client.get("/api/v1/applications").status_code == 503
        status = client.get("/api/v1/status").json()
        assert "unversioned" in status["components"][0]["description"]
    assert read_status(isolated_paths["db"]).needs_upgrade  # untouched


def test_operator_migration_sequence_then_resume(
    isolated_paths, test_auth_user, monkeypatch
) -> None:
    """Deploy → maintenance → backup + migrate via CLI → remove flag → service resumes."""
    monkeypatch.setattr("backend.config.DB_AUTO_MIGRATE", False)
    build_legacy_database(isolated_paths["db"])

    with TestClient(app):
        assert app.state.db is None

    isolated_paths["flag"].touch()
    result = CliRunner().invoke(migrate_group, ["upgrade"])
    assert result.exit_code == 0, result.output
    isolated_paths["flag"].unlink()

    with TestClient(app) as client:
        assert app.state.db is not None
        app.state.updater = StatusUpdater(app.state.db, DuplicateDetector(app.state.db))
        assert client.get("/api/v1/applications/meta/taxonomy").status_code == 200
        schema = next(
            c
            for c in client.get("/api/v1/status").json()["components"]
            if c["name"] == "Database Schema"
        )
        assert schema["status"] == "operational"
