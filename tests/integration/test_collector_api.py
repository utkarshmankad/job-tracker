"""Collector API: scoped credential, enrollment, rotation/revocation, idempotent batches,
replay protection, hostile payloads and log hygiene. Synthetic data only."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from starlette.testclient import TestClient

from backend.api.auth import AuthService, require_user
from backend.db.data_store import DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.integration.test_auth import FakeClock, FakeGoogle, _config

_BASE = "/api/v1"


@pytest.fixture
def env(tmp_path):
    assert require_user not in app.dependency_overrides
    db = DataStore(tmp_path / "collector-api.db")
    google = FakeGoogle()
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        app.state.auth = AuthService(_config(), verifier=google, clock=FakeClock())
        google.nonce = client.get(f"{_BASE}/auth/config").json()["nonce"]
        csrf = client.post(f"{_BASE}/auth/google", json={"credential": "c"}).json()["csrf_token"]
        # The collector is a separate program: it never holds the browser session cookie.
        collector_client = TestClient(app)
        yield SimpleNamespace(
            client=client, collector=collector_client, db=db, csrf={"X-CSRF-Token": csrf}
        )


def _create(env, scopes=("indeed",), name="laptop") -> dict:
    resp = env.client.post(
        f"{_BASE}/collectors", json={"name": name, "scopes": list(scopes)}, headers=env.csrf
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _enroll(env, code: str):
    return env.collector.post(f"{_BASE}/collector/enroll", json={"code": code})


def _bearer(credential: str) -> dict:
    return {"Authorization": f"Bearer {credential}"}


def _collector(env, scopes=("indeed",)) -> SimpleNamespace:
    setup = _create(env, scopes)
    enrolled = _enroll(env, setup["setup_code"])
    assert enrolled.status_code == 200, enrolled.text
    return SimpleNamespace(
        id=setup["collector"]["id"],
        credential=enrolled.json()["credential"],
        headers=_bearer(enrolled.json()["credential"]),
        setup=setup,
    )


def _obs(**overrides) -> dict:
    base = {
        "source_key": "indeed",
        "source_item_id": "0f1e2d3c4b5a6978",
        "company": "Quuxwidget Labs",
        "role": "Platform Engineer",
        "applied_on": (datetime.now(UTC) - timedelta(days=3)).date().isoformat(),
        "status": "applied",
        "raw_status": "Applied",
        "job_url": "https://www.indeed.com/viewjob?jk=0f1e2d3c4b5a6978&trk=public&refId=abc",
        "proves_submission": True,
        "extraction": "verified",
        "observed_at": datetime.now(UTC).isoformat(),
        "adapter_version": "indeed/0.2.0",
    }
    return {**base, **overrides}


def _browser_item(**overrides) -> dict:
    base = {
        "source_item_id": "0f1e2d3c4b5a6978",
        "company": "Quuxwidget Labs",
        "role": "Platform Engineer",
        "applied_on": (datetime.now(UTC) - timedelta(days=3)).date().isoformat(),
        "status": "applied",
        "raw_status": "Applied",
        "job_url": "https://www.indeed.com/viewjob?jk=0f1e2d3c4b5a6978&from=tracking",
    }
    return {**base, **overrides}


def _batch(observations: list[dict], key: str | None = None, sent_at: datetime | None = None):
    return {
        "batch_key": key or uuid.uuid4().hex,
        "sent_at": (sent_at or datetime.now(UTC)).isoformat(),
        "observations": observations,
    }


def _start(env, collector, source="indeed", run_key=None):
    return env.collector.post(
        f"{_BASE}/collector/runs",
        json={
            "run_key": run_key or uuid.uuid4().hex,
            "source_key": source,
            "account_label": "default",
            "collector_version": "0.1.0",
            "adapter_version": f"{source}/0.1.0",
        },
        headers=collector.headers,
    )


def _submit(env, collector, run_key, body):
    return env.collector.post(
        f"{_BASE}/collector/runs/{run_key}/observations",
        content=json.dumps(body),
        headers={**collector.headers, "Content-Type": "application/json"},
    )


# ------------------------------------------------------------------ #
# Setup, enrollment, rotation, revocation                              #
# ------------------------------------------------------------------ #


def test_setup_code_is_shown_once_and_credential_never_listed(env) -> None:
    setup = _create(env)
    assert setup["collector"]["state"] == "pending_enrollment"
    assert setup["setup_code"] in setup["command"]
    assert "enroll --api-url" in setup["command"]
    listed = env.client.get(f"{_BASE}/collectors").json()
    assert setup["setup_code"] not in json.dumps(listed)
    enrolled = _enroll(env, setup["setup_code"])
    credential = enrolled.json()["credential"]
    assert credential.startswith("jtc_") and enrolled.headers["cache-control"] == "no-store"
    listed = env.client.get(f"{_BASE}/collectors").json()
    assert listed[0]["state"] == "active"
    assert credential.split(".")[1] not in json.dumps(listed)
    stored = env.db.get_collector(setup["collector"]["id"])
    assert credential.split(".")[1] not in (stored.token_hash or "")


def test_setup_code_is_single_use_and_validated(env) -> None:
    setup = _create(env)
    assert _enroll(env, setup["setup_code"]).status_code == 200
    again = _enroll(env, setup["setup_code"])
    assert again.status_code == 400 and again.json()["detail"] == "Invalid or expired setup code."
    assert _enroll(env, "x" * 25).status_code == 400
    assert _enroll(env, "bad code with spaces!!").status_code == 400


def test_enroll_is_rate_limited(env) -> None:
    statuses = [_enroll(env, f"{'z' * 24}{n:02d}").status_code for n in range(12)]
    assert 429 in statuses


def test_admin_endpoints_require_session_and_csrf(env) -> None:
    assert (
        env.client.post(f"{_BASE}/collectors", json={"name": "x", "scopes": ["indeed"]}).status_code
        == 403
    )
    anonymous = TestClient(app)
    assert anonymous.get(f"{_BASE}/collectors").status_code == 401
    assert anonymous.get(f"{_BASE}/collection/sources").status_code == 401


def test_browser_import_previews_then_ingests_idempotently(env) -> None:
    body = {"source_key": "indeed", "items": [_browser_item()]}
    preview = env.client.post(
        f"{_BASE}/collection/browser-import/preview", json=body, headers=env.csrf
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["count"] == 1
    normalized = preview.json()["observations"][0]
    assert normalized["extraction"] == "verified"
    assert normalized["job_url"] == "https://indeed.com/viewjob?jk=0f1e2d3c4b5a6978"
    assert env.db.collection_metrics()["unique"] == {"source_items": 0, "observations": 0}

    first = env.client.post(f"{_BASE}/collection/browser-import", json=body, headers=env.csrf)
    assert first.status_code == 200, first.text
    assert first.json()["counts"] == {"created": 1}
    second = env.client.post(f"{_BASE}/collection/browser-import", json=body, headers=env.csrf)
    assert second.status_code == 200, second.text
    assert second.json()["counts"] == {"unchanged": 1}
    metrics = env.db.collection_metrics()
    assert metrics["unique"] == {"source_items": 1, "observations": 1}
    assert metrics["processed_across_runs"]["items_processed"] == 2
    collectors = env.client.get(f"{_BASE}/collectors").json()
    workflow = next(c for c in collectors if c["name"] == "ChatGPT browser workflow")
    assert workflow["state"] == "browser_workflow"
    assert workflow["token_hint"] == "signed-in browser"
    assert (
        env.client.post(
            f"{_BASE}/collectors/{workflow['id']}/rotate", headers=env.csrf
        ).status_code
        == 409
    )
    assert (
        env.client.post(
            f"{_BASE}/collectors/{workflow['id']}/revoke", headers=env.csrf
        ).status_code
        == 409
    )


def test_browser_import_accepts_linkedin_but_requires_matching_stable_url(env) -> None:
    linkedin = {
        "source_key": "linkedin",
        "items": [
            _browser_item(
                source_item_id="4187654321",
                job_url="https://www.linkedin.com/jobs/view/4187654321/?trackingId=private",
            )
        ],
    }
    ok = env.client.post(
        f"{_BASE}/collection/browser-import/preview", json=linkedin, headers=env.csrf
    )
    assert ok.status_code == 200, ok.text
    bad = env.client.post(
        f"{_BASE}/collection/browser-import/preview",
        json={"source_key": "linkedin", "items": [_browser_item()]},
        headers=env.csrf,
    )
    assert bad.status_code == 422 and "does not match linkedin" in bad.json()["detail"]


def test_browser_import_rejects_duplicates_extra_fields_and_missing_csrf(env) -> None:
    item = _browser_item()
    duplicate = {"source_key": "indeed", "items": [item, item]}
    resp = env.client.post(
        f"{_BASE}/collection/browser-import/preview", json=duplicate, headers=env.csrf
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Duplicate source item ID in import."
    extra = {"source_key": "indeed", "items": [{**item, "page_text": "private page"}]}
    resp = env.client.post(
        f"{_BASE}/collection/browser-import/preview", json=extra, headers=env.csrf
    )
    assert resp.status_code == 422 and "private page" not in resp.text
    without_csrf = env.client.post(
        f"{_BASE}/collection/browser-import",
        json={"source_key": "indeed", "items": [item]},
    )
    assert without_csrf.status_code == 403


def test_bearer_failures_are_indistinguishable(env) -> None:
    collector = _collector(env)
    token_id = collector.credential.split(".")[0]
    probes = [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-token"},
        _bearer(f"{token_id}.{'A' * 43}"),
        _bearer(f"jtc_{'0' * 16}.{'A' * 43}"),
    ]
    bodies = set()
    for headers in probes:
        resp = env.collector.get(f"{_BASE}/collector/me", headers=headers)
        assert resp.status_code == 401
        bodies.add(resp.text)
    assert len(bodies) == 1
    assert env.collector.get(f"{_BASE}/collector/me", headers=collector.headers).json()[
        "scopes"
    ] == ["indeed"]


def test_rotation_and_revocation(env) -> None:
    collector = _collector(env)
    rotated = env.client.post(f"{_BASE}/collectors/{collector.id}/rotate", headers=env.csrf)
    assert rotated.status_code == 200
    assert env.collector.get(f"{_BASE}/collector/me", headers=collector.headers).status_code == 401
    fresh = _enroll(env, rotated.json()["setup_code"]).json()["credential"]
    assert env.collector.get(f"{_BASE}/collector/me", headers=_bearer(fresh)).status_code == 200
    revoked = env.client.post(f"{_BASE}/collectors/{collector.id}/revoke", headers=env.csrf)
    assert revoked.json()["state"] == "revoked"
    assert env.collector.get(f"{_BASE}/collector/me", headers=_bearer(fresh)).status_code == 401
    assert (
        env.client.post(f"{_BASE}/collectors/{collector.id}/rotate", headers=env.csrf).status_code
        == 409
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/applications"),
        ("POST", "/applications"),
        ("DELETE", "/applications/1"),
        ("POST", "/applications/merge"),
        ("POST", "/evidence/1/accept"),
        ("POST", "/evidence/1/dismiss"),
        ("GET", "/collectors"),
        ("POST", "/collectors"),
        ("POST", "/collectors/1/revoke"),
        ("GET", "/collection/sources"),
    ],
)
def test_collector_credential_cannot_reach_user_endpoints(env, method, path) -> None:
    collector = _collector(env)
    anonymous = TestClient(app)  # no session cookie, only the bearer credential
    resp = anonymous.request(method, f"{_BASE}{path}", headers=collector.headers, json={})
    assert resp.status_code == 401


# ------------------------------------------------------------------ #
# Runs and batches                                                     #
# ------------------------------------------------------------------ #


def test_scope_is_enforced(env) -> None:
    collector = _collector(env, scopes=("indeed",))
    assert _start(env, collector, source="employer-acme").status_code == 403
    assert _start(env, collector, source="indeed").status_code == 200
    assert _start(env, collector, source="not-a-source").status_code == 422


def test_run_start_is_idempotent_and_private(env) -> None:
    first = _collector(env)
    second = _collector(env)
    key = uuid.uuid4().hex
    a = _start(env, first, run_key=key).json()
    b = _start(env, first, run_key=key).json()
    assert a["id"] == b["id"]
    assert _start(env, second, run_key=key).status_code == 409
    assert (
        env.collector.get(f"{_BASE}/collector/runs/{key}", headers=second.headers).status_code
        == 404
    )


def test_batch_is_idempotent_and_replay_safe(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    body = _batch([_obs()])
    first = _submit(env, collector, run_key, body)
    assert first.status_code == 200, first.text
    assert first.json()["counts"] == {"created": 1}
    replay = _submit(env, collector, run_key, body)
    assert replay.json() == first.json() and replay.headers["idempotent-replay"] == "true"
    # The same item in a new batch is unchanged; nothing new is stored.
    again = _submit(env, collector, run_key, _batch([_obs()])).json()
    assert again["results"][0]["outcome"] == "unchanged"
    # A status change is a new observation of the same item.
    changed = _submit(
        env, collector, run_key, _batch([_obs(status="rejected", raw_status="Not selected")])
    )
    assert changed.json()["results"][0]["item_key"] == "id:0f1e2d3c4b5a6978"
    assert len(env.db.collection_metrics()["items_by_source"]) == 1


def test_stale_unseen_batch_is_refused(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    old = datetime.now(UTC) - timedelta(hours=1)
    resp = _submit(env, collector, run_key, _batch([_obs()], sent_at=old))
    assert resp.status_code == 422 and "replay window" in resp.json()["detail"]


@pytest.mark.parametrize(
    "bad",
    [
        {"unexpected_field": "x"},
        {"job_url": "http://insecure.example/job"},
        {"job_url": "javascript:alert(1)"},
        {"source_item_id": "../../etc/passwd"},
        {"status": "hired-by-magic"},
        {"observed_at": "2026-01-01T00:00:00"},
        {"observed_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
        {"company": "   "},
        {"company": "x" * 500},
        {"extraction": "guessed"},
        {"status": "unknown", "proves_submission": True},
    ],
)
def test_hostile_observations_are_rejected_without_echo(env, bad) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    resp = _submit(env, collector, run_key, _batch([_obs(**bad)]))
    assert resp.status_code == 422
    for value in bad.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in resp.text


def test_personal_data_in_text_fields_is_redacted(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    _submit(
        env,
        collector,
        run_key,
        _batch([_obs(role="Engineer — contact jane.doe@example.com or +91 98765 43210")]),
    )
    stored = json.dumps([o.payload for o in env.db.list_run_observations(1)])
    assert "jane.doe@example.com" not in stored and "98765" not in stored
    assert "trk=" not in stored and "refId" not in stored


def test_payload_limits(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    too_many = _submit(
        env, collector, run_key, _batch([_obs(source_item_id=f"i{n}") for n in range(101)])
    )
    assert too_many.status_code == 422
    huge = env.collector.post(
        f"{_BASE}/collector/runs/{run_key}/observations",
        content=b"{" + b" " * (300 * 1024) + b"}",
        headers={**collector.headers, "Content-Type": "application/json"},
    )
    assert huge.status_code == 413


def test_observation_for_another_source_in_a_run_is_rejected(env) -> None:
    collector = _collector(env, scopes=("indeed", "employer-acme"))
    run_key = _start(env, collector).json()["run_key"]
    result = _submit(env, collector, run_key, _batch([_obs(source_key="employer-acme")])).json()
    assert result["results"][0] == {
        "index": 0,
        "outcome": "error",
        "reason": "source_mismatch",
        "item_key": None,
        "application_id": None,
        "observation_id": None,
    }


def test_finish_states_and_closed_runs(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    finish = f"{_BASE}/collector/runs/{run_key}/finish"
    assert (
        env.collector.post(
            finish, json={"status": "failed", "items_seen": 0}, headers=collector.headers
        ).status_code
        == 422
    )
    free_text = {
        "status": "failed",
        "items_seen": 0,
        "error_code": "selector_drift",
        "diagnostics": {"page_text": "Hello Jane"},
    }
    assert env.collector.post(finish, json=free_text, headers=collector.headers).status_code == 422
    ok = env.collector.post(
        finish,
        json={
            "status": "signed_out",
            "items_seen": 0,
            "error_code": "signed_out",
            "diagnostics": {"pages": 1, "state": "login_form"},
        },
        headers=collector.headers,
    )
    assert ok.status_code == 200 and ok.json()["error_message"].startswith(
        "The browser is not signed in"
    )
    again = env.collector.post(
        finish,
        json={"status": "signed_out", "items_seen": 0, "error_code": "signed_out"},
        headers=collector.headers,
    )
    assert again.status_code == 200
    assert _submit(env, collector, run_key, _batch([_obs()])).status_code == 409
    sources = env.client.get(f"{_BASE}/collection/sources").json()
    assert sources[0]["needs_attention"] and sources[0]["attention_reason"] == "signed_out"


def test_admin_views(env) -> None:
    collector = _collector(env)
    run = _start(env, collector).json()
    _submit(env, collector, run["run_key"], _batch([_obs(), _obs(source_item_id="li-2002")]))
    runs = env.client.get(f"{_BASE}/collection/runs").json()
    assert runs[0]["observations_received"] == 2
    detail = env.client.get(f"{_BASE}/collection/runs/{run['id']}").json()
    assert (
        len(detail["observations"]) == 2
        and detail["observations"][0]["company"] == "Quuxwidget Labs"
    )
    metrics = env.client.get(f"{_BASE}/collection/metrics").json()
    assert metrics["items_by_source"] == {"indeed": 2}
    assert (
        env.collector.get(f"{_BASE}/collector/metrics", headers=collector.headers).status_code
        == 200
    )


def test_logs_never_contain_secrets(env) -> None:
    with structlog.testing.capture_logs() as logs:
        setup = _create(env)
        enrolled = _enroll(env, setup["setup_code"]).json()
        credential = enrolled["credential"]
        headers = _bearer(credential)
        env.collector.get(f"{_BASE}/collector/me", headers=headers)
        env.collector.get(f"{_BASE}/collector/me", headers=_bearer(credential[:-4] + "AAAA"))
        env.client.post(f"{_BASE}/collectors/{setup['collector']['id']}/rotate", headers=env.csrf)
    text = json.dumps(logs, default=str)
    secret = credential.split(".")[1]
    assert secret not in text and setup["setup_code"] not in text
    assert "collector_enrolled" in text and "collector_auth_failed" in text


def test_collector_requests_are_rate_limited(env, monkeypatch) -> None:
    from backend import config as app_config

    collector = _collector(env)
    monkeypatch.setattr(app_config, "COLLECTOR_RATE_LIMIT_REQUESTS", 3)
    statuses = [
        env.collector.get(f"{_BASE}/collector/me", headers=collector.headers).status_code
        for _ in range(5)
    ]
    assert statuses[:3] == [200, 200, 200] and statuses[3:] == [429, 429]


def test_non_json_and_control_character_payloads(env) -> None:
    collector = _collector(env)
    run_key = _start(env, collector).json()["run_key"]
    garbage = env.collector.post(
        f"{_BASE}/collector/runs/{run_key}/observations",
        content=b"\x00\xff not json at all <script>alert(1)</script>",
        headers={**collector.headers, "Content-Type": "application/json"},
    )
    assert garbage.status_code == 422 and "<script>" not in garbage.text
    sneaky = _submit(
        env,
        collector,
        run_key,
        _batch([_obs(company="Quuxwidget\x00 Labs\x1b[31m", role="Eng‮ineer")]),
    )
    assert sneaky.status_code == 200
    payload = env.db.list_run_observations(1)[0].payload
    assert "\x00" not in payload["company"] and "\x1b" not in payload["company"]
    assert "\u202e" not in payload["role"] and payload["role"] == "Engineer"


def test_bogus_secrets_cannot_exhaust_a_real_collectors_budget(env, monkeypatch) -> None:
    """Failed attempts are limited per client; the real collector keeps its own budget."""
    from backend import config as app_config
    from backend.api import collection as collection_api

    collector = _collector(env)
    monkeypatch.setattr(app_config, "COLLECTOR_RATE_LIMIT_REQUESTS", 3)
    token_id = collector.credential.split(".")[0]
    forged = _bearer(f"{token_id}.{'B' * 43}")
    for _ in range(3):
        env.collector.get(f"{_BASE}/collector/me", headers=forged)
    # The attacker's address is now limited...
    assert env.collector.get(f"{_BASE}/collector/me", headers=forged).status_code == 429
    # ...but the genuine collector, from another address, is not.
    monkeypatch.setattr(collection_api, "client_address", lambda _request: "10.9.9.9")
    assert env.collector.get(f"{_BASE}/collector/me", headers=collector.headers).status_code == 200


# ------------------------------------------------------------------ #
# Source readiness: only supported sources may be put in scope         #
# ------------------------------------------------------------------ #


def _legacy_collector(env, scopes: list[str]) -> SimpleNamespace:
    """A collector created before a source was withdrawn (written directly, as the API no
    longer allows it), enrolled through the normal endpoint."""
    from backend.collection import credentials

    collector = env.db.create_collector(
        name="legacy", token_id=credentials.new_token_id(), scopes=scopes, created_by=None
    )
    code = credentials.new_enrollment_code()
    env.db.add_collector_enrollment(
        collector.id, credentials.code_hash(code), datetime.now(UTC) + timedelta(minutes=5)
    )
    enrolled = _enroll(env, code)
    assert enrolled.status_code == 200, enrolled.text
    return SimpleNamespace(
        id=collector.id,
        credential=enrolled.json()["credential"],
        headers=_bearer(enrolled.json()["credential"]),
    )


def test_source_catalog_marks_only_indeed_supported(env) -> None:
    catalog = {e["key"]: e for e in env.client.get(f"{_BASE}/collection/source-catalog").json()}
    assert set(catalog) == {"indeed", "linkedin", "naukri", "instahyre", "careernet"}
    assert catalog["indeed"] == {
        "key": "indeed",
        "label": "Indeed",
        "supported": True,
        "live_verified": "2026-10-09",
        "reason": None,
    }
    for key in ("linkedin", "naukri", "instahyre", "careernet"):
        assert catalog[key]["supported"] is False and catalog[key]["reason"]
        assert catalog[key]["live_verified"] is None


@pytest.mark.parametrize(
    "scopes",
    [["linkedin"], ["naukri"], ["instahyre"], ["careernet"], ["indeed", "linkedin"]],
)
def test_unsupported_scopes_are_rejected_on_create(env, scopes: list[str]) -> None:
    resp = env.client.post(
        f"{_BASE}/collectors", json={"name": "x", "scopes": scopes}, headers=env.csrf
    )
    assert resp.status_code == 422
    assert "unsupported source" in resp.text
    assert env.client.get(f"{_BASE}/collectors").json() == []


def test_supported_and_employer_scopes_are_accepted(env) -> None:
    created = _create(env, scopes=("indeed", "employer-acme"))
    assert created["collector"]["scopes"] == ["employer-acme", "indeed"]
    assert created["collector"]["unsupported_scopes"] == []


def test_legacy_unsupported_scope_stays_listed_but_cannot_rotate_or_run(env) -> None:
    legacy = _legacy_collector(env, ["indeed", "linkedin"])
    listed = env.client.get(f"{_BASE}/collectors").json()
    assert [(c["scopes"], c["unsupported_scopes"]) for c in listed] == [
        (["indeed", "linkedin"], ["linkedin"])
    ]
    rotated = env.client.post(f"{_BASE}/collectors/{legacy.id}/rotate", headers=env.csrf)
    assert rotated.status_code == 409 and "Revoke it" in rotated.json()["detail"]
    # The refused rotation changed nothing: the credential still works for Indeed ...
    assert _start(env, legacy, source="indeed").status_code == 200
    # ... but the withdrawn source cannot start a run although it is still in scope.
    assert _start(env, legacy, source="linkedin").status_code == 403
    revoked = env.client.post(f"{_BASE}/collectors/{legacy.id}/revoke", headers=env.csrf)
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert _start(env, legacy, source="indeed").status_code == 401


def test_supported_collector_rotation_is_unaffected(env) -> None:
    collector = _collector(env)
    rotated = env.client.post(f"{_BASE}/collectors/{collector.id}/rotate", headers=env.csrf)
    assert rotated.status_code == 200
    assert rotated.json()["collector"]["scopes"] == ["indeed"]
    assert _start(env, collector).status_code == 401  # old credential stopped at once


# ------------------------------------------------------------------ #
# Metrics: unique stored rows versus processing across runs            #
# ------------------------------------------------------------------ #


def _run_batch(env, collector, observations: list[dict], source: str = "indeed") -> None:
    run_key = _start(env, collector, source=source).json()["run_key"]
    assert _submit(env, collector, run_key, _batch(observations)).status_code == 200
    finish = env.collector.post(
        f"{_BASE}/collector/runs/{run_key}/finish",
        json={"status": "succeeded", "items_seen": len(observations)},
        headers=collector.headers,
    )
    assert finish.status_code == 200, finish.text


def test_rerun_counts_items_once_but_processing_twice(env) -> None:
    collector = _collector(env)
    batch = [_obs(source_item_id=f"{n:016x}") for n in range(1, 5)]
    _run_batch(env, collector, batch)
    _run_batch(env, collector, batch)  # identical rerun: nothing new is stored
    metrics = env.client.get(f"{_BASE}/collection/metrics").json()
    assert metrics["unique"] == {"source_items": 4, "observations": 4}
    assert metrics["processed_across_runs"]["items_processed"] == 8
    assert metrics["processed_across_runs"]["unchanged"] == 4
    assert sum(metrics["observations_by_decision"].values()) == 4


def test_collector_metrics_are_isolated_per_collector(env) -> None:
    first = _collector(env)
    second = _collector(env, scopes=("indeed", "employer-acme"))
    shared = _obs(source_item_id="00000000000000a2")
    _run_batch(env, first, [_obs(source_item_id="00000000000000a1"), shared])
    # `shared` was already stored by the first collector: unchanged, nothing new stored.
    _run_batch(env, second, [shared, _obs(source_item_id="00000000000000b3")])
    _run_batch(
        env,
        second,
        [_obs(source_key="employer-acme", source_item_id="emp-4", extraction="unverified")],
        source="employer-acme",
    )

    def own(collector) -> dict:
        resp = env.collector.get(f"{_BASE}/collector/metrics", headers=collector.headers)
        assert resp.status_code == 200
        return resp.json()

    total = env.client.get(f"{_BASE}/collection/metrics").json()
    assert total["unique"] == {"source_items": 4, "observations": 4}
    assert total["items_by_source"] == {"indeed": 3, "employer-acme": 1}
    assert total["processed_across_runs"]["items_processed"] == 5

    a, b = own(first), own(second)
    assert a["unique"] == {"source_items": 2, "observations": 2}
    assert a["items_by_source"] == {"indeed": 2}
    assert sum(a["items_by_decision"].values()) == 2
    assert a["processed_across_runs"]["items_processed"] == 2
    assert a["runs_by_status"] == {"succeeded": 1}

    assert b["unique"] == {"source_items": 2, "observations": 2}
    assert b["items_by_source"] == {"indeed": 1, "employer-acme": 1}
    assert sum(b["items_by_decision"].values()) == 2
    assert b["processed_across_runs"]["items_processed"] == 3
    assert b["processed_across_runs"]["unchanged"] == 1
    assert b["runs_by_status"] == {"succeeded": 2}
