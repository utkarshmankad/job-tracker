"""Evidence API: behaviour (explicit test auth) and protection (real sessions, CSRF, rate
limits) — the latter without the test_auth_user override."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.api.auth import AuthService, require_user
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, Evidence, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.integration.test_auth import FakeClock, FakeGoogle, _config

_BASE = "/api/v1"
T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def _seed(db: DataStore) -> dict:
    application = db.upsert_application(
        Application(
            company="Acme",
            role="Engineer",
            source_portal="LinkedIn",
            applied_date=utc_now(),
            current_status=ApplicationStatus.APPLIED,
        )
    )
    linked, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="m-linked",
            thread_id="t1",
            sender="Acme <jobs@acme.example>",
            recipient="owner@example.com",
            subject="Your application to Acme",
            snippet="Thanks for applying",
            occurred_at=T0,
            raw_metadata={
                "classification": "acknowledgement",
                "parser": {"portal": "LinkedIn", "status_signal": None, "company": "Acme"},
                "internal": "should never be exposed",
            },
        )
    )
    db.link_evidence(linked.id, application.id, "thread", 1.0)
    review, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="m-review",
            subject="Interview with Globex",
            occurred_at=T0 + timedelta(days=1),
        )
    )
    db.update_evidence_processing(
        review.id, "needs_review", review_reason="follow_up_without_application"
    )
    portal, _ = db.insert_evidence(
        Evidence(
            evidence_type="portal_import",
            source="naukri",
            external_id="n-1",
            occurred_at=T0 - timedelta(days=5),
        )
    )
    ignored, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="m-ignored",
            occurred_at=T0,
            processing_status="ignored",
        )
    )
    return {
        "app": application.id,
        "linked": linked.id,
        "review": review.id,
        "portal": portal.id,
        "ignored": ignored.id,
    }


@pytest.fixture
def api(tmp_path, test_auth_user):
    db = DataStore(tmp_path / "api.db")
    ids = _seed(db)
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        yield SimpleNamespace(client=client, db=db, ids=ids)


# ------------------------------------------------------------------ #
# Behaviour                                                            #
# ------------------------------------------------------------------ #


def test_list_defaults_hide_ignored_and_order_newest_first(api) -> None:
    body = api.client.get(f"{_BASE}/evidence").json()
    assert body["total"] == 3
    assert [e["external_id"] for e in body["items"]] == ["m-review", "m-linked", "n-1"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"linked": "true"}, ["m-linked"]),
        ({"linked": "false"}, ["m-review", "n-1"]),
        ({"source": "naukri"}, ["n-1"]),
        ({"evidence_type": "email"}, ["m-review", "m-linked"]),
        ({"processing_status": "needs_review"}, ["m-review"]),
        ({"processing_status": "ignored"}, ["m-ignored"]),
        ({"date_from": "2026-05-01T12:00:00Z"}, ["m-review"]),
        ({"date_to": "2026-04-30T00:00:00Z"}, ["n-1"]),
        ({"page": "2", "page_size": "2"}, ["n-1"]),
    ],
)
def test_list_filters(api, params, expected) -> None:
    body = api.client.get(f"{_BASE}/evidence", params=params).json()
    assert [e["external_id"] for e in body["items"]] == expected


@pytest.mark.parametrize(
    "params",
    [{"source": "myspace"}, {"evidence_type": "fax"}, {"processing_status": "done"}, {"page": "0"}],
)
def test_list_rejects_unknown_filter_values(api, params) -> None:
    assert api.client.get(f"{_BASE}/evidence", params=params).status_code == 422


def test_response_exposes_only_the_fixed_field_set(api) -> None:
    item = next(
        e
        for e in api.client.get(f"{_BASE}/evidence").json()["items"]
        if e["external_id"] == "m-linked"
    )
    assert "recipient" not in item
    assert "raw_metadata" not in item
    assert "content_fingerprint" not in item
    assert "owner@example.com" not in str(item)
    assert "should never be exposed" not in str(item)
    assert item["summary"] == {
        "classification": "acknowledgement",
        "portal": "LinkedIn",
        "status_signal": None,
    }
    assert item["link_method"] == "thread"


def test_counts(api) -> None:
    assert api.client.get(f"{_BASE}/evidence/counts").json() == {
        "total": 3,
        "unlinked": 2,
        "needs_review": 1,
    }


def test_application_evidence(api) -> None:
    resp = api.client.get(f"{_BASE}/applications/{api.ids['app']}/evidence")
    assert [e["external_id"] for e in resp.json()] == ["m-linked"]
    assert api.client.get(f"{_BASE}/applications/9999/evidence").status_code == 404


def test_link_and_unlink(api) -> None:
    linked = api.client.post(
        f"{_BASE}/evidence/{api.ids['review']}/link", json={"application_id": api.ids["app"]}
    )
    assert linked.status_code == 200
    assert (linked.json()["link_method"], linked.json()["link_confidence"]) == ("manual", 1.0)
    assert linked.json()["processing_status"] == "linked"
    detail = api.client.get(f"{_BASE}/applications/{api.ids['app']}").json()
    assert detail["last_evidence_at"].startswith("2026-05-02")

    unlinked = api.client.post(f"{_BASE}/evidence/{api.ids['review']}/unlink")
    assert unlinked.status_code == 200
    assert unlinked.json()["application_id"] is None
    assert unlinked.json()["review_reason"] == "manually_unlinked"
    assert api.client.get(f"{_BASE}/evidence/counts").json()["needs_review"] == 1


def test_link_does_not_change_application_status(api) -> None:
    api.client.post(
        f"{_BASE}/evidence/{api.ids['review']}/link", json={"application_id": api.ids["app"]}
    )
    assert api.db.get_application(api.ids["app"]).current_status is ApplicationStatus.APPLIED
    assert len(api.db.get_status_history(api.ids["app"])) == 0


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/evidence/9999/link", {"application_id": 1}, 404),
        ("/evidence/{review}/link", {"application_id": 9999}, 404),
        ("/evidence/{review}/link", {"application_id": 0}, 422),
        ("/evidence/{review}/link", {}, 422),
        ("/evidence/9999/unlink", None, 404),
    ],
)
def test_link_errors(api, path, body, status) -> None:
    url = _BASE + path.format(review=api.ids["review"])
    assert api.client.post(url, json=body).status_code == status


def test_existing_application_responses_stay_compatible(api) -> None:
    item = api.client.get(f"{_BASE}/applications").json()["items"][0]
    for legacy_field in (
        "id",
        "company",
        "role",
        "source_portal",
        "application_method",
        "job_url",
        "applied_date",
        "current_status",
        "thread_ids",
        "is_false_positive",
        "withdraw_reason",
        "created_at",
        "updated_at",
        "is_stale",
    ):
        assert legacy_field in item
    assert "last_evidence_at" in item  # additive
    assert "normalized_company" not in item  # internal identity columns stay internal


# ------------------------------------------------------------------ #
# Protection with real sessions (no test_auth_user override)           #
# ------------------------------------------------------------------ #


@pytest.fixture
def secured(tmp_path):
    assert require_user not in app.dependency_overrides
    db = DataStore(tmp_path / "secured.db")
    ids = _seed(db)
    google = FakeGoogle()
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        app.state.auth = AuthService(_config(), verifier=google, clock=FakeClock())
        yield SimpleNamespace(client=client, db=db, ids=ids, google=google)


def _sign_in(env) -> str:
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "c"})
    assert resp.status_code == 200
    return resp.json()["csrf_token"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/evidence"),
        ("GET", "/evidence/counts"),
        ("GET", "/applications/1/evidence"),
        ("POST", "/evidence/1/link"),
        ("POST", "/evidence/1/unlink"),
    ],
)
def test_evidence_endpoints_require_authentication(secured, method, path) -> None:
    resp = secured.client.request(method, _BASE + path, json={"application_id": 1})
    assert resp.status_code == 401
    assert resp.json()["code"] == "not_authenticated"


def test_mutations_require_csrf(secured) -> None:
    csrf = _sign_in(secured)
    url = f"{_BASE}/evidence/{secured.ids['review']}/link"
    body = {"application_id": secured.ids["app"]}
    missing = secured.client.post(url, json=body)
    assert (missing.status_code, missing.json()["code"]) == (403, "csrf_failed")
    assert secured.db.get_evidence(secured.ids["review"]).application_id is None
    ok = secured.client.post(url, json=body, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200
    unlink_url = f"{_BASE}/evidence/{secured.ids['review']}/unlink"
    assert secured.client.post(unlink_url).status_code == 403
    assert secured.client.post(unlink_url, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert secured.client.get(f"{_BASE}/evidence").status_code == 200  # reads need no CSRF


def test_mutations_are_rate_limited(secured, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.SENSITIVE_RATE_LIMIT_REQUESTS", 2)
    csrf = _sign_in(secured)
    headers = {"X-CSRF-Token": csrf}
    url = f"{_BASE}/evidence/{secured.ids['review']}"
    codes = [
        secured.client.post(f"{url}/unlink", headers=headers).status_code,
        secured.client.post(
            f"{url}/link", json={"application_id": secured.ids["app"]}, headers=headers
        ).status_code,
        secured.client.post(f"{url}/unlink", headers=headers).status_code,
    ]
    assert codes == [200, 200, 429]
