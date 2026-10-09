"""DataStore methods for Phase 3 source collection (revision 0005).

Kept in a mixin so data_store.py stays navigable; DataStore inherits it, so all database
access still goes through the DataStore class (CLAUDE.md). Every insert that a retried or
concurrent request could repeat uses INSERT … ON CONFLICT DO NOTHING followed by a read,
so the database's unique constraints — not application logic — decide idempotency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Engine, Table, func, update
from sqlalchemy import select as core_select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, SQLModel, col, select

from backend.db.models import (
    Application,
    CollectionBatch,
    CollectionRun,
    CollectionRunStatus,
    CollectionSource,
    Collector,
    CollectorEnrollment,
    Evidence,
    RecordState,
    SourceItem,
    SourceObservation,
    utc_now,
)

# A finished run in one of these states leaves its source flagged for the user.
ATTENTION_STATUSES = frozenset(
    {
        CollectionRunStatus.SIGNED_OUT.value,
        CollectionRunStatus.CHALLENGED.value,
        CollectionRunStatus.FAILED.value,
        CollectionRunStatus.UNSUPPORTED.value,
    }
)
SUCCESS_STATUSES = frozenset({CollectionRunStatus.SUCCEEDED.value})
FINAL_STATUSES = frozenset(s.value for s in CollectionRunStatus) - {
    CollectionRunStatus.RUNNING.value
}
_COUNTERS = (
    "items_seen",
    "observations_received",
    "created_count",
    "linked_count",
    "review_count",
    "unchanged_count",
    "error_count",
)


class CollectionConflictError(ValueError):
    """A request conflicts with stored collection state (e.g. a run key reused by another
    collector, or a finished run being finished differently)."""


def _table(model: type[SQLModel]) -> Table:
    return model.__table__  # type: ignore[attr-defined]


class CollectionStoreMixin:
    _engine: Engine

    # ------------------------------------------------------------------ #
    # Collectors and enrollment                                            #
    # ------------------------------------------------------------------ #

    def create_collector(
        self, *, name: str, token_id: str, scopes: list[str], created_by: str | None
    ) -> Collector:
        with Session(self._engine, expire_on_commit=False) as session:
            collector = Collector(
                name=name, token_id=token_id, scopes=sorted(set(scopes)), created_by=created_by
            )
            session.add(collector)
            session.commit()
            session.refresh(collector)
            return collector

    def add_collector_enrollment(
        self, collector_id: int, code_hash: str, expires_at: datetime
    ) -> CollectorEnrollment:
        """Issue a new setup code; any earlier unused code for this collector stops working."""
        with Session(self._engine, expire_on_commit=False) as session:
            now = utc_now()
            session.execute(
                update(_table(CollectorEnrollment))
                .where(
                    _table(CollectorEnrollment).c.collector_id == collector_id,
                    _table(CollectorEnrollment).c.used_at.is_(None),
                )
                .values(used_at=now)
            )
            enrollment = CollectorEnrollment(
                collector_id=collector_id, code_hash=code_hash, expires_at=expires_at
            )
            session.add(enrollment)
            session.commit()
            session.refresh(enrollment)
            return enrollment

    def redeem_collector_enrollment(self, code_hash: str, token_hash: str) -> Collector | None:
        """Exchange an unused, unexpired code for the collector credential, atomically: the
        conditional UPDATE lets exactly one of several concurrent redemptions win. Returns
        None for an unknown, used, expired or revoked code."""
        now = utc_now()
        table = _table(CollectorEnrollment)
        with Session(self._engine, expire_on_commit=False) as session:
            enrollment = session.exec(
                select(CollectorEnrollment).where(CollectorEnrollment.code_hash == code_hash)
            ).first()
            if enrollment is None or enrollment.used_at is not None or enrollment.expires_at <= now:
                return None
            collector = session.get(Collector, enrollment.collector_id)
            if collector is None or collector.revoked_at is not None:
                return None
            claimed = session.execute(
                update(table)
                .where(table.c.id == enrollment.id, table.c.used_at.is_(None))
                .values(used_at=now)
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                session.rollback()
                return None
            collector.token_hash = token_hash
            collector.enrolled_at = now
            session.add(collector)
            session.commit()
            session.refresh(collector)
            return collector

    def get_collector(self, collector_id: int) -> Collector | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(Collector, collector_id)

    def get_collector_by_token_id(self, token_id: str) -> Collector | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.exec(select(Collector).where(Collector.token_id == token_id)).first()

    def list_collectors(self) -> list[Collector]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(session.exec(select(Collector).order_by(col(Collector.id))).all())

    def touch_collector(self, collector_id: int) -> None:
        with Session(self._engine) as session:
            session.execute(
                update(_table(Collector))
                .where(_table(Collector).c.id == collector_id)
                .values(last_used_at=utc_now())
            )
            session.commit()

    def rotate_collector(self, collector_id: int, *, token_id: str) -> Collector:
        """Invalidate the current secret immediately and give the collector a new public
        token ID; the caller then issues a new enrollment code."""
        with Session(self._engine, expire_on_commit=False) as session:
            collector = session.get(Collector, collector_id)
            if collector is None:
                raise LookupError(f"Collector {collector_id} not found")
            if collector.revoked_at is not None:
                raise CollectionConflictError("A revoked collector cannot be rotated")
            collector.token_id = token_id
            collector.token_hash = None
            collector.rotated_at = utc_now()
            session.add(collector)
            session.commit()
            session.refresh(collector)
            return collector

    def revoke_collector(self, collector_id: int, *, revoked_by: str | None) -> Collector:
        """Permanently disable a collector: its secret and any pending setup code stop
        working. Idempotent. Its runs and observations are kept for provenance."""
        with Session(self._engine, expire_on_commit=False) as session:
            collector = session.get(Collector, collector_id)
            if collector is None:
                raise LookupError(f"Collector {collector_id} not found")
            if collector.revoked_at is None:
                now = utc_now()
                collector.revoked_at = now
                collector.revoked_by = revoked_by
                collector.token_hash = None
                session.add(collector)
                session.execute(
                    update(_table(CollectorEnrollment))
                    .where(
                        _table(CollectorEnrollment).c.collector_id == collector_id,
                        _table(CollectorEnrollment).c.used_at.is_(None),
                    )
                    .values(used_at=now)
                )
                session.commit()
                session.refresh(collector)
            return collector

    # ------------------------------------------------------------------ #
    # Sources and runs                                                     #
    # ------------------------------------------------------------------ #

    def get_or_create_collection_source(
        self, source_key: str, account_label: str, collector_id: int | None
    ) -> CollectionSource:
        statement = (
            sqlite_insert(_table(CollectionSource))
            .values(
                source_key=source_key,
                account_label=account_label,
                collector_id=collector_id,
                created_at=utc_now(),
                needs_attention=False,
            )
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            session.execute(statement)
            session.commit()
            source = session.exec(
                select(CollectionSource).where(
                    CollectionSource.source_key == source_key,
                    CollectionSource.account_label == account_label,
                )
            ).one()
            if collector_id is not None and source.collector_id != collector_id:
                source.collector_id = collector_id
                session.add(source)
                session.commit()
                session.refresh(source)
            return source

    def start_collection_run(
        self,
        *,
        run_key: str,
        collector_id: int,
        source_key: str,
        account_label: str,
        collector_version: str,
        adapter_version: str,
    ) -> tuple[CollectionRun, bool]:
        """Start a run, idempotently: repeating the same run_key returns the existing run.
        A run key that belongs to another collector or source is a conflict."""
        source = self.get_or_create_collection_source(source_key, account_label, collector_id)
        assert source.id is not None
        now = utc_now()
        statement = (
            sqlite_insert(_table(CollectionRun))
            .values(
                run_key=run_key,
                collector_id=collector_id,
                source_id=source.id,
                source_key=source_key,
                status=CollectionRunStatus.RUNNING.value,
                started_at=now,
                collector_version=collector_version,
                adapter_version=adapter_version,
                diagnostics={},
                **dict.fromkeys(_COUNTERS, 0),
            )
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            inserted = session.execute(statement).rowcount == 1  # type: ignore[attr-defined]
            run = session.exec(select(CollectionRun).where(CollectionRun.run_key == run_key)).one()
            if run.collector_id != collector_id or run.source_id != source.id:
                session.rollback()
                raise CollectionConflictError("This run key belongs to another run")
            if inserted:
                session.execute(
                    update(_table(CollectionSource))
                    .where(_table(CollectionSource).c.id == source.id)
                    .values(last_attempt_at=now, last_run_id=run.id)
                )
            session.commit()
            return run, inserted

    def get_collection_run_by_key(self, run_key: str) -> CollectionRun | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.exec(
                select(CollectionRun).where(CollectionRun.run_key == run_key)
            ).first()

    def get_collection_run(self, run_id: int) -> CollectionRun | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(CollectionRun, run_id)

    def increment_run_counters(self, run_id: int, **deltas: int) -> None:
        unknown = set(deltas) - set(_COUNTERS)
        if unknown:
            raise ValueError(f"Unknown run counters: {sorted(unknown)}")
        table = _table(CollectionRun)
        values = {name: table.c[name] + delta for name, delta in deltas.items() if delta}
        if not values:
            return
        with Session(self._engine) as session:
            session.execute(update(table).where(table.c.id == run_id).values(**values))
            session.commit()

    def finish_collection_run(
        self,
        run_key: str,
        *,
        status: str,
        items_seen: int,
        error_code: str | None,
        error_message: str | None,
        diagnostics: dict[str, Any],
    ) -> CollectionRun:
        """Close a run and update its source's last-run state. Finishing an already-finished
        run with the same status is a no-op; with a different status it is a conflict."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"Not a final run status: {status}")
        with Session(self._engine, expire_on_commit=False) as session:
            run = session.exec(
                select(CollectionRun).where(CollectionRun.run_key == run_key)
            ).first()
            if run is None:
                raise LookupError("Run not found")
            if run.status != CollectionRunStatus.RUNNING.value:
                if run.status != status:
                    raise CollectionConflictError(f"Run already finished as {run.status}")
                return run
            now = utc_now()
            run.status = status
            run.finished_at = now
            run.items_seen = max(run.items_seen, items_seen)
            run.error_code = error_code
            run.error_message = error_message
            run.diagnostics = diagnostics
            session.add(run)
            source = session.get(CollectionSource, run.source_id)
            if source is not None:
                source.last_status = status
                source.last_run_id = run.id
                if status in SUCCESS_STATUSES:
                    source.last_success_at = now
                    source.needs_attention = False
                    source.attention_reason = None
                elif status in ATTENTION_STATUSES:
                    source.needs_attention = True
                    source.attention_reason = error_code or status
                session.add(source)
            session.commit()
            session.refresh(run)
            return run

    def list_collection_sources(self) -> list[CollectionSource]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(CollectionSource).order_by(
                        col(CollectionSource.source_key), col(CollectionSource.account_label)
                    )
                ).all()
            )

    def list_collection_runs(
        self, *, source_key: str | None = None, collector_id: int | None = None, limit: int = 50
    ) -> list[CollectionRun]:
        conditions = []
        if source_key:
            conditions.append(col(CollectionRun.source_key) == source_key)
        if collector_id is not None:
            conditions.append(col(CollectionRun.collector_id) == collector_id)
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(CollectionRun)
                    .where(*conditions)
                    .order_by(col(CollectionRun.started_at).desc(), col(CollectionRun.id).desc())
                    .limit(limit)
                ).all()
            )

    def collection_metrics(self, collector_id: int | None = None) -> dict[str, Any]:
        """Aggregate counts only — no names, URLs or page content.

        Two kinds of figure, never mixed:

        - ``unique``, ``observations_by_decision``, ``items_by_decision`` and
          ``items_by_source`` count stored rows: each source item once, and each immutable
          observation (one per distinct content of an item) once.
        - ``processed_across_runs`` sums the per-run counters. An item seen in two runs is
          processed twice, so ``items_processed`` grows with every run even when nothing
          new is stored.

        With ``collector_id`` every figure is limited to that collector: its runs, the
        observations those runs stored, and the items those observations belong to. An
        item another collector stored first, and this collector only re-saw unchanged,
        is therefore not counted for it.
        """
        with Session(self._engine) as session:
            run_filter = (
                [col(CollectionRun.collector_id) == collector_id]
                if collector_id is not None
                else []
            )
            observation_filter = (
                [
                    col(SourceObservation.run_id).in_(
                        select(CollectionRun.id).where(CollectionRun.collector_id == collector_id)
                    )
                ]
                if collector_id is not None
                else []
            )
            item_filter = (
                [
                    col(SourceItem.id).in_(
                        select(SourceObservation.source_item_id).where(*observation_filter)
                    )
                ]
                if collector_id is not None
                else []
            )
            runs_by_status = dict(
                session.exec(
                    select(CollectionRun.status, func.count())
                    .where(*run_filter)
                    .group_by(col(CollectionRun.status))
                ).all()
            )
            processed = session.execute(
                core_select(
                    func.coalesce(func.sum(CollectionRun.observations_received), 0),
                    func.coalesce(func.sum(CollectionRun.created_count), 0),
                    func.coalesce(func.sum(CollectionRun.linked_count), 0),
                    func.coalesce(func.sum(CollectionRun.review_count), 0),
                    func.coalesce(func.sum(CollectionRun.unchanged_count), 0),
                    func.coalesce(func.sum(CollectionRun.error_count), 0),
                ).where(*run_filter)
            ).one()
            unique_items = session.exec(
                select(func.count()).select_from(SourceItem).where(*item_filter)
            ).one()
            unique_observations = session.exec(
                select(func.count()).select_from(SourceObservation).where(*observation_filter)
            ).one()
            observations_by_decision = dict(
                session.exec(
                    select(SourceObservation.decision, func.count())
                    .where(*observation_filter)
                    .group_by(col(SourceObservation.decision))
                ).all()
            )
            items_by_decision = dict(
                session.exec(
                    select(SourceItem.decision, func.count())
                    .where(*item_filter)
                    .group_by(col(SourceItem.decision))
                ).all()
            )
            items_by_source = dict(
                session.exec(
                    select(SourceItem.source_key, func.count())
                    .where(*item_filter)
                    .group_by(col(SourceItem.source_key))
                ).all()
            )
        keys = ("items_processed", "created", "linked", "review", "unchanged", "errors")
        return {
            "runs_by_status": {str(k): int(v) for k, v in runs_by_status.items()},
            "unique": {"source_items": int(unique_items), "observations": int(unique_observations)},
            "observations_by_decision": {
                str(k): int(v) for k, v in observations_by_decision.items()
            },
            "processed_across_runs": dict(zip(keys, (int(v) for v in processed), strict=True)),
            "items_by_decision": {str(k): int(v) for k, v in items_by_decision.items()},
            "items_by_source": {str(k): int(v) for k, v in items_by_source.items()},
        }

    # ------------------------------------------------------------------ #
    # Batches                                                              #
    # ------------------------------------------------------------------ #

    def get_collection_batch(self, run_id: int, batch_key: str) -> CollectionBatch | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.exec(
                select(CollectionBatch).where(
                    CollectionBatch.run_id == run_id, CollectionBatch.batch_key == batch_key
                )
            ).first()

    def save_collection_batch(
        self, run_id: int, batch_key: str, item_count: int, result: dict[str, Any]
    ) -> CollectionBatch:
        """Record a processed batch. If a concurrent replay already recorded it, the first
        stored result wins and is returned."""
        statement = (
            sqlite_insert(_table(CollectionBatch))
            .values(
                run_id=run_id,
                batch_key=batch_key,
                received_at=utc_now(),
                item_count=item_count,
                result=result,
            )
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            session.execute(statement)
            session.commit()
        batch = self.get_collection_batch(run_id, batch_key)
        assert batch is not None
        return batch

    # ------------------------------------------------------------------ #
    # Source items and observations                                        #
    # ------------------------------------------------------------------ #

    def upsert_source_item(self, values: dict[str, Any]) -> tuple[SourceItem, bool]:
        """Create the item on first sight; afterwards return the stored one unchanged."""
        now = utc_now()
        statement = (
            sqlite_insert(_table(SourceItem))
            .values(
                first_seen_at=now,
                last_seen_at=now,
                needs_attention=False,
                **values,
            )
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            inserted = session.execute(statement).rowcount == 1  # type: ignore[attr-defined]
            session.commit()
            item = session.exec(
                select(SourceItem).where(
                    SourceItem.source_key == values["source_key"],
                    SourceItem.item_key == values["item_key"],
                )
            ).one()
            return item, inserted

    def insert_source_observation(self, values: dict[str, Any]) -> tuple[SourceObservation, bool]:
        """Insert an immutable observation. The (item, content_hash) unique constraint makes
        this the claim: of two concurrent identical observations exactly one is new."""
        statement = (
            sqlite_insert(_table(SourceObservation))
            .values(received_at=utc_now(), **values)
            .on_conflict_do_nothing()
        )
        with Session(self._engine, expire_on_commit=False) as session:
            inserted = session.execute(statement).rowcount == 1  # type: ignore[attr-defined]
            session.commit()
            observation = session.exec(
                select(SourceObservation).where(
                    SourceObservation.source_item_id == values["source_item_id"],
                    SourceObservation.content_hash == values["content_hash"],
                )
            ).one()
            return observation, inserted

    def update_source_observation(self, observation_id: int, **values: Any) -> None:
        allowed = {"evidence_id", "decision", "decision_reason", "confidence"}
        if set(values) - allowed:
            raise ValueError("Only decision fields of an observation can change")
        with Session(self._engine) as session:
            session.execute(
                update(_table(SourceObservation))
                .where(_table(SourceObservation).c.id == observation_id)
                .values(**values)
            )
            session.commit()

    def update_source_item(self, item_id: int, **values: Any) -> None:
        protected = {"id", "source_key", "item_key", "id_kind", "first_seen_at"}
        if set(values) & protected:
            raise ValueError("Source item identity fields are immutable")
        with Session(self._engine) as session:
            session.execute(
                update(_table(SourceItem))
                .where(_table(SourceItem).c.id == item_id)
                .values(**values)
            )
            session.commit()

    def get_source_item(self, item_id: int) -> SourceItem | None:
        with Session(self._engine, expire_on_commit=False) as session:
            return session.get(SourceItem, item_id)

    def source_item_evidence(self, item_id: int) -> list[Evidence]:
        """Evidence created from this item's observations, oldest first."""
        with Session(self._engine, expire_on_commit=False) as session:
            evidence_ids = select(SourceObservation.evidence_id).where(
                SourceObservation.source_item_id == item_id,
                col(SourceObservation.evidence_id).is_not(None),
            )
            return list(
                session.exec(
                    select(Evidence)
                    .where(col(Evidence.id).in_(evidence_ids))
                    .order_by(col(Evidence.id))
                ).all()
            )

    def resolve_source_item_application(self, item_id: int) -> int | None:
        """The active application this item belongs to, if any: the newest application one
        of its evidence rows is linked to (by the resolver or a person), following merges
        to the surviving record. A person's later review decision therefore applies to all
        future observations of the item."""
        linked = [e for e in self.source_item_evidence(item_id) if e.application_id is not None]
        if not linked:
            return None
        latest = max(linked, key=lambda e: (e.decided_at or e.updated_at, e.id or 0))
        app_id = latest.application_id
        with Session(self._engine, expire_on_commit=False) as session:
            for _ in range(10):
                app = session.get(Application, app_id)
                if app is None:
                    return None
                if app.record_state == RecordState.ACTIVE.value:
                    return app.id
                app_id = app.merged_into_application_id
                if app_id is None:
                    return None
        return None

    def list_run_observations(self, run_id: int, limit: int = 500) -> list[SourceObservation]:
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(SourceObservation)
                    .where(SourceObservation.run_id == run_id)
                    .order_by(col(SourceObservation.id))
                    .limit(limit)
                ).all()
            )

    def collector_review_observations(self, limit: int = 100) -> list[SourceObservation]:
        """The newest observation behind each collected evidence row waiting for a person."""
        with Session(self._engine, expire_on_commit=False) as session:
            return list(
                session.exec(
                    select(SourceObservation)
                    .join(Evidence, col(Evidence.id) == col(SourceObservation.evidence_id))
                    .where(col(Evidence.processing_status) == "needs_review")
                    .order_by(col(SourceObservation.id).desc())
                    .limit(limit)
                ).all()
            )

    def collector_review_evidence_ids(self) -> list[int]:
        """Evidence from collected observations that is waiting for a person."""
        return [o.evidence_id for o in self.collector_review_observations(1000) if o.evidence_id]
