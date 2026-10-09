"""Source collection: scoped collectors, collection runs, source items and observations.

Revision ID: 0005_source_collection
Revises: 0004_merge_operations
Create Date: 2026-10-09

Additive only (docs/phase-3-source-collection.md):

- ``collector``: a local collection agent's revocable credential (secret hash only).
- ``collectorenrollment``: single-use, short-lived setup codes (hash only).
- ``collectionsource``: configured source accounts and their last-run state.
- ``collectionrun``: one visit of one source, idempotent by the collector's ``run_key``.
- ``collectionbatch``: submitted observation batches, unique per run (replay-safe).
- ``sourceitem``: one application as a job site records it, unique by
  ``(source_key, item_key)``.
- ``sourceobservation``: immutable observations, unique by ``(source_item_id,
  content_hash)``.

No existing table, column or value changes. Every step is "if missing", so it is safe to
re-run after a rollback stamp.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0005_source_collection"
down_revision: str | None = "0004_merge_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "sourceobservation",
    "sourceitem",
    "collectionbatch",
    "collectionrun",
    "collectionsource",
    "collectorenrollment",
    "collector",
)


def _indexes(bind: sa.Connection, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table) if ix["name"]}


def _index(bind: sa.Connection, table: str, columns: list[str], unique: bool = False) -> None:
    name = f"ix_{table}_{'_'.join(columns)}"
    if name not in _indexes(bind, table):
        op.create_index(name, table, columns, unique=unique)


def _ts(name: str, nullable: bool = True) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(), nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "collector" not in tables:
        op.create_table(
            "collector",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("token_id", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(), nullable=True),
            sa.Column("scopes", sa.JSON(), nullable=False),
            _ts("created_at", nullable=False),
            sa.Column("created_by", sa.String(), nullable=True),
            _ts("enrolled_at"),
            _ts("last_used_at"),
            _ts("rotated_at"),
            _ts("revoked_at"),
            sa.Column("revoked_by", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    _index(bind, "collector", ["token_id"], unique=True)

    if "collectorenrollment" not in tables:
        op.create_table(
            "collectorenrollment",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("collector_id", sa.Integer(), nullable=False),
            sa.Column("code_hash", sa.String(), nullable=False),
            _ts("created_at", nullable=False),
            _ts("expires_at", nullable=False),
            _ts("used_at"),
            sa.ForeignKeyConstraint(["collector_id"], ["collector.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _index(bind, "collectorenrollment", ["code_hash"], unique=True)
    _index(bind, "collectorenrollment", ["collector_id"])

    if "collectionsource" not in tables:
        op.create_table(
            "collectionsource",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_key", sa.String(), nullable=False),
            sa.Column("account_label", sa.String(), nullable=False),
            sa.Column("collector_id", sa.Integer(), nullable=True),
            _ts("created_at", nullable=False),
            _ts("last_attempt_at"),
            _ts("last_success_at"),
            sa.Column("last_status", sa.String(), nullable=True),
            sa.Column("last_run_id", sa.Integer(), nullable=True),
            sa.Column("needs_attention", sa.Boolean(), nullable=False),
            sa.Column("attention_reason", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["collector_id"], ["collector.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_key", "account_label", name="uq_collectionsource_account"),
        )
    _index(bind, "collectionsource", ["source_key"])
    _index(bind, "collectionsource", ["collector_id"])

    if "collectionrun" not in tables:
        op.create_table(
            "collectionrun",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_key", sa.String(), nullable=False),
            sa.Column("collector_id", sa.Integer(), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=False),
            sa.Column("source_key", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            _ts("started_at", nullable=False),
            _ts("finished_at"),
            sa.Column("collector_version", sa.String(), nullable=False),
            sa.Column("adapter_version", sa.String(), nullable=False),
            sa.Column("items_seen", sa.Integer(), nullable=False),
            sa.Column("observations_received", sa.Integer(), nullable=False),
            sa.Column("created_count", sa.Integer(), nullable=False),
            sa.Column("linked_count", sa.Integer(), nullable=False),
            sa.Column("review_count", sa.Integer(), nullable=False),
            sa.Column("unchanged_count", sa.Integer(), nullable=False),
            sa.Column("error_count", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(), nullable=True),
            sa.Column("error_message", sa.String(), nullable=True),
            sa.Column("diagnostics", sa.JSON(), server_default="{}", nullable=False),
            sa.ForeignKeyConstraint(["collector_id"], ["collector.id"]),
            sa.ForeignKeyConstraint(["source_id"], ["collectionsource.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _index(bind, "collectionrun", ["run_key"], unique=True)
    for column in ("collector_id", "source_id", "source_key", "status", "started_at"):
        _index(bind, "collectionrun", [column])

    if "collectionbatch" not in tables:
        op.create_table(
            "collectionbatch",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("batch_key", sa.String(), nullable=False),
            _ts("received_at", nullable=False),
            sa.Column("item_count", sa.Integer(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["collectionrun.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "batch_key", name="uq_collectionbatch_key"),
        )
    _index(bind, "collectionbatch", ["run_id"])

    if "sourceitem" not in tables:
        op.create_table(
            "sourceitem",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_key", sa.String(), nullable=False),
            sa.Column("item_key", sa.String(), nullable=False),
            sa.Column("id_kind", sa.String(), nullable=False),
            sa.Column("source_item_id", sa.String(), nullable=True),
            sa.Column("company", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=True),
            sa.Column("canonical_url", sa.String(), nullable=True),
            sa.Column("external_job_id", sa.String(), nullable=True),
            _ts("applied_at"),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("raw_status", sa.String(), nullable=True),
            _ts("first_seen_at", nullable=False),
            _ts("last_seen_at", nullable=False),
            sa.Column("first_run_id", sa.Integer(), nullable=True),
            sa.Column("last_run_id", sa.Integer(), nullable=True),
            sa.Column("latest_observation_id", sa.Integer(), nullable=True),
            sa.Column("application_id", sa.Integer(), nullable=True),
            sa.Column("decision", sa.String(), nullable=True),
            sa.Column("decision_reason", sa.String(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("needs_attention", sa.Boolean(), nullable=False),
            sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source_key", "item_key", name="uq_sourceitem_key"),
        )
    for column in ("source_key", "external_job_id", "last_seen_at", "application_id", "decision"):
        _index(bind, "sourceitem", [column])

    if "sourceobservation" not in tables:
        op.create_table(
            "sourceobservation",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_item_id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("batch_id", sa.Integer(), nullable=True),
            sa.Column("source_key", sa.String(), nullable=False),
            sa.Column("contract_version", sa.Integer(), nullable=False),
            sa.Column("collector_version", sa.String(), nullable=False),
            sa.Column("adapter_version", sa.String(), nullable=False),
            sa.Column("extraction", sa.String(), nullable=False),
            sa.Column("content_hash", sa.String(), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=False),
            _ts("observed_at", nullable=False),
            _ts("received_at", nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("evidence_id", sa.Integer(), nullable=True),
            sa.Column("decision", sa.String(), nullable=False),
            sa.Column("decision_reason", sa.String(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.ForeignKeyConstraint(["batch_id"], ["collectionbatch.id"]),
            sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"]),
            sa.ForeignKeyConstraint(["run_id"], ["collectionrun.id"]),
            sa.ForeignKeyConstraint(["source_item_id"], ["sourceitem.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_item_id", "content_hash", name="uq_sourceobservation_content"
            ),
        )
    for column in ("source_item_id", "run_id", "source_key", "evidence_id", "decision"):
        _index(bind, "sourceobservation", [column])


def downgrade() -> None:
    """Drop the 0005 tables. Refuses while any source observation exists: their evidence
    and applications would lose the provenance that explains where they came from."""
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    if "sourceobservation" in present:
        observations = sa.table("sourceobservation", sa.column("id", sa.Integer()))
        count = bind.execute(sa.select(sa.func.count()).select_from(observations)).scalar()
        if count:
            raise RuntimeError(
                f"{count} source observations exist; export or restore a backup before "
                "downgrading past 0005_source_collection."
            )
    # "If present" throughout: SQLite DDL is not fully transactional through pysqlite, so a
    # downgrade interrupted by a later revision's refusal must be safe to run again.
    for table in _TABLES:
        if table not in present:
            continue
        for name in sorted(_indexes(bind, table)):
            if name.startswith("ix_"):
                op.drop_index(name, table_name=table)
        op.drop_table(table)
