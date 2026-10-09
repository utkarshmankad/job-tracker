"""Integration tests for authentication on the real FastAPI app.

These tests deliberately do NOT use the `test_auth_user` override fixture: every request
goes through the real router-level `require_user` dependency, session cookie, CSRF check
and rate limiting. Google's verifier is replaced by a fake (the real verifier is covered
with locally signed RS256 tokens in tests/unit/test_auth.py, plus one wiring test below).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from backend.api.auth import (
    AuthConfig,
    AuthConfigError,
    AuthService,
    IdentityProviderUnavailable,
    require_user,
)
from backend.api.collection import admin_router as collection_admin_router
from backend.api.collection import collector_router
from backend.api.routes import public_router
from backend.api.routes import router as protected_router
from backend.db.data_store import ApplicationFilter, DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app

_BASE = "/api/v1"
CLIENT_ID = "integration-client.apps.googleusercontent.com"
OWNER = "owner@example.com"
SECRET = "integration-session-secret-" + "q" * 24
FRONTEND = "http://jobtracker.localhost:5173"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeGoogle:
    """Stands in for Google's verifier: echoes the latest login nonce into the claims."""

    email: str = OWNER
    email_verified: bool = True
    nonce: str | None = None

    def __call__(self, credential: str, client_id: str) -> dict:
        if credential == "malformed":
            raise ValueError("malformed")
        if credential == "expired":
            raise ValueError("Token expired")
        if credential == "idp-down":
            raise IdentityProviderUnavailable()
        return {
            "iss": "https://accounts.google.com",
            "aud": client_id,
            "sub": "google-sub-1",
            "email": self.email,
            "email_verified": self.email_verified,
            "nonce": self.nonce,
        }


def _config(**overrides: object) -> AuthConfig:
    values: dict = {
        "app_env": "development",
        "mode": "google",
        "allowed_email": OWNER,
        "google_client_id": CLIENT_ID,
        "session_secret": SECRET,
        "session_ttl_seconds": 3600,
        "allowed_origins": (FRONTEND,),
        "frontend_origin": None,
    }
    values.update(overrides)
    return AuthConfig(**values)


@pytest.fixture
def env(tmp_path) -> Iterator[SimpleNamespace]:
    assert require_user not in app.dependency_overrides, "auth tests must use real auth"
    clock = FakeClock()
    google = FakeGoogle()
    with TestClient(app) as client:
        db = DataStore(tmp_path / "auth.db")
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        app.state.auth = AuthService(_config(), verifier=google, clock=clock)
        yield SimpleNamespace(client=client, clock=clock, google=google, db=db)


def _login(env: SimpleNamespace, credential: str = "google-credential") -> dict:
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": credential})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------ #
# Public surface                                                       #
# ------------------------------------------------------------------ #


def test_health_is_public_and_sanitized(env) -> None:
    resp = env.client.get(f"{_BASE}/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


_PUBLIC = {
    ("GET", f"{_BASE}/health"),
    ("GET", f"{_BASE}/auth/config"),
    ("POST", f"{_BASE}/auth/google"),
    ("POST", f"{_BASE}/auth/local"),
    ("GET", f"{_BASE}/auth/session"),
    ("POST", f"{_BASE}/auth/logout"),
}


def test_only_health_and_auth_endpoints_are_public() -> None:
    public = {
        (method, _BASE + route.path)
        for route in public_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert public == _PUBLIC


def test_every_mounted_endpoint_is_public_or_protected() -> None:
    """Guards against a new router being mounted without the auth dependency."""
    mounted = {
        (method.upper(), re.sub(r"\{[^}]+\}", "1", path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert mounted == _PUBLIC | set(_PROTECTED) | set(_COLLECTOR) | _COLLECTOR_ENROLL


def test_auth_config_exposes_only_public_values(env) -> None:
    body = env.client.get(f"{_BASE}/auth/config").json()
    assert body["mode"] == "google"
    assert body["configured"] is True
    assert body["google_client_id"] == CLIENT_ID
    assert body["nonce"]
    assert SECRET not in str(body)
    assert OWNER not in str(body)


# ------------------------------------------------------------------ #
# Every protected route rejects anonymous callers                      #
# ------------------------------------------------------------------ #


def _routes(router) -> list[tuple[str, str]]:
    return sorted(
        (method, _BASE + re.sub(r"\{[^}]+\}", "1", route.path))
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )


# Session-cookie (+ CSRF) protected: the main API and the collection admin endpoints.
_PROTECTED = sorted(_routes(protected_router) + _routes(collection_admin_router))
# The local collector's own endpoints: scoped bearer credential, never the session cookie.
# Enrollment is authenticated by a single-use setup code instead (tested separately).
_COLLECTOR_ENROLL = {("POST", f"{_BASE}/collector/enroll")}
_COLLECTOR = [r for r in _routes(collector_router) if r not in _COLLECTOR_ENROLL]


def test_protected_route_inventory_covers_required_areas() -> None:
    paths = {path for _, path in _PROTECTED}
    for required in (
        "/applications",
        "/prospects",
        "/insights",
        "/applications/export",
        "/applications/duplicates",
        "/applications/duplicates/merge",
        "/poller/trigger",
        "/poller/status",
        "/diagnostics",
        "/status",
        "/poller/reauth/start",
        "/poller/reauth/callback",
    ):
        assert f"{_BASE}{required}" in paths


@pytest.mark.parametrize(("method", "path"), _PROTECTED)
def test_protected_route_requires_authentication(env, method: str, path: str) -> None:
    resp = env.client.request(method, path, json={})
    assert resp.status_code == 401, (method, path, resp.text)
    assert resp.json() == {"detail": "Authentication required.", "code": "not_authenticated"}
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(("method", "path"), _COLLECTOR)
def test_collector_route_requires_bearer_credential(env, method: str, path: str) -> None:
    """No collector endpoint accepts an anonymous caller — or a signed-in browser session."""
    resp = env.client.request(method, path, json={})
    assert resp.status_code == 401, (method, path, resp.text)
    assert resp.json()["code"] == "collector_unauthorized"


def test_collector_route_inventory() -> None:
    assert {path for _, path in _COLLECTOR} == {
        f"{_BASE}/collector/me",
        f"{_BASE}/collector/runs",
        f"{_BASE}/collector/runs/1",
        f"{_BASE}/collector/runs/1/observations",
        f"{_BASE}/collector/runs/1/finish",
        f"{_BASE}/collector/metrics",
    }


def test_anonymous_mutation_changes_nothing(env) -> None:
    resp = env.client.post(
        f"{_BASE}/applications",
        json={"source_portal": "LinkedIn", "applied_date": "2024-06-01", "company": "Acme"},
    )
    assert resp.status_code == 401
    _, total = env.db.get_applications(ApplicationFilter())
    assert total == 0


def test_detailed_status_and_diagnostics_require_auth(env) -> None:
    assert env.client.get(f"{_BASE}/status").status_code == 401
    assert env.client.get(f"{_BASE}/diagnostics").status_code == 401


# ------------------------------------------------------------------ #
# Google sign-in                                                       #
# ------------------------------------------------------------------ #


def test_valid_account_signs_in_and_reads_data(env) -> None:
    body = _login(env)
    assert body["user"] == {"email": OWNER}
    assert body["csrf_token"]
    assert env.client.cookies.get("jt_session")

    assert env.client.get(f"{_BASE}/poller/status").status_code == 200
    assert env.client.get(f"{_BASE}/status").status_code == 200
    session = env.client.get(f"{_BASE}/auth/session")
    assert session.status_code == 200
    assert session.json()["user"]["email"] == OWNER


def test_session_cookie_attributes(env) -> None:
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "c"})
    header = resp.headers["set-cookie"].lower()
    assert "jt_session=" in header
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    assert resp.headers["cache-control"] == "no-store"


def test_unauthorized_google_account_is_rejected(env) -> None:
    env.google.email = "intruder@example.com"
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "c"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_not_allowed"
    assert "set-cookie" not in resp.headers
    assert env.client.get(f"{_BASE}/poller/status").status_code == 401


def test_unverified_google_email_is_rejected(env) -> None:
    env.google.email_verified = False
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "c"})
    assert resp.status_code == 403


@pytest.mark.parametrize("credential", ["malformed", "expired"])
def test_malformed_and_expired_google_tokens_are_rejected(env, credential) -> None:
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": credential})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


def test_empty_credential_is_rejected(env) -> None:
    assert env.client.post(f"{_BASE}/auth/google", json={"credential": ""}).status_code == 422


def test_identity_provider_outage_returns_503(env) -> None:
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "idp-down"})
    assert resp.status_code == 503


def test_login_from_foreign_origin_is_rejected(env) -> None:
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(
        f"{_BASE}/auth/google",
        json={"credential": "c"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "origin_not_allowed"


def test_login_from_allowed_origin_succeeds(env) -> None:
    env.google.nonce = env.client.get(f"{_BASE}/auth/config").json()["nonce"]
    resp = env.client.post(
        f"{_BASE}/auth/google", json={"credential": "c"}, headers={"Origin": FRONTEND}
    )
    assert resp.status_code == 200


def test_real_google_verifier_is_wired_by_default(env) -> None:
    """With the default verifier, an unverifiable credential is rejected (no network:
    Google's certs are stubbed)."""
    app.state.auth = AuthService(_config(), clock=env.clock)
    with patch("google.oauth2.id_token._fetch_certs", return_value={}):
        resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "a.b.c"})
    assert resp.status_code == 401


# ------------------------------------------------------------------ #
# Sessions: expiry, tampering, CSRF, logout                            #
# ------------------------------------------------------------------ #


def test_expired_session_is_rejected(env) -> None:
    _login(env)
    env.clock.now += 3601
    resp = env.client.get(f"{_BASE}/poller/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == "session_expired"
    assert env.client.get(f"{_BASE}/auth/session").status_code == 401


@pytest.mark.parametrize("cookie", ["garbage", "v1.e30.invalid-signature", ""])
def test_malformed_session_cookie_is_rejected(env, cookie) -> None:
    env.client.cookies.set("jt_session", cookie)
    resp = env.client.get(f"{_BASE}/poller/status")
    assert resp.status_code == 401


def test_tampered_session_cookie_is_rejected(env) -> None:
    _login(env)
    token = env.client.cookies.get("jt_session")
    env.client.cookies.set("jt_session", token[:-2] + ("AA" if not token.endswith("AA") else "BB"))
    resp = env.client.get(f"{_BASE}/poller/status")
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_session"


def test_mutation_requires_csrf_token(env) -> None:
    csrf = _login(env)["csrf_token"]
    payload = {"source_portal": "LinkedIn", "applied_date": "2024-06-01", "company": "Acme"}

    missing = env.client.post(f"{_BASE}/applications", json=payload)
    assert missing.status_code == 403
    assert missing.json() == {
        "detail": "Request could not be verified. Reload the page.",
        "code": "csrf_failed",
    }
    wrong = env.client.post(f"{_BASE}/applications", json=payload, headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403

    ok = env.client.post(f"{_BASE}/applications", json=payload, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 201


def test_logout_requires_csrf_and_revokes_session(env) -> None:
    csrf = _login(env)["csrf_token"]
    stolen_cookie = env.client.cookies.get("jt_session")

    assert env.client.post(f"{_BASE}/auth/logout").status_code == 403
    resp = env.client.post(f"{_BASE}/auth/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 204
    assert "jt_session=" in resp.headers["set-cookie"]
    assert env.client.get(f"{_BASE}/poller/status").status_code == 401

    # A copy of the cookie taken before logout no longer works either.
    env.client.cookies.set("jt_session", stolen_cookie)
    replay = env.client.get(f"{_BASE}/poller/status")
    assert replay.status_code == 401
    assert replay.json()["code"] == "session_revoked"


def test_logout_without_session_is_harmless(env) -> None:
    assert env.client.post(f"{_BASE}/auth/logout").status_code == 204


# ------------------------------------------------------------------ #
# Rate limiting                                                        #
# ------------------------------------------------------------------ #


def test_login_attempts_are_rate_limited(env, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.AUTH_RATE_LIMIT_ATTEMPTS", 3)
    codes = [
        env.client.post(f"{_BASE}/auth/google", json={"credential": "malformed"}).status_code
        for _ in range(4)
    ]
    assert codes == [401, 401, 401, 429]
    limited = env.client.post(f"{_BASE}/auth/google", json={"credential": "malformed"})
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1


def test_sensitive_operations_are_rate_limited(env, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.SENSITIVE_RATE_LIMIT_REQUESTS", 2)
    _login(env)
    codes = [env.client.get(f"{_BASE}/diagnostics").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_anonymous_requests_do_not_consume_sensitive_budget(env, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.SENSITIVE_RATE_LIMIT_REQUESTS", 1)
    for _ in range(3):
        assert env.client.get(f"{_BASE}/diagnostics").status_code == 401
    _login(env)
    assert env.client.get(f"{_BASE}/diagnostics").status_code == 200


# ------------------------------------------------------------------ #
# Configuration fails closed                                           #
# ------------------------------------------------------------------ #


def test_unconfigured_google_mode_rejects_everything(env) -> None:
    app.state.auth = AuthService(_config(google_client_id=None), verifier=env.google)
    cfg = env.client.get(f"{_BASE}/auth/config").json()
    assert cfg == {"mode": "google", "configured": False, "google_client_id": None, "nonce": None}
    resp = env.client.post(f"{_BASE}/auth/google", json={"credential": "c"})
    assert resp.status_code == 503
    assert resp.json()["code"] == "auth_not_configured"
    assert env.client.get(f"{_BASE}/applications").status_code == 401


def test_missing_auth_service_fails_closed(env) -> None:
    del app.state.auth
    assert env.client.get(f"{_BASE}/poller/status").status_code == 401
    assert env.client.get(f"{_BASE}/health").status_code == 200


def test_production_startup_fails_without_auth_config(monkeypatch) -> None:
    monkeypatch.setattr("backend.config.APP_ENV", "production")
    monkeypatch.setattr("backend.config.AUTH_MODE", "google")
    monkeypatch.setattr("backend.config.AUTH_ALLOWED_EMAIL", None)
    monkeypatch.setattr("backend.config.AUTH_GOOGLE_CLIENT_ID", None)
    monkeypatch.setattr("backend.config.AUTH_SESSION_SECRET", None)
    with pytest.raises(AuthConfigError, match="AUTH_ALLOWED_EMAIL"):
        with TestClient(app):
            pass


def test_production_startup_rejects_local_mode(monkeypatch) -> None:
    monkeypatch.setattr("backend.config.APP_ENV", "production")
    monkeypatch.setattr("backend.config.AUTH_MODE", "local")
    monkeypatch.setattr("backend.config.AUTH_SESSION_SECRET", SECRET)
    monkeypatch.setattr("backend.config.FRONTEND_ORIGIN", "https://tracker.example.com")
    with pytest.raises(AuthConfigError, match="AUTH_MODE=local"):
        with TestClient(app):
            pass


def test_production_uses_secure_host_cookie(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("backend.config.APP_ENV", "production")
    monkeypatch.setattr("backend.config.AUTH_MODE", "google")
    monkeypatch.setattr("backend.config.AUTH_ALLOWED_EMAIL", OWNER)
    monkeypatch.setattr("backend.config.AUTH_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr("backend.config.AUTH_SESSION_SECRET", SECRET)
    monkeypatch.setattr("backend.config.FRONTEND_ORIGIN", "https://tracker.example.com")
    google = FakeGoogle()
    with TestClient(app, base_url="https://testserver") as client:
        service = app.state.auth
        assert service.config.is_production and service.ready
        app.state.auth = AuthService(service.config, verifier=google)
        google.nonce = client.get(f"{_BASE}/auth/config").json()["nonce"]
        resp = client.post(f"{_BASE}/auth/google", json={"credential": "c"})
        assert resp.status_code == 200
        header = resp.headers["set-cookie"].lower()
        assert header.startswith("__host-jt_session=")
        assert "secure" in header
        assert client.get(f"{_BASE}/poller/status").status_code == 200


# ------------------------------------------------------------------ #
# Local developer mode                                                 #
# ------------------------------------------------------------------ #


def test_local_mode_signs_in_from_loopback_only(tmp_path) -> None:
    local = _config(mode="local", allowed_email=None, google_client_id=None, session_secret=None)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        app.state.auth = AuthService(local)
        assert client.get(f"{_BASE}/auth/config").json()["mode"] == "local"
        assert client.get(f"{_BASE}/poller/status").status_code == 401
        resp = client.post(f"{_BASE}/auth/local")
        assert resp.status_code == 200
        assert client.get(f"{_BASE}/poller/status").status_code == 200

    with TestClient(app, client=("203.0.113.7", 50000)) as remote:
        app.state.auth = AuthService(local)
        assert remote.post(f"{_BASE}/auth/local").status_code == 403


def test_local_login_disabled_in_google_mode(env) -> None:
    assert env.client.post(f"{_BASE}/auth/local").status_code == 403


# ------------------------------------------------------------------ #
# CORS                                                                 #
# ------------------------------------------------------------------ #


def test_cors_allows_configured_origin_with_credentials(env) -> None:
    resp = env.client.options(
        f"{_BASE}/applications",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == FRONTEND
    assert resp.headers["access-control-allow-credentials"] == "true"
    assert "x-csrf-token" in resp.headers["access-control-allow-headers"].lower()


def test_cors_rejects_unknown_origin(env) -> None:
    resp = env.client.options(
        f"{_BASE}/applications",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers
