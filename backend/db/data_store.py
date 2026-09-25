"""DataStore: single access point for all database operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import ColumnElement, event, func, inspect, or_
from sqlmodel import Session, SQLModel, col, create_engine, select

from backend.config import DB_PATH, STALE_DAYS_THRESHOLD
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    ApplicationThreadId,
    PollerState,
    ProcessedMessage,
    Prospect,
    ProspectStatus,
    StatusHistory,
    SuppressRule,
    utc_now,
)


def is_application_stale(app: Application, threshold_days: int = STALE_DAYS_THRESHOLD) -> bool:
    """Return True when an Applied-status application has had no update in threshold_days."""
    if app.current_status != ApplicationStatus.APPLIED:
        return False
    cutoff = utc_now() - timedelta(days=threshold_days)
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
    page: int = 1
    page_size: int = 50


class DataStore:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
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

        SQLModel.metadata.create_all(self._engine)
        self._migrate_schema()
        self._ensure_poller_state()
        self._backfill_thread_id_index()
        self._backfill_application_events()

    def _migrate_schema(self) -> None:
        # Column introspection uses SQLAlchemy Core's inspect() — no raw SQL needed.
        # ALTER TABLE ADD COLUMN has no portable Core/ORM construct in SQLAlchemy, so it
        # stays a raw DDL string via text() — a documented exception to "no raw SQL
        # strings" (see CLAUDE.md), kept local to DataStore so it's still the single
        # point of DB access.
        from sqlalchemy import text

        with self._engine.connect() as conn:
            cols = {c["name"] for c in inspect(conn).get_columns("application")}
            if "withdraw_reason" not in cols:
                conn.execute(text("ALTER TABLE application ADD COLUMN withdraw_reason VARCHAR"))
            if "application_method" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE application ADD COLUMN application_method "
                        "VARCHAR NOT NULL DEFAULT 'Unknown'"
                    )
                )
            conn.execute(
                text(
                    "UPDATE application SET source_portal = 'Instahyre' "
                    "WHERE source_portal = 'Instahire'"
                )
            )
            prospect_cols = {c["name"] for c in inspect(conn).get_columns("prospect")}
            if "application_id" not in prospect_cols:
                conn.execute(text("ALTER TABLE prospect ADD COLUMN application_id INTEGER"))
            conn.commit()

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

    def upsert_application(self, app: Application) -> Application:
        with Session(self._engine, expire_on_commit=False) as session:
            if app.id is None:
                session.add(app)
                session.commit()
                session.refresh(app)
                self._sync_thread_ids(session, app.id, app.thread_ids)
                return app
            db_app = session.get(Application, app.id)
            if db_app is None:
                session.add(app)
                session.commit()
                session.refresh(app)
                self._sync_thread_ids(session, app.id, app.thread_ids)
                return app
            # Update scalar fields only; relationships are left untouched.
            app.updated_at = utc_now()
            for field_name in Application.model_fields:
                if field_name not in ("id", "created_at"):
                    setattr(db_app, field_name, getattr(app, field_name))
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

    def get_application_events(self, application_id: int) -> list[ApplicationEvent]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(ApplicationEvent)
                .where(ApplicationEvent.application_id == application_id)
                .order_by(col(ApplicationEvent.occurred_at))
            )
            return list(session.exec(stmt).all())

    def get_application_events_for_apps(self, app_ids: set[int]) -> list[ApplicationEvent]:
        if not app_ids:
            return []
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(ApplicationEvent).where(col(ApplicationEvent.application_id).in_(app_ids))
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
                select(col(Application.source_portal).distinct()).order_by(
                    col(Application.source_portal)
                )
            ).all()
            methods = session.exec(
                select(col(Application.application_method).distinct()).order_by(
                    col(Application.application_method)
                )
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
            return session.get(Application, link.application_id)

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
                .where(col(Application.is_false_positive).is_(False))
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
                .where(col(Application.is_false_positive).is_(False))
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
                .where(Application.is_false_positive == False)  # noqa: E712
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
                .where(col(Application.is_false_positive).is_(False))
                .where(col(Application.current_status).notin_([s.value for s in terminal]))
                .where(or_(*conditions))
            )
            return list(session.exec(stmt).all())

    def delete_application(self, id: int) -> bool:
        with Session(self._engine) as session:
            app = session.get(Application, id)
            if app is None:
                return False
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
            # Flush child deletes before deleting the parent — SQLAlchemy's unit-of-work
            # dependency sort doesn't reliably order these plain foreign_key=... columns
            # (no relationship()) ahead of the parent delete in the same flush, which trips
            # SQLite's FK enforcement (PRAGMA foreign_keys=ON, set per-connection above).
            session.flush()
            session.delete(app)
            session.commit()
            return True

    def merge_applications(self, primary_id: int, duplicate_id: int) -> Application:
        """Merge a duplicate into a primary while preserving threads and status history."""
        if primary_id == duplicate_id:
            raise ValueError("Primary and duplicate must be different applications")
        status_rank = {
            ApplicationStatus.APPLIED: 0,
            ApplicationStatus.RESUME_SHORTLISTED: 1,
            ApplicationStatus.INTERVIEW_SCHEDULED: 2,
            ApplicationStatus.INTERVIEW_IN_PROGRESS: 3,
            ApplicationStatus.OFFER_NEGOTIATION: 4,
            ApplicationStatus.REJECTED: 4,
            ApplicationStatus.WITHDRAWN: 4,
            ApplicationStatus.OFFER: 5,
            ApplicationStatus.JOINED: 6,
        }
        with Session(self._engine, expire_on_commit=False) as session:
            primary = session.get(Application, primary_id)
            duplicate = session.get(Application, duplicate_id)
            if primary is None or duplicate is None:
                raise ValueError("One or both applications were not found")

            threads = list(
                dict.fromkeys(
                    json.loads(primary.thread_ids or "[]")
                    + json.loads(duplicate.thread_ids or "[]")
                )
            )
            primary.thread_ids = json.dumps(threads)
            primary.applied_date = min(primary.applied_date, duplicate.applied_date)
            if status_rank[duplicate.current_status] > status_rank[primary.current_status]:
                primary.current_status = duplicate.current_status
            for field_name in ("company", "role", "job_url"):
                if not getattr(primary, field_name) and getattr(duplicate, field_name):
                    setattr(primary, field_name, getattr(duplicate, field_name))
            if primary.source_portal in ("Direct/Unknown", "Unknown"):
                primary.source_portal = duplicate.source_portal
            if primary.application_method == "Unknown":
                primary.application_method = duplicate.application_method
            primary.updated_at = utc_now()

            for history in session.exec(
                select(StatusHistory).where(StatusHistory.application_id == duplicate_id)
            ).all():
                history.application_id = primary_id
                session.add(history)
            for application_event in session.exec(
                select(ApplicationEvent).where(ApplicationEvent.application_id == duplicate_id)
            ).all():
                application_event.application_id = primary_id
                session.add(application_event)
            for prospect in session.exec(
                select(Prospect).where(Prospect.application_id == duplicate_id)
            ).all():
                prospect.application_id = primary_id
                session.add(prospect)
            for thread_link in session.exec(
                select(ApplicationThreadId).where(
                    ApplicationThreadId.application_id == duplicate_id
                )
            ).all():
                session.delete(thread_link)
            session.flush()
            session.delete(duplicate)
            session.add(primary)
            session.commit()
            session.refresh(primary)
            self._sync_thread_ids(session, primary.id, primary.thread_ids)
            return primary

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

    def get_status_history(self, application_id: int) -> list[StatusHistory]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(StatusHistory).where(StatusHistory.application_id == application_id)
            return list(session.exec(stmt).all())

    # ------------------------------------------------------------------ #
    # Diagnostics / maintenance                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def inspect_schema_tables(db_path: Path) -> set[str]:
        """Return table names actually present on disk, without creating missing ones.

        Deliberately bypasses DataStore's normal constructor — that calls
        SQLModel.metadata.create_all(), which would silently create the missing tables
        this check exists to detect. Uses a throwaway engine for inspection only.
        """
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            return set(inspect(engine).get_table_names())
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
        with Session(self._engine) as session:
            session.add(ProcessedMessage(message_id=message_id, result=result))
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
            return list(session.exec(select(StatusHistory)).all())

    def get_status_history_for_apps(self, app_ids: set[int]) -> list[StatusHistory]:
        """Return status history rows for only the given application IDs."""
        if not app_ids:
            return []
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(StatusHistory).where(col(StatusHistory.application_id).in_(app_ids))
            return list(session.exec(stmt).all())

    def get_stale_applications(
        self, threshold_days: int = STALE_DAYS_THRESHOLD
    ) -> list[Application]:
        cutoff = utc_now() - timedelta(days=threshold_days)
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(Application).where(
                Application.current_status == ApplicationStatus.APPLIED,
                col(Application.updated_at) < cutoff,
            )
            return list(session.exec(stmt).all())
