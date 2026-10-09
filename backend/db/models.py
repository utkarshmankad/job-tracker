"""SQLModel table definitions — source of truth for schema."""

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, Float, Index, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import DateTime, TypeDecorator
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    """Timezone-aware 'now' — the only clock repo code should call (never bare utcnow())."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores datetimes as naive UTC in SQLite (its only native format) while keeping every
    Python-level value timezone-aware. Naive values passed in are assumed to already be UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class ApplicationStatus(str, enum.Enum):
    APPLIED = "Applied"
    RESUME_SHORTLISTED = "Resume Shortlisted"
    INTERVIEW_SCHEDULED = "Interview Scheduled"
    INTERVIEW_IN_PROGRESS = "Interview In Progress"
    OFFER_NEGOTIATION = "Offer Negotiation"
    OFFER = "Offer"
    JOINED = "Joined"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"


class Application(SQLModel, table=True):
    __table_args__ = (Index("ix_application_identity", "normalized_company", "normalized_role"),)

    id: int | None = Field(default=None, primary_key=True)
    company: str | None = None
    role: str | None = None
    source_portal: str = Field(index=True)
    application_method: str = Field(
        default="Unknown",
        sa_column=Column(
            "application_method",
            String,
            default="Unknown",
            server_default="Unknown",
            index=True,
            nullable=False,
        ),
    )
    job_url: str | None = None
    applied_date: datetime = Field(index=True, sa_type=UTCDateTime)
    current_status: ApplicationStatus = Field(
        default=ApplicationStatus.APPLIED,
        sa_column=Column(
            "current_status",
            SAEnum(ApplicationStatus, values_callable=lambda obj: [e.value for e in obj]),
            default=ApplicationStatus.APPLIED.value,
            index=True,
            nullable=False,
        ),
    )
    thread_ids: str = "[]"  # JSON-encoded list[str]
    is_false_positive: bool = False
    withdraw_reason: str | None = None  # "self_withdraw" | "company_closed"
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utc_now, index=True, sa_type=UTCDateTime)
    # Phase 2 identity columns (all nullable, additive). The first three are derived from
    # company/role/job_url by DataStore on every save; last_evidence_at from linked evidence.
    normalized_company: str | None = Field(default=None, index=True)
    normalized_role: str | None = None
    canonical_job_url: str | None = Field(default=None, index=True)
    external_job_id: str | None = Field(default=None, index=True)
    last_evidence_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    status_history: list["StatusHistory"] = Relationship(back_populates="application")


class StatusHistory(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    from_status: str | None = None
    to_status: str
    trigger: str  # "email" | "manual"
    changed_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    message_id: str | None = None
    application: Application | None = Relationship(back_populates="status_history")


class ApplicationEventType(str, enum.Enum):
    APPLICATION_SUBMITTED = "Application Submitted"
    RECRUITER_RESPONSE = "Recruiter Response"
    INTERVIEW_SCHEDULED = "Interview Scheduled"
    INTERVIEW_ATTENDED = "Interview Attended"
    INTERVIEW_RESCHEDULED = "Interview Rescheduled"
    INTERVIEW_CANCELLED = "Interview Cancelled"
    OFFER_RECEIVED = "Offer Received"
    REJECTED = "Rejected"


class InterviewRound(str, enum.Enum):
    RECRUITER_SCREEN = "Recruiter Screen"
    HIRING_MANAGER = "Hiring Manager"
    TECHNICAL = "Technical / System Design"
    LEADERSHIP = "Leadership / Behavioural"
    EXECUTIVE_FINAL = "Executive / Final"
    OTHER = "Other"


class ApplicationEvent(SQLModel, table=True):
    """A distinct milestone; interview rounds are events, not applications."""

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    event_type: ApplicationEventType = Field(
        sa_column=Column(
            "event_type",
            SAEnum(ApplicationEventType, values_callable=lambda obj: [e.value for e in obj]),
            index=True,
            nullable=False,
        )
    )
    occurred_at: datetime = Field(index=True, sa_type=UTCDateTime)
    interview_round: InterviewRound | None = Field(
        default=None,
        sa_column=Column(
            "interview_round",
            SAEnum(InterviewRound, values_callable=lambda obj: [e.value for e in obj]),
            nullable=True,
        ),
    )
    source: str = "email"  # email | manual | backfill | calendar
    source_message_id: str | None = Field(default=None, index=True)
    status_history_id: int | None = Field(default=None, index=True, unique=True)
    notes: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)


class ApplicationThreadId(SQLModel, table=True):
    """Indexed lookup table for Application.thread_ids (still the JSON source of truth on
    Application itself). Kept in sync by DataStore.upsert_application — replaces an
    unindexed LIKE scan over the JSON blob with an indexed equality lookup."""

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    thread_id: str = Field(index=True)


class SuppressRule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sender_pattern: str
    subject_pattern: str | None = None
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)


class PollerState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    last_history_id: str | None = None
    last_sync_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    status: str = "SLEEPING"
    error_message: str | None = None


class ProcessedMessage(SQLModel, table=True):
    message_id: str = Field(primary_key=True)
    processed_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    result: str  # "applied" | "status_update" | "suppressed" | "ignored"


class ProspectStatus(str, enum.Enum):
    NEW = "New"
    REVIEWED = "Reviewed"
    DISMISSED = "Dismissed"
    CONVERTED = "Converted"


class Prospect(SQLModel, table=True):
    """A job lead or important recruiting interaction that is not yet an application."""

    id: int | None = Field(default=None, primary_key=True)
    source_portal: str = Field(default="LinkedIn", index=True)
    category: str = Field(index=True)  # meeting | recruiter_outreach | profile_interest
    title: str
    sender: str
    snippet: str | None = None
    received_at: datetime = Field(index=True, sa_type=UTCDateTime)
    gmail_message_id: str = Field(index=True, unique=True)
    gmail_thread_id: str = Field(index=True)
    application_id: int | None = Field(default=None, foreign_key="application.id", index=True)
    status: ProspectStatus = Field(
        default=ProspectStatus.NEW,
        sa_column=Column(
            "status",
            SAEnum(ProspectStatus, values_callable=lambda obj: [e.value for e in obj]),
            default=ProspectStatus.NEW.value,
            index=True,
            nullable=False,
        ),
    )
    classification_reason: str
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utc_now, index=True, sa_type=UTCDateTime)


# ------------------------------------------------------------------ #
# Evidence (Phase 2) — see docs/phase-2-identity-resolution.md         #
# ------------------------------------------------------------------ #
# Enumerated columns are stored as plain VARCHAR and validated in Python: a value added by a
# later release must not make older code raise LookupError when it reads the row.


class EvidenceType(enum.StrEnum):
    EMAIL = "email"
    PORTAL_IMPORT = "portal_import"
    BROWSER_OBSERVATION = "browser_observation"
    MANUAL = "manual"


class EvidenceSource(enum.StrEnum):
    GMAIL = "gmail"
    LINKEDIN = "linkedin"
    NAUKRI = "naukri"
    INDEED = "indeed"
    INSTAHYRE = "instahyre"
    CAREERNET = "careernet"
    COMPANY_PORTAL = "company_portal"
    OTHER = "other"


class EvidenceStatus(enum.StrEnum):
    PENDING = "pending"  # recorded, not yet classified
    PROCESSING = "processing"  # claimed by one worker (guards concurrent resolution)
    LINKED = "linked"  # attached to an existing application
    CREATED_APPLICATION = "created_application"  # an acknowledgement that created one
    NEEDS_REVIEW = "needs_review"  # job-related but identity unclear
    INFORMATIONAL = "informational"  # relevant but not about an application (prospects)
    IGNORED = "ignored"  # not job mail / suppressed; stored minimally
    DISMISSED = "dismissed"  # a person marked it irrelevant
    DEFERRED = "deferred"  # a person postponed the review decision
    ERROR = "error"


class DecisionSource(enum.StrEnum):
    RESOLVER = "resolver"
    HUMAN = "human"  # always takes precedence over later automated decisions


class LinkMethod(enum.StrEnum):
    THREAD = "thread"
    JOB_URL = "job_url"
    EXTERNAL_JOB_ID = "external_job_id"
    COMPANY_ROLE = "company_role"
    COMPANY_ONLY = "company_only"
    CREATED = "created"
    MANUAL = "manual"
    BACKFILL = "backfill"


class Evidence(SQLModel, table=True):
    """One observation received by the tracker (an email, a portal record, …).

    Never holds a message body: only metadata, Gmail's preview snippet, and structured
    classification results in raw_metadata.
    """

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_evidence_source_external_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    evidence_type: str = Field(index=True)
    source: str = Field(index=True)
    external_id: str | None = None
    thread_id: str | None = Field(default=None, index=True)
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    normalized_subject: str | None = None
    snippet: str | None = None
    occurred_at: datetime = Field(index=True, sa_type=UTCDateTime)
    captured_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    raw_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("raw_metadata", JSON, nullable=False, server_default="{}"),
    )
    content_fingerprint: str = Field(index=True, unique=True)
    processing_status: str = Field(default=EvidenceStatus.PENDING.value, index=True)
    review_reason: str | None = None
    application_id: int | None = Field(default=None, foreign_key="application.id", index=True)
    link_method: str | None = None
    link_confidence: float | None = Field(default=None, sa_type=Float)
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    # Revision 0003 — identity signals derived from the fields above (runtime-maintained)
    sender_address: str | None = Field(default=None, index=True)
    sender_domain: str | None = Field(default=None, index=True)
    canonical_job_url: str | None = Field(default=None, index=True)
    external_job_id: str | None = Field(default=None, index=True)
    # Revision 0003 — resolver audit trail and decision ownership
    resolver_version: str | None = None
    resolver_decision: str | None = Field(default=None, index=True)
    resolver_confidence: float | None = Field(default=None, sa_type=Float)
    resolver_result: dict[str, Any] | None = Field(
        default=None, sa_column=Column("resolver_result", JSON, nullable=True)
    )
    decided_by: str | None = None  # DecisionSource; "human" decisions are never overwritten
    decided_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    deferred_until: datetime | None = Field(default=None, sa_type=UTCDateTime)
