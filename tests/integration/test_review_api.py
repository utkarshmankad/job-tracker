"""Review-queue API: candidates and explanations, accept, create, dismiss, defer, metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.api.auth import AuthService, require_user
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, Evidence, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import IdentityResolver, MessageKind, signals_from_evidence
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.integration.test_auth import FakeClock, FakeGoogle, _config

_BASE = "/api/v1"


def _seed(db: DataStore) -> dict:
    apps = [
        db.upsert_application(
            Application(
                company="Infosys",
                role=role,
                source_portal="Naukri",
                applied_date=utc_now() - timedelta(days=20),
                current_status=ApplicationStatus.APPLIED,
            )
        )
        for role in ("Data Engineer", "Platform Engineer")
    ]
    other = db.upsert_application(
        Application(
            company="Globex", role="Analyst", source_portal="LinkedIn", applied_date=utc_now()
        )
    )
    evidence, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="rej",
            thread_id="t-rej",
            sender="Naukri <noreply@naukri.com>",
            subject="Your application to Infosys",
            snippet="We regret to inform you",
            occurred_at=utc_now() - timedelta(days=1),
            raw_metadata={
                "classification": "status_update",
                "parser": {
                    "portal": "Naukri",
                    "company": "Infosys",
                    "role": None,
                    "status_signal": "Rejected",
                },
            },
        )
    )
    result = IdentityResolver(db).resolve(
        signals_from_evidence(evidence, MessageKind.STATUS_UPDATE)
    )
    db.record_resolution(
        evidence.id,
        resolution=result.to_json(),
        status="needs_review",
        review_reason=result.reason,
    )
    unmatched, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="sched",
            subject="Interview scheduled: Stark Industries",
            occurred_at=utc_now(),
            raw_metadata={
                "parser": {
                    "portal": "Greenhouse",
                    "company": "Stark Industries",
                    "role": "SRE",
                    "status_signal": "Interview Scheduled",
                }
            },
        )
    )
    db.record_resolution(
        unmatched.id,
        resolution={"version": "2.0.0", "outcome": "review_required", "candidates": []},
        status="needs_review",
        review_reason="status_update_without_application",
    )
    return {
        "data": apps[0].id,
        "platform": apps[1].id,
        "globex": other.id,
        "rej": evidence.id,
        "sched": unmatched.id,
        "reason": result.reason,
    }


@pytest.fixture
def api(tmp_path, test_auth_user):
    db = DataStore(tmp_path / "review.db")
    ids = _seed(db)
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        yield SimpleNamespace(client=client, db=db, ids=ids)


def test_review_queue_shows_candidates_and_explanation(api) -> None:
    body = api.client.get(f"{_BASE}/evidence/review").json()
    assert body["total"] == 2
    item = next(i for i in body["items"] if i["id"] == api.ids["rej"])
    resolution = item["resolution"]
    assert resolution["outcome"] == "review_required"
    assert resolution["reason"] == api.ids["reason"] == "ambiguous_candidates"
    assert resolution["explanation"]
    assert resolution["version"] == "2.0.0"
    candidate_ids = {c["application_id"] for c in resolution["candidates"]}
    assert {api.ids["data"], api.ids["platform"]} <= candidate_ids
    first = resolution["candidates"][0]
    assert first["application"]["company"] == "Infosys"
    assert "company_exact" in first["signals"]
    assert "raw_metadata" not in item and "recipient" not in item


def test_evidence_detail(api) -> None:
    resp = api.client.get(f"{_BASE}/evidence/{api.ids['rej']}")
    assert resp.status_code == 200
    assert resp.json()["resolution"]["candidates"]
    assert api.client.get(f"{_BASE}/evidence/999999").status_code == 404


def test_accept_candidate_is_idempotent_and_applies_status(api) -> None:
    url = f"{_BASE}/evidence/{api.ids['rej']}/accept"
    first = api.client.post(url, json={"application_id": api.ids["data"]})
    assert first.status_code == 200
    body = first.json()
    assert (body["application_id"], body["decided_by"], body["link_method"]) == (
        api.ids["data"],
        "human",
        "manual",
    )
    assert api.db.get_application(api.ids["data"]).current_status is ApplicationStatus.REJECTED
    second = api.client.post(url, json={"application_id": api.ids["data"]})
    assert second.status_code == 200
    history = [h.to_status for h in api.db.get_status_history(api.ids["data"])]
    assert history.count("Rejected") == 1
    assert api.db.get_application(api.ids["platform"]).current_status is ApplicationStatus.APPLIED
    assert api.client.get(f"{_BASE}/evidence/review").json()["total"] == 1


def test_accept_rejects_non_candidates(api) -> None:
    resp = api.client.post(
        f"{_BASE}/evidence/{api.ids['rej']}/accept", json={"application_id": api.ids["globex"]}
    )
    assert resp.status_code == 409
    assert api.db.get_evidence(api.ids["rej"]).application_id is None


def test_create_application_from_review_is_idempotent(api) -> None:
    url = f"{_BASE}/evidence/{api.ids['sched']}/create-application"
    first = api.client.post(url, json={})
    assert first.status_code == 200
    created = first.json()
    assert (created["company"], created["role"], created["current_status"]) == (
        "Stark Industries",
        "SRE",
        "Interview Scheduled",
    )
    again = api.client.post(url, json={"company": "Ignored Name"})
    assert again.status_code == 200 and again.json()["id"] == created["id"]
    # A different evidence item already linked elsewhere cannot be turned into a new one.
    api.client.post(
        f"{_BASE}/evidence/{api.ids['rej']}/accept", json={"application_id": api.ids["data"]}
    )
    conflict = api.client.post(f"{_BASE}/evidence/{api.ids['rej']}/create-application", json={})
    assert conflict.status_code == 409


def test_create_application_requires_a_company(api) -> None:
    evidence, _ = api.db.insert_evidence(
        Evidence(
            evidence_type="email", source="gmail", external_id="nocompany", occurred_at=utc_now()
        )
    )
    resp = api.client.post(f"{_BASE}/evidence/{evidence.id}/create-application", json={})
    assert resp.status_code == 422


def test_dismiss_and_defer(api) -> None:
    dismiss = f"{_BASE}/evidence/{api.ids['sched']}/dismiss"
    assert api.client.post(dismiss).json()["processing_status"] == "dismissed"
    assert api.client.post(dismiss).status_code == 200  # idempotent
    assert api.client.get(f"{_BASE}/evidence/review").json()["total"] == 1

    until = (datetime.now(UTC) + timedelta(days=7)).replace(microsecond=0)
    defer = f"{_BASE}/evidence/{api.ids['rej']}/defer"
    body = api.client.post(defer, json={"until": until.isoformat()}).json()
    assert body["processing_status"] == "deferred" and body["decided_by"] == "human"
    assert api.client.post(defer, json={"until": until.isoformat()}).status_code == 200
    assert api.client.get(f"{_BASE}/evidence/review").json()["total"] == 0
    deferred = api.client.get(
        f"{_BASE}/evidence/review", params={"include_deferred": "true"}
    ).json()
    assert [i["id"] for i in deferred["items"]] == [api.ids["rej"]]


def test_dismissing_linked_evidence_conflicts(api) -> None:
    api.client.post(
        f"{_BASE}/evidence/{api.ids['rej']}/accept", json={"application_id": api.ids["data"]}
    )
    assert api.client.post(f"{_BASE}/evidence/{api.ids['rej']}/dismiss").status_code == 409
    assert api.client.post(f"{_BASE}/evidence/{api.ids['rej']}/defer", json={}).status_code == 409


def test_metrics_report_counts_only(api) -> None:
    body = api.client.get(f"{_BASE}/evidence/metrics").json()
    assert set(body) == {"process", "totals"}
    assert set(body["process"]) >= {
        "evidence_processed",
        "auto_linked",
        "new_application_created",
        "sent_to_review",
        "ignored",
        "duplicate_skipped",
        "resolver_errors",
    }
    assert body["totals"]["by_decision"]["review_required"] == 2
    assert "Infosys" not in str(body) and "@" not in str(body)


# ------------------------------------------------------------------ #
# Protection with real sessions                                        #
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
    return env.client.post(f"{_BASE}/auth/google", json={"credential": "c"}).json()["csrf_token"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/evidence/review"),
        ("GET", "/evidence/metrics"),
        ("GET", "/evidence/1"),
        ("POST", "/evidence/1/accept"),
        ("POST", "/evidence/1/create-application"),
        ("POST", "/evidence/1/dismiss"),
        ("POST", "/evidence/1/defer"),
    ],
)
def test_review_endpoints_require_authentication(secured, method, path) -> None:
    assert secured.client.request(method, _BASE + path, json={}).status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/evidence/{rej}/accept", {"application_id": "{data}"}),
        ("/evidence/{sched}/create-application", {}),
        ("/evidence/{sched}/dismiss", None),
        ("/evidence/{sched}/defer", {}),
    ],
)
def test_review_actions_require_csrf(secured, path, body) -> None:
    csrf = _sign_in(secured)
    url = _BASE + path.format(**secured.ids)
    if body and body.get("application_id") == "{data}":
        body = {"application_id": secured.ids["data"]}
    assert secured.client.post(url, json=body).status_code == 403
    assert secured.client.post(url, json=body, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_review_actions_are_rate_limited(secured, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.SENSITIVE_RATE_LIMIT_REQUESTS", 2)
    headers = {"X-CSRF-Token": _sign_in(secured)}
    url = f"{_BASE}/evidence/{secured.ids['sched']}/dismiss"
    codes = [secured.client.post(url, headers=headers).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
