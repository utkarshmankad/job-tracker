"""Collector credentials and setup codes.

A collector credential looks like ``jtc_<token_id>.<secret>``. The token ID is public (it
is the lookup key and appears in audit logs); the secret is 256 bits from ``secrets`` and
is stored server-side only as a SHA-256 digest, compared in constant time. The local agent
keeps the full credential in the operating-system keychain.

Setup codes are single-use, expire after ``COLLECTOR_ENROLLMENT_TTL_SECONDS`` and are also
stored only as digests. The web UI shows a setup code once; the CLI exchanges it for the
credential, so no long-lived secret is ever displayed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

TOKEN_PREFIX = "jtc_"
_TOKEN = re.compile(r"^jtc_([a-f0-9]{16})\.([A-Za-z0-9_-]{43})$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    secret: str

    @property
    def credential(self) -> str:
        return f"{TOKEN_PREFIX}{self.token_id}.{self.secret}"


def new_token_id() -> str:
    return secrets.token_hex(8)


def new_secret() -> str:
    return secrets.token_urlsafe(32)  # 43 URL-safe characters


def new_enrollment_code() -> str:
    return secrets.token_urlsafe(24)


def digest(value: str, purpose: str) -> str:
    """Domain-separated SHA-256 (the values are high-entropy, so no slow KDF is needed)."""
    return hashlib.sha256(f"job-tracker:{purpose}:v1:{value}".encode()).hexdigest()


def secret_hash(secret: str) -> str:
    return digest(secret, "collector-secret")


def code_hash(code: str) -> str:
    return digest(code, "collector-enrollment")


def parse_credential(value: str | None) -> tuple[str, str] | None:
    """(token_id, secret) from ``jtc_<id>.<secret>``, or None if malformed."""
    if not value:
        return None
    match = _TOKEN.match(value.strip())
    return (match.group(1), match.group(2)) if match else None


def valid_code_format(code: str) -> bool:
    return bool(_CODE.match(code))


def secret_matches(secret: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(secret_hash(secret), stored_hash)
