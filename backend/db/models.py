"""SQLModel table definitions — source of truth for schema."""

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Column
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import DateTime, TypeDecorator
from sqlmodel import Field, Relationship, SQLModel


def utc_now() -> datetime:
    """Timezone-aware 'now' — the only clock repo code should call (never bare utcnow())."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores datetimes as naive UTC in SQLite (its only native format) while keeping every
    Python-level value timezone-aware. Naive values passed in are assumed to already be UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: Optional[datetime], dialect) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


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
    id: Optional[int] = Field(default=None, primary_key=True)
    company: Optional[str] = None
    role: Optional[str] = None
    source_portal: str = Field(index=True)
    job_url: Optional[str] = None
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
    withdraw_reason: Optional[str] = None  # "self_withdraw" | "company_closed"
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=utc_now, index=True, sa_type=UTCDateTime)
    status_history: List["StatusHistory"] = Relationship(back_populates="application")


class StatusHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    from_status: Optional[str] = None
    to_status: str
    trigger: str  # "email" | "manual"
    changed_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    message_id: Optional[str] = None
    application: Optional[Application] = Relationship(back_populates="status_history")


class ApplicationThreadId(SQLModel, table=True):
    """Indexed lookup table for Application.thread_ids (still the JSON source of truth on
    Application itself). Kept in sync by DataStore.upsert_application — replaces an
    unindexed LIKE scan over the JSON blob with an indexed equality lookup."""

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    thread_id: str = Field(index=True)


class SuppressRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    sender_pattern: str
    subject_pattern: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)


class PollerState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    last_history_id: Optional[str] = None
    last_sync_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    status: str = "SLEEPING"
    error_message: Optional[str] = None


class ProcessedMessage(SQLModel, table=True):
    message_id: str = Field(primary_key=True)
    processed_at: datetime = Field(default_factory=utc_now, sa_type=UTCDateTime)
    result: str  # "applied" | "status_update" | "suppressed" | "ignored"
