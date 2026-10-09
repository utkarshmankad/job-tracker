"""Versioned, source-neutral observation contract shared by the local collector and the
ingestion API.

An observation is the minimum the tracker needs to recognise one application on one job
site: company, role, applied date, the site's status, the site's own item ID (if any) and a
canonical job link. Nothing else from the page is accepted — no page text, messages,
recruiter names, email addresses, cookies or account identifiers. Pydantic models forbid
extra fields, so a payload carrying anything more is rejected rather than silently stored.

Identity (docs/phase-3-source-collection.md §3):
- with a stable site ID: ``item_key = "id:" + source_item_id``;
- without one: ``item_key = "fp:" + fingerprint[:40]``, where the fingerprint is the
  SHA-256 of ``collector-fp-v1``, the source key, the normalized company and role, the
  applied date and the canonical URL, joined by U+001F. Status is deliberately excluded,
  so a status change is a new observation of the same item, never a new item.
- ``content_hash`` covers everything that can change about an item (status, labels, URL…)
  but not when or by which software version it was observed, so re-observing an unchanged
  item is recognised as unchanged.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend import config as app_config
from backend.db.models import ApplicationStatus
from backend.engine.normalization import (
    canonical_job_url,
    external_job_id_from_url,
    normalize_company,
    normalize_role,
)

CONTRACT_VERSION = app_config.COLLECTOR_CONTRACT_VERSION
FINGERPRINT_VERSION = "collector-fp-v1"
_SEP = "\x1f"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EMPLOYER = re.compile(app_config.COLLECTOR_EMPLOYER_SOURCE_PATTERN)
# Query parameters that identify a job on known portals; everything else (tracking, session
# and referral parameters) is dropped from stored URLs.
_KEPT_QUERY_PARAMS = frozenset({"gh_jid", "jk", "jobid", "job_id", "currentjobid"})

SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:~-]{1,128}$")]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9._/+-]{1,40}$")]
Code = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")]


class CollectorStatus(enum.StrEnum):
    """Source-neutral status. Each adapter maps its site's labels onto these."""

    APPLIED = "applied"
    VIEWED = "viewed"  # the employer opened the application
    IN_REVIEW = "in_review"
    SHORTLISTED = "shortlisted"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"  # the posting closed; says nothing about this application
    UNKNOWN = "unknown"


# Only statuses that are evidence of a specific application stage advance the tracker.
_STATUS_SIGNAL: dict[CollectorStatus, ApplicationStatus] = {
    CollectorStatus.SHORTLISTED: ApplicationStatus.RESUME_SHORTLISTED,
    CollectorStatus.INTERVIEW: ApplicationStatus.INTERVIEW_SCHEDULED,
    CollectorStatus.OFFER: ApplicationStatus.OFFER,
    CollectorStatus.REJECTED: ApplicationStatus.REJECTED,
    CollectorStatus.WITHDRAWN: ApplicationStatus.WITHDRAWN,
}

# How each source appears in the tracker (Application.source_portal / application_method).
SOURCE_PORTAL = {
    "linkedin": ("LinkedIn", "Easy Apply"),
    "naukri": ("Naukri", "Unknown"),
    "indeed": ("Indeed", "Unknown"),
    "instahyre": ("Instahyre", "Unknown"),
    "careernet": ("CareerNet", "Unknown"),
}


def status_signal(status: CollectorStatus) -> ApplicationStatus | None:
    return _STATUS_SIGNAL.get(status)


def is_valid_source_key(value: str) -> bool:
    return value in app_config.COLLECTOR_SOURCES or bool(_EMPLOYER.match(value))


def portal_for(source_key: str) -> tuple[str, str]:
    """(source_portal, application_method) for a new application from this source."""
    if source_key in SOURCE_PORTAL:
        return SOURCE_PORTAL[source_key]
    return ("Company Site", "Company Site")


def evidence_source(source_key: str) -> str:
    """The Evidence.source channel for a collector source."""
    return source_key if source_key in SOURCE_PORTAL else "company_portal"


def redact_text(value: str) -> str:
    """Remove control characters, email addresses and phone numbers; collapse whitespace."""
    text = _CONTROL.sub(" ", value)
    text = _EMAIL.sub("[redacted]", text)
    text = _PHONE.sub("[redacted]", text)
    return " ".join(text.split())


def safe_job_url(value: str | None) -> str | None:
    """Canonical https job URL without credentials, fragments, tracking or personal query
    parameters. Returns None for anything that is not an https URL."""
    if not value:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() in _KEPT_QUERY_PARAMS)
    )
    cleaned = urlunsplit(("https", parts.hostname.lower(), parts.path, query, ""))
    return canonical_job_url(cleaned) or None


def _clean(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = redact_text(value)[:limit].strip()
    return text or None


class ObservedApplication(BaseModel):
    """One application as one job site shows it (contract version 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_key: str = Field(min_length=2, max_length=48)
    source_item_id: SafeId | None = None
    company: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=300)
    applied_on: date | None = None
    status: CollectorStatus
    raw_status: str | None = Field(default=None, max_length=80)
    job_url: str | None = Field(default=None, max_length=2048)
    proves_submission: bool
    extraction: Literal["verified", "fallback", "heuristic"]
    observed_at: datetime
    adapter_version: Version

    @field_validator("source_key")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if not is_valid_source_key(value):
            raise ValueError("unknown source key")
        return value

    @field_validator("company")
    @classmethod
    def _company(cls, value: str) -> str:
        cleaned = _clean(value, 200)
        if not cleaned or cleaned == "[redacted]":
            raise ValueError("company is required")
        return cleaned

    @field_validator("role", "raw_status")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        return _clean(value, 300)

    @field_validator("job_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = safe_job_url(value)
        if cleaned is None:
            raise ValueError("job_url must be an https URL")
        return cleaned

    @field_validator("observed_at")
    @classmethod
    def _observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        value = value.astimezone(UTC)
        now = datetime.now(UTC)
        if value > now + timedelta(minutes=5):
            raise ValueError("observed_at is in the future")
        if value < now - timedelta(days=app_config.COLLECTOR_MAX_OBSERVATION_AGE_DAYS):
            raise ValueError("observed_at is too old")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ObservedApplication:
        if self.applied_on and self.applied_on > self.observed_at.date():
            raise ValueError("applied_on is after observed_at")
        if self.proves_submission and self.status is CollectorStatus.UNKNOWN:
            raise ValueError("an unknown status cannot prove submission")
        return self

    # -- identity ------------------------------------------------------------------

    def fingerprint(self) -> str:
        parts = [
            FINGERPRINT_VERSION,
            self.source_key,
            normalize_company(self.company),
            normalize_role(self.role),
            self.applied_on.isoformat() if self.applied_on else "",
            self.job_url or "",
        ]
        return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()

    def item_identity(self) -> tuple[str, str]:
        """(item_key, id_kind) — the site's own ID when it has one."""
        if self.source_item_id:
            return f"id:{self.source_item_id}", "source_id"
        return f"fp:{self.fingerprint()[:40]}", "fingerprint"

    def content(self) -> dict[str, Any]:
        """The normalized, minimal payload stored with the observation."""
        return {
            "company": self.company,
            "role": self.role,
            "applied_on": self.applied_on.isoformat() if self.applied_on else None,
            "status": self.status.value,
            "raw_status": self.raw_status,
            "job_url": self.job_url,
            "proves_submission": self.proves_submission,
        }

    def content_hash(self) -> str:
        canonical = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def external_job(self) -> tuple[str, str] | None:
        """(portal, job ID) derived from the job URL, if the URL carries one."""
        return external_job_id_from_url(self.job_url)


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    sent_at: datetime
    observations: list[ObservedApplication] = Field(
        min_length=1, max_length=app_config.COLLECTOR_MAX_BATCH_OBSERVATIONS
    )


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    source_key: str
    account_label: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    collector_version: Version
    adapter_version: Version

    @field_validator("source_key")
    @classmethod
    def _known_source(cls, value: str) -> str:
        if not is_valid_source_key(value):
            raise ValueError("unknown source key")
        return value


DiagnosticValue = int | bool | Code


class RunFinish(BaseModel):
    """How a run ended. ``diagnostics`` holds aggregate counters and fixed codes only — the
    server never accepts free text from the collector, so page content cannot leak in."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "partial", "failed", "signed_out", "challenged", "unsupported"]
    items_seen: int = Field(ge=0, le=100_000)
    error_code: Code | None = None
    diagnostics: dict[Code, DiagnosticValue] = Field(default_factory=dict, max_length=30)


# Fixed, safe descriptions shown in the UI for each error code.
ERROR_MESSAGES = {
    "signed_out": "The browser is not signed in to this site. Sign in there, then run again.",
    "challenge": "The site asked for a CAPTCHA or verification. Complete it in the browser.",
    "rate_limited": "The site is limiting requests. Wait before collecting again.",
    "consent_required": "The site is showing a consent or terms page. Resolve it in the browser.",
    "selector_drift": "The page layout changed; this adapter needs maintenance.",
    "unexpected_page": "The collector reached a page it does not recognise and stopped.",
    "unsupported": "This site or page is not supported by any adapter.",
    "navigation_failed": "The history page could not be opened.",
    "submission_failed": "Observations could not be sent to the tracker.",
    "interrupted": "The run was interrupted before it finished.",
}


def error_message(code: str | None) -> str | None:
    if code is None:
        return None
    return ERROR_MESSAGES.get(code, "The collection run stopped with an error.")
