"""Integration tests for the FastAPI routes."""

from __future__ import annotations

from datetime import datetime, timedelta
import pytest
from starlette.testclient import TestClient
from sqlmodel import Session

from backend.config import STALE_DAYS_THRESHOLD
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus
from backend.main import app


@pytest.fixture
def seeded_client(tmp_path):
    db_path = tmp_path / "test.db"
    test_db = DataStore(db_path)
    with TestClient(app, raise_server_exceptions=True) as client:
        # Override AFTER lifespan runs so the test DB isn't replaced by startup event.
        app.state.db = test_db
        yield client, test_db


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

_BASE = "/api/v1"

_APP_PAYLOAD = {
    "source_portal": "LinkedIn",
    "applied_date": "2024-06-01",
    "current_status": "Applied",
    "company": "Acme Corp",
    "role": "Software Engineer",
}


def _create_app(client: TestClient, payload: dict | None = None) -> dict:
    resp = client.post(f"{_BASE}/applications", json=payload or _APP_PAYLOAD)
    assert resp.status_code == 201
    return resp.json()


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #


def test_list_applications_empty(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/applications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["page_size"] == 50


def test_create_application_returns_201(seeded_client):
    client, _ = seeded_client
    resp = client.post(f"{_BASE}/applications", json=_APP_PAYLOAD)
    assert resp.status_code == 201
    body = resp.json()
    assert body["company"] == "Acme Corp"
    assert body["source_portal"] == "LinkedIn"
    assert body["current_status"] == "Applied"
    assert body["is_stale"] is True  # applied_date is old, so updated_at = applied_date → stale
    assert "id" in body


def test_get_application_by_id(seeded_client):
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    resp = client.get(f"{_BASE}/applications/{app_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == app_id
    assert body["company"] == "Acme Corp"
    assert "status_history" in body
    assert isinstance(body["status_history"], list)


def test_get_application_not_found_returns_404(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/applications/99999")
    assert resp.status_code == 404


def test_patch_status_manual_update(seeded_client):
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    resp = client.patch(
        f"{_BASE}/applications/{app_id}",
        json={"current_status": "Resume Shortlisted"},
    )
    assert resp.status_code == 200
    assert resp.json()["current_status"] == "Resume Shortlisted"

    detail = client.get(f"{_BASE}/applications/{app_id}")
    history = detail.json()["status_history"]
    triggers = [h["trigger"] for h in history]
    assert "manual" in triggers


def test_delete_application(seeded_client):
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    resp = client.delete(f"{_BASE}/applications/{app_id}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    resp = client.get(f"{_BASE}/applications/{app_id}")
    assert resp.status_code == 404


def test_export_csv_headers(seeded_client):
    client, _ = seeded_client
    _create_app(client)

    resp = client.get(f"{_BASE}/applications/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]

    lines = resp.text.strip().splitlines()
    assert len(lines) >= 1
    header = lines[0]
    assert "id" in header
    assert "company" in header
    assert "current_status" in header


def test_export_json_returns_list(seeded_client):
    client, _ = seeded_client
    _create_app(client)

    resp = client.get(f"{_BASE}/applications/export?format=json")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["company"] == "Acme Corp"


def test_insights_insufficient_data(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/insights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["insufficient_data"] is True
    assert body["total_applications"] == 0
    assert body["channels"] == []


def test_stale_flag_on_old_application(seeded_client):
    client, test_db = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    old_date = datetime.utcnow() - timedelta(days=STALE_DAYS_THRESHOLD + 1)
    with Session(test_db._engine) as session:
        record = session.get(Application, app_id)
        record.updated_at = old_date
        session.add(record)
        session.commit()

    resp = client.get(f"{_BASE}/applications/{app_id}")
    assert resp.status_code == 200
    assert resp.json()["is_stale"] is True


def test_suppress_rule_crud(seeded_client):
    client, _ = seeded_client

    resp = client.get(f"{_BASE}/suppress-rules")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = client.post(
        f"{_BASE}/suppress-rules",
        json={"sender_pattern": "noreply@spam.com", "subject_pattern": "You applied"},
    )
    assert resp.status_code == 201
    rule = resp.json()
    assert rule["sender_pattern"] == "noreply@spam.com"
    assert rule["subject_pattern"] == "You applied"
    rule_id = rule["id"]

    resp = client.get(f"{_BASE}/suppress-rules")
    assert len(resp.json()) == 1

    resp = client.delete(f"{_BASE}/suppress-rules/{rule_id}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}

    resp = client.get(f"{_BASE}/suppress-rules")
    assert resp.json() == []


def test_poller_status_endpoint(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/poller/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "last_sync_at" in body
    assert "error_message" in body
    assert body["status"] == "SLEEPING"
