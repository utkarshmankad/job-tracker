"""SQLModel table definitions — source of truth for schema."""

import enum
from datetime import datetime
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


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
    applied_date: datetime = Field(index=True)
    current_status: ApplicationStatus = Field(default=ApplicationStatus.APPLIED, index=True)
    thread_ids: str = "[]"  # JSON-encoded list[str]
    is_false_positive: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    status_history: List["StatusHistory"] = Relationship(back_populates="application")


class StatusHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    from_status: Optional[str] = None
    to_status: str
    trigger: str  # "email" | "manual"
    changed_at: datetime = Field(default_factory=datetime.utcnow)
    message_id: Optional[str] = None
    application: Optional[Application] = Relationship(back_populates="status_history")


class SuppressRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    sender_pattern: str
    subject_pattern: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PollerState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    last_history_id: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    status: str = "SLEEPING"
    error_message: Optional[str] = None


class ProcessedMessage(SQLModel, table=True):
    message_id: str = Field(primary_key=True)
    processed_at: datetime = Field(default_factory=datetime.utcnow)
    result: str  # "applied" | "status_update" | "suppressed" | "ignored"
