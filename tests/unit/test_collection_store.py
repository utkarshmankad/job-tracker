"""DataStore source-collection storage: enrollment, runs, batches, items, observations."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from backend.db.collection_store import CollectionConflictError
from backend.db.data_store import DataStore
from backend.db.models import utc_now


@pytest.fixture
def store(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "collect.db")


def _collector(store: DataStore, scopes=("linkedin",), token_id="tid-1"):
    return store.create_collector(
        name="laptop", token_id=token_id, scopes=list(scopes), created_by="owner@example.com"
    )


def _run(store: DataStore, collector_id: int, run_key: str = "run-1", source: str = "linkedin"):
    return store.start_collection_run(
        run_key=run_key,
        collector_id=collector_id,
        source_key=source,
        account_label="default",
        collector_version="0.1.0",
        adapter_version="linkedin/1",
    )


def _observation(item_id: int, run_id: int, content_hash: str = "c1") -> dict:
    return {
        "source_item_id": item_id,
        "run_id": run_id,
        "source_key": "linkedin",
        "contract_version": 1,
        "collector_version": "0.1.0",
        "adapter_version": "linkedin/1",
        "extraction": "verified",
        "content_hash": content_hash,
        "fingerprint": "fp",
        "observed_at": utc_now(),
        "payload": {"status": "applied"},
        "decision": "review",
    }


# ------------------------------------------------------------------ #
# Enrollment, rotation, revocation                                     #
# ------------------------------------------------------------------ #


def test_enrollment_code_is_single_use(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None and collector.token_hash is None
    store.add_collector_enrollment(collector.id, "code-hash", utc_now() + timedelta(minutes=10))
    enrolled = store.redeem_collector_enrollment("code-hash", "secret-hash")
    assert enrolled is not None and enrolled.token_hash == "secret-hash"
    assert enrolled.enrolled_at is not None
    assert store.redeem_collector_enrollment("code-hash", "other") is None
    assert store.get_collector(collector.id).token_hash == "secret-hash"


def test_expired_unknown_and_superseded_codes_are_refused(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    store.add_collector_enrollment(collector.id, "old", utc_now() + timedelta(minutes=10))
    store.add_collector_enrollment(collector.id, "expired", utc_now() - timedelta(seconds=1))
    assert store.redeem_collector_enrollment("old", "h") is None  # superseded by a newer code
    assert store.redeem_collector_enrollment("expired", "h") is None
    assert store.redeem_collector_enrollment("never-issued", "h") is None


def test_concurrent_redemption_has_one_winner(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    store.add_collector_enrollment(collector.id, "race", utc_now() + timedelta(minutes=10))
    results: list[object] = []
    barrier = threading.Barrier(4)

    def redeem(n: int) -> None:
        barrier.wait()
        results.append(store.redeem_collector_enrollment("race", f"hash-{n}"))

    threads = [threading.Thread(target=redeem, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert store.get_collector(collector.id).token_hash == winners[0].token_hash


def test_rotation_invalidates_the_secret_and_changes_token_id(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    store.add_collector_enrollment(collector.id, "c", utc_now() + timedelta(minutes=10))
    store.redeem_collector_enrollment("c", "secret")
    rotated = store.rotate_collector(collector.id, token_id="tid-2")
    assert rotated.token_hash is None and rotated.token_id == "tid-2"
    assert store.get_collector_by_token_id("tid-1") is None


def test_revocation_is_permanent_and_kills_pending_codes(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    store.add_collector_enrollment(collector.id, "pending", utc_now() + timedelta(minutes=10))
    revoked = store.revoke_collector(collector.id, revoked_by="owner@example.com")
    assert revoked.revoked_at is not None and revoked.token_hash is None
    assert store.redeem_collector_enrollment("pending", "h") is None
    assert store.revoke_collector(collector.id, revoked_by="x").revoked_by == "owner@example.com"
    with pytest.raises(CollectionConflictError):
        store.rotate_collector(collector.id, token_id="new")


# ------------------------------------------------------------------ #
# Runs and batches                                                     #
# ------------------------------------------------------------------ #


def test_run_start_is_idempotent_and_keys_are_owned(store: DataStore) -> None:
    first = _collector(store)
    second = _collector(store, token_id="tid-other")
    assert first.id is not None and second.id is not None
    run, created = _run(store, first.id)
    again, created_again = _run(store, first.id)
    assert created and not created_again and run.id == again.id
    with pytest.raises(CollectionConflictError):
        _run(store, second.id)
    with pytest.raises(CollectionConflictError):
        _run(store, first.id, source="naukri")


def test_finish_updates_source_state(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    _run(store, collector.id, "r-ok")
    store.finish_collection_run(
        "r-ok",
        status="succeeded",
        items_seen=3,
        error_code=None,
        error_message=None,
        diagnostics={"pages": 1},
    )
    source = store.list_collection_sources()[0]
    assert source.last_status == "succeeded" and source.last_success_at is not None
    assert not source.needs_attention

    _run(store, collector.id, "r-out")
    store.finish_collection_run(
        "r-out",
        status="signed_out",
        items_seen=0,
        error_code="signed_out",
        error_message="Sign in required",
        diagnostics={},
    )
    source = store.list_collection_sources()[0]
    assert source.needs_attention and source.attention_reason == "signed_out"
    assert source.last_success_at is not None  # the earlier success is kept


def test_finish_is_idempotent_but_not_rewritable(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    _run(store, collector.id)
    kwargs = dict(items_seen=1, error_code=None, error_message=None, diagnostics={})
    store.finish_collection_run("run-1", status="succeeded", **kwargs)
    store.finish_collection_run("run-1", status="succeeded", **kwargs)
    with pytest.raises(CollectionConflictError):
        store.finish_collection_run("run-1", status="failed", **kwargs)
    with pytest.raises(ValueError):
        store.finish_collection_run("run-1", status="running", **kwargs)
    with pytest.raises(LookupError):
        store.finish_collection_run("missing", status="failed", **kwargs)


def test_batch_replay_returns_first_result(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    run, _ = _run(store, collector.id)
    assert run.id is not None
    first = store.save_collection_batch(run.id, "b1", 2, {"outcomes": ["created"]})
    second = store.save_collection_batch(run.id, "b1", 2, {"outcomes": ["different"]})
    assert first.id == second.id and second.result == {"outcomes": ["created"]}


def test_run_counters_accumulate(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    run, _ = _run(store, collector.id)
    assert run.id is not None
    store.increment_run_counters(run.id, created_count=1, review_count=2)
    store.increment_run_counters(run.id, created_count=1)
    run = store.get_collection_run(run.id)
    assert (run.created_count, run.review_count) == (2, 2)
    with pytest.raises(ValueError):
        store.increment_run_counters(run.id, status=1)


# ------------------------------------------------------------------ #
# Items and observations                                               #
# ------------------------------------------------------------------ #


def test_source_items_and_observations_are_idempotent(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    run, _ = _run(store, collector.id)
    values = {
        "source_key": "linkedin",
        "item_key": "id:42",
        "id_kind": "source_id",
        "company": "Co",
    }
    item, created = store.upsert_source_item(values)
    same, created_again = store.upsert_source_item({**values, "company": "Changed"})
    assert created and not created_again and same.id == item.id and same.company == "Co"
    assert item.id is not None and run.id is not None
    obs, new = store.insert_source_observation(_observation(item.id, run.id))
    dup, new_again = store.insert_source_observation(_observation(item.id, run.id))
    assert new and not new_again and obs.id == dup.id
    other, new_status = store.insert_source_observation(_observation(item.id, run.id, "c2"))
    assert new_status and other.id != obs.id


def test_concurrent_identical_observations_have_one_claim(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    run, _ = _run(store, collector.id)
    item, _ = store.upsert_source_item(
        {"source_key": "linkedin", "item_key": "fp:abc", "id_kind": "fingerprint", "company": "Co"}
    )
    assert item.id is not None and run.id is not None
    claims: list[bool] = []
    barrier = threading.Barrier(4)

    def insert() -> None:
        barrier.wait()
        claims.append(store.insert_source_observation(_observation(item.id, run.id))[1])

    threads = [threading.Thread(target=insert) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claims) == [False, False, False, True]


def test_item_identity_is_immutable(store: DataStore) -> None:
    item, _ = store.upsert_source_item(
        {"source_key": "naukri", "item_key": "id:1", "id_kind": "source_id", "company": "Co"}
    )
    assert item.id is not None
    store.update_source_item(item.id, status="rejected", decision="linked")
    with pytest.raises(ValueError):
        store.update_source_item(item.id, item_key="id:2")
    assert store.get_source_item(item.id).status == "rejected"


def test_metrics_are_aggregate_only(store: DataStore) -> None:
    collector = _collector(store)
    assert collector.id is not None
    run, _ = _run(store, collector.id)
    assert run.id is not None
    store.increment_run_counters(run.id, observations_received=3, created_count=1)
    metrics = store.collection_metrics()
    assert metrics["runs_by_status"] == {"running": 1}
    # Run counters are processing totals, never presented as unique stored rows.
    assert metrics["processed_across_runs"]["items_processed"] == 3
    assert metrics["unique"] == {"source_items": 0, "observations": 0}
    assert set(metrics) == {
        "runs_by_status",
        "unique",
        "observations_by_decision",
        "processed_across_runs",
        "items_by_decision",
        "items_by_source",
    }
