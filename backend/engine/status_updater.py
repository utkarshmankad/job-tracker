"""Status transition logic — all state changes go through StatusUpdater._advance_status()."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import structlog

from backend.db.data_store import DataStore
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    EvidenceStatus,
    LinkMethod,
    utc_now,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import (
    IdentityResolver,
    MessageKind,
    classify_message_kind,
)
from backend.parser.email_parser import ParsedApplication

log = structlog.get_logger(__name__)

_STATUS_EVENT_MAP = {
    ApplicationStatus.APPLIED: ApplicationEventType.APPLICATION_SUBMITTED,
    ApplicationStatus.RESUME_SHORTLISTED: ApplicationEventType.RECRUITER_RESPONSE,
    ApplicationStatus.INTERVIEW_SCHEDULED: ApplicationEventType.INTERVIEW_SCHEDULED,
    ApplicationStatus.INTERVIEW_IN_PROGRESS: ApplicationEventType.INTERVIEW_ATTENDED,
    ApplicationStatus.OFFER_NEGOTIATION: ApplicationEventType.OFFER_RECEIVED,
    ApplicationStatus.OFFER: ApplicationEventType.OFFER_RECEIVED,
    ApplicationStatus.JOINED: ApplicationEventType.OFFER_RECEIVED,
    ApplicationStatus.REJECTED: ApplicationEventType.REJECTED,
}

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


@dataclass(frozen=True)
class ProcessingOutcome:
    application: Application | None
    is_new: bool
    result: str  # applied | status_update | thread_merged | needs_review
    kind: MessageKind
    link_method: str | None = None
    link_confidence: float | None = None
    review_reason: str | None = None


class StatusUpdater:
    def __init__(
        self,
        db: DataStore,
        detector: DuplicateDetector,
        resolver: IdentityResolver | None = None,
    ) -> None:
        self._db = db
        self._detector = detector
        self._resolver = resolver or IdentityResolver(db)

    def process(
        self, parsed: ParsedApplication, evidence_id: int | None = None
    ) -> tuple[Application | None, bool]:
        """Process a parsed email. Returns (application or None when it needs review,
        is_new_application). See process_evidence for the full outcome."""
        outcome = self.process_evidence(parsed, evidence_id)
        return outcome.application, outcome.is_new

    def process_evidence(
        self, parsed: ParsedApplication, evidence_id: int | None = None
    ) -> ProcessingOutcome:
        """Resolve identity, then link, create, or leave for review.

        Only an acknowledgement with no matching application creates one. A follow-up,
        scheduling or status email that matches nothing — or matches ambiguously — is left
        as needs_review evidence instead of becoming a duplicate application.
        """
        kind = classify_message_kind(parsed)
        match = self._resolver.resolve(parsed)

        if match.application is not None:
            assert match.application.id is not None
            app_id = match.application.id
            self._detector.merge(match.application, parsed)
            if evidence_id is not None:
                self._db.link_evidence(
                    evidence_id, app_id, match.method or "manual", match.confidence or 0.0
                )
            record = self._db.get_application(app_id)
            if record is None:
                raise RuntimeError(f"Application {app_id} vanished mid-update")
            if parsed.status_signal is not None:
                self._advance_status(record, parsed.status_signal, parsed.message_id)
                record = self._db.get_application(app_id)
                if record is None:
                    raise RuntimeError(f"Application {app_id} vanished after status advance")
            result = "status_update" if parsed.status_signal else "thread_merged"
            self._db.mark_processed(parsed.message_id, result)
            return ProcessingOutcome(record, False, result, kind, match.method, match.confidence)

        if match.ambiguity is None and kind is MessageKind.ACKNOWLEDGEMENT:
            record = self._create_new(parsed)
            assert record.id is not None
            if evidence_id is not None:
                self._db.link_evidence(
                    evidence_id,
                    record.id,
                    LinkMethod.CREATED.value,
                    1.0,
                    status=EvidenceStatus.CREATED_APPLICATION.value,
                )
                refreshed = self._db.get_application(record.id)
                record = refreshed or record
            self._db.mark_processed(parsed.message_id, "applied")
            return ProcessingOutcome(record, True, "applied", kind, LinkMethod.CREATED.value, 1.0)

        reason = match.ambiguity or f"{kind.value}_without_application"
        if evidence_id is not None:
            self._db.update_evidence_processing(
                evidence_id, EvidenceStatus.NEEDS_REVIEW.value, review_reason=reason
            )
        self._db.mark_processed(parsed.message_id, "needs_review")
        log.info("evidence_needs_review", message_id=parsed.message_id, reason=reason)
        return ProcessingOutcome(None, False, "needs_review", kind, review_reason=reason)

    def _advance_status(
        self,
        record: Application,
        signal: ApplicationStatus,
        message_id: str,
    ) -> None:
        assert record.id is not None
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
        record.updated_at = utc_now()
        self._db.upsert_application(record)
        history = self._db.append_status_history(
            application_id=record.id,
            from_status=from_val,
            to_status=signal.value,
            trigger="email",
            message_id=message_id,
        )
        self._record_status_event(record.id, signal, record.updated_at, message_id, history.id)

    def _record_status_event(
        self,
        application_id: int,
        status: ApplicationStatus,
        occurred_at: datetime,
        message_id: str | None,
        status_history_id: int | None,
        source: str = "email",
    ) -> None:
        event_type = _STATUS_EVENT_MAP.get(status)
        if event_type is None:
            return
        self._db.add_application_event(
            ApplicationEvent(
                application_id=application_id,
                event_type=event_type,
                occurred_at=occurred_at,
                source=source,
                source_message_id=message_id,
                status_history_id=status_history_id,
            )
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
            application_method="Unknown",
            job_url=parsed.job_url,
            applied_date=parsed.applied_date,
            current_status=ApplicationStatus.APPLIED,
            thread_ids=json.dumps([parsed.thread_id]),
        )
        saved = self._db.upsert_application(app)
        assert saved.id is not None
        history = self._db.append_status_history(
            application_id=saved.id,
            from_status=None,
            to_status=ApplicationStatus.APPLIED.value,
            trigger="email",
            message_id=parsed.message_id,
        )
        self._record_status_event(
            saved.id,
            ApplicationStatus.APPLIED,
            parsed.applied_date,
            parsed.message_id,
            history.id,
        )
        if parsed.status_signal is not None:
            self._advance_status(saved, parsed.status_signal, parsed.message_id)
            result = self._db.get_application(saved.id)
            if result is None:
                raise RuntimeError(f"Application {saved.id} vanished after creation advance")
            saved = result
        return saved

    def create_manual(
        self,
        company: str | None,
        role: str | None,
        source_portal: str,
        job_url: str | None,
        applied_date: datetime,
        target_status: ApplicationStatus,
        application_method: str = "Unknown",
    ) -> Application:
        """Create a new application from a manual (non-email) source, e.g. LinkedIn import."""
        app = Application(
            company=company,
            role=role,
            source_portal=source_portal,
            application_method=application_method,
            job_url=job_url,
            applied_date=applied_date,
            current_status=ApplicationStatus.APPLIED,
            updated_at=applied_date,
        )
        saved = self._db.upsert_application(app)
        assert saved.id is not None
        history = self._db.append_status_history(
            application_id=saved.id,
            from_status=None,
            to_status=ApplicationStatus.APPLIED.value,
            trigger="manual",
            message_id=None,
        )
        self._record_status_event(
            saved.id,
            ApplicationStatus.APPLIED,
            applied_date,
            None,
            history.id,
            source="manual",
        )
        if target_status != ApplicationStatus.APPLIED:
            saved = self.manual_update(saved.id, target_status)
        return saved

    def manual_update(
        self,
        application_id: int,
        new_status: ApplicationStatus,
        withdraw_reason: str | None = None,
    ) -> Application:
        record = self._db.get_application(application_id)
        if record is None:
            raise ValueError(f"Application {application_id} not found")

        from_val = record.current_status.value
        record.current_status = new_status
        record.updated_at = utc_now()
        if new_status == ApplicationStatus.WITHDRAWN:
            record.withdraw_reason = withdraw_reason or "self_withdraw"
        elif record.withdraw_reason is not None:
            record.withdraw_reason = None
        updated = self._db.upsert_application(record)
        history = self._db.append_status_history(
            application_id=application_id,
            from_status=from_val,
            to_status=new_status.value,
            trigger="manual",
            message_id=None,
        )
        self._record_status_event(
            application_id,
            new_status,
            updated.updated_at,
            None,
            history.id,
            source="manual",
        )
        return updated
