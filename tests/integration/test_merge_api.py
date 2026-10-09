"""Merge workflow API: preview, execute, operations, undo, duplicates, analytics."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.api.auth import AuthService, require_user
from backend.db.data_store import DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.integration.test_auth import FakeClock, FakeGoogle, _config
from tests.unit.test_merge import make_app

_BASE = "/api/v1"


def _seed(db: DataStore) -> list[int]:
    return [
        make_app(db, "a", company="Acme", role="Engineer", days_ago=30, interview_day=6).id,
        make_app(db, "b", company="Acme Pvt Ltd", role="Engineer", days_ago=20, interview_day=6).id,
        make_app(
            db, "c", company="Acme", role="Software Engineer", days_ago=10, portal="Naukri"
        ).id,
    ]


@pytest.fixture
def api(tmp_path, test_auth_user):
    db = DataStore(tmp_path / "merge-api.db")
    ids = _seed(db)
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        yield SimpleNamespace(client=client, db=db, ids=ids)


def _preview(env, survivor=None):
    body = {"application_ids": env.ids}
    if survivor is not None:
        body["survivor_id"] = survivor
    resp = env.client.post(f"{_BASE}/applications/merge/preview", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _execute(env, preview, choices=None, key=None, **overrides):
    body = {
        "application_ids": preview["application_ids"],
        "survivor_id": preview["survivor_id"],
        "field_choices": choices
        if choices is not None
        else {name: preview["survivor_id"] for name in preview["conflicts"]},
        "preview_token": preview["preview_token"],
        "idempotency_key": key or uuid.uuid4().hex,
        "reason": "duplicate acknowledgements",
    }
    body.update(overrides)
    return env.client.post(f"{_BASE}/applications/merge", json=body)


def _analytics(env) -> dict:
    flow = env.client.get(f"{_BASE}/insights/flow").json()
    pulse = env.client.get(f"{_BASE}/insights/pulse").json()
    conversions = env.client.get(f"{_BASE}/insights/conversions").json()
    listed = env.client.get(f"{_BASE}/applications").json()["total"]
    for payload in (flow, pulse, conversions):
        payload.pop("generated_at", None)
    return {"total": listed, "flow": flow, "pulse": pulse, "conversions": conversions}


def test_preview_payload(api) -> None:
    preview = _preview(api)
    assert preview["safe"] is True and preview["blocking"] == []
    assert len(preview["applications"]) == 3
    assert {"company", "role", "source_portal"} <= set(preview["conflicts"])
    assert "Nothing is permanently deleted" in preview["note"]
    assert preview["counts"]["evidence"] == 3
    fields = {f["name"]: f for f in preview["fields"]}
    assert set(fields["company"]["values"]) == {str(i) for i in api.ids}
    assert fields["applied_date"]["rule"] == "earliest"
    assert api.db.list_merge_operations()[1] == 0


def test_preview_validation(api) -> None:
    assert (
        api.client.post(
            f"{_BASE}/applications/merge/preview", json={"application_ids": [api.ids[0]]}
        ).status_code
        == 422
    )
    dup = {"application_ids": [api.ids[0], api.ids[0]]}
    assert api.client.post(f"{_BASE}/applications/merge/preview", json=dup).status_code == 422
    unknown = api.client.post(
        f"{_BASE}/applications/merge/preview", json={"application_ids": [api.ids[0], 999999]}
    ).json()
    assert unknown["safe"] is False


def test_merge_undo_round_trip_and_analytics(api) -> None:
    before = _analytics(api)
    assert before["total"] == 3

    preview = _preview(api, survivor=api.ids[2])
    resp = _execute(
        api, preview, choices={**{n: api.ids[2] for n in preview["conflicts"]}, "role": api.ids[0]}
    )
    assert resp.status_code == 200, resp.text
    op = resp.json()
    assert op["status"] == "applied" and op["survivor_application_id"] == api.ids[2]
    assert sorted(op["source_application_ids"]) == sorted(api.ids[:2])
    assert op["counts"]["evidence"] == 2
    assert op["superseded"]["status_history"] == 2

    after = _analytics(api)
    assert after["total"] == 1
    survivor = api.client.get(f"{_BASE}/applications/{api.ids[2]}").json()
    assert survivor["role"] == "Engineer"
    assert [h["to_status"] for h in survivor["status_history"]] == ["Applied"]
    merged = api.client.get(f"{_BASE}/applications/{api.ids[0]}").json()
    assert merged["record_state"] == "merged" and merged["merged_into_application_id"] == api.ids[2]
    with_merged = api.client.get(f"{_BASE}/applications", params={"include_merged": "true"}).json()
    assert with_merged["total"] == 3
    assert api.client.get(f"{_BASE}/applications", params={"is_stale": "true"}).json()["total"] <= 1

    listing = api.client.get(f"{_BASE}/applications/merges").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == op["id"]
    detail = api.client.get(f"{_BASE}/applications/merges/{op['id']}").json()
    assert [a["survivor"] for a in detail["applications"]].count(True) == 1

    undone = api.client.post(f"{_BASE}/applications/merges/{op['id']}/undo")
    assert undone.status_code == 200 and undone.json()["status"] == "undone"
    assert _analytics(api) == before
    again = api.client.post(f"{_BASE}/applications/merges/{op['id']}/undo")
    assert again.status_code == 200 and again.json()["undone_at"] == undone.json()["undone_at"]


def test_merge_is_idempotent(api) -> None:
    preview = _preview(api)
    key = uuid.uuid4().hex
    first = _execute(api, preview, key=key)
    second = _execute(api, preview, key=key)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert api.db.list_merge_operations()[1] == 1
    other = _execute(
        api,
        preview,
        key=key,
        survivor_id=preview["application_ids"][0]
        if preview["survivor_id"] != preview["application_ids"][0]
        else preview["application_ids"][1],
    )
    assert other.status_code == 409


def test_stale_preview_is_rejected(api) -> None:
    preview = _preview(api)
    api.client.patch(f"{_BASE}/applications/{api.ids[1]}", json={"role": "Platform Engineer"})
    resp = _execute(api, preview)
    assert resp.status_code == 409
    assert "changed since the preview" in resp.json()["detail"]
    assert api.db.list_merge_operations()[1] == 0


def test_unresolved_conflicts_and_bad_choices(api) -> None:
    preview = _preview(api)
    assert _execute(api, preview, choices={}).status_code == 422
    bad = {name: 999 for name in preview["conflicts"]}
    assert _execute(api, preview, choices=bad).status_code == 422
    assert _execute(api, preview, idempotency_key="bad key!").status_code == 422


def test_unsafe_undo_returns_conflict_details(api) -> None:
    preview = _preview(api, survivor=api.ids[0])
    op = _execute(api, preview).json()
    api.client.patch(f"{_BASE}/applications/{api.ids[0]}", json={"role": "Director"})
    resp = api.client.post(f"{_BASE}/applications/merges/{op['id']}/undo")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "overwriting newer changes" in detail["message"]
    assert any("role was edited" in c for c in detail["conflicts"])
    assert api.client.get(f"{_BASE}/applications").json()["total"] == 1


def test_merged_records_cannot_be_deleted(api) -> None:
    preview = _preview(api, survivor=api.ids[0])
    _execute(api, preview)
    assert api.client.delete(f"{_BASE}/applications/{api.ids[0]}").status_code == 409
    assert api.client.delete(f"{_BASE}/applications/{api.ids[1]}").status_code == 409
    bulk = api.client.post(
        f"{_BASE}/applications/bulk-delete", json={"application_ids": [api.ids[1]]}
    )
    assert bulk.json()["updated"] == 0 and bulk.json()["failed_ids"] == [api.ids[1]]
    assert api.db.get_application(api.ids[1]) is not None


def test_duplicate_suggestions_and_dismissal(api) -> None:
    pairs = api.client.get(f"{_BASE}/applications/duplicates").json()
    assert pairs and all("pair_key" in p for p in pairs)
    first = pairs[0]
    ids = [first["primary"]["id"], first["duplicate"]["id"]]
    for _ in range(2):
        resp = api.client.post(
            f"{_BASE}/applications/duplicates/dismiss", json={"application_ids": ids}
        )
        assert resp.status_code == 200
    remaining = api.client.get(f"{_BASE}/applications/duplicates").json()
    assert first["pair_key"] not in {p["pair_key"] for p in remaining}
    assert api.client.get(f"{_BASE}/applications").json()["total"] == 3  # nothing deleted


def test_merge_operation_not_found(api) -> None:
    assert api.client.get(f"{_BASE}/applications/merges/4242").status_code == 404
    assert api.client.post(f"{_BASE}/applications/merges/4242/undo").status_code == 404


def test_operation_payload_has_no_message_content(api) -> None:
    preview = _preview(api)
    op = _execute(api, preview).json()
    op.pop("initiated_by")  # the owner's own account, recorded on purpose
    text = json.dumps(op)
    assert "thread-" not in text and "@" not in text


# ------------------------------------------------------------------ #
# Real sessions: authentication, CSRF, rate limits                     #
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
        ("POST", "/applications/merge/preview"),
        ("POST", "/applications/merge"),
        ("GET", "/applications/merges"),
        ("GET", "/applications/merges/1"),
        ("POST", "/applications/merges/1/undo"),
        ("POST", "/applications/duplicates/dismiss"),
    ],
)
def test_merge_endpoints_require_authentication(secured, method, path) -> None:
    assert secured.client.request(method, _BASE + path, json={}).status_code == 401


def test_merge_and_undo_require_csrf(secured) -> None:
    csrf = _sign_in(secured)
    headers = {"X-CSRF-Token": csrf}
    body = {"application_ids": secured.ids}
    assert secured.client.post(f"{_BASE}/applications/merge/preview", json=body).status_code == 403
    preview = secured.client.post(
        f"{_BASE}/applications/merge/preview", json=body, headers=headers
    ).json()
    execute = {
        "application_ids": preview["application_ids"],
        "survivor_id": preview["survivor_id"],
        "field_choices": {n: preview["survivor_id"] for n in preview["conflicts"]},
        "preview_token": preview["preview_token"],
        "idempotency_key": uuid.uuid4().hex,
    }
    assert secured.client.post(f"{_BASE}/applications/merge", json=execute).status_code == 403
    assert secured.db.list_merge_operations()[1] == 0
    op = secured.client.post(f"{_BASE}/applications/merge", json=execute, headers=headers).json()
    assert op["initiated_by"] == "owner@example.com"
    undo = f"{_BASE}/applications/merges/{op['id']}/undo"
    assert secured.client.post(undo).status_code == 403
    assert secured.client.post(undo, headers=headers).json()["undone_by"] == "owner@example.com"


def test_merge_endpoints_are_rate_limited(secured, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.SENSITIVE_RATE_LIMIT_REQUESTS", 2)
    headers = {"X-CSRF-Token": _sign_in(secured)}
    body = {"application_ids": secured.ids}
    codes = [
        secured.client.post(
            f"{_BASE}/applications/merge/preview", json=body, headers=headers
        ).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]
