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
    """Return True when an Applied-status application has had no update within threshold_days."""
    if app.current_status != ApplicationStatus.APPLIED:
        return False
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    return app.updated_at < cutoff

log = structlog.get_logger(__name__)


@dataclass
class ApplicationFilter:
    status: Optional[ApplicationStatus] = None
    source_portal: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    search: Optional[str] = None  # matches company or role (case-insensitive)
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
        self._ensure_poller_state()

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
    ) -> None:
        with Session(self._engine) as session:
            state = session.get(PollerState, 1)
            if state is None:
                raise RuntimeError("PollerState row missing — DB may be corrupted")
            state.status = status
            if last_history_id is not None:
                state.last_history_id = last_history_id
            if last_sync_at is not None:
                state.last_sync_at = last_sync_at
            if error_message is not None:
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
