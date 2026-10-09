"""Evidence model: evidence table, application identity columns, prospect backfill.

Revision ID: 0002_evidence_model
Revises: 0001_baseline
Create Date: 2026-10-09

Additive only (docs/phase-2-identity-resolution.md §5):

- creates ``evidence`` with its indexes and uniqueness rules;
- adds nullable ``application`` columns normalized_company, normalized_role,
  canonical_job_url, external_job_id, last_evidence_at (+3 indexes);
- backfills one ``email``/``gmail`` evidence row per ``prospect`` (each is a real Gmail
  message with its own ID, thread, sender, subject, snippet and received date), linked when
  the prospect already points at an application;
- sets ``application.last_evidence_at`` from linked evidence where it is NULL.

Every step is "if missing" / ON CONFLICT DO NOTHING, so the revision is safe to re-run after
the documented rollback stamp to 0001_baseline. The derived identity columns are filled by
DataStore runtime maintenance, which owns the normalization rules. Frozen: this file must not
import application code; the fingerprint and subject rules are copied and pinned by tests.
"""

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

revision: str = "0002_evidence_model"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROSPECT_FALLBACK_TITLE = "LinkedIn recruiting activity"  # not a real subject
_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)

_EVIDENCE_INDEXES: dict[str, tuple[list[str], bool]] = {
    "ix_evidence_application_id": (["application_id"], False),
    "ix_evidence_content_fingerprint": (["content_fingerprint"], True),
    "ix_evidence_evidence_type": (["evidence_type"], False),
    "ix_evidence_occurred_at": (["occurred_at"], False),
    "ix_evidence_processing_status": (["processing_status"], False),
    "ix_evidence_source": (["source"], False),
    "ix_evidence_thread_id": (["thread_id"], False),
}
_APPLICATION_COLUMNS: list[tuple[str, sa.types.TypeEngine[Any]]] = [
    ("normalized_company", sa.String()),
    ("normalized_role", sa.String()),
    ("canonical_job_url", sa.String()),
    ("external_job_id", sa.String()),
    ("last_evidence_at", sa.DateTime()),
]
_APPLICATION_INDEXES = {
    "ix_application_canonical_job_url": "canonical_job_url",
    "ix_application_external_job_id": "external_job_id",
    "ix_application_normalized_company": "normalized_company",
}


def _fingerprint_external(evidence_type: str, source: str, external_id: str) -> str:
    # Frozen copy of backend.engine.normalization.evidence_fingerprint (evidence-v1, external
    # ID form); tests/unit/test_evidence.py asserts both produce identical values.
    joined = "\x1f".join(["evidence-v1", evidence_type, source, "ext", external_id.strip()])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalize_subject(value: str | None) -> str | None:
    # Frozen copy of backend.engine.normalization.normalize_subject.
    if not value:
        return None
    text = " ".join(_REPLY_PREFIX.sub("", value).casefold().split())
    return text or None


def _create_evidence_table() -> None:
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("evidence_type", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("recipient", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("normalized_subject", sa.String(), nullable=True),
        sa.Column("snippet", sa.String(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("raw_metadata", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("content_fingerprint", sa.String(), nullable=False),
        sa.Column("processing_status", sa.String(), nullable=False),
        sa.Column("review_reason", sa.String(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("link_method", sa.String(), nullable=True),
        sa.Column("link_confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="uq_evidence_source_external_id"),
    )


def _backfill_prospects(bind: sa.Connection) -> None:
    prospect = sa.table(
        "prospect",
        sa.column("id", sa.Integer()),
        sa.column("category", sa.String()),
        sa.column("title", sa.String()),
        sa.column("sender", sa.String()),
        sa.column("snippet", sa.String()),
        sa.column("received_at", sa.DateTime()),
        sa.column("gmail_message_id", sa.String()),
        sa.column("gmail_thread_id", sa.String()),
        sa.column("application_id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    evidence = sa.table(
        "evidence",
        sa.column("evidence_type", sa.String()),
        sa.column("source", sa.String()),
        sa.column("external_id", sa.String()),
        sa.column("thread_id", sa.String()),
        sa.column("sender", sa.String()),
        sa.column("subject", sa.String()),
        sa.column("normalized_subject", sa.String()),
        sa.column("snippet", sa.String()),
        sa.column("occurred_at", sa.DateTime()),
        sa.column("captured_at", sa.DateTime()),
        sa.column("raw_metadata", sa.JSON()),
        sa.column("content_fingerprint", sa.String()),
        sa.column("processing_status", sa.String()),
        sa.column("application_id", sa.Integer()),
        sa.column("link_method", sa.String()),
        sa.column("link_confidence", sa.Float()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    now = datetime.now(UTC).replace(tzinfo=None)  # stored as naive UTC, like UTCDateTime
    rows = bind.execute(sa.select(prospect).order_by(prospect.c.id)).mappings().all()
    for row in rows:
        title = (row["title"] or "").strip()
        subject = None if not title or title == _PROSPECT_FALLBACK_TITLE else title
        linked = row["application_id"] is not None
        statement = (
            sqlite_insert(evidence)
            .values(
                evidence_type="email",
                source="gmail",
                external_id=row["gmail_message_id"],
                thread_id=row["gmail_thread_id"],
                sender=row["sender"],
                subject=subject,
                normalized_subject=_normalize_subject(subject),
                snippet=(row["snippet"] or None) and row["snippet"][:500],
                occurred_at=row["received_at"],
                captured_at=row["created_at"],
                raw_metadata={
                    "classification": "prospect",
                    "prospect_category": row["category"],
                    "backfill": {
                        "origin": "prospect",
                        "prospect_id": row["id"],
                        "revision": revision,
                    },
                },
                content_fingerprint=_fingerprint_external(
                    "email", "gmail", row["gmail_message_id"]
                ),
                processing_status="informational",
                application_id=row["application_id"],
                link_method="backfill" if linked else None,
                link_confidence=1.0 if linked else None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
        )
        bind.execute(statement)


def _set_last_evidence_at(bind: sa.Connection) -> None:
    application = sa.table(
        "application",
        sa.column("id", sa.Integer()),
        sa.column("last_evidence_at", sa.DateTime()),
    )
    evidence = sa.table(
        "evidence",
        sa.column("application_id", sa.Integer()),
        sa.column("occurred_at", sa.DateTime()),
    )
    newest = (
        sa.select(sa.func.max(evidence.c.occurred_at))
        .where(evidence.c.application_id == application.c.id)
        .scalar_subquery()
    )
    has_evidence = sa.exists().where(evidence.c.application_id == application.c.id)
    bind.execute(
        application.update()
        .where(application.c.last_evidence_at.is_(None))
        .where(has_evidence)
        .values(last_evidence_at=newest)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if "evidence" not in sa.inspect(bind).get_table_names():
        _create_evidence_table()

    present = {ix["name"] for ix in sa.inspect(bind).get_indexes("evidence")}
    for name, (columns, unique) in _EVIDENCE_INDEXES.items():
        if name not in present:
            op.create_index(name, "evidence", columns, unique=unique)

    app_columns = {c["name"] for c in sa.inspect(bind).get_columns("application")}
    missing = [(n, t) for n, t in _APPLICATION_COLUMNS if n not in app_columns]
    if missing:
        with op.batch_alter_table("application") as batch_op:
            for name, type_ in missing:
                batch_op.add_column(sa.Column(name, type_, nullable=True))

    app_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("application")}
    for name, column in _APPLICATION_INDEXES.items():
        if name not in app_indexes:
            op.create_index(name, "application", [column], unique=False)

    _backfill_prospects(bind)
    _set_last_evidence_at(bind)


def downgrade() -> None:
    """Remove only the Phase 2 objects. Evidence rows and the derived identity columns are
    lost; prefer restoring the pre-migration backup (docs/database-operations.md)."""
    for name in _APPLICATION_INDEXES:
        op.drop_index(name, table_name="application")
    with op.batch_alter_table("application") as batch_op:
        for name, _type in reversed(_APPLICATION_COLUMNS):
            batch_op.drop_column(name)
    for name in _EVIDENCE_INDEXES:
        op.drop_index(name, table_name="evidence")
    op.drop_table("evidence")
