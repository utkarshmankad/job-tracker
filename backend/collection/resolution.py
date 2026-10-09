"""What a collected observation means for the tracker — conservative by construction.

Order of decisions for one new observation (its evidence already claimed):

1. **Known source item.** If an earlier observation of the same site item was linked to an
   application — by the resolver or by a person in review — the new observation is linked
   to that application (following merges to the survivor), and a status the site reports
   is applied only through ``StatusUpdater`` (valid forward transitions only). Re-collecting
   or a status change therefore never creates another application.
2. **Item already in review.** If the item's earlier evidence is still waiting for a person,
   the new observation joins the queue instead of being decided separately.
3. **Phase 2 resolver** (same signals, weights and thresholds as Gmail evidence):
   - ``linked`` is accepted only if ``verify_link`` finds no conflicting job ID, URL,
     company, role, date or source and the target is active — and, for observations whose
     selectors were not live-verified, only when a strong identifier (same job ID or
     canonical URL) names the application. Otherwise → review.
   - ``new_application`` creates an application only when the row proves submission *and*
     the extraction was ``verified``. Otherwise → review.
   - ``review_required`` → the evidence review queue; ``ignored`` → ignored.

Nothing here merges applications, overwrites a person's decision (``record_resolution``
refuses human-owned evidence), or changes an application except through the documented
creation and status-transition paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

import structlog

from backend.collection.contract import (
    CollectorStatus,
    ObservedApplication,
    portal_for,
    status_signal,
)
from backend.collection.ingest import Decider, Decision
from backend.db.data_store import DataStore, EvidenceConflictError
from backend.db.models import (
    Application,
    ApplicationStatus,
    DecisionSource,
    Evidence,
    EvidenceStatus,
    LinkMethod,
    ObservationDecision,
    SourceItem,
    SourceObservation,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import (
    RESOLVER_VERSION,
    IdentityResolver,
    MessageKind,
    Outcome,
    ResolutionResult,
    signals_from_evidence,
)
from backend.engine.reconciliation import verify_link
from backend.engine.status_updater import StatusUpdater

log = structlog.get_logger(__name__)

_PENDING_REVIEW = {EvidenceStatus.NEEDS_REVIEW.value, EvidenceStatus.DEFERRED.value}


def _resolution(outcome: str, reason: str, confidence: float, app_id: int | None) -> dict[str, Any]:
    return {
        "version": RESOLVER_VERSION,
        "outcome": outcome,
        "reason": reason,
        "explanation": reason.replace("_", " ").capitalize(),
        "confidence": confidence,
        "selected_application_id": app_id,
        "link_method": LinkMethod.SOURCE_ITEM.value if app_id else None,
        "message_kind": None,
        "candidates": [],
    }


class ResolverDecider:
    def __init__(self, store: DataStore) -> None:
        self._store = store
        self._resolver = IdentityResolver(store)
        self._updater = StatusUpdater(store, DuplicateDetector(store), self._resolver)

    def decide(
        self,
        item: SourceItem,
        observation: SourceObservation,
        observed: ObservedApplication,
        evidence: Evidence,
    ) -> Decision:
        assert item.id is not None and evidence.id is not None
        known = self._store.resolve_source_item_application(item.id)
        if known is not None:
            return self._link_known(item, observed, evidence, known)
        if any(
            e.processing_status in _PENDING_REVIEW and e.id != evidence.id
            for e in self._store.source_item_evidence(item.id)
        ):
            return self._review(evidence, "source_item_pending_review", None)

        kind = (
            MessageKind.ACKNOWLEDGEMENT if observed.proves_submission else MessageKind.STATUS_UPDATE
        )
        signals = signals_from_evidence(evidence, kind)
        result = self._resolver.resolve(signals)

        if result.outcome is Outcome.LINKED:
            assert result.application_id is not None
            target = self._store.get_application(result.application_id)
            checks = verify_link(signals, target)
            best = result.candidates[0] if result.candidates else None
            strong = (
                best is not None and best.application_id == result.application_id and best.strong
            )
            if not all(checks.values()):
                return self._review(evidence, "auto_link_failed_checks", result)
            if observed.extraction != "verified" and not strong:
                return self._review(evidence, "unverified_extraction_needs_confirmation", result)
            return self._link(evidence, observed, result, result.application_id)

        if result.outcome is Outcome.NEW_APPLICATION:
            if not observed.proves_submission:
                return self._review(evidence, "no_submission_proof", result)
            if observed.extraction != "verified":
                return self._review(evidence, "unverified_extraction_new_application", result)
            return self._create(evidence, observed, result)

        if result.outcome is Outcome.IGNORED:
            self._store.record_resolution(
                evidence.id, resolution=result.to_json(), status=EvidenceStatus.IGNORED.value
            )
            return Decision(ObservationDecision.IGNORED.value, result.reason, result.confidence)
        return self._review(evidence, result.reason, result)

    # ------------------------------------------------------------------ #

    def _link_known(
        self, item: SourceItem, observed: ObservedApplication, evidence: Evidence, app_id: int
    ) -> Decision:
        assert evidence.id is not None
        confidence = 1.0 if item.id_kind == "source_id" else 0.9
        recorded = self._store.record_resolution(
            evidence.id,
            resolution=_resolution("linked", "known_source_item", confidence, app_id),
            status=EvidenceStatus.LINKED.value,
            application_id=app_id,
            link_method=LinkMethod.SOURCE_ITEM.value,
            link_confidence=confidence,
        )
        if recorded is None:
            return Decision(ObservationDecision.UNCHANGED.value, "human_decision_retained")
        self._apply_status(app_id, observed, evidence)
        return Decision(ObservationDecision.LINKED.value, "known_source_item", confidence, app_id)

    def _link(
        self,
        evidence: Evidence,
        observed: ObservedApplication,
        result: ResolutionResult,
        app_id: int,
    ) -> Decision:
        assert evidence.id is not None
        recorded = self._store.record_resolution(
            evidence.id,
            resolution=result.to_json(),
            status=EvidenceStatus.LINKED.value,
            application_id=app_id,
            link_method=result.link_method,
            link_confidence=result.confidence,
        )
        if recorded is None:
            return Decision(ObservationDecision.UNCHANGED.value, "human_decision_retained")
        self._apply_status(app_id, observed, evidence)
        return Decision(ObservationDecision.LINKED.value, result.reason, result.confidence, app_id)

    def _create(
        self, evidence: Evidence, observed: ObservedApplication, result: ResolutionResult
    ) -> Decision:
        assert evidence.id is not None
        portal, method = portal_for(observed.source_key)
        applied = (
            datetime.combine(observed.applied_on, time.min, tzinfo=UTC)
            if observed.applied_on
            else observed.observed_at
        )
        try:
            created = self._store.create_application_from_evidence(
                Application(
                    company=observed.company,
                    role=observed.role,
                    source_portal=portal,
                    application_method=method,
                    job_url=observed.job_url,
                    applied_date=applied,
                    current_status=ApplicationStatus.APPLIED,
                ),
                evidence.id,
                decided_by=DecisionSource.RESOLVER.value,
                history_trigger="collector",
                resolution=result.to_json(),
            )
        except EvidenceConflictError:
            return Decision(ObservationDecision.UNCHANGED.value, "human_decision_retained")
        assert created.id is not None
        self._apply_status(created.id, observed, evidence)
        log.info(
            "collector_application_created", application_id=created.id, evidence_id=evidence.id
        )
        return Decision(
            ObservationDecision.CREATED.value, result.reason, result.confidence, created.id
        )

    def _review(self, evidence: Evidence, reason: str, result: ResolutionResult | None) -> Decision:
        assert evidence.id is not None
        base = result.to_json() if result else _resolution("review_required", reason, 0.0, None)
        recorded = self._store.record_resolution(
            evidence.id,
            resolution={**base, "outcome": "review_required", "reason": reason},
            status=EvidenceStatus.NEEDS_REVIEW.value,
            review_reason=reason,
        )
        if recorded is None:
            return Decision(ObservationDecision.UNCHANGED.value, "human_decision_retained")
        return Decision(
            ObservationDecision.REVIEW.value, reason, result.confidence if result else None
        )

    def _apply_status(self, app_id: int, observed: ObservedApplication, evidence: Evidence) -> None:
        """Apply the site's status to the application through the one documented path
        (StatusUpdater, which only performs valid forward transitions)."""
        signal = status_signal(observed.status)
        if signal is None or observed.status is CollectorStatus.UNKNOWN:
            return
        record = self._store.get_application(app_id)
        if record is None:
            return
        self._updater._apply_signal(
            record, signal, evidence.external_id or f"evidence:{evidence.id}", "collector"
        )


def collection_decider(db: DataStore) -> Decider:
    return ResolverDecider(db)
