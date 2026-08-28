"""Integration tests for the FastAPI routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session
from starlette.testclient import TestClient

from backend.config import STALE_DAYS_THRESHOLD
from backend.db.data_store import DataStore
from backend.db.models import Application, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app


@pytest.fixture
def seeded_client(tmp_path):
    db_path = tmp_path / "test.db"
    test_db = DataStore(db_path)
    with TestClient(app, raise_server_exceptions=True) as client:
        # Override AFTER lifespan runs so the test DB isn't replaced by startup event.
        app.state.db = test_db
        app.state.updater = StatusUpdater(test_db, DuplicateDetector(test_db))
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


def test_bulk_update_status(seeded_client):
    client, _ = seeded_client
    id1 = _create_app(client, {**_APP_PAYLOAD, "company": "Acme"})["id"]
    id2 = _create_app(client, {**_APP_PAYLOAD, "company": "Beta"})["id"]

    resp = client.post(
        f"{_BASE}/applications/bulk-status",
        json={"application_ids": [id1, id2], "status": "Resume Shortlisted"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 2
    assert body["failed_ids"] == []

    for app_id in (id1, id2):
        detail = client.get(f"{_BASE}/applications/{app_id}")
        assert detail.json()["current_status"] == "Resume Shortlisted"


def test_bulk_update_status_reports_failed_ids_for_missing_applications(seeded_client):
    client, _ = seeded_client
    app_id = _create_app(client)["id"]

    resp = client.post(
        f"{_BASE}/applications/bulk-status",
        json={"application_ids": [app_id, 999999], "status": "Rejected"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1
    assert body["failed_ids"] == [999999]


def test_bulk_delete_applications(seeded_client):
    client, _ = seeded_client
    id1 = _create_app(client, {**_APP_PAYLOAD, "company": "Acme"})["id"]
    id2 = _create_app(client, {**_APP_PAYLOAD, "company": "Beta"})["id"]

    resp = client.post(f"{_BASE}/applications/bulk-delete", json={"application_ids": [id1, id2]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 2
    assert body["failed_ids"] == []

    assert client.get(f"{_BASE}/applications/{id1}").status_code == 404
    assert client.get(f"{_BASE}/applications/{id2}").status_code == 404


def test_bulk_delete_reports_failed_ids_for_missing_applications(seeded_client):
    client, _ = seeded_client
    app_id = _create_app(client)["id"]

    resp = client.post(
        f"{_BASE}/applications/bulk-delete", json={"application_ids": [app_id, 999999]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1
    assert body["failed_ids"] == [999999]


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

    old_date = utc_now() - timedelta(days=STALE_DAYS_THRESHOLD + 1)
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


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_list_applications_bad_date_from_returns_400(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/applications?date_from=not-a-date")
    assert resp.status_code == 400
    assert "date_from" in resp.json()["detail"].lower()


def test_list_applications_bad_date_to_returns_400(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/applications?date_to=garbage")
    assert resp.status_code == 400
    assert "date_to" in resp.json()["detail"].lower()


def test_list_applications_valid_dates_accepted(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/applications?date_from=2024-01-01&date_to=2025-12-31")
    assert resp.status_code == 200


def test_create_application_invalid_date_returns_422(seeded_client):
    client, _ = seeded_client
    payload = {**_APP_PAYLOAD, "applied_date": "not-a-date"}
    resp = client.post(f"{_BASE}/applications", json=payload)
    assert resp.status_code == 422  # Pydantic validates datetime field


def test_suppress_rule_invalid_sender_regex_returns_400(seeded_client):
    client, _ = seeded_client
    resp = client.post(
        f"{_BASE}/suppress-rules",
        json={"sender_pattern": "[unclosed", "subject_pattern": None},
    )
    assert resp.status_code == 400
    assert "sender_pattern" in resp.json()["detail"].lower()


def test_suppress_rule_invalid_subject_regex_returns_400(seeded_client):
    client, _ = seeded_client
    resp = client.post(
        f"{_BASE}/suppress-rules",
        json={"sender_pattern": r"naukri\.com", "subject_pattern": "[bad"},
    )
    assert resp.status_code == 400
    assert "subject_pattern" in resp.json()["detail"].lower()


def test_suppress_rule_valid_regex_accepted(seeded_client):
    client, _ = seeded_client
    resp = client.post(
        f"{_BASE}/suppress-rules",
        json={"sender_pattern": r"noreply@.*\.com", "subject_pattern": r"job\s+alert"},
    )
    assert resp.status_code == 201


def test_system_status_endpoint(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "overall" in body
    assert "components" in body
    assert "stats" in body
    assert body["overall"] in ("operational", "degraded", "outage")


def test_patch_false_positive(seeded_client):
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    resp = client.patch(f"{_BASE}/applications/{app_id}", json={"is_false_positive": True})
    assert resp.status_code == 200
    assert resp.json()["is_false_positive"] is True


def test_delete_nonexistent_application(seeded_client):
    client, _ = seeded_client
    resp = client.delete(f"{_BASE}/applications/99999")
    assert resp.status_code == 404


def test_insights_flow_endpoint(seeded_client):
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/insights/flow")
    assert resp.status_code == 200
    body = resp.json()
    assert "nodes" in body
    assert "links" in body
    assert "kpis" in body


def test_reauth_poller_endpoint(seeded_client):
    client, _ = seeded_client
    resp = client.post(f"{_BASE}/poller/reauth")
    assert resp.status_code == 200
    assert "message" in resp.json()
    # Verify poller state was updated
    status_resp = client.get(f"{_BASE}/poller/status")
    assert status_resp.json()["status"] == "AUTH_REQUIRED"


def test_patch_application_company_and_role(seeded_client):
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    resp = client.patch(
        f"{_BASE}/applications/{app_id}",
        json={"company": "New Corp", "role": "Staff Engineer", "job_url": "https://example.com"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["company"] == "New Corp"
    assert body["role"] == "Staff Engineer"
    assert body["job_url"] == "https://example.com"


def test_patch_application_not_found_returns_404(seeded_client):
    client, _ = seeded_client
    resp = client.patch(f"{_BASE}/applications/99999", json={"company": "X"})
    assert resp.status_code == 404


def test_suppress_rule_delete_nonexistent_returns_404(seeded_client):
    client, _ = seeded_client
    resp = client.delete(f"{_BASE}/suppress-rules/99999")
    assert resp.status_code == 404


# ------------------------------------------------------------------ #
# Trigger-poll endpoint (new in recent changes)                        #
# ------------------------------------------------------------------ #


def test_trigger_poll_returns_triggered(seeded_client):
    """POST /poller/trigger must return 200 with triggered=True when scheduler runs."""
    client, _ = seeded_client
    resp = client.post(f"{_BASE}/poller/trigger")
    # Scheduler is started by the lifespan, so it should be present.
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] is True
    assert "skipped" in body


def test_trigger_poll_no_scheduler_returns_503():
    """POST /poller/trigger must return 503 when no scheduler is in app state."""
    from starlette.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        # Remove the scheduler from state to simulate missing scheduler.
        if hasattr(app.state, "poller_scheduler"):
            del app.state.poller_scheduler
        resp = client.post(f"{_BASE}/poller/trigger")
        assert resp.status_code == 503
        assert "not running" in resp.json()["detail"].lower()


# ------------------------------------------------------------------ #
# System-status endpoint — poller error states                         #
# ------------------------------------------------------------------ #


def test_system_status_api_error_shows_degraded(seeded_client):
    """When poller.status == 'API_ERROR', /status must report poller as degraded."""
    client, test_db = seeded_client
    test_db.update_poller_state(status="API_ERROR", error_message="quota exceeded")

    resp = client.get(f"{_BASE}/status")
    assert resp.status_code == 200
    body = resp.json()
    poller_component = next((c for c in body["components"] if c["name"] == "Gmail Poller"), None)
    assert poller_component is not None
    assert poller_component["status"] == "degraded"
    assert "quota exceeded" in poller_component["description"]


def test_system_status_auth_error_shows_outage(seeded_client):
    """When poller.status == 'AUTH_ERROR', /status must report poller as outage."""
    client, test_db = seeded_client
    test_db.update_poller_state(status="AUTH_ERROR")

    resp = client.get(f"{_BASE}/status")
    assert resp.status_code == 200
    body = resp.json()
    poller_component = next((c for c in body["components"] if c["name"] == "Gmail Poller"), None)
    assert poller_component is not None
    assert poller_component["status"] == "outage"


def test_system_status_stale_poll_shows_degraded(seeded_client):
    """When last_sync_at is >15 min ago, /status must mark poller as degraded."""
    client, test_db = seeded_client
    stale_time = utc_now() - timedelta(minutes=20)
    test_db.update_poller_state(status="SLEEPING", last_sync_at=stale_time)

    resp = client.get(f"{_BASE}/status")
    assert resp.status_code == 200
    body = resp.json()
    poller_component = next((c for c in body["components"] if c["name"] == "Gmail Poller"), None)
    assert poller_component is not None
    assert poller_component["status"] == "degraded"
    assert "min ago" in poller_component["description"]


# ------------------------------------------------------------------ #
# Diagnostics endpoint                                                 #
# ------------------------------------------------------------------ #


# ------------------------------------------------------------------ #
# Delete — cascade, suppress-on-delete, and Gmail-unavailable paths    #
# ------------------------------------------------------------------ #


def test_delete_application_with_status_history(seeded_client):
    """DELETE must cascade through status history rows without IntegrityError."""
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    # Generate status history via a manual patch
    client.patch(
        f"{_BASE}/applications/{app_id}",
        json={"current_status": "Resume Shortlisted"},
    )
    detail = client.get(f"{_BASE}/applications/{app_id}").json()
    assert len(detail["status_history"]) > 0, "precondition: history must exist"

    resp = client.delete(f"{_BASE}/applications/{app_id}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert client.get(f"{_BASE}/applications/{app_id}").status_code == 404


def test_delete_adds_suppress_rule_for_sender_domain(seeded_client):
    """DELETE must add a sender-domain suppress rule when Gmail service is available."""
    client, test_db = seeded_client

    # Create application directly so thread_ids is non-empty
    saved = test_db.upsert_application(
        Application(
            company="Acme Corp",
            role="Engineer",
            source_portal="Direct/Consultancy",
            applied_date=datetime(2024, 6, 1, tzinfo=UTC),
            thread_ids='["thread-abc123"]',
        )
    )
    app_id = saved.id

    # Mock Gmail service returning a thread with a known sender
    mock_service = MagicMock()
    mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": [
            {"payload": {"headers": [{"name": "From", "value": "recruiter@acme-hiring.com"}]}}
        ]
    }

    real_poller = app.state.poller_scheduler.poller
    original_service = real_poller.service
    real_poller.service = mock_service
    try:
        resp = client.delete(f"{_BASE}/applications/{app_id}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert client.get(f"{_BASE}/applications/{app_id}").status_code == 404

        rules = client.get(f"{_BASE}/suppress-rules").json()
        patterns = [r["sender_pattern"] for r in rules]
        assert any(
            "acme\\-hiring\\.com" in p or "acme-hiring" in p for p in patterns
        ), f"Expected acme-hiring.com suppress rule, got: {patterns}"
    finally:
        real_poller.service = original_service


def test_delete_without_gmail_still_succeeds(seeded_client):
    """DELETE must succeed even when Gmail service is not authenticated (no suppress rule added)."""
    client, _ = seeded_client
    created = _create_app(client)
    app_id = created["id"]

    real_poller = app.state.poller_scheduler.poller
    original_service = real_poller.service
    real_poller.service = None  # simulate unauthenticated Gmail
    try:
        resp = client.delete(f"{_BASE}/applications/{app_id}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert client.get(f"{_BASE}/applications/{app_id}").status_code == 404

        rules = client.get(f"{_BASE}/suppress-rules").json()
        assert rules == [], "No suppress rule should be added when Gmail is unavailable"
    finally:
        real_poller.service = original_service


def test_diagnostics_endpoint_available(seeded_client):
    """GET /api/v1/diagnostics must return 200 with structured results."""
    client, _ = seeded_client
    resp = client.get(f"{_BASE}/diagnostics")
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "passed" in body
    assert "failed" in body
    assert isinstance(body["results"], list)
    for result in body["results"]:
        assert "name" in result
        assert "ok" in result
        assert "detail" in result


def test_reauth_start_returns_auth_url(seeded_client):
    client, _ = seeded_client
    real_poller = app.state.poller_scheduler.poller
    with patch.object(
        real_poller,
        "build_reauth_url",
        return_value=("https://accounts.google.com/auth?x=1", "state-xyz"),
    ):
        resp = client.get(f"{_BASE}/poller/reauth/start")
    assert resp.status_code == 200
    assert resp.json() == {"auth_url": "https://accounts.google.com/auth?x=1"}
    assert app.state.reauth_state == "state-xyz"


def test_reauth_callback_rejects_mismatched_state(seeded_client):
    client, _ = seeded_client
    app.state.reauth_state = "expected-state"
    resp = client.get(
        f"{_BASE}/poller/reauth/callback", params={"code": "abc", "state": "wrong-state"}
    )
    assert resp.status_code == 400


def test_reauth_callback_succeeds_and_clears_state(seeded_client):
    client, db = seeded_client
    app.state.reauth_state = "match-state"
    real_poller = app.state.poller_scheduler.poller
    with patch.object(real_poller, "complete_reauth") as mock_complete:
        resp = client.get(
            f"{_BASE}/poller/reauth/callback", params={"code": "abc", "state": "match-state"}
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RUNNING"
    mock_complete.assert_called_once()
    assert app.state.reauth_state is None
    assert db.get_poller_state().status == "RUNNING"


def test_reauth_start_requires_admin_token_when_configured(seeded_client):
    client, _ = seeded_client
    real_poller = app.state.poller_scheduler.poller
    with (
        patch.object(
            real_poller,
            "build_reauth_url",
            return_value=("https://accounts.google.com/auth?x=1", "state-xyz"),
        ),
        patch("backend.api.routes.ADMIN_TOKEN", "secret-token"),
    ):
        resp_no_token = client.get(f"{_BASE}/poller/reauth/start")
        resp_wrong_token = client.get(f"{_BASE}/poller/reauth/start", params={"token": "wrong"})
        resp_right_token = client.get(
            f"{_BASE}/poller/reauth/start", params={"token": "secret-token"}
        )
    assert resp_no_token.status_code == 403
    assert resp_wrong_token.status_code == 403
    assert resp_right_token.status_code == 200
