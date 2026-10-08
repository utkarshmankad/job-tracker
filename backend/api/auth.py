"""Single-user authentication: Google identity verification, signed session cookies,
CSRF protection and rate limiting.

Every protected route depends on `require_user`; the router in backend/api/routes.py is
mounted with it as a router-level dependency in backend/main.py, so authorization lives in
one place. Only /api/v1/health and the /api/v1/auth/* endpoints are reachable without a
session.

Design (see docs/authentication.md):
- The browser obtains a Google ID token from Google Identity Services and posts it to
  /auth/google. The backend verifies signature, issuer, audience, expiry and a single-use
  login nonce, then requires a verified email equal to AUTH_ALLOWED_EMAIL.
- On success the backend issues its own short-lived session as an HMAC-signed, HttpOnly,
  SameSite=Lax cookie. No reusable secret ever reaches the frontend bundle.
- State-changing requests must also carry X-CSRF-Token, an HMAC of the session id that the
  frontend receives from /auth/session and keeps in memory only.

Nothing here logs tokens, cookies, authorization headers, or email addresses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Request

from backend import config as app_config

log = structlog.get_logger(__name__)

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_TOKEN_VERSION = "v1"
_LOCAL_DEFAULT_EMAIL = "developer@jobtracker.localhost"
_LOCAL_DEFAULT_SUBJECT = "local-developer"


class AuthError(Exception):
    """Raised for every authentication/authorization failure; rendered by main.py as
    {"detail": message, "code": code} with the given status."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


def not_authenticated(message: str = "Authentication required.") -> AuthError:
    return AuthError(401, "not_authenticated", message)


class AuthConfigError(RuntimeError):
    """Authentication is misconfigured in a way that must stop the server from starting."""


# ------------------------------------------------------------------ #
# Configuration                                                        #
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class AuthConfig:
    app_env: str
    mode: str
    allowed_email: str | None
    google_client_id: str | None
    session_secret: str | None
    session_ttl_seconds: int
    allowed_origins: tuple[str, ...]
    frontend_origin: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix makes browsers enforce Secure, Path=/ and no Domain attribute.
        return "__Host-jt_session" if self.cookie_secure else "jt_session"

    def problems(self) -> list[str]:
        """Names of missing/invalid settings. Never includes the values themselves."""
        found: list[str] = []
        if self.mode not in ("google", "local"):
            found.append("AUTH_MODE must be 'google' or 'local'")
        if self.mode == "local" and self.is_production:
            found.append("AUTH_MODE=local is not allowed when APP_ENV=production")
        if self.mode == "google":
            if not self.allowed_email:
                found.append("AUTH_ALLOWED_EMAIL is not set")
            if not self.google_client_id:
                found.append("AUTH_GOOGLE_CLIENT_ID is not set")
        if self.mode == "google" or self.is_production:
            if not self.session_secret:
                found.append("AUTH_SESSION_SECRET is not set")
            elif len(self.session_secret) < app_config.AUTH_SESSION_SECRET_MIN_LENGTH:
                found.append(
                    "AUTH_SESSION_SECRET must be at least "
                    f"{app_config.AUTH_SESSION_SECRET_MIN_LENGTH} characters"
                )
        if not (
            app_config.AUTH_SESSION_TTL_MIN_SECONDS
            <= self.session_ttl_seconds
            <= app_config.AUTH_SESSION_TTL_MAX_SECONDS
        ):
            found.append(
                "AUTH_SESSION_TTL_SECONDS must be between "
                f"{app_config.AUTH_SESSION_TTL_MIN_SECONDS} and "
                f"{app_config.AUTH_SESSION_TTL_MAX_SECONDS}"
            )
        if self.is_production and not (
            self.frontend_origin and self.frontend_origin.startswith("https://")
        ):
            found.append("FRONTEND_ORIGIN must be set to the https:// frontend origin")
        return found


def load_auth_config(allowed_origins: Sequence[str]) -> AuthConfig:
    """Build the auth config from backend.config (read at call time so tests can patch it)."""
    return AuthConfig(
        app_env=app_config.APP_ENV,
        mode=app_config.AUTH_MODE,
        allowed_email=app_config.AUTH_ALLOWED_EMAIL,
        google_client_id=app_config.AUTH_GOOGLE_CLIENT_ID,
        session_secret=app_config.AUTH_SESSION_SECRET,
        session_ttl_seconds=app_config.AUTH_SESSION_TTL_SECONDS,
        allowed_origins=tuple(allowed_origins),
        frontend_origin=app_config.FRONTEND_ORIGIN,
    )


def validate_auth_config(cfg: AuthConfig) -> list[str]:
    """Fail closed in production; elsewhere return the problems so sign-in reports
    'not configured' while every protected route still rejects requests."""
    problems = cfg.problems()
    if problems and cfg.is_production:
        raise AuthConfigError(
            "Authentication is not configured for production: " + "; ".join(problems)
        )
    if problems:
        log.warning("auth_not_configured", problems=problems, app_env=cfg.app_env)
    return problems


# ------------------------------------------------------------------ #
# Signed tokens                                                        #
# ------------------------------------------------------------------ #


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _derive_key(secret: str, purpose: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


@dataclass(frozen=True)
class Session:
    sid: str
    email: str
    subject: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True)
class AuthenticatedUser:
    email: str
    session_id: str
    expires_at: int


class SessionCodec:
    """HMAC-SHA256 signed session tokens: v1.<base64url(json)>.<base64url(signature)>."""

    def __init__(self, secret: str) -> None:
        self._session_key = _derive_key(secret, "job-tracker/session/v1")
        self._csrf_key = _derive_key(secret, "job-tracker/csrf/v1")
        self._nonce_key = _derive_key(secret, "job-tracker/login-nonce/v1")

    def issue(self, email: str, subject: str, now: int, ttl_seconds: int) -> tuple[str, Session]:
        session = Session(
            sid=secrets.token_urlsafe(18),
            email=email,
            subject=subject,
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
        payload = {
            "sid": session.sid,
            "email": session.email,
            "sub": session.subject,
            "iat": session.issued_at,
            "exp": session.expires_at,
        }
        body = f"{_TOKEN_VERSION}.{_b64encode(json.dumps(payload, separators=(',', ':')).encode())}"
        return f"{body}.{self._sign(self._session_key, body)}", session

    def decode(self, token: str, now: int) -> Session:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
            raise AuthError(401, "invalid_session", "Session is invalid. Sign in again.")
        body = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2], self._sign(self._session_key, body)):
            raise AuthError(401, "invalid_session", "Session is invalid. Sign in again.")
        try:
            payload = json.loads(_b64decode(parts[1]))
            session = Session(
                sid=_require_str(payload, "sid"),
                email=_require_str(payload, "email"),
                subject=_require_str(payload, "sub"),
                issued_at=_require_int(payload, "iat"),
                expires_at=_require_int(payload, "exp"),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise AuthError(401, "invalid_session", "Session is invalid. Sign in again.") from exc
        if session.expires_at <= now:
            raise AuthError(401, "session_expired", "Your session has expired. Sign in again.")
        if session.issued_at > now + app_config.GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS:
            raise AuthError(401, "invalid_session", "Session is invalid. Sign in again.")
        return session

    def csrf_token(self, session_id: str) -> str:
        return self._sign(self._csrf_key, session_id)

    def verify_csrf(self, session_id: str, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(str(presented), self.csrf_token(session_id))

    def issue_nonce(self, now: int) -> str:
        body = f"{now}.{secrets.token_urlsafe(18)}"
        return f"{body}.{self._sign(self._nonce_key, body)}"

    def nonce_is_authentic(self, nonce: str, now: int, ttl_seconds: int) -> bool:
        parts = nonce.split(".")
        if len(parts) != 3 or not parts[0].isdigit():
            return False
        body = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2], self._sign(self._nonce_key, body)):
            return False
        issued = int(parts[0])
        return issued <= now + app_config.GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS and (
            now - issued <= ttl_seconds
        )

    @staticmethod
    def _sign(key: bytes, body: str) -> str:
        return _b64encode(hmac.new(key, body.encode("utf-8"), hashlib.sha256).digest())


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(key)
    return value


# ------------------------------------------------------------------ #
# Rate limiting                                                        #
# ------------------------------------------------------------------ #


class RateLimiter:
    """Sliding-window limiter kept in process memory. The backend runs as a single Fly
    machine, so in-memory state is authoritative; it resets on restart."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        """Record one request. Returns seconds to wait if the limit is exceeded, else None."""
        now = self._clock()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] >= window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, int(window_seconds - (now - bucket[0])) + 1)
            bucket.append(now)
            return None


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ #
# Google identity                                                      #
# ------------------------------------------------------------------ #

GoogleVerifier = Callable[[str, str], Mapping[str, Any]]


class IdentityProviderUnavailable(Exception):
    """Google's signing certificates could not be fetched."""


def verify_google_id_token(credential: str, client_id: str) -> Mapping[str, Any]:
    """Verify signature (Google certs), issuer, audience and expiry of a Google ID token.

    Raises ValueError for any invalid token and IdentityProviderUnavailable when Google's
    certificates cannot be fetched.
    """
    from google.auth import exceptions as google_exceptions
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            audience=client_id,
            clock_skew_in_seconds=app_config.GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS,
        )
    except google_exceptions.TransportError as exc:
        raise IdentityProviderUnavailable() from exc
    except (google_exceptions.GoogleAuthError, KeyError, TypeError) as exc:
        # GoogleAuthError: wrong issuer/malformed; KeyError/TypeError: missing claims.
        raise ValueError("invalid Google ID token") from exc
    return dict(claims)


# ------------------------------------------------------------------ #
# Service                                                              #
# ------------------------------------------------------------------ #


@dataclass
class AuthService:
    config: AuthConfig
    verifier: GoogleVerifier = verify_google_id_token
    clock: Callable[[], float] = time.time
    limiter: RateLimiter = field(default_factory=RateLimiter)
    problems: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.problems = self.config.problems()
        secret = self.config.session_secret
        if not secret and self.config.mode == "local" and not self.config.is_production:
            # Local developer mode without a configured secret: sessions are signed with a
            # per-process key and end when the server restarts.
            secret = secrets.token_urlsafe(48)
        self._codec = SessionCodec(secret) if secret else None
        self._revoked: dict[str, int] = {}
        self._used_nonces: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._codec is not None and not self.problems

    def now(self) -> int:
        return int(self.clock())

    def _require_codec(self) -> SessionCodec:
        if self._codec is None or self.problems:
            raise AuthError(503, "auth_not_configured", "Sign-in is not configured on the server.")
        return self._codec

    # -- login ---------------------------------------------------------

    def new_login_nonce(self) -> str | None:
        if not self.ready or self.config.mode != "google":
            return None
        return self._require_codec().issue_nonce(self.now())

    def login_with_google(self, credential: str) -> tuple[str, Session]:
        codec = self._require_codec()
        if self.config.mode != "google":
            raise AuthError(403, "mode_not_enabled", "Google sign-in is not enabled.")
        assert self.config.google_client_id is not None
        try:
            claims = self.verifier(credential, self.config.google_client_id)
        except IdentityProviderUnavailable as exc:
            log.warning("auth_identity_provider_unavailable")
            raise AuthError(
                503, "identity_provider_unavailable", "Google sign-in is temporarily unavailable."
            ) from exc
        except ValueError as exc:
            log.info("auth_login_rejected", reason="invalid_google_token")
            raise AuthError(401, "invalid_token", "Google sign-in could not be verified.") from exc

        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            log.info("auth_login_rejected", reason="wrong_issuer")
            raise AuthError(401, "invalid_token", "Google sign-in could not be verified.")
        if claims.get("aud") != self.config.google_client_id:
            log.info("auth_login_rejected", reason="wrong_audience")
            raise AuthError(401, "invalid_token", "Google sign-in could not be verified.")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not self._consume_nonce(codec, nonce):
            log.info("auth_login_rejected", reason="invalid_nonce")
            raise AuthError(401, "invalid_token", "Sign-in expired. Try again.")

        email = claims.get("email")
        subject = claims.get("sub")
        if (
            not isinstance(email, str)
            or not isinstance(subject, str)
            or claims.get("email_verified") is not True
            or email.strip().lower() != self.config.allowed_email
        ):
            log.info("auth_login_rejected", reason="account_not_allowed")
            raise AuthError(
                403, "account_not_allowed", "This Google account is not allowed to use Job Tracker."
            )

        log.info("auth_login_succeeded", method="google")
        return codec.issue(
            email.strip().lower(), subject, self.now(), self.config.session_ttl_seconds
        )

    def login_local(self, client_host: str) -> tuple[str, Session]:
        codec = self._require_codec()
        if self.config.mode != "local" or self.config.is_production:
            raise AuthError(403, "mode_not_enabled", "Local developer sign-in is not enabled.")
        if client_host not in LOOPBACK_HOSTS:
            raise AuthError(
                403, "mode_not_enabled", "Local developer sign-in only works from this machine."
            )
        log.info("auth_login_succeeded", method="local")
        return codec.issue(
            self.config.allowed_email or _LOCAL_DEFAULT_EMAIL,
            _LOCAL_DEFAULT_SUBJECT,
            self.now(),
            self.config.session_ttl_seconds,
        )

    def _consume_nonce(self, codec: SessionCodec, nonce: str) -> bool:
        now = self.now()
        ttl = app_config.AUTH_LOGIN_NONCE_TTL_SECONDS
        if not codec.nonce_is_authentic(nonce, now, ttl):
            return False
        with self._lock:
            self._used_nonces = {n: exp for n, exp in self._used_nonces.items() if exp > now}
            if nonce in self._used_nonces:
                return False
            self._used_nonces[nonce] = now + ttl + app_config.GOOGLE_ID_TOKEN_CLOCK_SKEW_SECONDS
        return True

    # -- sessions ------------------------------------------------------

    def authenticate(self, token: str | None) -> Session:
        if not token:
            raise not_authenticated()
        if self._codec is None or self.problems:
            raise not_authenticated()
        session = self._codec.decode(token, self.now())
        expected = self.config.allowed_email or (
            _LOCAL_DEFAULT_EMAIL if self.config.mode == "local" else None
        )
        if session.email != expected:
            # Allowed account changed since this session was issued.
            raise AuthError(401, "invalid_session", "Session is invalid. Sign in again.")
        with self._lock:
            if session.sid in self._revoked:
                raise AuthError(401, "session_revoked", "You have been signed out.")
        return session

    def csrf_token(self, session: Session) -> str:
        return self._require_codec().csrf_token(session.sid)

    def check_csrf(self, session: Session, presented: str | None) -> None:
        if not self._require_codec().verify_csrf(session.sid, presented):
            raise AuthError(403, "csrf_failed", "Request could not be verified. Reload the page.")

    def revoke(self, session: Session) -> None:
        now = self.now()
        with self._lock:
            self._revoked = {sid: exp for sid, exp in self._revoked.items() if exp > now}
            self._revoked[session.sid] = session.expires_at

    # -- rate limiting -------------------------------------------------

    def enforce_rate_limit(self, scope: str, client: str, limit: int, window: int) -> None:
        retry_after = self.limiter.hit(f"{scope}:{client}", limit, window)
        if retry_after is not None:
            log.warning("rate_limited", scope=scope)
            raise AuthError(
                429,
                "rate_limited",
                "Too many requests. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )


# ------------------------------------------------------------------ #
# FastAPI dependencies                                                 #
# ------------------------------------------------------------------ #


def get_auth_service(request: Request) -> AuthService:
    service: AuthService | None = getattr(request.app.state, "auth", None)
    if service is None:
        # Startup did not configure auth (e.g. lifespan not run): fail closed.
        raise not_authenticated()
    return service


def require_user(request: Request) -> AuthenticatedUser:
    """Router-level dependency guarding every protected endpoint.

    Rejects requests without a valid, unexpired, unrevoked session cookie (401) and
    state-changing requests without a matching X-CSRF-Token header (403).
    """
    service = get_auth_service(request)
    session = service.authenticate(request.cookies.get(service.config.cookie_name))
    if request.method.upper() not in SAFE_METHODS:
        service.check_csrf(session, request.headers.get(CSRF_HEADER))
    return AuthenticatedUser(
        email=session.email, session_id=session.sid, expires_at=session.expires_at
    )


def sensitive_rate_limit(request: Request) -> None:
    """Per-client limit shared by destructive and expensive operations."""
    service = get_auth_service(request)
    service.enforce_rate_limit(
        "sensitive",
        client_address(request),
        app_config.SENSITIVE_RATE_LIMIT_REQUESTS,
        app_config.SENSITIVE_RATE_LIMIT_WINDOW_SECONDS,
    )


def login_rate_limit(request: Request) -> None:
    service = get_auth_service(request)
    service.enforce_rate_limit(
        "login",
        client_address(request),
        app_config.AUTH_RATE_LIMIT_ATTEMPTS,
        app_config.AUTH_RATE_LIMIT_WINDOW_SECONDS,
    )


def require_allowed_origin(request: Request) -> None:
    """Reject sign-in requests sent by pages on other origins (login CSRF)."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    service = get_auth_service(request)
    if origin not in service.config.allowed_origins:
        log.info("auth_origin_rejected")
        raise AuthError(403, "origin_not_allowed", "Request origin is not allowed.")
