"""Turn validated observation batches into source items, immutable observations and
evidence, idempotently.

For each observation:

1. The item is created on first sight (unique ``(source_key, item_key)``).
2. The observation is inserted once per distinct content (unique ``(item, content_hash)``).
   An identical re-observation only moves ``last_seen_at``: outcome ``unchanged``.
3. A new observation becomes one ``portal_import`` evidence row (unique by source and
   ``<source>:<item_key>#<content hash>``), with provenance back to the run and observation.
4. A decider (``backend/collection/resolution.py``) decides what the evidence means, after
   claiming it, so concurrent or retried batches decide each observation exactly once.

An observation whose decision never completed (the process stopped mid-way) is still
``pending`` and is picked up again by the next submission of the same content, so an
interrupted run can simply be retried.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from typing import Any, Protocol

import structlog
from sqlalchemy.exc import SQLAlchemyError

from backend.collection.contract import (
    CONTRACT_VERSION,
    ObservationBatch,
    ObservedApplication,
    evidence_source,
    portal_for,
    status_signal,
)
from backend.db.data_store import DataStore
from backend.db.models import (
    CollectionRun,
    CollectionRunStatus,
    Evidence,
    EvidenceType,
    ObservationDecision,
    SourceItem,
    SourceObservation,
    utc_now,
)

log = structlog.get_logger(__name__)

COUNTER_FOR_OUTCOME = {
    ObservationDecision.CREATED.value: "created_count",
    ObservationDecision.LINKED.value: "linked_count",
    ObservationDecision.REVIEW.value: "review_count",
    ObservationDecision.UNCHANGED.value: "unchanged_count",
    ObservationDecision.ERROR.value: "error_count",
}


@dataclass(frozen=True)
class Decision:
    outcome: str  # an ObservationDecision value
    reason: str
    confidence: float | None = None
    application_id: int | None = None


class Decider(Protocol):
    def decide(
        self,
        item: SourceItem,
        observation: SourceObservation,
        observed: ObservedApplication,
        evidence: Evidence,
    ) -> Decision: ...


class PendingDecider:
    """Stores observations without deciding them (used until a resolver is configured)."""

    def decide(
        self,
        item: SourceItem,
        observation: SourceObservation,
        observed: ObservedApplication,
        evidence: Evidence,
    ) -> Decision:
        return Decision(ObservationDecision.PENDING.value, "awaiting_resolution")


@dataclass
class ObservationResult:
    index: int
    outcome: str
    reason: str
    item_key: str | None = None
    application_id: int | None = None
    observation_id: int | None = None


@dataclass
class BatchResult:
    results: list[ObservationResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for result in self.results:
            out[result.outcome] = out.get(result.outcome, 0) + 1
        return dict(sorted(out.items()))

    def to_json(self) -> dict[str, Any]:
        return {"results": [asdict(r) for r in self.results], "counts": self.counts()}


class RunNotOpenError(ValueError):
    """Observations were sent to a run that has already finished."""


def _occurred_at(observed: ObservedApplication) -> datetime:
    if observed.applied_on:
        return datetime.combine(observed.applied_on, time.min, tzinfo=UTC)
    return observed.observed_at


class ObservationIngestor:
    def __init__(self, store: DataStore, decider: Decider | None = None) -> None:
        self._store = store
        self._decider = decider or PendingDecider()

    def ingest_batch(self, run: CollectionRun, batch: ObservationBatch) -> BatchResult:
        if run.status != CollectionRunStatus.RUNNING.value:
            raise RunNotOpenError("This collection run has already finished")
        assert run.id is not None
        result = BatchResult()
        for index, observed in enumerate(batch.observations):
            if observed.source_key != run.source_key:
                result.results.append(
                    ObservationResult(index, ObservationDecision.ERROR.value, "source_mismatch")
                )
                continue
            try:
                result.results.append(self._ingest_one(run, observed, index))
            except (ValueError, LookupError, RuntimeError, SQLAlchemyError) as exc:
                log.warning(
                    "collector_observation_failed",
                    run_id=run.id,
                    index=index,
                    error=type(exc).__name__,
                )
                result.results.append(
                    ObservationResult(index, ObservationDecision.ERROR.value, "processing_error")
                )
        deltas = {"observations_received": len(batch.observations)}
        for item in result.results:
            counter = COUNTER_FOR_OUTCOME.get(item.outcome)
            if counter:
                deltas[counter] = deltas.get(counter, 0) + 1
        self._store.increment_run_counters(run.id, **deltas)
        log.info("collector_batch_ingested", run_id=run.id, **result.counts())
        return result

    # ------------------------------------------------------------------ #

    def _ingest_one(
        self, run: CollectionRun, observed: ObservedApplication, index: int
    ) -> ObservationResult:
        assert run.id is not None
        item_key, id_kind = observed.item_identity()
        external = observed.external_job()
        item, _ = self._store.upsert_source_item(
            {
                "source_key": observed.source_key,
                "item_key": item_key,
                "id_kind": id_kind,
                "source_item_id": observed.source_item_id,
                "company": observed.company,
                "role": observed.role,
                "canonical_url": observed.job_url,
                "external_job_id": external[1] if external else None,
                "applied_at": _occurred_at(observed) if observed.applied_on else None,
                "status": observed.status.value,
                "raw_status": observed.raw_status,
                "first_run_id": run.id,
                "last_run_id": run.id,
            }
        )
        assert item.id is not None
        content_hash = observed.content_hash()
        row, created = self._store.insert_source_observation(
            {
                "source_item_id": item.id,
                "run_id": run.id,
                "source_key": observed.source_key,
                "contract_version": CONTRACT_VERSION,
                "collector_version": run.collector_version,
                "adapter_version": observed.adapter_version,
                "extraction": observed.extraction,
                "content_hash": content_hash,
                "fingerprint": observed.fingerprint(),
                "observed_at": observed.observed_at,
                "payload": observed.content(),
                "decision": ObservationDecision.PENDING.value,
            }
        )
        assert row.id is not None
        now = utc_now()
        if not created and row.decision != ObservationDecision.PENDING.value:
            self._store.update_source_item(item.id, last_seen_at=now, last_run_id=run.id)
            return ObservationResult(
                index,
                ObservationDecision.UNCHANGED.value,
                "already_observed",
                item_key,
                item.application_id,
                row.id,
            )

        evidence = self._evidence_for(row, item, observed, run)
        assert evidence.id is not None
        if row.evidence_id is None:
            self._store.update_source_observation(row.id, evidence_id=evidence.id)
        if not self._store.claim_evidence(evidence.id):
            # Another request is deciding this observation right now (or a person owns it).
            return ObservationResult(
                index, ObservationDecision.UNCHANGED.value, "in_progress", item_key, None, row.id
            )
        decision = self._decider.decide(item, row, observed, evidence)
        self._store.update_source_observation(
            row.id,
            decision=decision.outcome,
            decision_reason=decision.reason,
            confidence=decision.confidence,
        )
        item_update: dict[str, Any] = {
            "last_seen_at": now,
            "last_run_id": run.id,
            "latest_observation_id": row.id,
            "status": observed.status.value,
            "raw_status": observed.raw_status,
            "decision": decision.outcome,
            "decision_reason": decision.reason,
            "confidence": decision.confidence,
            "needs_attention": decision.outcome == ObservationDecision.REVIEW.value,
        }
        if decision.application_id is not None:
            item_update["application_id"] = decision.application_id
        self._store.update_source_item(item.id, **item_update)
        return ObservationResult(
            index,
            decision.outcome,
            decision.reason,
            item_key,
            decision.application_id,
            row.id,
        )

    def _evidence_for(
        self,
        row: SourceObservation,
        item: SourceItem,
        observed: ObservedApplication,
        run: CollectionRun,
    ) -> Evidence:
        portal, _method = portal_for(observed.source_key)
        signal = status_signal(observed.status)
        source = evidence_source(observed.source_key)
        evidence, _ = self._store.insert_evidence(
            Evidence(
                evidence_type=EvidenceType.PORTAL_IMPORT.value,
                source=source,
                external_id=f"{observed.source_key}:{item.item_key}#{row.content_hash[:16]}",
                occurred_at=_occurred_at(observed),
                raw_metadata={
                    "parser": {
                        "classification": "collector",
                        "company": observed.company,
                        "role": observed.role,
                        "job_url": observed.job_url,
                        "portal": portal,
                        "status_signal": signal.value if signal else None,
                    },
                    "collector": {
                        "contract_version": CONTRACT_VERSION,
                        "source_key": observed.source_key,
                        "item_key": item.item_key,
                        "run_key": run.run_key,
                        "observation_id": row.id,
                        "status": observed.status.value,
                        "proves_submission": observed.proves_submission,
                        "extraction": observed.extraction,
                    },
                },
            )
        )
        return evidence
