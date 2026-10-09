"""Reversible multi-application merges: soft-merge columns, merge operations, dismissals.

Revision ID: 0004_merge_operations
Revises: 0003_resolver_audit
Create Date: 2026-10-09

Additive only (docs/phase-2-identity-resolution.md §12):

- ``application``: record_state (NOT NULL, server default 'active' so every existing row and
  every row an older release inserts is active), merged_into_application_id,
  merge_operation_id, merged_at (+3 indexes). merged_into_application_id has no database
  FK — SQLite cannot add one without rebuilding the table; the merge code validates it.
- ``statushistory`` / ``applicationevent``: superseded_by_merge_id (+index) — duplicates
  hidden by a merge, never deleted.
- ``mergeoperation``: durable, checksummed snapshot of each merge and its undo record.
- ``duplicatedismissal``: duplicate suggestions a person rejected.

No existing value is changed. Every step is "if missing", so it is safe to re-run after a
rollback stamp.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0004_merge_operations"
down_revision: str | None = "0003_resolver_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind: sa.Connection, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table) if ix["name"]}


def upgrade() -> None:
    bind = op.get_bind()

    app_cols = _columns(bind, "application")
    new_app_cols: list[sa.Column[Any]] = [
        sa.Column("record_state", sa.String(), server_default="active", nullable=False),
        sa.Column("merged_into_application_id", sa.Integer(), nullable=True),
        sa.Column("merge_operation_id", sa.Integer(), nullable=True),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
    ]
    missing = [c for c in new_app_cols if c.name not in app_cols]
    if missing:
        with op.batch_alter_table("application") as batch_op:
            for column in missing:
                batch_op.add_column(column)
    present = _indexes(bind, "application")
    for name, column_name in (
        ("ix_application_record_state", "record_state"),
        ("ix_application_merged_into_application_id", "merged_into_application_id"),
        ("ix_application_merge_operation_id", "merge_operation_id"),
    ):
        if name not in present:
            op.create_index(name, "application", [column_name], unique=False)

    for table in ("statushistory", "applicationevent"):
        if "superseded_by_merge_id" not in _columns(bind, table):
            with op.batch_alter_table(table) as batch_op:
                batch_op.add_column(
                    sa.Column("superseded_by_merge_id", sa.Integer(), nullable=True)
                )
        index = f"ix_{table}_superseded_by_merge_id"
        if index not in _indexes(bind, table):
            op.create_index(index, table, ["superseded_by_merge_id"], unique=False)

    tables = set(sa.inspect(bind).get_table_names())
    if "mergeoperation" not in tables:
        op.create_table(
            "mergeoperation",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("operation_version", sa.Integer(), nullable=False),
            sa.Column("survivor_application_id", sa.Integer(), nullable=False),
            sa.Column("source_application_ids", sa.JSON(), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("snapshot_checksum", sa.String(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("field_values", sa.JSON(), nullable=False),
            sa.Column("preview_token", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("initiated_by", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("undone_at", sa.DateTime(), nullable=True),
            sa.Column("undone_by", sa.String(), nullable=True),
            sa.Column("undo_metadata", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["survivor_application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    present = _indexes(bind, "mergeoperation")
    for name, column_name, unique in (
        ("ix_mergeoperation_created_at", "created_at", False),
        ("ix_mergeoperation_idempotency_key", "idempotency_key", True),
        ("ix_mergeoperation_survivor_application_id", "survivor_application_id", False),
    ):
        if name not in present:
            op.create_index(name, "mergeoperation", [column_name], unique=unique)

    if "duplicatedismissal" not in tables:
        op.create_table(
            "duplicatedismissal",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("pair_key", sa.String(), nullable=False),
            sa.Column("dismissed_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if "ix_duplicatedismissal_pair_key" not in _indexes(bind, "duplicatedismissal"):
        op.create_index(
            "ix_duplicatedismissal_pair_key", "duplicatedismissal", ["pair_key"], unique=True
        )


def downgrade() -> None:
    """Remove the 0004 objects. Refuses while any merge is in effect: dropping the soft-merge
    columns would silently resurrect merged duplicates into every list."""
    bind = op.get_bind()
    application = sa.table("application", sa.column("record_state", sa.String()))
    merged = bind.execute(
        sa.select(sa.func.count())
        .select_from(application)
        .where(application.c.record_state == "merged")
    ).scalar()
    if merged:
        raise RuntimeError(
            f"{merged} applications are merged; undo those merges (or restore a backup) "
            "before downgrading past 0004_merge_operations."
        )
    op.drop_index("ix_duplicatedismissal_pair_key", table_name="duplicatedismissal")
    op.drop_table("duplicatedismissal")
    for name in (
        "ix_mergeoperation_created_at",
        "ix_mergeoperation_idempotency_key",
        "ix_mergeoperation_survivor_application_id",
    ):
        op.drop_index(name, table_name="mergeoperation")
    op.drop_table("mergeoperation")
    for table in ("applicationevent", "statushistory"):
        op.drop_index(f"ix_{table}_superseded_by_merge_id", table_name=table)
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("superseded_by_merge_id")
    for name in (
        "ix_application_merge_operation_id",
        "ix_application_merged_into_application_id",
        "ix_application_record_state",
    ):
        op.drop_index(name, table_name="application")
    with op.batch_alter_table("application") as batch_op:
        for name in (
            "merged_at",
            "merge_operation_id",
            "merged_into_application_id",
            "record_state",
        ):
            batch_op.drop_column(name)
