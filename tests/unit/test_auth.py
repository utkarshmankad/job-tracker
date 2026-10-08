"""Unit tests for backend/api/auth.py — config validation, signed sessions, CSRF, nonces,
rate limiting and server-side Google ID token verification."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt
from google.auth import exceptions as google_exceptions
from google.auth import jwt as google_jwt

from backend.api.auth import (
    AuthConfig,
    AuthConfigError,
    AuthError,
    AuthService,
    IdentityProviderUnavailable,
    RateLimiter,
    SessionCodec,
    _b64encode,
    validate_auth_config,
    verify_google_id_token,
)

CLIENT_ID = "test-client.apps.googleusercontent.com"
OWNER = "owner@example.com"
SECRET = "unit-test-session-secret-" + "x" * 24
NOW = 1_800_000_000


class FakeClock:
    def __init__(self, start: float = NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config(**overrides: object) -> AuthConfig:
    base = AuthConfig(
        app_env="development",
        mode="google",
        allowed_email=OWNER,
        google_client_id=CLIENT_ID,
        session_secret=SECRET,
        session_ttl_seconds=3600,
        allowed_origins=("http://jobtracker.localhost:5173",),
        frontend_origin=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# ------------------------------------------------------------------ #
# Configuration                                                        #
# ------------------------------------------------------------------ #


def test_complete_google_config_has_no_problems() -> None:
    assert _config().problems() == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"allowed_email": None}, "AUTH_ALLOWED_EMAIL"),
        ({"google_client_id": None}, "AUTH_GOOGLE_CLIENT_ID"),
        ({"session_secret": None}, "AUTH_SESSION_SECRET is not set"),
        ({"session_secret": "short"}, "at least 32"),
        ({"session_ttl_seconds": 10}, "AUTH_SESSION_TTL_SECONDS"),
        ({"session_ttl_seconds": 10**8}, "AUTH_SESSION_TTL_SECONDS"),
        ({"mode": "none"}, "AUTH_MODE must be"),
    ],
)
def test_config_problems_name_the_missing_setting(overrides: dict, expected: str) -> None:
    problems = _config(**overrides).problems()
    assert any(expected in p for p in problems), problems


def test_problems_never_echo_secret_values() -> None:
    problems = _config(session_secret="tiny-secret-value").problems()
    assert problems
    assert all("tiny-secret-value" not in p for p in problems)


def test_production_rejects_local_mode() -> None:
    cfg = _config(app_env="production", mode="local", frontend_origin="https://tracker.example.com")
    assert any("AUTH_MODE=local is not allowed" in p for p in cfg.problems())


def test_production_requires_https_frontend_origin() -> None:
    assert any("FRONTEND_ORIGIN" in p for p in _config(app_env="production").problems())
    assert any(
        "FRONTEND_ORIGIN" in p
        for p in _config(app_env="production", frontend_origin="http://x.example").problems()
    )
    assert _config(app_env="production", frontend_origin="https://x.example").problems() == []


def test_validate_fails_closed_in_production() -> None:
    cfg = _config(app_env="production", google_client_id=None, frontend_origin="https://x.dev")
    with pytest.raises(AuthConfigError, match="AUTH_GOOGLE_CLIENT_ID"):
        validate_auth_config(cfg)


def test_validate_reports_but_does_not_raise_outside_production() -> None:
    assert validate_auth_config(_config(google_client_id=None)) == [
        "AUTH_GOOGLE_CLIENT_ID is not set"
    ]


def test_local_mode_outside_production_needs_no_google_settings() -> None:
    cfg = _config(mode="local", allowed_email=None, google_client_id=None, session_secret=None)
    assert cfg.problems() == []


def test_cookie_is_host_prefixed_and_secure_in_production() -> None:
    prod = _config(app_env="production", frontend_origin="https://x.example")
    assert prod.cookie_secure is True
    assert prod.cookie_name == "__Host-jt_session"
    assert _config().cookie_secure is False
    assert _config().cookie_name == "jt_session"


# ------------------------------------------------------------------ #
# Session tokens                                                       #
# ------------------------------------------------------------------ #


def test_session_round_trip() -> None:
    codec = SessionCodec(SECRET)
    token, session = codec.issue(OWNER, "sub-1", NOW, 600)
    decoded = codec.decode(token, NOW + 10)
    assert decoded == session
    assert decoded.expires_at == NOW + 600


def test_expired_session_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    token, _ = codec.issue(OWNER, "sub-1", NOW, 600)
    with pytest.raises(AuthError) as err:
        codec.decode(token, NOW + 600)
    assert (err.value.status_code, err.value.code) == (401, "session_expired")


@pytest.mark.parametrize(
    "token",
    ["", "garbage", "v1.only-two", "v2.abc.def", "v1.abc.def.ghi", "v1.!!!.sig"],
)
def test_malformed_session_is_rejected(token: str) -> None:
    with pytest.raises(AuthError) as err:
        SessionCodec(SECRET).decode(token, NOW)
    assert (err.value.status_code, err.value.code) == (401, "invalid_session")


def test_tampered_payload_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    token, _ = codec.issue(OWNER, "sub-1", NOW, 600)
    version, _payload, sig = token.split(".")
    forged = _b64encode(
        json.dumps(
            {"sid": "x", "email": "attacker@example.com", "sub": "s", "iat": NOW, "exp": NOW + 9}
        ).encode()
    )
    with pytest.raises(AuthError) as err:
        codec.decode(f"{version}.{forged}.{sig}", NOW)
    assert err.value.code == "invalid_session"


def test_session_signed_with_other_secret_is_rejected() -> None:
    token, _ = SessionCodec("another-secret-" + "y" * 32).issue(OWNER, "s", NOW, 600)
    with pytest.raises(AuthError):
        SessionCodec(SECRET).decode(token, NOW)


def test_correctly_signed_but_invalid_payload_is_rejected() -> None:
    codec = SessionCodec(SECRET)
    body = "v1." + _b64encode(json.dumps({"sid": "x", "email": OWNER}).encode())
    token = f"{body}.{codec._sign(codec._session_key, body)}"
    with pytest.raises(AuthError) as err:
        codec.decode(token, NOW)
    assert err.value.code == "invalid_session"


def test_csrf_token_is_bound_to_session() -> None:
    codec = SessionCodec(SECRET)
    token = codec.csrf_token("sid-a")
    assert codec.verify_csrf("sid-a", token)
    assert not codec.verify_csrf("sid-b", token)
    assert not codec.verify_csrf("sid-a", None)
    assert not codec.verify_csrf("sid-a", "")


def test_nonce_authenticity_and_expiry() -> None:
    codec = SessionCodec(SECRET)
    nonce = codec.issue_nonce(NOW)
    assert codec.nonce_is_authentic(nonce, NOW + 5, 600)
    assert not codec.nonce_is_authentic(nonce, NOW + 601, 600)
    assert not codec.nonce_is_authentic(nonce + "x", NOW, 600)
    assert not codec.nonce_is_authentic("not.a.nonce", NOW, 600)
    assert not SessionCodec("other-" + "z" * 40).nonce_is_authentic(nonce, NOW, 600)


# ------------------------------------------------------------------ #
# Rate limiting                                                        #
# ------------------------------------------------------------------ #


def test_rate_limiter_blocks_after_limit_and_recovers() -> None:
    clock = FakeClock(0)
    limiter = RateLimiter(clock=clock)
    assert [limiter.hit("k", 3, 60) for _ in range(3)] == [None, None, None]
    retry_after = limiter.hit("k", 3, 60)
    assert retry_after is not None and 1 <= retry_after <= 61
    assert limiter.hit("other", 3, 60) is None  # keys are independent
    clock.advance(61)
    assert limiter.hit("k", 3, 60) is None


# ------------------------------------------------------------------ #
# AuthService                                                          #
# ------------------------------------------------------------------ #


def _claims(nonce: str, **overrides: object) -> dict:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-1",
        "email": OWNER,
        "email_verified": True,
        "nonce": nonce,
    }
    claims.update(overrides)
    return claims


def _service(claims_fn=None, **config_overrides: object) -> tuple[AuthService, FakeClock]:
    clock = FakeClock()
    holder: dict = {}

    def verifier(credential: str, client_id: str) -> dict:
        assert client_id == CLIENT_ID
        if credential == "invalid":
            raise ValueError("bad token")
        if credential == "idp-down":
            raise IdentityProviderUnavailable()
        return claims_fn(holder["nonce"]) if claims_fn else _claims(holder["nonce"])

    service = AuthService(_config(**config_overrides), verifier=verifier, clock=clock)
    holder["nonce"] = service.new_login_nonce()
    return service, clock


def test_google_login_issues_session_for_allowed_account() -> None:
    service, _ = _service()
    token, session = service.login_with_google("credential")
    assert session.email == OWNER
    assert service.authenticate(token) == session


def test_google_login_normalises_email_case() -> None:
    service, _ = _service(lambda n: _claims(n, email="Owner@Example.com"))
    _, session = service.login_with_google("credential")
    assert session.email == OWNER


@pytest.mark.parametrize(
    "claims_fn",
    [
        lambda n: _claims(n, email="someone-else@example.com"),
        lambda n: _claims(n, email_verified=False),
        lambda n: _claims(n, email_verified="true"),
        lambda n: {k: v for k, v in _claims(n).items() if k != "email"},
    ],
)
def test_google_login_rejects_unauthorized_accounts(claims_fn) -> None:
    service, _ = _service(claims_fn)
    with pytest.raises(AuthError) as err:
        service.login_with_google("credential")
    assert (err.value.status_code, err.value.code) == (403, "account_not_allowed")


@pytest.mark.parametrize(
    "claims_fn",
    [
        lambda n: _claims(n, iss="https://evil.example.com"),
        lambda n: _claims(n, aud="another-client"),
        lambda n: _claims("forged-nonce"),
        lambda n: {k: v for k, v in _claims(n).items() if k != "nonce"},
    ],
)
def test_google_login_rejects_untrusted_claims(claims_fn) -> None:
    service, _ = _service(claims_fn)
    with pytest.raises(AuthError) as err:
        service.login_with_google("credential")
    assert (err.value.status_code, err.value.code) == (401, "invalid_token")


def test_google_login_nonce_is_single_use() -> None:
    service, _ = _service()
    service.login_with_google("credential")
    with pytest.raises(AuthError) as err:
        service.login_with_google("credential")
    assert err.value.code == "invalid_token"


def test_google_login_rejects_expired_nonce() -> None:
    service, clock = _service()
    clock.advance(3600)
    with pytest.raises(AuthError) as err:
        service.login_with_google("credential")
    assert err.value.code == "invalid_token"


def test_google_login_rejects_invalid_token() -> None:
    service, _ = _service()
    with pytest.raises(AuthError) as err:
        service.login_with_google("invalid")
    assert (err.value.status_code, err.value.code) == (401, "invalid_token")


def test_google_login_reports_identity_provider_outage() -> None:
    service, _ = _service()
    with pytest.raises(AuthError) as err:
        service.login_with_google("idp-down")
    assert err.value.status_code == 503


def test_login_unavailable_when_not_configured() -> None:
    service, _ = _service(google_client_id=None)
    assert service.ready is False
    assert service.new_login_nonce() is None
    with pytest.raises(AuthError) as err:
        service.login_with_google("credential")
    assert (err.value.status_code, err.value.code) == (503, "auth_not_configured")


def test_unconfigured_service_rejects_every_session() -> None:
    issued, _ = SessionCodec(SECRET).issue(OWNER, "s", NOW, 600)
    service, _ = _service(google_client_id=None)
    with pytest.raises(AuthError) as err:
        service.authenticate(issued)
    assert err.value.status_code == 401


def test_session_for_previously_allowed_account_is_rejected() -> None:
    service, _ = _service()
    token, _ = service.login_with_google("credential")
    rotated = AuthService(_config(allowed_email="new-owner@example.com"), clock=service.clock)
    with pytest.raises(AuthError) as err:
        rotated.authenticate(token)
    assert err.value.code == "invalid_session"


def test_revoked_session_is_rejected() -> None:
    service, _ = _service()
    token, session = service.login_with_google("credential")
    service.revoke(session)
    with pytest.raises(AuthError) as err:
        service.authenticate(token)
    assert err.value.code == "session_revoked"


def test_missing_token_is_not_authenticated() -> None:
    service, _ = _service()
    with pytest.raises(AuthError) as err:
        service.authenticate(None)
    assert (err.value.status_code, err.value.code) == (401, "not_authenticated")


def test_check_csrf_raises_403() -> None:
    service, _ = _service()
    _, session = service.login_with_google("credential")
    service.check_csrf(session, service.csrf_token(session))
    with pytest.raises(AuthError) as err:
        service.check_csrf(session, "wrong")
    assert (err.value.status_code, err.value.code) == (403, "csrf_failed")


def test_local_login_requires_local_mode_and_loopback() -> None:
    local = AuthService(
        _config(mode="local", allowed_email=None, google_client_id=None, session_secret=None)
    )
    assert local.ready
    token, session = local.login_local("127.0.0.1")
    assert local.authenticate(token) == session
    with pytest.raises(AuthError) as err:
        local.login_local("203.0.113.9")
    assert err.value.status_code == 403

    google, _ = _service()
    with pytest.raises(AuthError) as err:
        google.login_local("127.0.0.1")
    assert (err.value.status_code, err.value.code) == (403, "mode_not_enabled")


def test_local_mode_is_unusable_in_production() -> None:
    service = AuthService(
        _config(app_env="production", mode="local", frontend_origin="https://x.example")
    )
    with pytest.raises(AuthError) as err:
        service.login_local("127.0.0.1")
    assert err.value.status_code == 503


def test_service_rate_limit_raises_429_with_retry_after() -> None:
    service, _ = _service()
    service.enforce_rate_limit("login", "1.2.3.4", 1, 60)
    with pytest.raises(AuthError) as err:
        service.enforce_rate_limit("login", "1.2.3.4", 1, 60)
    assert err.value.status_code == 429
    assert int(err.value.headers["Retry-After"]) >= 1


# ------------------------------------------------------------------ #
# Real Google ID token verification (signature, expiry, audience)      #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def google_signing_material() -> tuple[crypt.RSASigner, str, crypt.RSASigner]:
    def make() -> tuple[bytes, bytes]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-google")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
            .not_valid_after(dt.datetime(2100, 1, 1, tzinfo=dt.UTC))
            .sign(key, hashes.SHA256())
        )
        pem_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return pem_key, cert.public_bytes(serialization.Encoding.PEM)

    key_pem, cert_pem = make()
    other_key_pem, _ = make()
    signer = crypt.RSASigner.from_string(key_pem, key_id="kid-1")
    other_signer = crypt.RSASigner.from_string(other_key_pem, key_id="kid-1")
    return signer, cert_pem.decode(), other_signer


def _id_token(signer: crypt.RSASigner, **overrides: object) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    payload: dict = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-1",
        "email": OWNER,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return google_jwt.encode(signer, payload).decode()


def _verify_with_certs(cert_pem: str, token: str) -> dict:
    with patch("google.oauth2.id_token._fetch_certs", return_value={"kid-1": cert_pem}):
        return dict(verify_google_id_token(token, CLIENT_ID))


def test_real_google_token_verifies(google_signing_material) -> None:
    signer, cert_pem, _ = google_signing_material
    claims = _verify_with_certs(cert_pem, _id_token(signer, nonce="n-1"))
    assert claims["email"] == OWNER
    assert claims["nonce"] == "n-1"


def test_real_google_token_expired_is_rejected(google_signing_material) -> None:
    signer, cert_pem, _ = google_signing_material
    past = int(dt.datetime.now(dt.UTC).timestamp()) - 7200
    with pytest.raises(ValueError):
        _verify_with_certs(cert_pem, _id_token(signer, iat=past, exp=past + 600))


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "someone-elses-client"}, {"iss": "https://evil.example.com"}],
)
def test_real_google_token_wrong_audience_or_issuer(google_signing_material, overrides) -> None:
    signer, cert_pem, _ = google_signing_material
    with pytest.raises(ValueError):
        _verify_with_certs(cert_pem, _id_token(signer, **overrides))


def test_real_google_token_missing_issuer_is_rejected(google_signing_material) -> None:
    signer, cert_pem, _ = google_signing_material
    token = _id_token(signer)
    with patch("google.oauth2.id_token.verify_token", return_value={"aud": CLIENT_ID}):
        with pytest.raises(ValueError):
            _verify_with_certs(cert_pem, token)


def test_real_google_token_signed_by_unknown_key_is_rejected(google_signing_material) -> None:
    _, cert_pem, other_signer = google_signing_material
    with pytest.raises(ValueError):
        _verify_with_certs(cert_pem, _id_token(other_signer))


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "a.b"])
def test_real_google_token_malformed_is_rejected(google_signing_material, token) -> None:
    _, cert_pem, _ = google_signing_material
    with pytest.raises(ValueError):
        _verify_with_certs(cert_pem, token)


def test_cert_fetch_failure_is_reported_as_unavailable(google_signing_material) -> None:
    signer, _, _ = google_signing_material
    with patch(
        "google.oauth2.id_token._fetch_certs",
        side_effect=google_exceptions.TransportError("network down"),
    ):
        with pytest.raises(IdentityProviderUnavailable):
            verify_google_id_token(_id_token(signer), CLIENT_ID)
