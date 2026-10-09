"""DataStore: single access point for all database operations."""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import ColumnElement, Table, event, func, inspect, or_, update
from sqlalchemy import select as core_select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, SQLModel, col, create_engine, select

from backend.config import (
    DB_PATH,
    EVIDENCE_SNIPPET_MAX_CHARS,
    RESOLVER_PROCESSING_CLAIM_TTL_SECONDS,
    STALE_DAYS_THRESHOLD,
)
from backend.db import merge_snapshot, schema
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    ApplicationThreadId,
    DecisionSource,
    DuplicateDismissal,
    Evidence,
    EvidenceSource,
    EvidenceStatus,
    EvidenceType,
    LinkMethod,
    MergeOperation,
    PollerState,
    ProcessedMessage,
    Prospect,
    ProspectStatus,
    RecordState,
    StatusHistory,
    SuppressRule,
    UTCDateTime,
    utc_now,
)
from backend.db.schema import SchemaPolicy, SchemaStatus
from backend.engine.normalization import (
    canonical_job_url,
    evidence_fingerprint,
    external_job_id_from_url,
    normalize_company,
    normalize_email_address,
    normalize_role,
    normalize_subject,
    registrable_domain,
    sender_domain,
)


def is_application_stale(
    app: Application, threshold_days: int = STALE_DAYS_THRESHOLD, now: datetime | None = None
) -> bool:
    """Return True when an Applied-status application has had no update in threshold_days
    (as of `now`, default the current time)."""
    if app.current_status != ApplicationStatus.APPLIED:
        return False
    cutoff = (now or utc_now()) - timedelta(days=threshold_days)
    updated = (
        app.updated_at
        if isinstance(app.updated_at, datetime)
        else datetime(
            app.updated_at.year,
            app.updated_at.month,
            app.updated_at.day,
            tzinfo=UTC,
        )
    )
    return updated < cutoff


log = structlog.get_logger(__name__)


@dataclass
class EvidenceFilter:
    linked: bool | None = None  # True: has application; False: no application
    source: str | None = None
    evidence_type: str | None = None
    processing_status: str | None = None
    statuses: tuple[str, ...] | None = None  # any of these processing statuses
    date_from: datetime | None = None  # on occurred_at
    date_to: datetime | None = None
    application_id: int | None = None
    # Non-job mail is recorded as minimal `ignored` evidence; hidden unless asked for.
    include_ignored: bool = False
    page: int = 1
    page_size: int = 50


def _table(model: type[SQLModel]) -> Table:
    """The Core table of a SQLModel class (SQLModel's stubs do not declare __table__)."""
    return model.__table__  # type: ignore[attr-defined]


def _active_app() -> ColumnElement[bool]:
    """Default visibility: merged records are hidden from lists, counts and analytics."""
    return col(Application.record_state) == RecordState.ACTIVE.value


def _live_history() -> ColumnElement[bool]:
    return col(StatusHistory.superseded_by_merge_id).is_(None)


def _live_event() -> ColumnElement[bool]:
    return col(ApplicationEvent.superseded_by_merge_id).is_(None)


class EvidenceNotFoundError(LookupError):
    pass


class ApplicationNotFoundError(LookupError):
    pass


class MergeError(RuntimeError):
    """Base class for merge/undo failures that change nothing."""


class MergeInvalidError(MergeError):
    pass


class MergeStaleError(MergeError):
    """The involved records changed since the preview, or one is already merged."""


class MergeNotFoundError(LookupError):
    pass


class MergeUndoConflictError(MergeError):
    def __init__(self, conflicts: list[str]) -> None:
        super().__init__("; ".join(conflicts))
        self.conflicts = conflicts


class EvidenceConflictError(RuntimeError):
    """The requested evidence action contradicts its current state (e.g. it is linked)."""


def _evidence_identity(
    sender: str | None, metadata: dict[str, Any] | None
) -> dict[str, str | None]:
    """Derived evidence identity columns (revision 0003) from sender and parser metadata."""
    parser = (metadata or {}).get("parser")
    job_url = parser.get("job_url") if isinstance(parser, dict) else None
    url = canonical_job_url(job_url) if isinstance(job_url, str) else None
    extracted = external_job_id_from_url(url)
    domain = sender_domain(sender)
    return {
        "sender_address": normalize_email_address(sender) or None,
        "sender_domain": registrable_domain(domain) or None if domain else None,
        "canonical_job_url": url,
        "external_job_id": extracted[1] if extracted else None,
    }


def _check_choice(value: str | None, allowed: type[enum.StrEnum], field_name: str) -> None:
    if value is not None and value not in {member.value for member in allowed}:
        raise ValueError(f"Invalid {field_name}: {value!r}")


@dataclass
class ApplicationFilter:
    status: ApplicationStatus | None = None
    source_portal: str | None = None
    application_method: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    search: str | None = None  # matches company or role (case-insensitive)
    is_stale: bool | None = None
    interviewed: bool | None = None
    outcome: str | None = None
    include_merged: bool = False  # administrative: also return soft-merged records
    page: int = 1
    page_size: int = 50


class DataStore:
    def __init__(self, db_path: Path = DB_PATH, schema_policy: SchemaPolicy | None = None) -> None:
        """Open the database and apply the schema policy (backend/db/schema.py).

        Schema changes happen only through Alembic revisions. An empty database is created
        at head; an outdated one is upgraded (with a backup first) under the AUTO policy or
        rejected with SchemaOutdatedError under VERIFY (production). Runtime data
        maintenance below runs only once the schema is current.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self._engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.schema_status: SchemaStatus = schema.prepare(
            self._engine, db_path, schema_policy or schema.default_policy()
        )
        if self.schema_status.is_current:
            self._ensure_poller_state()
            self._backfill_thread_id_index()
            self._backfill_application_events()
            self._backfill_identity_fields()

    def upgrade_schema(self) -> SchemaStatus:
        """Apply pending Alembic revisions now (operator/maintenance use)."""
        self.schema_status = schema.upgrade(self._engine)
        return self.schema_status

    def stamp_schema(self, revision: str) -> SchemaStatus:
        """Record an older revision without changing schema objects (operator rollback)."""
        self.schema_status = schema.stamp(self._engine, revision)
        return self.schema_status

    def close(self) -> None:
        """Dispose of pooled connections (lets backup/restore tooling release the file)."""
        self._engine.dispose()

    def _backfill_application_events(self) -> None:
        """Create idempotent milestone events from existing status history."""
        mapping = {
            ApplicationStatus.APPLIED.value: ApplicationEventType.APPLICATION_SUBMITTED,
            ApplicationStatus.RESUME_SHORTLISTED.value: ApplicationEventType.RECRUITER_RESPONSE,
            ApplicationStatus.INTERVIEW_SCHEDULED.value: ApplicationEventType.INTERVIEW_SCHEDULED,
            ApplicationStatus.INTERVIEW_IN_PROGRESS.value: ApplicationEventType.INTERVIEW_ATTENDED,
            ApplicationStatus.OFFER_NEGOTIATION.value: ApplicationEventType.OFFER_RECEIVED,
            ApplicationStatus.OFFER.value: ApplicationEventType.OFFER_RECEIVED,
            ApplicationStatus.JOINED.value: ApplicationEventType.OFFER_RECEIVED,
            ApplicationStatus.REJECTED.value: ApplicationEventType.REJECTED,
        }
        with Session(self._engine) as session:
            existing_history_ids = set(
                session.exec(
                    select(ApplicationEvent.status_history_id).where(
                        col(ApplicationEvent.status_history_id).is_not(None)
                    )
                ).all()
            )
            for history in session.exec(select(StatusHistory)).all():
                if history.id in existing_history_ids or history.to_status not in mapping:
                    continue
                session.add(
                    ApplicationEvent(
                        application_id=history.application_id,
                        event_type=mapping[history.to_status],
                        occurred_at=history.changed_at,
                        source="backfill",
                        source_message_id=history.message_id,
                        status_history_id=history.id,
                    )
                )
            session.commit()

    def _backfill_thread_id_index(self) -> None:
        """One-time backfill for DBs created before ApplicationThreadId existed — populates
        it from each Application's thread_ids JSON blob. No-op once every row is indexed
        (checked via a cheap count comparison, not re-parsing JSON on every startup).

        Selects only (id, thread_ids) — not the full Application entity — so a legacy DB
        with corrupted current_status data (enum NAMES instead of values, the exact case
        backend.diagnostics's enum check exists to catch) can't make DataStore's own
        constructor crash via the ORM's enum coercion on load.
        """
        with Session(self._engine) as session:
            app_count = session.exec(select(func.count()).select_from(Application)).one()
            indexed_app_count = session.exec(
                select(func.count(col(ApplicationThreadId.application_id).distinct()))
            ).one()
            if indexed_app_count >= app_count:
                return
            rows = session.exec(select(Application.id, Application.thread_ids)).all()
            for app_id, thread_ids_json in rows:
                self._sync_thread_ids(session, app_id, thread_ids_json)

    def _ensure_poller_state(self) -> None:
        with Session(self._engine) as session:
            if session.get(PollerState, 1) is None:
                session.add(PollerState(id=1))
                session.commit()

    # ------------------------------------------------------------------ #
    # Applications                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _apply_identity_fields(app: Application) -> None:
        """Derive the Phase 2 identity columns from company/role/job_url."""
        app.normalized_company = normalize_company(app.company) or None
        app.normalized_role = normalize_role(app.role) or None
        app.canonical_job_url = canonical_job_url(app.job_url)
        if app.external_job_id is None:
            extracted = external_job_id_from_url(app.canonical_job_url)
            app.external_job_id = extracted[1] if extracted else None

    def upsert_application(self, app: Application) -> Application:
        with Session(self._engine, expire_on_commit=False) as session:
            if app.id is None:
                self._apply_identity_fields(app)
                session.add(app)
                session.commit()
                session.refresh(app)
                self._sync_thread_ids(session, app.id, app.thread_ids)
                return app
            db_app = session.get(Application, app.id)
            if db_app is None:
                self._apply_identity_fields(app)
                session.add(app)
                session.commit()
                session.refresh(app)
                self._sync_thread_ids(session, app.id, app.thread_ids)
                return app
            # Update scalar fields only; relationships are left untouched.
            app.updated_at = utc_now()
            for field_name in Application.model_fields:
                # last_evidence_at is owned by the evidence operations; a caller holding an
                # older copy of the row must not roll it back.
                if field_name not in ("id", "created_at", "last_evidence_at"):
                    setattr(db_app, field_name, getattr(app, field_name))
            self._apply_identity_fields(db_app)
            session.commit()
            session.refresh(db_app)
            self._sync_thread_ids(session, db_app.id, db_app.thread_ids)
            return db_app

    # ------------------------------------------------------------------ #
    # Prospects                                                            #
    # ------------------------------------------------------------------ #

    def upsert_prospect(self, prospect: Prospect) -> tuple[Prospect, bool]:
        """Insert a prospect idempotently by Gmail message ID."""
        with Session(self._engine, expire_on_commit=False) as session:
            existing = session.exec(
                select(Prospect).where(Prospect.gmail_message_id == prospect.gmail_message_id)
            ).first()
            if existing is not None:
                return existing, False
            session.add(prospect)
            session.commit()
            session.refresh(prospect)
            return prospect, True

    def get_prospects(
        self, status: ProspectStatus | None = None, limit: int = 100
    ) -> list[Prospect]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(Prospect)
            if status is not None:
                stmt = stmt.where(Prospect.status == status)
            stmt = stmt.order_by(col(Prospect.received_at).desc()).limit(limit)
            return list(session.exec(stmt).all())

    def update_prospect_status(
        self,
        prospect_id: int,
        status: ProspectStatus,
        application_id: int | None = None,
    ) -> Prospect:
        with Session(self._engine, expire_on_commit=False) as session:
            prospect = session.get(Prospect, prospect_id)
            if prospect is None:
                raise ValueError(f"Prospect {prospect_id} not found")
            prospect.status = status
            if application_id is not None:
                if session.get(Application, application_id) is None:
                    raise ValueError(f"Application {application_id} not found")
                prospect.application_id = application_id
            prospect.updated_at = utc_now()
            session.add(prospect)
            session.commit()
            session.refresh(prospect)
            return prospect

    # ------------------------------------------------------------------ #
    # Application events                                                   #
    # ------------------------------------------------------------------ #

    def add_application_event(self, event: ApplicationEvent) -> ApplicationEvent:
        with Session(self._engine, expire_on_commit=False) as session:
            if session.get(Application, event.application_id) is None:
                raise ValueError(f"Application {event.application_id} not found")
            if event.status_history_id is not None:
                existing = session.exec(
                    select(ApplicationEvent).where(
                        ApplicationEvent.status_history_id == event.status_history_id
                    )
                ).first()
                if existing is not None:
                    return existing
            session.add(event)
            session.commit()
            session.refresh(event)
            return event

    def get_application_events(
        self, application_id: int, include_superseded: bool = False
    ) -> list[ApplicationEvent]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(ApplicationEvent)
                .where(ApplicationEvent.application_id == application_id)
                .order_by(col(ApplicationEvent.occurred_at), col(ApplicationEvent.id))
            )
            if not include_superseded:
                stmt = stmt.where(_live_event())
            return list(session.exec(stmt).all())

    def get_application_events_for_apps(self, app_ids: set[int]) -> list[ApplicationEvent]:
        if not app_ids:
            return []
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(ApplicationEvent).where(
                col(ApplicationEvent.application_id).in_(app_ids), _live_event()
            )
            return list(session.exec(stmt).all())

    def _sync_thread_ids(
        self, session: Session, application_id: int | None, thread_ids_json: str | None
    ) -> None:
        """Keep ApplicationThreadId in sync with Application.thread_ids (the JSON source
        of truth) — cheap since a job's thread count is always small (1-few).

        Takes the id/JSON scalars rather than an Application so callers can populate it
        (e.g. the startup backfill) via a column-scoped select that never touches the
        current_status column — see _backfill_thread_id_index for why that matters.
        """
        assert application_id is not None
        thread_ids: list[str] = json.loads(thread_ids_json or "[]")
        existing = {
            row.thread_id
            for row in session.exec(
                select(ApplicationThreadId).where(
                    ApplicationThreadId.application_id == application_id
                )
            ).all()
        }
        for thread_id in thread_ids:
            if thread_id not in existing:
                session.add(ApplicationThreadId(application_id=application_id, thread_id=thread_id))
        session.commit()

    def get_applications(self, filters: ApplicationFilter) -> tuple[list[Application], int]:
        with Session(self._engine, expire_on_commit=False) as session:
            conditions: list[ColumnElement[bool]] = []
            if not filters.include_merged:
                conditions.append(_active_app())
            if filters.status is not None:
                conditions.append(col(Application.current_status) == filters.status)
            if filters.source_portal is not None:
                conditions.append(col(Application.source_portal) == filters.source_portal)
            if filters.application_method is not None:
                conditions.append(col(Application.application_method) == filters.application_method)
            if filters.date_from is not None:
                conditions.append(col(Application.applied_date) >= filters.date_from)
            if filters.date_to is not None:
                conditions.append(col(Application.applied_date) <= filters.date_to)
            if filters.search is not None:
                term = f"%{filters.search}%"
                conditions.append(
                    or_(
                        col(Application.company).ilike(term),
                        col(Application.role).ilike(term),
                    )
                )
            if filters.is_stale is True:
                stale_cutoff = utc_now() - timedelta(days=STALE_DAYS_THRESHOLD)
                conditions.append(col(Application.current_status) == ApplicationStatus.APPLIED)
                conditions.append(col(Application.applied_date) < stale_cutoff)
            elif filters.is_stale is False:
                stale_cutoff = utc_now() - timedelta(days=STALE_DAYS_THRESHOLD)
                conditions.append(
                    ~(
                        (col(Application.current_status) == ApplicationStatus.APPLIED)
                        & (col(Application.applied_date) < stale_cutoff)
                    )
                )
            if filters.interviewed is not None:
                attended_ids = select(ApplicationEvent.application_id).where(
                    ApplicationEvent.event_type == ApplicationEventType.INTERVIEW_ATTENDED
                )
                if filters.interviewed:
                    conditions.append(col(Application.id).in_(attended_ids))
                else:
                    conditions.append(col(Application.id).notin_(attended_ids))
            if filters.outcome is not None:
                outcome_statuses = {
                    "offer": [ApplicationStatus.OFFER, ApplicationStatus.JOINED],
                    "rejected": [ApplicationStatus.REJECTED],
                    "withdrawn": [ApplicationStatus.WITHDRAWN],
                    "active": [
                        ApplicationStatus.APPLIED,
                        ApplicationStatus.RESUME_SHORTLISTED,
                        ApplicationStatus.INTERVIEW_SCHEDULED,
                        ApplicationStatus.INTERVIEW_IN_PROGRESS,
                        ApplicationStatus.OFFER_NEGOTIATION,
                    ],
                }
                statuses = outcome_statuses.get(filters.outcome.lower())
                if statuses:
                    conditions.append(col(Application.current_status).in_(statuses))

            base_stmt = select(Application)
            for cond in conditions:
                base_stmt = base_stmt.where(cond)

            count_stmt = select(func.count()).select_from(base_stmt.subquery())
            total: int = session.exec(count_stmt).one()

            offset = (filters.page - 1) * filters.page_size
            items_stmt = base_stmt.offset(offset).limit(filters.page_size)
            items = list(session.exec(items_stmt).all())

            return items, total

    def get_application_taxonomy(self) -> dict[str, list[str]]:
        """Return actual stored values so UI filters never drift from imported data."""
        with Session(self._engine) as session:
            sources = session.exec(
                select(col(Application.source_portal).distinct())
                .where(_active_app())
                .order_by(col(Application.source_portal))
            ).all()
            methods = session.exec(
                select(col(Application.application_method).distinct())
                .where(_active_app())
                .order_by(col(Application.application_method))
            ).all()
        return {
            "sources": [value for value in sources if value],
            "methods": [value for value in methods if value],
        }

    def get_application(self, id: int) -> Application | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(Application, id)

    def find_application_by_thread_id(self, thread_id: str) -> Application | None:
        """Return the Application that owns thread_id, or None.

        Indexed equality lookup via ApplicationThreadId — replaces an unindexed LIKE
        scan over the Application.thread_ids JSON blob, which got slower as the table grew.
        """
        with Session(self._engine, expire_on_commit=False) as session:
            link = session.exec(
                select(ApplicationThreadId).where(ApplicationThreadId.thread_id == thread_id)
            ).first()
            if link is None:
                return None
            app = session.get(Application, link.application_id)
            hops = 0
            # Thread links move with a merge; follow any older pointer to the active survivor.
            while (
                app is not None
                and app.record_state == RecordState.MERGED.value
                and app.merged_into_application_id is not None
                and hops < 10
            ):
                app = session.get(Application, app.merged_into_application_id)
                hops += 1
            if app is None or app.record_state != RecordState.ACTIVE.value:
                return None
            return app

    def get_applications_missing_fields(
        self, offset: int = 0, limit: int | None = None
    ) -> list[Application]:
        """Return non-false-positive applications where company or role is NULL.

        Ordered by id so repeated paginated calls (offset += limit) see a stable
        sequence even as fields get filled in between calls.
        """
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(Application)
                .where(col(Application.is_false_positive).is_(False), _active_app())
                .where(
                    or_(
                        col(Application.company).is_(None),
                        col(Application.role).is_(None),
                    )
                )
                .order_by(col(Application.id))
                .offset(offset)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(session.exec(stmt).all())

    def count_applications_missing_fields(self) -> int:
        with Session(self._engine) as session:
            stmt = (
                select(func.count())
                .select_from(Application)
                .where(col(Application.is_false_positive).is_(False), _active_app())
                .where(
                    or_(
                        col(Application.company).is_(None),
                        col(Application.role).is_(None),
                    )
                )
            )
            return session.exec(stmt).one()

    def find_application_by_company_role(self, company: str, role: str) -> Application | None:
        """Return the most-recent non-false-positive Application matching company+role
        (case-insensitive)."""
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(Application)
                .where(Application.is_false_positive == False, _active_app())  # noqa: E712
                .where(func.lower(Application.company) == company.lower())
                .where(func.lower(Application.role) == role.lower())
                .order_by(col(Application.created_at).desc())
            )
            return session.exec(stmt).first()

    def find_active_applications_by_companies(self, company_names: list[str]) -> list[Application]:
        """Return non-Withdrawn, non-false-positive applications whose company matches any
        name (case-insensitive)."""
        terminal = {
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.REJECTED,
            ApplicationStatus.JOINED,
            ApplicationStatus.OFFER,
        }
        with Session(self._engine, expire_on_commit=False) as session:
            conditions = [
                col(Application.company).ilike(name) for name in company_names if name.strip()
            ]
            if not conditions:
                return []
            stmt = (
                select(Application)
                .where(col(Application.is_false_positive).is_(False), _active_app())
                .where(col(Application.current_status).notin_([s.value for s in terminal]))
                .where(or_(*conditions))
            )
            return list(session.exec(stmt).all())

    def delete_application(self, id: int) -> bool:
        with Session(self._engine) as session:
            app = session.get(Application, id)
            if app is None:
                return False
            if (
                app.record_state == RecordState.MERGED.value
                or session.exec(
                    select(Application.id).where(Application.merged_into_application_id == id)
                ).first()
                is not None
            ):
                raise MergeError(
                    f"Application {id} is part of a merge; undo the merge before deleting it"
                )
            # Delete dependent rows first; the FKs are NOT NULL so SQLAlchemy cannot
            # null them out via its default orphan strategy.
            for history_row in session.exec(
                select(StatusHistory).where(StatusHistory.application_id == id)
            ).all():
                session.delete(history_row)
            for thread_row in session.exec(
                select(ApplicationThreadId).where(ApplicationThreadId.application_id == id)
            ).all():
                session.delete(thread_row)
            for event_row in session.exec(
                select(ApplicationEvent).where(ApplicationEvent.application_id == id)
            ).all():
                session.delete(event_row)
            for prospect in session.exec(
                select(Prospect).where(Prospect.application_id == id)
            ).all():
                prospect.application_id = None
                session.add(prospect)
            self._detach_evidence(session, [id], reason="application_deleted")
            # Flush child deletes before deleting the parent — SQLAlchemy's unit-of-work
            # dependency sort doesn't reliably order these plain foreign_key=... columns
            # (no relationship()) ahead of the parent delete in the same flush, which trips
            # SQLite's FK enforcement (PRAGMA foreign_keys=ON, set per-connection above).
            session.flush()
            session.delete(app)
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Merges (revision 0004) — docs/phase-2-identity-resolution.md §12     #
    # ------------------------------------------------------------------ #

    _CHILD_MODELS: tuple[tuple[str, type[SQLModel]], ...] = (
        ("evidence", Evidence),
        ("status_history", StatusHistory),
        ("events", ApplicationEvent),
        ("thread_links", ApplicationThreadId),
        ("prospects", Prospect),
    )

    def load_merge_state(self, application_ids: list[int]) -> dict[str, Any]:
        """Read-only snapshot of everything a merge of these applications touches, plus its
        state token. Used by the preview; never modifies data."""
        with Session(self._engine) as session:
            return self._load_merge_state(session, sorted(set(application_ids)))

    @staticmethod
    def _load_merge_state(session: Session, ids: list[int]) -> dict[str, Any]:
        app_table = _table(Application)
        rows = session.execute(core_select(app_table).where(app_table.c.id.in_(ids))).mappings()
        applications = {
            str(row["id"]): {k: merge_snapshot.serialize_value(v) for k, v in row.items()}
            for row in rows
        }
        ev = _table(Evidence).c
        sh = _table(StatusHistory).c
        ae = _table(ApplicationEvent).c
        tl = _table(ApplicationThreadId).c
        pr = _table(Prospect).c

        def fetch(columns: list[Any], app_column: Any, order: Any) -> list[dict[str, Any]]:
            result = session.execute(
                core_select(*columns).where(app_column.in_(ids)).order_by(order)
            ).mappings()
            return [{k: merge_snapshot.serialize_value(v) for k, v in r.items()} for r in result]

        state: dict[str, Any] = {
            "version": merge_snapshot.SNAPSHOT_VERSION,
            "application_ids": ids,
            "applications": applications,
            "evidence": fetch(
                [
                    ev.id,
                    ev.application_id,
                    ev.link_method,
                    ev.link_confidence,
                    ev.processing_status,
                    ev.decided_by,
                    ev.occurred_at,
                ],
                ev.application_id,
                ev.id,
            ),
            "status_history": fetch(
                [
                    sh.id,
                    sh.application_id,
                    sh.from_status,
                    sh.to_status,
                    sh.trigger,
                    sh.changed_at,
                    sh.superseded_by_merge_id,
                ],
                sh.application_id,
                sh.id,
            ),
            "events": fetch(
                [
                    ae.id,
                    ae.application_id,
                    ae.event_type,
                    ae.interview_round,
                    ae.occurred_at,
                    ae.status_history_id,
                    ae.superseded_by_merge_id,
                ],
                ae.application_id,
                ae.id,
            ),
            "thread_links": fetch(
                [tl.id, tl.application_id, tl.thread_id], tl.application_id, tl.id
            ),
            "prospects": fetch([pr.id, pr.application_id], pr.application_id, pr.id),
        }
        state["token"] = merge_snapshot.state_token(state)
        return state

    @staticmethod
    def _coerce_field(name: str, value: Any) -> Any:
        if name == "applied_date":
            return merge_snapshot.parse_datetime(value)
        if name == "current_status":
            return ApplicationStatus(value)
        if name == "is_false_positive":
            return bool(value)
        return value

    def execute_merge(
        self,
        *,
        application_ids: list[int],
        survivor_id: int,
        field_values: dict[str, Any],
        expected_token: str,
        idempotency_key: str,
        initiated_by: str | None = None,
        reason: str | None = None,
    ) -> tuple[MergeOperation, bool]:
        """Merge applications into the survivor in one transaction. Returns (operation,
        created). Nothing is deleted: source applications become `merged`, their evidence,
        history, events, thread links and prospects move to the survivor, and duplicated
        history/events are flagged superseded. Raises MergeStaleError when anything changed
        since the preview, MergeInvalidError for bad input."""
        ids = sorted(set(application_ids))
        if len(ids) < 2 or survivor_id not in ids or len(ids) != len(application_ids):
            raise MergeInvalidError(
                "Provide two or more distinct applications, including the survivor"
            )
        unknown = set(field_values) - set(merge_snapshot.MERGEABLE_FIELDS)
        if unknown:
            raise MergeInvalidError(f"Fields cannot be merged: {sorted(unknown)}")
        sources = [i for i in ids if i != survivor_id]
        app_table = _table(Application)
        with Session(self._engine, expire_on_commit=False) as session:
            existing = session.exec(
                select(MergeOperation).where(MergeOperation.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                same = (
                    existing.survivor_application_id == survivor_id
                    and sorted(existing.source_application_ids) == sources
                )
                if not same:
                    raise MergeInvalidError("Idempotency key was already used for another merge")
                return existing, False

            # Take SQLite's write lock before reading, so concurrent merges serialize and the
            # second one validates against the first one's committed result.
            session.execute(
                update(app_table).where(app_table.c.id == survivor_id).values(id=app_table.c.id)
            )
            state = self._load_merge_state(session, ids)
            if len(state["applications"]) != len(ids):
                missing = sorted(set(ids) - {int(k) for k in state["applications"]})
                raise MergeInvalidError(f"Applications not found: {missing}")
            for app_id, row in state["applications"].items():
                if row.get("record_state") != RecordState.ACTIVE.value:
                    raise MergeStaleError(f"Application {app_id} is already merged")
            if state["token"] != expected_token:
                raise MergeStaleError("These applications changed since the preview")

            snapshot = {k: v for k, v in state.items() if k != "token"}
            operation = MergeOperation(
                survivor_application_id=survivor_id,
                source_application_ids=sources,
                snapshot=snapshot,
                snapshot_checksum=merge_snapshot.checksum(snapshot),
                result={},
                field_values={
                    k: merge_snapshot.serialize_value(v) for k, v in field_values.items()
                },
                preview_token=expected_token,
                idempotency_key=idempotency_key,
                initiated_by=initiated_by,
                reason=reason,
            )
            session.add(operation)
            session.flush()
            assert operation.id is not None
            now = utc_now()

            moved: dict[str, list[dict[str, int]]] = {kind: [] for kind, _ in self._CHILD_MODELS}
            for kind, model in self._CHILD_MODELS:
                model_col = getattr(model, "application_id")
                for child in session.exec(select(model).where(col(model_col).in_(sources))).all():
                    moved[kind].append({"id": child.id, "from": child.application_id})  # type: ignore[attr-defined]
                    child.application_id = survivor_id  # type: ignore[attr-defined]
                    session.add(child)
            session.flush()

            history = [
                {
                    "id": h.id,
                    "from_status": h.from_status,
                    "to_status": h.to_status,
                    "changed_at": merge_snapshot.serialize_value(h.changed_at),
                }
                for h in session.exec(
                    select(StatusHistory).where(
                        StatusHistory.application_id == survivor_id, _live_history()
                    )
                ).all()
            ]
            superseded_history = merge_snapshot.superseded_history_ids(history)
            events = [
                {
                    "id": e.id,
                    "event_type": merge_snapshot.serialize_value(e.event_type),
                    "interview_round": merge_snapshot.serialize_value(e.interview_round),
                    "occurred_at": merge_snapshot.serialize_value(e.occurred_at),
                    "status_history_id": e.status_history_id,
                }
                for e in session.exec(
                    select(ApplicationEvent).where(
                        ApplicationEvent.application_id == survivor_id, _live_event()
                    )
                ).all()
            ]
            superseded_events = merge_snapshot.superseded_event_ids(events, set(superseded_history))
            if superseded_history:
                session.execute(
                    update(_table(StatusHistory))
                    .where(_table(StatusHistory).c.id.in_(superseded_history))
                    .values(superseded_by_merge_id=operation.id)
                )
            if superseded_events:
                session.execute(
                    update(_table(ApplicationEvent))
                    .where(_table(ApplicationEvent).c.id.in_(superseded_events))
                    .values(superseded_by_merge_id=operation.id)
                )

            survivor = session.get(Application, survivor_id)
            assert survivor is not None
            for name, value in field_values.items():
                setattr(survivor, name, self._coerce_field(name, value))
            threads: list[str] = []
            for app_id in [survivor_id, *sources]:
                for thread in json.loads(
                    state["applications"][str(app_id)].get("thread_ids") or "[]"
                ):
                    if thread not in threads:
                        threads.append(thread)
            survivor.thread_ids = json.dumps(threads)
            survivor.external_job_id = field_values.get("external_job_id", survivor.external_job_id)
            self._apply_identity_fields(survivor)
            survivor.updated_at = now
            session.add(survivor)
            for source_id in sources:
                source = session.get(Application, source_id)
                assert source is not None
                source.record_state = RecordState.MERGED.value
                source.merged_into_application_id = survivor_id
                source.merge_operation_id = operation.id
                source.merged_at = now
                source.updated_at = now
                session.add(source)
            session.flush()
            for app_id in ids:
                self._refresh_last_evidence_at(session, app_id)
            session.flush()

            after = self._load_merge_state(session, ids)
            operation.result = {
                "moved": moved,
                "superseded": {"status_history": superseded_history, "events": superseded_events},
                "after": {
                    "survivor": {
                        name: after["applications"][str(survivor_id)].get(name)
                        for name in (*merge_snapshot.MERGEABLE_FIELDS, "thread_ids")
                    },
                    "sources": {
                        str(i): after["applications"][str(i)].get("updated_at") for i in sources
                    },
                },
                "counts": {kind: len(rows) for kind, rows in moved.items()},
            }
            session.add(operation)
            session.commit()
            session.refresh(operation)
            log.info(
                "applications_merged",
                operation_id=operation.id,
                survivor_id=survivor_id,
                sources=len(sources),
            )
            return operation, True

    def undo_merge(
        self, operation_id: int, *, undone_by: str | None = None
    ) -> tuple[MergeOperation, bool]:
        """Restore every involved application and relationship from the merge snapshot in
        one transaction. Returns (operation, undone_now); repeating an undo is a no-op.
        Raises MergeUndoConflictError, changing nothing, when later edits would be lost."""
        app_table = _table(Application)
        with Session(self._engine, expire_on_commit=False) as session:
            operation = session.get(MergeOperation, operation_id)
            if operation is None:
                raise MergeNotFoundError(f"Merge operation {operation_id} not found")
            if operation.undone_at is not None:
                return operation, False
            survivor_id = operation.survivor_application_id
            session.execute(
                update(app_table).where(app_table.c.id == survivor_id).values(id=app_table.c.id)
            )
            snapshot = merge_snapshot.validate_snapshot(
                operation.snapshot, operation.snapshot_checksum
            )
            result = operation.result or {}
            conflicts: list[str] = []

            survivor = session.get(Application, survivor_id)
            if survivor is None:
                conflicts.append(f"Survivor application {survivor_id} no longer exists.")
            elif survivor.record_state != RecordState.ACTIVE.value:
                conflicts.append(
                    f"Application {survivor_id} was merged again later (operation "
                    f"{survivor.merge_operation_id}); undo that merge first."
                )
            else:
                current = self._load_merge_state(session, [survivor_id])["applications"][
                    str(survivor_id)
                ]
                for name, value in (result.get("after", {}).get("survivor") or {}).items():
                    if current.get(name) != value:
                        conflicts.append(
                            f"Application {survivor_id} {name.replace('_', ' ')} was edited after "
                            "the merge; undoing would overwrite that change."
                        )
            for source_id in operation.source_application_ids:
                source = session.get(Application, source_id)
                if (
                    source is None
                    or source.record_state != RecordState.MERGED.value
                    or source.merge_operation_id != operation.id
                ):
                    conflicts.append(f"Application {source_id} is no longer part of this merge.")
            for kind, model in self._CHILD_MODELS:
                for item in (result.get("moved") or {}).get(kind, []):
                    child = session.get(model, item["id"])
                    label = kind.replace("_", " ")
                    if child is None:
                        conflicts.append(f"A moved {label} record ({item['id']}) no longer exists.")
                    elif child.application_id != survivor_id:  # type: ignore[attr-defined]
                        conflicts.append(
                            f"A moved {label} record ({item['id']}) was re-linked to application "
                            f"{child.application_id} after the merge."  # type: ignore[attr-defined]
                        )
            if conflicts:
                raise MergeUndoConflictError(conflicts)

            for kind, model in self._CHILD_MODELS:
                for item in (result.get("moved") or {}).get(kind, []):
                    child = session.get(model, item["id"])
                    child.application_id = item["from"]  # type: ignore[union-attr]
                    session.add(child)
            for model in (StatusHistory, ApplicationEvent):
                table = _table(model)
                session.execute(
                    update(table)
                    .where(table.c.superseded_by_merge_id == operation.id)
                    .values(superseded_by_merge_id=None)
                )
            session.flush()

            for app_id, row in snapshot["applications"].items():
                values = {
                    column.name: self._restore_value(column, row.get(column.name))
                    for column in app_table.columns
                    if column.name != "id"
                }
                session.execute(
                    update(app_table).where(app_table.c.id == int(app_id)).values(**values)
                )
            session.flush()
            for app_id in snapshot["application_ids"]:
                self._refresh_last_evidence_at(session, app_id)

            snapshot_ids = {
                kind: {r["id"] for r in snapshot[kind]} for kind, _ in self._CHILD_MODELS
            }
            kept: dict[str, list[int]] = {}
            for kind, model in self._CHILD_MODELS:
                model_col = getattr(model, "application_id")
                later = [
                    c.id  # type: ignore[attr-defined]
                    for c in session.exec(select(model).where(model_col == survivor_id)).all()
                    if c.id not in snapshot_ids[kind]  # type: ignore[attr-defined]
                ]
                if later:
                    kept[kind] = later
            operation.undone_at = utc_now()
            operation.undone_by = undone_by
            operation.undo_metadata = {
                "restored_applications": snapshot["application_ids"],
                "kept_with_survivor": kept,
            }
            session.add(operation)
            session.commit()
            session.refresh(operation)
            log.info("merge_undone", operation_id=operation.id)
            return operation, True

    @staticmethod
    def _restore_value(column: Any, value: Any) -> Any:
        if value is None:
            return None
        if column.name == "current_status":
            return ApplicationStatus(value)
        if isinstance(column.type, UTCDateTime) or column.name.endswith(("_at", "_date")):
            return merge_snapshot.parse_datetime(value)
        return value

    def list_merge_operations(
        self, page: int = 1, page_size: int = 50
    ) -> tuple[list[MergeOperation], int]:
        with Session(self._engine, expire_on_commit=False) as session:
            total = session.exec(select(func.count()).select_from(MergeOperation)).one()
            items = session.exec(
                select(MergeOperation)
                .order_by(col(MergeOperation.created_at).desc(), col(MergeOperation.id).desc())
                .offset((max(page, 1) - 1) * page_size)
                .limit(page_size)
            ).all()
            return list(items), total

    def find_merge_by_idempotency_key(self, key: str) -> MergeOperation | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.exec(
                select(MergeOperation).where(MergeOperation.idempotency_key == key)
            ).first()

    def has_merged_sources(self, application_id: int) -> bool:
        with Session(self._engine) as session:
            return (
                session.exec(
                    select(Application.id).where(
                        Application.merged_into_application_id == application_id
                    )
                ).first()
                is not None
            )

    def get_merge_operation(self, operation_id: int) -> MergeOperation | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(MergeOperation, operation_id)

    @staticmethod
    def pair_key(first: int, second: int) -> str:
        low, high = sorted((first, second))
        return f"{low}:{high}"

    def dismiss_duplicate(
        self, first: int, second: int, *, dismissed_by: str | None = None
    ) -> DuplicateDismissal:
        """Record that two applications are not duplicates (advisory; idempotent)."""
        if first == second:
            raise ValueError("A duplicate pair needs two different applications")
        key = self.pair_key(first, second)
        statement = (
            sqlite_insert(_table(DuplicateDismissal))
            .values(pair_key=key, dismissed_by=dismissed_by, created_at=utc_now())
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            session.execute(statement)
            session.commit()
            row = session.exec(
                select(DuplicateDismissal).where(DuplicateDismissal.pair_key == key)
            ).one()
            return row

    def dismissed_pair_keys(self) -> set[str]:
        with Session(self._engine) as session:
            return set(session.exec(select(DuplicateDismissal.pair_key)).all())

    # ------------------------------------------------------------------ #
    # Status history                                                       #
    # ------------------------------------------------------------------ #

    def append_status_history(
        self,
        application_id: int,
        from_status: str | None,
        to_status: str,
        trigger: str,
        message_id: str | None = None,
    ) -> StatusHistory:
        with Session(self._engine, expire_on_commit=False) as session:
            entry = StatusHistory(
                application_id=application_id,
                from_status=from_status,
                to_status=to_status,
                trigger=trigger,
                message_id=message_id,
            )
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return entry

    def get_status_history(
        self, application_id: int, include_superseded: bool = False
    ) -> list[StatusHistory]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(StatusHistory)
                .where(StatusHistory.application_id == application_id)
                .order_by(col(StatusHistory.changed_at), col(StatusHistory.id))
            )
            if not include_superseded:
                stmt = stmt.where(_live_history())
            return list(session.exec(stmt).all())

    # ------------------------------------------------------------------ #
    # Diagnostics / maintenance                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def inspect_schema_tables(db_path: Path) -> set[str]:
        """Return table names actually present on disk, without creating missing ones.

        Deliberately bypasses DataStore's normal constructor — that applies the schema
        policy, which could create the very tables this check exists to detect. Uses a
        throwaway read-only engine for inspection only.
        """
        engine = schema.readonly_engine(db_path)
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    # ------------------------------------------------------------------ #
    # Evidence (Phase 2)                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_evidence(evidence: Evidence) -> None:
        _check_choice(evidence.evidence_type, EvidenceType, "evidence_type")
        _check_choice(evidence.source, EvidenceSource, "source")
        _check_choice(evidence.processing_status, EvidenceStatus, "processing_status")
        _check_choice(evidence.link_method, LinkMethod, "link_method")
        if evidence.link_confidence is not None and not 0 <= evidence.link_confidence <= 1:
            raise ValueError("link_confidence must be between 0 and 1")

    def insert_evidence(self, evidence: Evidence) -> tuple[Evidence, bool]:
        """Insert evidence idempotently. Returns (stored row, created).

        A unique content fingerprint and a unique (source, external_id) pair are enforced by
        the database; the insert is ``INSERT ... ON CONFLICT DO NOTHING`` followed by a read,
        so concurrent writers of the same evidence end up with one row and both receive it.
        """
        self._validate_evidence(evidence)
        if evidence.snippet:
            evidence.snippet = evidence.snippet[:EVIDENCE_SNIPPET_MAX_CHARS]
        if evidence.normalized_subject is None:
            evidence.normalized_subject = normalize_subject(evidence.subject)
        for key, value in _evidence_identity(evidence.sender, evidence.raw_metadata).items():
            if getattr(evidence, key) is None:
                setattr(evidence, key, value)
        if not evidence.content_fingerprint:
            evidence.content_fingerprint = evidence_fingerprint(
                evidence_type=evidence.evidence_type,
                source=evidence.source,
                external_id=evidence.external_id,
                thread_id=evidence.thread_id,
                sender=evidence.sender,
                recipient=evidence.recipient,
                subject=evidence.subject,
                occurred_at=evidence.occurred_at,
                snippet=evidence.snippet,
            )
        values = evidence.model_dump(exclude={"id"})
        statement = sqlite_insert(_table(Evidence)).values(**values).on_conflict_do_nothing()
        with Session(self._engine, expire_on_commit=False) as session:
            created = session.execute(statement).rowcount == 1  # type: ignore[attr-defined]
            session.commit()
            stored = session.exec(
                select(Evidence).where(Evidence.content_fingerprint == evidence.content_fingerprint)
            ).first()
            if stored is None and evidence.external_id:
                # Same (source, external_id) already stored under another evidence_type.
                stored = session.exec(
                    select(Evidence).where(
                        Evidence.source == evidence.source,
                        Evidence.external_id == evidence.external_id,
                    )
                ).first()
            if stored is None:
                raise RuntimeError("Evidence insert neither created nor found a row")
            return stored, created

    def get_evidence(self, evidence_id: int) -> Evidence | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(Evidence, evidence_id)

    def get_evidence_by_external_id(self, source: str, external_id: str) -> Evidence | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.exec(
                select(Evidence).where(
                    Evidence.source == source, Evidence.external_id == external_id
                )
            ).first()

    def list_evidence(self, filters: EvidenceFilter) -> tuple[list[Evidence], int]:
        """Evidence newest first, with the total matching count for pagination."""
        conditions: list[ColumnElement[bool]] = []
        if filters.linked is True:
            conditions.append(col(Evidence.application_id).is_not(None))
        elif filters.linked is False:
            conditions.append(col(Evidence.application_id).is_(None))
        if filters.source:
            conditions.append(col(Evidence.source) == filters.source)
        if filters.evidence_type:
            conditions.append(col(Evidence.evidence_type) == filters.evidence_type)
        if filters.processing_status:
            conditions.append(col(Evidence.processing_status) == filters.processing_status)
        elif filters.statuses:
            conditions.append(col(Evidence.processing_status).in_(filters.statuses))
        elif not filters.include_ignored:
            conditions.append(col(Evidence.processing_status) != EvidenceStatus.IGNORED.value)
        if filters.date_from:
            conditions.append(col(Evidence.occurred_at) >= filters.date_from)
        if filters.date_to:
            conditions.append(col(Evidence.occurred_at) <= filters.date_to)
        if filters.application_id is not None:
            conditions.append(col(Evidence.application_id) == filters.application_id)
        with Session(self._engine, expire_on_commit=False) as session:
            total = session.exec(
                select(func.count()).select_from(Evidence).where(*conditions)
            ).one()
            items = session.exec(
                select(Evidence)
                .where(*conditions)
                .order_by(col(Evidence.occurred_at).desc(), col(Evidence.id).desc())
                .offset((max(filters.page, 1) - 1) * filters.page_size)
                .limit(filters.page_size)
            ).all()
            return list(items), total

    def get_evidence_for_application(self, application_id: int) -> list[Evidence]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(Evidence)
                    .where(Evidence.application_id == application_id)
                    .order_by(col(Evidence.occurred_at), col(Evidence.id))
                ).all()
            )

    def count_evidence(self) -> dict[str, int]:
        """total excludes `ignored` non-job mail; unlinked = not attached to an application
        (and not ignored); needs_review = explicitly waiting for a decision."""
        not_ignored = col(Evidence.processing_status) != EvidenceStatus.IGNORED.value
        with Session(self._engine) as session:
            total = session.exec(
                select(func.count()).select_from(Evidence).where(not_ignored)
            ).one()
            unlinked = session.exec(
                select(func.count())
                .select_from(Evidence)
                .where(not_ignored, col(Evidence.application_id).is_(None))
            ).one()
            needs_review = session.exec(
                select(func.count())
                .select_from(Evidence)
                .where(Evidence.processing_status == EvidenceStatus.NEEDS_REVIEW.value)
            ).one()
        return {"total": total, "unlinked": unlinked, "needs_review": needs_review}

    def link_evidence(
        self,
        evidence_id: int,
        application_id: int,
        method: str,
        confidence: float,
        status: str = EvidenceStatus.LINKED.value,
        decided_by: str = DecisionSource.RESOLVER.value,
    ) -> Evidence:
        """Attach evidence to an application in one transaction, refreshing
        last_evidence_at on both the previous and the new application. An automated link
        never replaces a human decision."""
        _check_choice(method, LinkMethod, "link_method")
        _check_choice(status, EvidenceStatus, "processing_status")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            if session.get(Application, application_id) is None:
                raise ApplicationNotFoundError(f"Application {application_id} not found")
            if (
                decided_by != DecisionSource.HUMAN.value
                and evidence.decided_by == DecisionSource.HUMAN.value
            ):
                return evidence
            previous = evidence.application_id
            evidence.application_id = application_id
            evidence.link_method = method
            evidence.link_confidence = confidence
            evidence.processing_status = status
            evidence.review_reason = None
            evidence.decided_by = decided_by
            evidence.decided_at = utc_now()
            evidence.updated_at = utc_now()
            session.add(evidence)
            session.flush()
            for app_id in {previous, application_id} - {None}:
                self._refresh_last_evidence_at(session, app_id)  # type: ignore[arg-type]
            session.commit()
            session.refresh(evidence)
            return evidence

    def unlink_evidence(self, evidence_id: int, reason: str = "manually_unlinked") -> Evidence:
        """Detach evidence (it returns to needs_review). Status changes it caused stay in
        status history; correct those with a manual status update if needed."""
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            previous = evidence.application_id
            evidence.application_id = None
            evidence.link_method = None
            evidence.link_confidence = None
            evidence.processing_status = EvidenceStatus.NEEDS_REVIEW.value
            evidence.review_reason = reason
            evidence.decided_by = DecisionSource.HUMAN.value
            evidence.decided_at = utc_now()
            evidence.updated_at = utc_now()
            session.add(evidence)
            session.flush()
            if previous is not None:
                self._refresh_last_evidence_at(session, previous)
            session.commit()
            session.refresh(evidence)
            return evidence

    def update_evidence_processing(
        self,
        evidence_id: int,
        status: str,
        *,
        review_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        """Record a processing result; metadata keys are merged into raw_metadata."""
        _check_choice(status, EvidenceStatus, "processing_status")
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            evidence.processing_status = status
            evidence.review_reason = review_reason
            if metadata:
                evidence.raw_metadata = {**(evidence.raw_metadata or {}), **metadata}
            evidence.updated_at = utc_now()
            session.add(evidence)
            session.commit()
            session.refresh(evidence)
            return evidence

    def update_evidence_details(
        self,
        evidence_id: int,
        *,
        sender: str | None,
        subject: str | None,
        snippet: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        """Fill in descriptive fields once an item turns out to be job-related. Only for
        evidence identified by an external ID, whose fingerprint does not depend on them."""
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            if not evidence.external_id:
                raise ValueError("Details of fingerprint-identified evidence are immutable")
            evidence.sender = sender
            evidence.subject = subject
            evidence.normalized_subject = normalize_subject(subject)
            evidence.snippet = snippet[:EVIDENCE_SNIPPET_MAX_CHARS] if snippet else None
            if metadata:
                evidence.raw_metadata = {**(evidence.raw_metadata or {}), **metadata}
            for key, value in _evidence_identity(sender, evidence.raw_metadata).items():
                setattr(evidence, key, value)
            evidence.updated_at = utc_now()
            session.add(evidence)
            session.commit()
            session.refresh(evidence)
            return evidence

    def recompute_last_evidence_at(self, application_id: int | None = None) -> int:
        """Set last_evidence_at from linked evidence (NULL when none) for one application
        or all of them. Safe to run any time; returns the number of rows written."""
        app_table = _table(Application)
        newest = (
            select(func.max(Evidence.occurred_at))
            .where(col(Evidence.application_id) == app_table.c.id)
            .scalar_subquery()
        )
        statement = update(app_table).values(last_evidence_at=newest)
        if application_id is not None:
            statement = statement.where(app_table.c.id == application_id)
        with Session(self._engine) as session:
            written = session.execute(statement).rowcount  # type: ignore[attr-defined]
            session.commit()
        return int(written or 0)

    @staticmethod
    def _newest_evidence_at(session: Session, application_id: int) -> datetime | None:
        return session.exec(
            select(func.max(Evidence.occurred_at)).where(Evidence.application_id == application_id)
        ).one()

    def _refresh_last_evidence_at(self, session: Session, application_id: int) -> None:
        app_table = _table(Application)
        session.execute(
            update(app_table)
            .where(app_table.c.id == application_id)
            .values(last_evidence_at=self._newest_evidence_at(session, application_id))
        )

    @staticmethod
    def _detach_evidence(session: Session, application_ids: list[int], reason: str) -> None:
        for evidence in session.exec(
            select(Evidence).where(col(Evidence.application_id).in_(application_ids))
        ).all():
            evidence.application_id = None
            evidence.link_method = None
            evidence.link_confidence = None
            evidence.processing_status = EvidenceStatus.NEEDS_REVIEW.value
            evidence.review_reason = reason
            evidence.decided_by = None  # the decision pointed at a record that no longer exists
            evidence.decided_at = None
            evidence.updated_at = utc_now()
            session.add(evidence)

    def _backfill_identity_fields(self) -> None:
        """Keep derived identity columns in step with the current normalization rules.

        Applications: normalized company/role and canonical URL are recomputed for every row
        and written only where they changed (rows from an older release or an older rule
        set); external_job_id is filled from the job URL only where it is NULL. Evidence:
        sender and job-URL signals are filled where NULL. Core columns only, so legacy enum
        data cannot break startup.
        """
        app_table = _table(Application)
        c = app_table.c
        ev_table = _table(Evidence)
        e = ev_table.c
        with Session(self._engine) as session:
            rows = session.execute(
                core_select(
                    c.id,
                    c.company,
                    c.role,
                    c.job_url,
                    c.normalized_company,
                    c.normalized_role,
                    c.canonical_job_url,
                    c.external_job_id,
                )
            ).all()
            for row in rows:
                url = canonical_job_url(row.job_url)
                wanted: dict[str, Any] = {
                    "normalized_company": normalize_company(row.company) or None,
                    "normalized_role": normalize_role(row.role) or None,
                    "canonical_job_url": url,
                }
                if row.external_job_id is None:
                    extracted = external_job_id_from_url(url)
                    if extracted:
                        wanted["external_job_id"] = extracted[1]
                changed = {k: v for k, v in wanted.items() if getattr(row, k) != v}
                if changed:
                    session.execute(update(app_table).where(c.id == row.id).values(**changed))

            pending = session.execute(
                select(e.id, e.sender, e.raw_metadata).where(
                    or_(
                        (e.sender_address.is_(None)) & (e.sender.is_not(None)),
                        e.canonical_job_url.is_(None),
                    )
                )
            ).all()
            for ev_id, sender, metadata in pending:
                derived = _evidence_identity(sender, metadata)
                values = {k: v for k, v in derived.items() if v is not None}
                if values:
                    session.execute(update(ev_table).where(e.id == ev_id).values(**values))
            session.commit()

    # ------------------------------------------------------------------ #
    # Identity resolution support (Phase 2, revision 0003)                 #
    # ------------------------------------------------------------------ #

    def get_applications_by_ids(
        self, ids: list[int], include_merged: bool = False
    ) -> list[Application]:
        if not ids:
            return []
        conditions: list[ColumnElement[bool]] = [col(Application.id).in_(ids)]
        if not include_merged:
            conditions.append(_active_app())
        with Session(self._engine, expire_on_commit=False) as session:
            return list(session.exec(select(Application).where(*conditions)).all())

    def find_applications_by_external_job_id(self, job_id: str, limit: int) -> list[Application]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(Application)
                    .where(Application.external_job_id == job_id, _active_app())
                    .order_by(col(Application.id))
                    .limit(limit)
                ).all()
            )

    def find_applications_by_canonical_url(self, url: str, limit: int) -> list[Application]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(Application)
                    .where(Application.canonical_job_url == url, _active_app())
                    .order_by(col(Application.id))
                    .limit(limit)
                ).all()
            )

    def find_applications_by_company(
        self, normalized_company: str, since: datetime | None, limit: int
    ) -> list[Application]:
        """Exact normalized company (indexed), most recent first."""
        conditions = [col(Application.normalized_company) == normalized_company, _active_app()]
        if since is not None:
            conditions.append(col(Application.applied_date) >= since)
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(Application)
                    .where(*conditions)
                    .order_by(col(Application.applied_date).desc(), col(Application.id))
                    .limit(limit)
                ).all()
            )

    def find_applications_by_company_prefix(
        self, first_token: str, since: datetime | None, limit: int
    ) -> list[Application]:
        """Companies whose normalized name starts with the same first word — an index range
        scan, used to surface near-variants ("acme" / "acme india") for scoring."""
        conditions = [
            _active_app(),
            col(Application.normalized_company) >= first_token,
            col(Application.normalized_company) < first_token + "\U0010ffff",
        ]
        if since is not None:
            conditions.append(col(Application.applied_date) >= since)
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(Application)
                    .where(*conditions)
                    .order_by(col(Application.applied_date).desc(), col(Application.id))
                    .limit(limit)
                ).all()
            )

    def find_linked_applications_by_sender(
        self, sender_address: str, limit: int
    ) -> list[tuple[int, str | None]]:
        """(application_id, decided_by) for evidence from this sender that is linked."""
        with Session(self._engine) as session:
            rows = session.exec(
                select(Evidence.application_id, Evidence.decided_by)
                .where(
                    Evidence.sender_address == sender_address,
                    col(Evidence.application_id).is_not(None),
                )
                .distinct()
                .limit(limit)
            ).all()
        return [(int(app_id), decided_by) for app_id, decided_by in rows if app_id is not None]

    def find_linked_applications_by_sender_domain(self, domain: str, limit: int) -> list[int]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(Evidence.application_id)
                .where(
                    Evidence.sender_domain == domain,
                    col(Evidence.application_id).is_not(None),
                )
                .distinct()
                .limit(limit)
            ).all()
        return [int(app_id) for app_id in rows if app_id is not None]

    def evidence_identities_for_applications(
        self, ids: list[int]
    ) -> dict[int, dict[str, set[str]]]:
        """Per application: sender domains, and sender addresses split by decision owner,
        seen on its linked evidence."""
        result: dict[int, dict[str, set[str]]] = {
            i: {"domains": set(), "senders_human": set(), "senders_resolver": set()} for i in ids
        }
        if not ids:
            return result
        e = _table(Evidence).c
        with Session(self._engine) as session:
            rows = session.execute(
                select(e.application_id, e.sender_domain, e.sender_address, e.decided_by).where(
                    e.application_id.in_(ids)
                )
            ).all()
        for app_id, domain, address, decided_by in rows:
            if app_id is None:
                continue
            bucket = result[int(app_id)]
            if domain:
                bucket["domains"].add(domain)
            if address:
                key = (
                    "senders_human"
                    if decided_by == DecisionSource.HUMAN.value
                    else "senders_resolver"
                )
                bucket[key].add(address)
        return result

    def claim_evidence(self, evidence_id: int) -> bool:
        """Atomically mark evidence as being processed. Returns False when another worker
        holds a fresh claim or a person owns the decision. A claim older than
        RESOLVER_PROCESSING_CLAIM_TTL_SECONDS (a crashed worker) can be taken over."""
        ev_table = _table(Evidence)
        e = ev_table.c
        now = utc_now()
        stale = now - timedelta(seconds=RESOLVER_PROCESSING_CLAIM_TTL_SECONDS)
        statement = (
            update(ev_table)
            .where(
                e.id == evidence_id,
                or_(e.decided_by.is_(None), e.decided_by != DecisionSource.HUMAN.value),
                or_(
                    e.processing_status != EvidenceStatus.PROCESSING.value,
                    e.updated_at < stale,
                ),
            )
            .values(processing_status=EvidenceStatus.PROCESSING.value, updated_at=now)
        )
        with Session(self._engine) as session:
            claimed = session.execute(statement).rowcount == 1  # type: ignore[attr-defined]
            session.commit()
        return claimed

    def record_resolution(
        self,
        evidence_id: int,
        *,
        resolution: dict[str, Any],
        status: str,
        review_reason: str | None = None,
        application_id: int | None = None,
        link_method: str | None = None,
        link_confidence: float | None = None,
    ) -> Evidence | None:
        """Persist an automated decision and its link in one transaction. Returns None —
        and changes nothing — when a person already owns the decision."""
        _check_choice(status, EvidenceStatus, "processing_status")
        _check_choice(link_method, LinkMethod, "link_method")
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            if evidence.decided_by == DecisionSource.HUMAN.value:
                return None
            if application_id is not None and session.get(Application, application_id) is None:
                raise ApplicationNotFoundError(f"Application {application_id} not found")
            previous = evidence.application_id
            now = utc_now()
            evidence.application_id = application_id
            evidence.link_method = link_method
            evidence.link_confidence = link_confidence
            evidence.processing_status = status
            evidence.review_reason = review_reason
            evidence.resolver_version = resolution.get("version")
            evidence.resolver_decision = resolution.get("outcome")
            evidence.resolver_confidence = resolution.get("confidence")
            evidence.resolver_result = resolution
            evidence.decided_by = DecisionSource.RESOLVER.value
            evidence.decided_at = now
            evidence.updated_at = now
            session.add(evidence)
            session.flush()
            for app_id in {previous, application_id} - {None}:
                self._refresh_last_evidence_at(session, app_id)  # type: ignore[arg-type]
            session.commit()
            session.refresh(evidence)
            return evidence

    def create_application_from_evidence(
        self,
        application: Application,
        evidence_id: int,
        *,
        decided_by: str,
        history_trigger: str,
        resolution: dict[str, Any] | None = None,
    ) -> Application:
        """Create an application at Applied, its first status-history entry and milestone,
        and link the evidence to it — all in one transaction (called by StatusUpdater)."""
        _check_choice(decided_by, DecisionSource, "decided_by")
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            if (
                decided_by == DecisionSource.RESOLVER.value
                and evidence.decided_by == DecisionSource.HUMAN.value
            ):
                raise EvidenceConflictError("A person already decided this evidence")
            if evidence.application_id is not None:
                raise EvidenceConflictError("Evidence is already linked to an application")
            application.current_status = ApplicationStatus.APPLIED
            self._apply_identity_fields(application)
            session.add(application)
            session.flush()
            assert application.id is not None
            message_id = evidence.external_id if evidence.source == EvidenceSource.GMAIL else None
            history = StatusHistory(
                application_id=application.id,
                from_status=None,
                to_status=ApplicationStatus.APPLIED.value,
                trigger=history_trigger,
                message_id=message_id,
            )
            session.add(history)
            session.flush()
            session.add(
                ApplicationEvent(
                    application_id=application.id,
                    event_type=ApplicationEventType.APPLICATION_SUBMITTED,
                    occurred_at=application.applied_date,
                    source=history_trigger,
                    source_message_id=message_id,
                    status_history_id=history.id,
                )
            )
            now = utc_now()
            evidence.application_id = application.id
            evidence.link_method = LinkMethod.CREATED.value
            evidence.link_confidence = 1.0
            evidence.processing_status = EvidenceStatus.CREATED_APPLICATION.value
            evidence.review_reason = None
            evidence.decided_by = decided_by
            evidence.decided_at = now
            evidence.updated_at = now
            if resolution is not None:
                evidence.resolver_version = resolution.get("version")
                evidence.resolver_decision = resolution.get("outcome")
                evidence.resolver_confidence = resolution.get("confidence")
                evidence.resolver_result = resolution
            session.add(evidence)
            session.flush()
            self._refresh_last_evidence_at(session, application.id)
            session.commit()
            session.refresh(application)
            self._sync_thread_ids(session, application.id, application.thread_ids)
            return application

    def apply_human_review(
        self,
        evidence_id: int,
        *,
        status: str,
        review_reason: str | None = None,
        deferred_until: datetime | None = None,
    ) -> Evidence:
        """Record a person's non-link review decision (dismissed / deferred)."""
        _check_choice(status, EvidenceStatus, "processing_status")
        with Session(self._engine, expire_on_commit=False) as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
            if evidence.application_id is not None:
                raise EvidenceConflictError("Unlink the evidence before dismissing or deferring it")
            now = utc_now()
            evidence.processing_status = status
            evidence.review_reason = review_reason
            evidence.deferred_until = deferred_until
            evidence.decided_by = DecisionSource.HUMAN.value
            evidence.decided_at = now
            evidence.updated_at = now
            session.add(evidence)
            session.commit()
            session.refresh(evidence)
            return evidence

    def has_event_for_message(self, application_id: int, message_id: str, event_type: str) -> bool:
        with Session(self._engine) as session:
            return (
                session.exec(
                    select(func.count())
                    .select_from(ApplicationEvent)
                    .where(
                        ApplicationEvent.application_id == application_id,
                        ApplicationEvent.source_message_id == message_id,
                        ApplicationEvent.event_type == event_type,
                    )
                ).one()
                > 0
            )

    def evidence_decision_counts(self) -> dict[str, dict[str, int]]:
        """Persistent totals by resolver decision, processing status and decision owner."""
        with Session(self._engine) as session:
            out: dict[str, dict[str, int]] = {}
            for name, column in (
                ("by_decision", Evidence.resolver_decision),
                ("by_status", Evidence.processing_status),
                ("by_decided_by", Evidence.decided_by),
            ):
                rows = session.exec(select(column, func.count()).group_by(column)).all()
                out[name] = {str(key) if key is not None else "none": int(n) for key, n in rows}
            return out

    # ------------------------------------------------------------------ #
    # Backup primitives — the only raw sqlite3 use in the codebase         #
    # (see CLAUDE.md). Higher-level workflow lives in backend/db/backup.py #
    # ------------------------------------------------------------------ #

    @staticmethod
    def online_backup(source: Path, destination: Path, *, source_read_only: bool = False) -> None:
        """Copy a database with SQLite's online backup API.

        Safe while the app is running: the backup API reads a consistent snapshot through
        SQLite itself (including pages still in the WAL), unlike copying the .db/-wal/-shm
        files. `destination` must not exist; the copy is converted to a standalone
        rollback-journal file so it has no -wal/-shm companions. `source_read_only` opens the
        source with mode=ro, so the copy cannot write to it (reconciliation dry-runs).
        """
        if not source.is_file():
            raise FileNotFoundError(f"Database not found: {source}")
        # Exclusive create: never overwrite an existing file, even in a race.
        with open(destination, "xb"):
            pass
        src = (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            if source_read_only
            else sqlite3.connect(str(source))
        )
        try:
            dst = sqlite3.connect(str(destination))
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()

    @staticmethod
    def integrity_check(db_path: Path, *, read_only: bool = True) -> list[str]:
        """Run PRAGMA integrity_check. Returns ["ok"] when healthy, otherwise SQLite's list
        of problems. Use read_only=False for a live WAL database (a read-only connection
        cannot create the -shm file it needs); the check itself never writes."""
        if not db_path.is_file():
            return [f"database file not found: {db_path}"]
        target = f"file:{db_path}?mode=ro" if read_only else f"file:{db_path}?mode=rw"
        conn = sqlite3.connect(target, uri=True)
        try:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.DatabaseError as exc:
            return [f"not a valid SQLite database: {exc}"]
        finally:
            conn.close()

    @staticmethod
    def foreign_key_check(db_path: Path) -> list[tuple[Any, ...]]:
        """Run PRAGMA foreign_key_check read-only. Returns the violating rows (empty when
        every foreign key resolves)."""
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
        finally:
            conn.close()

    @staticmethod
    def table_columns(db_path: Path) -> dict[str, list[str]]:
        """Column names per table, read-only (reflection)."""
        engine = schema.readonly_engine(db_path)
        try:
            inspector = inspect(engine)
            return {
                name: [c["name"] for c in inspector.get_columns(name)]
                for name in sorted(inspector.get_table_names())
            }
        finally:
            engine.dispose()

    @staticmethod
    def table_digests(
        db_path: Path, columns: dict[str, list[str]] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Row count and a SHA-256 over every row (ordered by all columns) for each table,
        read-only through SQLAlchemy Core reflection. Two databases with equal digests hold
        logically identical data; used to prove dry-runs and undo leave data unchanged.
        `columns` restricts the digest to those tables and columns (an additive migration
        leaves the digest of the pre-existing columns unchanged)."""
        engine = schema.readonly_engine(db_path)
        try:
            from sqlalchemy import MetaData

            metadata = MetaData()
            metadata.reflect(bind=engine)
            digests: dict[str, dict[str, Any]] = {}
            with engine.connect() as conn:
                for name in sorted(metadata.tables):
                    table = metadata.tables[name]
                    if columns is not None and name not in columns:
                        continue
                    picked = (
                        [table.c[c] for c in columns[name] if c in table.c]
                        if columns is not None
                        else list(table.columns)
                    )
                    rows = conn.execute(core_select(*picked).order_by(*picked)).all()
                    payload = json.dumps(
                        [list(r) for r in rows], default=str, separators=(",", ":")
                    )
                    digests[name] = {
                        "rows": len(rows),
                        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    }
            return digests
        finally:
            engine.dispose()

    @staticmethod
    def core_aggregates(db_path: Path) -> dict[str, Any]:
        """Schema-agnostic figures (every revision has these columns), read-only: all
        applications by status and source, status-history rows, events by type. Used to
        show a migration leaves the business data unchanged."""
        engine = schema.readonly_engine(db_path)
        try:
            from sqlalchemy import MetaData

            metadata = MetaData()
            metadata.reflect(bind=engine, only=["application", "statushistory", "applicationevent"])
            app = metadata.tables["application"]
            history = metadata.tables["statushistory"]
            events = metadata.tables["applicationevent"]
            with engine.connect() as conn:

                def grouped(column: Any) -> dict[str, int]:
                    rows = conn.execute(
                        core_select(column, func.count()).group_by(column).order_by(column)
                    ).all()
                    return {str(k): int(v) for k, v in rows}

                return {
                    "applications": conn.execute(
                        core_select(func.count()).select_from(app)
                    ).scalar_one(),
                    "by_status": grouped(app.c.current_status),
                    "by_source": grouped(app.c.source_portal),
                    "by_method": grouped(app.c.application_method),
                    "status_history": conn.execute(
                        core_select(func.count()).select_from(history)
                    ).scalar_one(),
                    "events_by_type": grouped(events.c.event_type),
                }
        finally:
            engine.dispose()

    @staticmethod
    def sensitive_terms(db_path: Path) -> set[str]:
        """Distinct personal/company strings stored in a database (companies, roles, thread
        IDs, message IDs, senders, subjects), read-only. Reconciliation scans its own output
        for these and refuses to finish if any appears."""
        engine = schema.readonly_engine(db_path)
        try:
            from sqlalchemy import MetaData

            wanted = {
                "application": ("company", "role", "job_url"),
                "applicationthreadid": ("thread_id",),
                "processedmessage": ("message_id",),
                "statushistory": ("message_id",),
                "evidence": ("sender", "subject", "snippet", "external_id", "thread_id"),
                "prospect": ("company", "title", "sender", "gmail_message_id", "thread_id"),
            }
            metadata = MetaData()
            metadata.reflect(bind=engine)
            terms: set[str] = set()
            with engine.connect() as conn:
                for table_name, column_names in wanted.items():
                    table = metadata.tables.get(table_name)
                    if table is None:
                        continue
                    for column_name in column_names:
                        if column_name not in table.c:
                            continue
                        values = conn.execute(core_select(table.c[column_name]).distinct()).all()
                        terms.update(
                            str(v[0]).strip()
                            for v in values
                            if v[0] and len(str(v[0]).strip()) >= 4
                        )
            return terms
        finally:
            engine.dispose()

    @staticmethod
    def count_rows_readonly(db_path: Path) -> dict[str, int]:
        """Row counts for the main tables of a database file, opened read-only through
        SQLAlchemy Core. Tables that do not exist are omitted."""
        tables = {
            "application": Application,
            "statushistory": StatusHistory,
            "applicationevent": ApplicationEvent,
            "applicationthreadid": ApplicationThreadId,
            "prospect": Prospect,
            "processedmessage": ProcessedMessage,
            "evidence": Evidence,
        }
        engine = schema.readonly_engine(db_path)
        try:
            present = set(inspect(engine).get_table_names())
            counts: dict[str, int] = {}
            with Session(engine) as session:
                for name, model in tables.items():
                    if name in present:
                        counts[name] = session.exec(select(func.count()).select_from(model)).one()
            return counts
        finally:
            engine.dispose()

    def get_raw_status_values(self) -> list[str]:
        """Return distinct current_status values exactly as stored on disk, bypassing
        the SAEnum column's coercion — reading through the mapped ORM attribute (or even
        a Core select() on the mapped column, which still applies the column type's
        result-level coercion) would itself raise LookupError on legacy NAME-format
        data, which is precisely the corruption this check exists to detect. Only a
        genuinely raw SQL string skips that — a documented exception to "no raw SQL
        strings" (see CLAUDE.md)."""
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT DISTINCT current_status FROM application"))
            return [row[0] for row in rows]

    def count_applications_and_processed(self) -> tuple[int, int]:
        """Return (n_applications, n_processed_messages) without deleting anything."""
        with Session(self._engine) as session:
            n_apps = session.exec(select(func.count()).select_from(Application)).one()
            n_proc = session.exec(select(func.count()).select_from(ProcessedMessage)).one()
            return n_apps, n_proc

    def reset_for_rebackfill(self) -> tuple[int, int]:
        """Delete all applications, status history, and processed-message records, and
        clear last_history_id so the next poll re-backfills the full BACKFILL_DAYS window.
        Returns (n_applications_deleted, n_processed_messages_deleted)."""
        with Session(self._engine) as session:
            n_apps = session.exec(select(func.count()).select_from(Application)).one()
            n_proc = session.exec(select(func.count()).select_from(ProcessedMessage)).one()
            for entry in session.exec(select(StatusHistory)).all():
                session.delete(entry)
            for link in session.exec(select(ApplicationThreadId)).all():
                session.delete(link)
            # Milestone events and prospect links reference applications too (NOT NULL /
            # nullable FKs respectively), exactly as delete_application handles them.
            for event_row in session.exec(select(ApplicationEvent)).all():
                session.delete(event_row)
            for prospect in session.exec(
                select(Prospect).where(col(Prospect.application_id).is_not(None))
            ).all():
                prospect.application_id = None
                session.add(prospect)
            # Evidence survives a re-backfill (it is the record of what was received); it is
            # detached and returned to pending so re-polling re-links it.
            for evidence in session.exec(
                select(Evidence).where(col(Evidence.application_id).is_not(None))
            ).all():
                evidence.application_id = None
                evidence.link_method = None
                evidence.link_confidence = None
                evidence.processing_status = EvidenceStatus.PENDING.value
                evidence.updated_at = utc_now()
                session.add(evidence)
            # Merge audit rows and dismissed pairs describe applications that are about to
            # be deleted; mergeoperation also holds a foreign key to the survivor.
            for operation in session.exec(select(MergeOperation)).all():
                session.delete(operation)
            for dismissal in session.exec(select(DuplicateDismissal)).all():
                session.delete(dismissal)
            session.flush()  # child deletes before parent — see delete_application for why
            for app in session.exec(select(Application)).all():
                session.delete(app)
            for msg in session.exec(select(ProcessedMessage)).all():
                session.delete(msg)
            session.commit()
        state = self.get_poller_state()
        self.update_poller_state(status=state.status, clear_last_history_id=True)
        return n_apps, n_proc

    def bulk_import_applications(self, rows: list[dict], now: datetime) -> int:
        """Clear existing applications and insert rows from an external source (e.g. an
        Excel export), each with an initial status-history entry. Returns rows inserted."""
        self.reset_for_rebackfill()
        with Session(self._engine) as session:
            for r in rows:
                app = Application(
                    company=r["company"],
                    role=r["role"],
                    source_portal=r["source_portal"],
                    job_url=None,
                    applied_date=r["applied_date"],
                    current_status=ApplicationStatus(r["current_status"]),
                    thread_ids="[]",
                    is_false_positive=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(app)
                session.commit()
                session.refresh(app)
                session.add(
                    StatusHistory(
                        application_id=app.id,
                        from_status=None,
                        to_status=r["current_status"],
                        trigger="manual",
                        changed_at=r["applied_date"],
                        message_id=None,
                    )
                )
                session.commit()
        return len(rows)

    # ------------------------------------------------------------------ #
    # Suppress rules                                                       #
    # ------------------------------------------------------------------ #

    def add_suppress_rule(
        self, sender_pattern: str, subject_pattern: str | None = None
    ) -> SuppressRule:
        with Session(self._engine, expire_on_commit=False) as session:
            rule = SuppressRule(sender_pattern=sender_pattern, subject_pattern=subject_pattern)
            session.add(rule)
            session.commit()
            session.refresh(rule)
            return rule

    def get_suppress_rules(self) -> list[SuppressRule]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(session.exec(select(SuppressRule)).all())

    def delete_suppress_rule(self, id: int) -> bool:
        with Session(self._engine) as session:
            rule = session.get(SuppressRule, id)
            if rule is None:
                return False
            session.delete(rule)
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Processed messages                                                   #
    # ------------------------------------------------------------------ #

    def mark_processed(self, message_id: str, result: str) -> None:
        """Record (or update) the processing result. Idempotent: concurrent or repeated
        processing of the same message updates the existing ledger row."""
        statement = sqlite_insert(_table(ProcessedMessage)).values(
            message_id=message_id, result=result, processed_at=utc_now()
        )
        statement = statement.on_conflict_do_update(
            index_elements=["message_id"],
            set_={
                "result": statement.excluded.result,
                "processed_at": statement.excluded.processed_at,
            },
        )
        with Session(self._engine) as session:
            session.execute(statement)
            session.commit()

    def is_processed(self, message_id: str) -> bool:
        with Session(self._engine) as session:
            return session.get(ProcessedMessage, message_id) is not None

    def clear_processed(self, message_id: str) -> None:
        """Remove a message's processed-marker so it can be re-ingested.

        Used for portal backfills: a message previously skipped (e.g. no
        matching portal rule existed yet) needs to go through processing
        again now that a rule covers it.
        """
        with Session(self._engine) as session:
            row = session.get(ProcessedMessage, message_id)
            if row is not None:
                session.delete(row)
                session.commit()

    # ------------------------------------------------------------------ #
    # Poller state                                                         #
    # ------------------------------------------------------------------ #

    def get_poller_state(self) -> PollerState:
        with Session(self._engine, expire_on_commit=False) as session:
            state = session.get(PollerState, 1)
            if state is None:
                raise RuntimeError("PollerState row missing — DB may be corrupted")
            return state

    def update_poller_state(
        self,
        status: str,
        last_history_id: str | None = None,
        last_sync_at: datetime | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
        clear_last_history_id: bool = False,
    ) -> None:
        with Session(self._engine) as session:
            state = session.get(PollerState, 1)
            if state is None:
                raise RuntimeError("PollerState row missing — DB may be corrupted")
            state.status = status
            if clear_last_history_id:
                state.last_history_id = None
            elif last_history_id is not None:
                state.last_history_id = last_history_id
            if last_sync_at is not None:
                state.last_sync_at = last_sync_at
            if clear_error:
                state.error_message = None
            elif error_message is not None:
                state.error_message = error_message
            session.add(state)
            session.commit()

    # ------------------------------------------------------------------ #
    # Insights helpers                                                     #
    # ------------------------------------------------------------------ #

    def get_all_status_history(self) -> list[StatusHistory]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(session.exec(select(StatusHistory).where(_live_history())).all())

    def get_status_history_for_apps(self, app_ids: set[int]) -> list[StatusHistory]:
        """Return status history rows for only the given application IDs."""
        if not app_ids:
            return []
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(StatusHistory).where(
                col(StatusHistory.application_id).in_(app_ids), _live_history()
            )
            return list(session.exec(stmt).all())

    def get_stale_applications(
        self, threshold_days: int = STALE_DAYS_THRESHOLD
    ) -> list[Application]:
        cutoff = utc_now() - timedelta(days=threshold_days)
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(Application).where(
                Application.current_status == ApplicationStatus.APPLIED,
                col(Application.updated_at) < cutoff,
                _active_app(),
            )
            return list(session.exec(stmt).all())
