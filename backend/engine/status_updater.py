"""Status transition logic — all state changes go through StatusUpdater._advance_status()."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import structlog

from backend.db.data_store import DataStore, EvidenceConflictError, EvidenceNotFoundError
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    DecisionSource,
    Evidence,
    EvidenceStatus,
    LinkMethod,
    utc_now,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import (
    IdentityResolver,
    MessageKind,
    Outcome,
    ResolutionResult,
    classify_message_kind,
    signals_from_parsed,
)
from backend.engine.resolver_metrics import metrics
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


# Interview rounds repeat within one application; each round's message adds a milestone.
_REPEATABLE_MILESTONES = {
    ApplicationStatus.INTERVIEW_SCHEDULED,
    ApplicationStatus.INTERVIEW_IN_PROGRESS,
}


def _evidence_signal(evidence: Evidence) -> ApplicationStatus | None:
    parser = (evidence.raw_metadata or {}).get("parser") or {}
    value = parser.get("status_signal") if isinstance(parser, dict) else None
    try:
        return ApplicationStatus(value) if value else None
    except ValueError:
        return None


def _evidence_message_id(evidence: Evidence) -> str:
    return evidence.external_id or f"evidence:{evidence.id}"


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
        """Resolve a parsed email to an application and act on the decision.

        Human decisions are never overridden; concurrent workers cannot both act on the same
        evidence (claim); a linked follow-up may advance status or add a milestone, but never
        creates a second application. Repeated processing is idempotent.
        """
        kind = classify_message_kind(parsed)
        metrics.increment("evidence_processed")
        if evidence_id is not None:
            evidence = self._db.get_evidence(evidence_id)
            if evidence is not None and evidence.decided_by == DecisionSource.HUMAN.value:
                return self._human_retained(parsed, evidence.application_id, kind)
            if not self._db.claim_evidence(evidence_id):
                metrics.increment("concurrent_claim_skipped")
                log.info("evidence_claimed_elsewhere", evidence_id=evidence_id)
                return ProcessingOutcome(None, False, "in_progress", kind)

        result = self._resolver.resolve(signals_from_parsed(parsed))
        log.info(
            "evidence_resolved",
            evidence_id=evidence_id,
            outcome=result.outcome.value,
            reason=result.reason,
            confidence=result.confidence,
            candidates=len(result.candidates),
            resolver_version=result.version,
        )

        if result.outcome is Outcome.LINKED:
            assert result.application_id is not None
            return self._apply_link(parsed, evidence_id, result, kind)
        if result.outcome is Outcome.NEW_APPLICATION:
            return self._apply_new(parsed, evidence_id, result, kind)

        if evidence_id is not None:
            recorded = self._db.record_resolution(
                evidence_id,
                resolution=result.to_json(),
                status=EvidenceStatus.NEEDS_REVIEW.value,
                review_reason=result.reason,
            )
            if recorded is None:
                return self._human_retained(parsed, None, kind)
        self._db.mark_processed(parsed.message_id, "needs_review")
        metrics.increment("sent_to_review")
        return ProcessingOutcome(None, False, "needs_review", kind, review_reason=result.reason)

    def _apply_link(
        self,
        parsed: ParsedApplication,
        evidence_id: int | None,
        result: ResolutionResult,
        kind: MessageKind,
    ) -> ProcessingOutcome:
        app_id = result.application_id
        assert app_id is not None
        record = self._db.get_application(app_id)
        if record is None:
            raise RuntimeError(f"Application {app_id} vanished mid-resolution")
        if evidence_id is not None:
            recorded = self._db.record_resolution(
                evidence_id,
                resolution=result.to_json(),
                status=EvidenceStatus.LINKED.value,
                application_id=app_id,
                link_method=result.link_method,
                link_confidence=result.confidence,
            )
            if recorded is None:
                return self._human_retained(parsed, None, kind)
        self._detector.merge(record, parsed)
        record = self._db.get_application(app_id)
        if record is None:
            raise RuntimeError(f"Application {app_id} vanished mid-update")
        if parsed.status_signal is not None:
            self._apply_signal(record, parsed.status_signal, parsed.message_id, "email")
            record = self._db.get_application(app_id) or record
        processed = "status_update" if parsed.status_signal else "thread_merged"
        self._db.mark_processed(parsed.message_id, processed)
        metrics.increment("auto_linked")
        return ProcessingOutcome(
            record, False, processed, kind, result.link_method, result.confidence
        )

    def _apply_new(
        self,
        parsed: ParsedApplication,
        evidence_id: int | None,
        result: ResolutionResult,
        kind: MessageKind,
    ) -> ProcessingOutcome:
        if evidence_id is None:
            record = self._create_new(parsed)
        else:
            try:
                record = self._db.create_application_from_evidence(
                    self._application_from_parsed(parsed),
                    evidence_id,
                    decided_by=DecisionSource.RESOLVER.value,
                    history_trigger="email",
                    resolution=result.to_json(),
                )
            except EvidenceConflictError:
                return self._human_retained(parsed, None, kind)
        self._db.mark_processed(parsed.message_id, "applied")
        metrics.increment("new_application_created")
        return ProcessingOutcome(
            record, True, "applied", kind, LinkMethod.CREATED.value, result.confidence
        )

    def _human_retained(
        self, parsed: ParsedApplication, application_id: int | None, kind: MessageKind
    ) -> ProcessingOutcome:
        metrics.increment("human_decision_retained")
        self._db.mark_processed(parsed.message_id, "human_decision")
        app = self._db.get_application(application_id) if application_id else None
        return ProcessingOutcome(app, False, "human_decision", kind)

    @staticmethod
    def _application_from_parsed(parsed: ParsedApplication) -> Application:
        return Application(
            company=parsed.company,
            role=parsed.role,
            source_portal=parsed.source_portal,
            application_method="Unknown",
            job_url=parsed.job_url,
            applied_date=parsed.applied_date,
            current_status=ApplicationStatus.APPLIED,
            thread_ids=json.dumps([parsed.thread_id]),
        )

    def _apply_signal(
        self, record: Application, signal: ApplicationStatus, message_id: str, trigger: str
    ) -> None:
        """Advance status when the transition is valid. A repeated interview signal on an
        application already at that stage (another round in the same thread) is recorded as
        a milestone event instead, once per message."""
        if signal in _TRANSITIONS.get(record.current_status, set()):
            self._advance_status(record, signal, message_id, trigger=trigger)
            return
        if signal == record.current_status and signal in _REPEATABLE_MILESTONES:
            assert record.id is not None
            event_type = _STATUS_EVENT_MAP[signal]
            if not self._db.has_event_for_message(record.id, message_id, event_type.value):
                self._record_status_event(record.id, signal, utc_now(), message_id, None, trigger)
            return
        self._advance_status(record, signal, message_id, trigger=trigger)  # logs the refusal

    # ------------------------------------------------------------------ #
    # Human review decisions                                               #
    # ------------------------------------------------------------------ #

    def accept_candidate(self, evidence_id: int, application_id: int) -> Evidence:
        """A person confirms which application the evidence belongs to. Idempotent; applies
        the evidence's status signal (if any) like an email would."""
        evidence = self._db.get_evidence(evidence_id)
        if evidence is None:
            raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
        if (
            evidence.application_id == application_id
            and evidence.decided_by == DecisionSource.HUMAN.value
        ):
            return evidence
        linked = self._db.link_evidence(
            evidence_id,
            application_id,
            LinkMethod.MANUAL.value,
            1.0,
            decided_by=DecisionSource.HUMAN.value,
        )
        signal = _evidence_signal(linked)
        record = self._db.get_application(application_id)
        if signal is not None and record is not None:
            self._apply_signal(record, signal, _evidence_message_id(linked), "review")
        return linked

    def create_from_review(
        self,
        evidence_id: int,
        *,
        company: str | None = None,
        role: str | None = None,
        source_portal: str | None = None,
    ) -> Application:
        """A person decides the evidence is a new application. Idempotent: repeating the
        call returns the application it created."""
        evidence = self._db.get_evidence(evidence_id)
        if evidence is None:
            raise EvidenceNotFoundError(f"Evidence {evidence_id} not found")
        if evidence.application_id is not None:
            if (
                evidence.link_method == LinkMethod.CREATED.value
                and evidence.decided_by == DecisionSource.HUMAN.value
            ):
                existing = self._db.get_application(evidence.application_id)
                if existing is not None:
                    return existing
            raise EvidenceConflictError("Evidence is already linked; unlink it first")
        parser = (evidence.raw_metadata or {}).get("parser") or {}
        company = company or parser.get("company")
        if not company:
            raise ValueError("A company is required to create an application")
        application = Application(
            company=company,
            role=role or parser.get("role"),
            source_portal=source_portal or parser.get("portal") or "Direct/Unknown",
            application_method="Unknown",
            job_url=parser.get("job_url"),
            applied_date=evidence.occurred_at,
            current_status=ApplicationStatus.APPLIED,
            thread_ids=json.dumps([evidence.thread_id] if evidence.thread_id else []),
        )
        created = self._db.create_application_from_evidence(
            application,
            evidence_id,
            decided_by=DecisionSource.HUMAN.value,
            history_trigger="review",
        )
        signal = _evidence_signal(evidence)
        if signal is not None:
            self._apply_signal(created, signal, _evidence_message_id(evidence), "review")
            created = self._db.get_application(created.id or 0) or created
        return created

    def _advance_status(
        self,
        record: Application,
        signal: ApplicationStatus,
        message_id: str,
        trigger: str = "email",
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
            trigger=trigger,
            message_id=message_id,
        )
        self._record_status_event(
            record.id, signal, record.updated_at, message_id, history.id, trigger
        )

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
