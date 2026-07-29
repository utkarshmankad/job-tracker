"""DataStore: single access point for all database operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy import event, func, or_
from sqlmodel import Session, SQLModel, col, create_engine, select

from backend.config import DB_PATH, STALE_DAYS_THRESHOLD
from backend.db.models import (
    Application,
    ApplicationStatus,
    PollerState,
    ProcessedMessage,
    StatusHistory,
    SuppressRule,
)


def is_application_stale(
    app: Application, threshold_days: int = STALE_DAYS_THRESHOLD
) -> bool:
    """Return True when an Applied-status application was submitted more than threshold_days ago with no response."""
    if app.current_status != ApplicationStatus.APPLIED:
        return False
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    applied = app.applied_date if isinstance(app.applied_date, datetime) else datetime(app.applied_date.year, app.applied_date.month, app.applied_date.day)
    return applied < cutoff

log = structlog.get_logger(__name__)


@dataclass
class ApplicationFilter:
    status: Optional[ApplicationStatus] = None
    source_portal: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    search: Optional[str] = None  # matches company or role (case-insensitive)
    is_stale: Optional[bool] = None
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

    def _migrate_schema(self) -> None:
        # Deliberate exception to "no raw SQL strings": SQLModel/SQLAlchemy ORM has no
        # portable API for column introspection (PRAGMA table_info) or ALTER TABLE ADD
        # COLUMN — both require raw SQL via SQLAlchemy Core's text(), kept local to
        # DataStore so it's still the single point of DB access.
        from sqlalchemy import text
        with self._engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(application)"))}
            if "withdraw_reason" not in cols:
                conn.execute(text("ALTER TABLE application ADD COLUMN withdraw_reason VARCHAR"))
                conn.commit()

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
                return app
            db_app = session.get(Application, app.id)
            if db_app is None:
                session.add(app)
                session.commit()
                session.refresh(app)
                return app
            # Update scalar fields only; relationships are left untouched.
            app.updated_at = datetime.utcnow()
            for field_name in Application.model_fields:
                if field_name not in ("id", "created_at"):
                    setattr(db_app, field_name, getattr(app, field_name))
            session.commit()
            session.refresh(db_app)
            return db_app

    def get_applications(self, filters: ApplicationFilter) -> tuple[list[Application], int]:
        with Session(self._engine, expire_on_commit=False) as session:
            conditions = []
            if filters.status is not None:
                conditions.append(Application.current_status == filters.status)
            if filters.source_portal is not None:
                conditions.append(Application.source_portal == filters.source_portal)
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
                stale_cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS_THRESHOLD)
                conditions.append(Application.current_status == ApplicationStatus.APPLIED)
                conditions.append(col(Application.applied_date) < stale_cutoff)
            elif filters.is_stale is False:
                stale_cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS_THRESHOLD)
                conditions.append(
                    ~(
                        (Application.current_status == ApplicationStatus.APPLIED)
                        & (col(Application.applied_date) < stale_cutoff)
                    )
                )

            base_stmt = select(Application)
            for cond in conditions:
                base_stmt = base_stmt.where(cond)

            count_stmt = select(func.count()).select_from(base_stmt.subquery())
            total: int = session.exec(count_stmt).one()

            offset = (filters.page - 1) * filters.page_size
            items_stmt = base_stmt.offset(offset).limit(filters.page_size)
            items = list(session.exec(items_stmt).all())

            return items, total

    def get_application(self, id: int) -> Application | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(Application, id)

    def find_application_by_thread_id(self, thread_id: str) -> Application | None:
        """Return the Application that owns thread_id, or None. Uses SQL LIKE on the JSON blob."""
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(Application).where(
                Application.thread_ids.contains(f'"{thread_id}"')
            )
            return session.exec(stmt).first()

    def get_applications_missing_fields(self) -> list[Application]:
        """Return non-false-positive applications where company or role is NULL."""
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = (
                select(Application)
                .where(Application.is_false_positive == False)  # noqa: E712
                .where(
                    or_(
                        Application.company == None,  # noqa: E711
                        Application.role == None,  # noqa: E711
                    )
                )
            )
            return list(session.exec(stmt).all())

    def find_application_by_company_role(
        self, company: str, role: str
    ) -> Application | None:
        """Return the most-recent non-false-positive Application matching company+role (case-insensitive)."""
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
        """Return non-Withdrawn, non-false-positive applications whose company matches any name (case-insensitive)."""
        terminal = {ApplicationStatus.WITHDRAWN, ApplicationStatus.REJECTED, ApplicationStatus.JOINED, ApplicationStatus.OFFER}
        with Session(self._engine, expire_on_commit=False) as session:
            conditions = [
                col(Application.company).ilike(name)
                for name in company_names
                if name.strip()
            ]
            if not conditions:
                return []
            stmt = (
                select(Application)
                .where(Application.is_false_positive == False)  # noqa: E712
                .where(Application.current_status.notin_([s.value for s in terminal]))  # type: ignore[attr-defined]
                .where(or_(*conditions))
            )
            return list(session.exec(stmt).all())

    def delete_application(self, id: int) -> bool:
        with Session(self._engine) as session:
            app = session.get(Application, id)
            if app is None:
                return False
            # Delete status history rows first; the FK is NOT NULL so SQLAlchemy
            # cannot null them out via its default orphan strategy.
            for row in session.exec(select(StatusHistory).where(StatusHistory.application_id == id)).all():
                session.delete(row)
            session.delete(app)
            session.commit()
            return True

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
    ) -> None:
        with Session(self._engine) as session:
            entry = StatusHistory(
                application_id=application_id,
                from_status=from_status,
                to_status=to_status,
                trigger=trigger,
                message_id=message_id,
            )
            session.add(entry)
            session.commit()

    def get_status_history(self, application_id: int) -> list[StatusHistory]:
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(StatusHistory).where(
                StatusHistory.application_id == application_id
            )
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
        from sqlalchemy import inspect
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    def get_raw_status_values(self) -> list[str]:
        """Return distinct current_status values exactly as stored on disk, bypassing
        the SAEnum column's coercion — reading through the ORM column would itself raise
        LookupError on legacy NAME-format data, which is precisely the corruption this
        check exists to detect."""
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
            for app in session.exec(select(Application)).all():
                session.delete(app)
            for msg in session.exec(select(ProcessedMessage)).all():
                session.delete(msg)
            session.commit()
        state = self.get_poller_state()
        self.update_poller_state(status=state.status, clear_last_history_id=True)
        return n_apps, n_proc

    def bulk_import_applications(
        self, rows: list[dict], now: datetime
    ) -> int:
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
                session.add(StatusHistory(
                    application_id=app.id,
                    from_status=None,
                    to_status=r["current_status"],
                    trigger="manual",
                    changed_at=r["applied_date"],
                    message_id=None,
                ))
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
            stmt = select(StatusHistory).where(StatusHistory.application_id.in_(app_ids))
            return list(session.exec(stmt).all())

    def get_stale_applications(self, threshold_days: int = STALE_DAYS_THRESHOLD) -> list[Application]:
        cutoff = datetime.utcnow() - timedelta(days=threshold_days)
        with Session(self._engine, expire_on_commit=False) as session:
            stmt = select(Application).where(
                Application.current_status == ApplicationStatus.APPLIED,
                col(Application.updated_at) < cutoff,
            )
            return list(session.exec(stmt).all())
