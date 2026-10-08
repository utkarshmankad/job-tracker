"""Baseline: the origin/main (pre-Phase-1) schema — create or adopt.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-09

Before Phase 1 the schema was created by ``SQLModel.metadata.create_all()`` plus
``DataStore._migrate_schema()`` (three ``ALTER TABLE ... ADD COLUMN`` statements and an
``Instahire`` → ``Instahyre`` data fix) on every startup. This revision reproduces that end
state exactly and is written to be safe on any of:

- an empty database (creates everything);
- an unversioned database created by origin/main (only stamps — every object already exists);
- an older unversioned database that predates some tables/columns (adds what is missing,
  exactly as ``create_all`` + ``_migrate_schema`` used to).

It is deliberately frozen: column types are spelled out here instead of importing
backend.db.models, so later model changes cannot alter what this revision does.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    # Matches SAEnum(values_callable=...) on SQLite: VARCHAR(longest value), no CHECK.
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False)


APPLICATION_STATUS = _enum(
    "Applied",
    "Resume Shortlisted",
    "Interview Scheduled",
    "Interview In Progress",
    "Offer Negotiation",
    "Offer",
    "Joined",
    "Rejected",
    "Withdrawn",
    name="applicationstatus",
)
EVENT_TYPE = _enum(
    "Application Submitted",
    "Recruiter Response",
    "Interview Scheduled",
    "Interview Attended",
    "Interview Rescheduled",
    "Interview Cancelled",
    "Offer Received",
    "Rejected",
    name="applicationeventtype",
)
INTERVIEW_ROUND = _enum(
    "Recruiter Screen",
    "Hiring Manager",
    "Technical / System Design",
    "Leadership / Behavioural",
    "Executive / Final",
    "Other",
    name="interviewround",
)
PROSPECT_STATUS = _enum("New", "Reviewed", "Dismissed", "Converted", name="prospectstatus")


def _tables() -> dict[str, list[sa.schema.SchemaItem]]:
    """Table definitions in creation order (parents before children)."""
    return {
        "application": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("company", sa.String(), nullable=True),
            sa.Column("role", sa.String(), nullable=True),
            sa.Column("source_portal", sa.String(), nullable=False),
            sa.Column("application_method", sa.String(), server_default="Unknown", nullable=False),
            sa.Column("job_url", sa.String(), nullable=True),
            sa.Column("applied_date", sa.DateTime(), nullable=False),
            sa.Column("current_status", APPLICATION_STATUS, nullable=False),
            sa.Column("thread_ids", sa.String(), nullable=False),
            sa.Column("is_false_positive", sa.Boolean(), nullable=False),
            sa.Column("withdraw_reason", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ],
        "suppressrule": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("sender_pattern", sa.String(), nullable=False),
            sa.Column("subject_pattern", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ],
        "pollerstate": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("last_history_id", sa.String(), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("error_message", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ],
        "processedmessage": [
            sa.Column("message_id", sa.String(), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=False),
            sa.Column("result", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("message_id"),
        ],
        "statushistory": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("application_id", sa.Integer(), nullable=False),
            sa.Column("from_status", sa.String(), nullable=True),
            sa.Column("to_status", sa.String(), nullable=False),
            sa.Column("trigger", sa.String(), nullable=False),
            sa.Column("changed_at", sa.DateTime(), nullable=False),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
        ],
        "applicationevent": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("application_id", sa.Integer(), nullable=False),
            sa.Column("event_type", EVENT_TYPE, nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("interview_round", INTERVIEW_ROUND, nullable=True),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("source_message_id", sa.String(), nullable=True),
            sa.Column("status_history_id", sa.Integer(), nullable=True),
            sa.Column("notes", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
        ],
        "applicationthreadid": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("application_id", sa.Integer(), nullable=False),
            sa.Column("thread_id", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
        ],
        "prospect": [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_portal", sa.String(), nullable=False),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("sender", sa.String(), nullable=False),
            sa.Column("snippet", sa.String(), nullable=True),
            sa.Column("received_at", sa.DateTime(), nullable=False),
            sa.Column("gmail_message_id", sa.String(), nullable=False),
            sa.Column("gmail_thread_id", sa.String(), nullable=False),
            sa.Column("application_id", sa.Integer(), nullable=True),
            sa.Column("status", PROSPECT_STATUS, nullable=False),
            sa.Column("classification_reason", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["application_id"], ["application.id"]),
            sa.PrimaryKeyConstraint("id"),
        ],
    }


# (table, column) pairs that older databases lacked and DataStore._migrate_schema() added with
# ALTER TABLE ... ADD COLUMN. SQLite cannot add a FOREIGN KEY this way, so — exactly as
# before — prospect.application_id is added without one on such databases.
_LEGACY_COLUMNS: list[tuple[str, sa.Column]] = [
    ("application", sa.Column("withdraw_reason", sa.String(), nullable=True)),
    (
        "application",
        sa.Column("application_method", sa.String(), server_default="Unknown", nullable=False),
    ),
    ("prospect", sa.Column("application_id", sa.Integer(), nullable=True)),
]

# name -> (table, columns, unique)
_INDEXES: dict[str, tuple[str, list[str], bool]] = {
    "ix_application_application_method": ("application", ["application_method"], False),
    "ix_application_applied_date": ("application", ["applied_date"], False),
    "ix_application_current_status": ("application", ["current_status"], False),
    "ix_application_source_portal": ("application", ["source_portal"], False),
    "ix_application_updated_at": ("application", ["updated_at"], False),
    "ix_applicationevent_application_id": ("applicationevent", ["application_id"], False),
    "ix_applicationevent_event_type": ("applicationevent", ["event_type"], False),
    "ix_applicationevent_occurred_at": ("applicationevent", ["occurred_at"], False),
    "ix_applicationevent_source_message_id": ("applicationevent", ["source_message_id"], False),
    "ix_applicationevent_status_history_id": ("applicationevent", ["status_history_id"], True),
    "ix_applicationthreadid_application_id": ("applicationthreadid", ["application_id"], False),
    "ix_applicationthreadid_thread_id": ("applicationthreadid", ["thread_id"], False),
    "ix_prospect_application_id": ("prospect", ["application_id"], False),
    "ix_prospect_category": ("prospect", ["category"], False),
    "ix_prospect_gmail_message_id": ("prospect", ["gmail_message_id"], True),
    "ix_prospect_gmail_thread_id": ("prospect", ["gmail_thread_id"], False),
    "ix_prospect_received_at": ("prospect", ["received_at"], False),
    "ix_prospect_source_portal": ("prospect", ["source_portal"], False),
    "ix_prospect_status": ("prospect", ["status"], False),
    "ix_prospect_updated_at": ("prospect", ["updated_at"], False),
}


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    for name, items in _tables().items():
        if name not in existing_tables:
            op.create_table(name, *items)

    inspector = sa.inspect(bind)
    for table, column in _LEGACY_COLUMNS:
        present = {c["name"] for c in inspector.get_columns(table)}
        if column.name not in present:
            with op.batch_alter_table(table) as batch_op:
                batch_op.add_column(column)

    inspector = sa.inspect(bind)
    for index_name, (table, columns, unique) in _INDEXES.items():
        index_names: set[str | None] = {ix["name"] for ix in inspector.get_indexes(table)}
        if index_name not in index_names:
            op.create_index(index_name, table, columns, unique=unique)

    # Data fix formerly re-run on every startup by DataStore._migrate_schema().
    application = sa.table("application", sa.column("source_portal", sa.String()))
    op.execute(
        application.update()
        .where(application.c.source_portal == "Instahire")
        .values(source_portal="Instahyre")
    )


def downgrade() -> None:
    raise RuntimeError(
        "0001_baseline cannot be downgraded: it is the schema of the existing data. "
        "Restore a pre-migration backup instead (scripts/restore_database.py)."
    )
