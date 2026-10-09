"""Resolver audit trail: evidence identity signals, decision columns, identity index.

Revision ID: 0003_resolver_audit
Revises: 0002_evidence_model
Create Date: 2026-10-09

Additive only (docs/phase-2-identity-resolution.md §11):

- ``evidence``: sender_address, sender_domain, canonical_job_url, external_job_id (derived,
  indexed — filled by DataStore runtime maintenance, which owns normalization), and
  resolver_version, resolver_decision (indexed), resolver_confidence, resolver_result (JSON),
  decided_by, decided_at, deferred_until;
- ``application``: composite index ix_application_identity (normalized_company,
  normalized_role) for indexed candidate generation;
- records who owns each existing decision: links made through the manual link endpoint
  (link_method ``manual``) are human decisions; other decided rows were made by the
  Phase 2 rule set, recorded as resolver version ``1``.

Every step is "if missing" / only-where-NULL, so re-running after a rollback stamp is safe.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0003_resolver_audit"
down_revision: str | None = "0002_evidence_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVIDENCE_COLUMNS: list[tuple[str, sa.types.TypeEngine[Any]]] = [
    ("sender_address", sa.String()),
    ("sender_domain", sa.String()),
    ("canonical_job_url", sa.String()),
    ("external_job_id", sa.String()),
    ("resolver_version", sa.String()),
    ("resolver_decision", sa.String()),
    ("resolver_confidence", sa.Float()),
    ("resolver_result", sa.JSON()),
    ("decided_by", sa.String()),
    ("decided_at", sa.DateTime()),
    ("deferred_until", sa.DateTime()),
]
_EVIDENCE_INDEXES = {
    "ix_evidence_sender_address": "sender_address",
    "ix_evidence_sender_domain": "sender_domain",
    "ix_evidence_canonical_job_url": "canonical_job_url",
    "ix_evidence_external_job_id": "external_job_id",
    "ix_evidence_resolver_decision": "resolver_decision",
}
# Phase 2 (rule set v1) processing_status → resolver decision.
_V1_DECISIONS = {
    "linked": "linked",
    "created_application": "new_application",
    "needs_review": "review_required",
    "ignored": "ignored",
    "informational": "ignored",
}


def _record_decision_owners(bind: sa.Connection) -> None:
    evidence = sa.table(
        "evidence",
        sa.column("processing_status", sa.String()),
        sa.column("link_method", sa.String()),
        sa.column("updated_at", sa.DateTime()),
        sa.column("decided_by", sa.String()),
        sa.column("decided_at", sa.DateTime()),
        sa.column("resolver_version", sa.String()),
        sa.column("resolver_decision", sa.String()),
    )
    undecided = evidence.c.decided_by.is_(None)
    bind.execute(
        evidence.update()
        .where(undecided, evidence.c.link_method == "manual")
        .values(decided_by="human", decided_at=evidence.c.updated_at)
    )
    for status, decision in _V1_DECISIONS.items():
        bind.execute(
            evidence.update()
            .where(undecided, evidence.c.processing_status == status)
            .values(
                decided_by="resolver",
                decided_at=evidence.c.updated_at,
                resolver_version="1",
                resolver_decision=decision,
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    present = {c["name"] for c in sa.inspect(bind).get_columns("evidence")}
    missing = [(n, t) for n, t in _EVIDENCE_COLUMNS if n not in present]
    if missing:
        with op.batch_alter_table("evidence") as batch_op:
            for name, type_ in missing:
                batch_op.add_column(sa.Column(name, type_, nullable=True))

    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("evidence")}
    for name, column in _EVIDENCE_INDEXES.items():
        if name not in indexes:
            op.create_index(name, "evidence", [column], unique=False)

    app_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("application")}
    if "ix_application_identity" not in app_indexes:
        op.create_index(
            "ix_application_identity",
            "application",
            ["normalized_company", "normalized_role"],
            unique=False,
        )

    _record_decision_owners(bind)


def downgrade() -> None:
    """Remove only the 0003 objects (resolver audit data is lost)."""
    op.drop_index("ix_application_identity", table_name="application")
    for name in _EVIDENCE_INDEXES:
        op.drop_index(name, table_name="evidence")
    with op.batch_alter_table("evidence") as batch_op:
        for name, _type in reversed(_EVIDENCE_COLUMNS):
            batch_op.drop_column(name)
