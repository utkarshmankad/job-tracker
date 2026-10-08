"""Normalization and fingerprints shared by identity matching and the evidence store.

Pure functions with no database or network access. The company/URL rules are the ones the
Phase 1 DuplicateDetector used, moved here so the stored identity columns, the resolver and
the detector can never disagree.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FINGERPRINT_VERSION = "evidence-v1"
_SEPARATOR = "\x1f"  # ASCII unit separator: cannot occur in normalized text fields
_LEGAL_SUFFIXES = re.compile(r"\b(pvt|private|limited|ltd|inc|llc|technologies|technology)\b")
_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_EMAIL_IN_ANGLE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_BARE_EMAIL = re.compile(r"([^\s<>@,;\"']+@[^\s<>@,;\"']+)")


def normalize_company(value: str | None) -> str:
    """Lower-case alphanumerics with common legal suffixes removed ("" when empty)."""
    text = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return _LEGAL_SUFFIXES.sub("", text).strip()


def normalize_role(value: str | None) -> str:
    """Lower-case alphanumerics, single-spaced ("" when empty)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def canonical_job_url(value: str | None) -> str | None:
    """Scheme/host lower-cased, trailing slash and utm_* parameters and fragment dropped."""
    if not value or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().lower()
    kept = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(kept), "")
    )


def normalize_subject(value: str | None) -> str | None:
    """Reply/forward prefixes stripped, case-folded, whitespace collapsed (None when empty)."""
    if not value:
        return None
    text = " ".join(_REPLY_PREFIX.sub("", value).casefold().split())
    return text or None


def normalize_email_address(value: str | None) -> str:
    """The address part of 'Name <user@host>' or a bare address, lower-cased ("" if none)."""
    if not value:
        return ""
    match = _EMAIL_IN_ANGLE.search(value) or _BARE_EMAIL.search(value)
    return match.group(1).lower() if match else " ".join(value.lower().split())


def _utc_second(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def evidence_fingerprint(
    *,
    evidence_type: str,
    source: str,
    external_id: str | None,
    thread_id: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    occurred_at: datetime | None = None,
    snippet: str | None = None,
) -> str:
    """Deterministic SHA-256 identity of one piece of evidence.

    With an external ID the fingerprint depends only on (type, source, external ID), so
    re-importing the same item always collides no matter how its details were refined.
    Without one it is derived from stable normalized fields. Never uses hash(), whose value
    is randomized per process.
    """
    if external_id:
        parts = [FINGERPRINT_VERSION, evidence_type, source, "ext", external_id.strip()]
    else:
        parts = [
            FINGERPRINT_VERSION,
            evidence_type,
            source,
            "fields",
            (thread_id or "").strip(),
            normalize_email_address(sender),
            normalize_email_address(recipient),
            normalize_subject(subject) or "",
            _utc_second(occurred_at) if occurred_at else "",
            " ".join((snippet or "").split()),
        ]
    return hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
