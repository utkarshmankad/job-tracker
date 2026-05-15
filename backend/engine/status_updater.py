"""Status transition logic — all state changes go through StatusUpdater._advance_status()."""

from __future__ import annotations

import json
from datetime import datetime

import structlog

from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus
from backend.engine.duplicate_detector import DuplicateDetector
from backend.parser.email_parser import ParsedApplication

log = structlog.get_logger(__name__)

# Forward-only transitions valid for automated (email) signals.
# Allow skipping stages because emails are processed newest-first during backfill,
# so we may see a later-stage signal before the original application confirmation.
_TRANSITIONS: dict[ApplicationStatus, set[ApplicationStatus]] = {
    ApplicationStatus.APPLIED: {
        ApplicationStatus.RESUME_SHORTLISTED,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.INTERVIEW_IN_PROGRESS,
        ApplicationStatus.OFFER_NEGOTIATION,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.RESUME_SHORTLISTED: {
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.INTERVIEW_IN_PROGRESS,
        ApplicationStatus.OFFER_NEGOTIATION,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.INTERVIEW_SCHEDULED: {
        ApplicationStatus.INTERVIEW_IN_PROGRESS,
        ApplicationStatus.OFFER_NEGOTIATION,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.INTERVIEW_IN_PROGRESS: {
        ApplicationStatus.OFFER_NEGOTIATION,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
    },
    ApplicationStatus.OFFER_NEGOTIATION: {
        ApplicationStatus.OFFER,
        ApplicationStatus.WITHDRAWN,
    },
    ApplicationStatus.OFFER: {
        ApplicationStatus.JOINED,
        ApplicationStatus.WITHDRAWN,
    },
    ApplicationStatus.JOINED: set(),
    ApplicationStatus.REJECTED: set(),
    ApplicationStatus.WITHDRAWN: set(),
}


class StatusUpdater:
    def __init__(self, db: DataStore, detector: DuplicateDetector) -> None:
        self._db = db
        self._detector = detector

    def process(self, parsed: ParsedApplication) -> Application:
        record = self._find_existing(parsed)
        is_new = record is None

        if is_new:
            record = self._create_new(parsed)
        else:
            self._detector.merge(record, parsed)
            record = self._db.get_application(record.id)  # type: ignore[arg-type]
            if parsed.status_signal is not None:
                self._advance_status(record, parsed.status_signal, parsed.message_id)
                record = self._db.get_application(record.id)  # type: ignore[arg-type]

        result = "applied" if is_new else ("status_update" if parsed.status_signal else "applied")
        self._db.mark_processed(parsed.message_id, result)
        return record  # type: ignore[return-value]

    def _find_existing(self, parsed: ParsedApplication) -> Application | None:
        apps, _ = self._db.get_applications(ApplicationFilter(page_size=10_000))
        for app in apps:
            thread_ids: list[str] = json.loads(app.thread_ids or "[]")
            if parsed.thread_id in thread_ids:
                return app
        return self._detector.find_duplicate(parsed)

    def _advance_status(
        self,
        record: Application,
        signal: ApplicationStatus,
        message_id: str,
    ) -> None:
        current = record.current_status
        valid_next = _TRANSITIONS.get(current, set())
        if signal not in valid_next:
            log.warning(
                "invalid_status_transition",
                application_id=record.id,
                from_status=current,
                to_status=signal,
            )
            return

        from_val = current.value
        record.current_status = signal
        record.updated_at = datetime.utcnow()
        self._db.upsert_application(record)
        self._db.append_status_history(
            application_id=record.id,  # type: ignore[arg-type]
            from_status=from_val,
            to_status=signal.value,
            trigger="email",
            message_id=message_id,
        )

    def _create_new(self, parsed: ParsedApplication) -> Application:
        # Always start at APPLIED so that status_signal advances via _advance_status
        # (which enforces forward-only transitions and logs history).  Without this,
        # emails processed newest-first during backfill would create records at whatever
        # late-stage status the first-seen email implies.
        app = Application(
            company=parsed.company,
            role=parsed.role,
            source_portal=parsed.source_portal,
            job_url=parsed.job_url,
            applied_date=parsed.applied_date,
            current_status=ApplicationStatus.APPLIED,
            thread_ids=json.dumps([parsed.thread_id]),
        )
        saved = self._db.upsert_application(app)
        self._db.append_status_history(
            application_id=saved.id,  # type: ignore[arg-type]
            from_status=None,
            to_status=ApplicationStatus.APPLIED.value,
            trigger="email",
            message_id=parsed.message_id,
        )
        if parsed.status_signal is not None:
            self._advance_status(saved, parsed.status_signal, parsed.message_id)
            saved = self._db.get_application(saved.id)  # type: ignore[arg-type]
        return saved  # type: ignore[return-value]

    def manual_update(self, application_id: int, new_status: ApplicationStatus) -> Application:
        record = self._db.get_application(application_id)
        if record is None:
            raise ValueError(f"Application {application_id} not found")

        from_val = record.current_status.value
        record.current_status = new_status
        record.updated_at = datetime.utcnow()
        updated = self._db.upsert_application(record)
        self._db.append_status_history(
            application_id=application_id,
            from_status=from_val,
            to_status=new_status.value,
            trigger="manual",
            message_id=None,
        )
        return updated
