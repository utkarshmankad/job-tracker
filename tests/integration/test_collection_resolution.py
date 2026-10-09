"""Collected observations → evidence → applications: conservative resolution, review
routing, idempotency, provenance and concurrency. Synthetic data only."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.collection.contract import ObservationBatch, ObservedApplication
from backend.collection.ingest import ObservationIngestor
from backend.collection.resolution import collection_decider
from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.main import app
from tests.unit.test_merge import merge

_BASE = "/api/v1"
LI_URL = "https://www.linkedin.com/jobs/view/4123456789/"


@pytest.fixture
def env(tmp_path, test_auth_user):
    db = DataStore(tmp_path / "resolution.db")
    collector = db.create_collector(
        name="laptop", token_id="tid", scopes=["linkedin", "naukri"], created_by=None
    )
    assert collector.id is not None
    with TestClient(app) as client:
        app.state.db = db
        app.state.updater = StatusUpdater(db, DuplicateDetector(db))
        yield SimpleNamespace(client=client, db=db, collector_id=collector.id)


def _run(env, source: str = "linkedin"):
    run, _ = env.db.start_collection_run(
        run_key=uuid.uuid4().hex,
        collector_id=env.collector_id,
        source_key=source,
        account_label="default",
        collector_version="0.1.0",
        adapter_version=f"{source}/0.1.0",
    )
    return run


def _obs(**overrides) -> ObservedApplication:
    base = {
        "source_key": "linkedin",
        "source_item_id": "li-1",
        "company": "Northwind Robotics",
        "role": "Engineering Manager",
        "applied_on": (datetime.now(UTC) - timedelta(days=5)).date(),
        "status": "applied",
        "raw_status": "Applied",
        "job_url": LI_URL,
        "proves_submission": True,
        "extraction": "verified",
        "observed_at": datetime.now(UTC),
        "adapter_version": "linkedin/0.1.0",
    }
    return ObservedApplication.model_validate({**base, **overrides})


def _ingest(env, *observations, run=None):
    run = run or _run(env, observations[0].source_key)
    batch = ObservationBatch(
        batch_key=uuid.uuid4().hex, sent_at=utc_now(), observations=list(observations)
    )
    return ObservationIngestor(env.db, collection_decider(env.db)).ingest_batch(run, batch).results


def _apps(env) -> list[Application]:
    return env.db.get_applications(ApplicationFilter(page_size=1000))[0]


def _existing(env, **fields) -> Application:
    values = {
        "company": "Northwind Robotics",
        "role": "Engineering Manager",
        "source_portal": "LinkedIn",
        "applied_date": utc_now() - timedelta(days=5),
        "current_status": ApplicationStatus.APPLIED,
        **fields,
    }
    return env.db.upsert_application(Application(**values))


# ------------------------------------------------------------------ #
# Creation, repeats, status changes, provenance                        #
# ------------------------------------------------------------------ #


def test_verified_submission_creates_one_application_with_provenance(env) -> None:
    [result] = _ingest(env, _obs())
    assert result.outcome == "created"
    [created] = _apps(env)
    assert (created.company, created.source_portal, created.application_method) == (
        "Northwind Robotics",
        "LinkedIn",
        "Easy Apply",
    )
    assert created.job_url == "https://linkedin.com/jobs/view/4123456789"
    observation = env.db.list_run_observations(1)[0]
    evidence = env.db.get_evidence(observation.evidence_id)
    assert evidence.application_id == created.id
    assert evidence.processing_status == "created_application"
    assert evidence.raw_metadata["collector"]["observation_id"] == observation.id
    assert env.db.get_source_item(observation.source_item_id).application_id == created.id


def test_repeats_status_changes_and_overlapping_runs_never_duplicate(env) -> None:
    _ingest(env, _obs())
    assert _ingest(env, _obs())[0].outcome == "unchanged"
    assert _ingest(env, _obs(), run=_run(env))[0].outcome == "unchanged"
    rejected = _ingest(env, _obs(status="rejected", raw_status="Not selected"))[0]
    assert (rejected.outcome, rejected.reason) == ("linked", "known_source_item")
    [only] = _apps(env)
    assert only.current_status == ApplicationStatus.REJECTED
    history = env.db.get_status_history(only.id)
    assert history[-1].trigger == "collector"


def test_fingerprint_items_survive_status_changes(env) -> None:
    first = _ingest(env, _obs(source_item_id=None, job_url=None))[0]
    second = _ingest(env, _obs(source_item_id=None, job_url=None, status="shortlisted"))[0]
    assert first.item_key == second.item_key and first.item_key.startswith("fp:")
    assert second.outcome == "linked" and len(_apps(env)) == 1


# ------------------------------------------------------------------ #
# Conservative resolution and review                                   #
# ------------------------------------------------------------------ #


def test_unverified_selectors_never_create_applications(env) -> None:
    [result] = _ingest(env, _obs(extraction="unverified"))
    assert (result.outcome, result.reason) == ("review", "unverified_extraction_new_application")
    assert _apps(env) == []
    queue = env.client.get(f"{_BASE}/collection/review").json()
    assert len(queue) == 1
    assert queue[0]["evidence"]["review_reason"] == "unverified_extraction_new_application"
    assert (queue[0]["company"], queue[0]["extraction"]) == ("Northwind Robotics", "unverified")
    assert env.client.get(f"{_BASE}/evidence/review").json()["total"] == 1


def test_a_persons_review_decision_applies_to_later_observations(env) -> None:
    _ingest(env, _obs(extraction="unverified"))
    pending = _ingest(env, _obs(extraction="unverified", status="in_review"))[0]
    assert (pending.outcome, pending.reason) == ("review", "source_item_pending_review")
    evidence_id = env.db.collector_review_evidence_ids()[-1]
    created = env.client.post(f"{_BASE}/evidence/{evidence_id}/create-application", json={})
    assert created.status_code == 200, created.text
    later = _ingest(env, _obs(extraction="unverified", status="interview"))[0]
    assert (later.outcome, later.reason) == ("linked", "known_source_item")
    [only] = _apps(env)
    assert only.current_status == ApplicationStatus.INTERVIEW_SCHEDULED


def test_strong_identifier_links_even_unverified_rows(env) -> None:
    existing = _existing(env, job_url=LI_URL, source_portal="Direct/Unknown")
    [result] = _ingest(env, _obs(extraction="unverified"))
    assert result.outcome == "linked" and result.application_id == existing.id
    assert len(_apps(env)) == 1


def test_text_only_match_from_unverified_rows_needs_confirmation(env) -> None:
    _existing(env)
    [result] = _ingest(env, _obs(extraction="unverified", job_url=None))
    assert (result.outcome, result.reason) == ("review", "unverified_extraction_needs_confirmation")


def test_text_only_match_from_verified_rows_links(env) -> None:
    existing = _existing(env)
    [result] = _ingest(env, _obs(job_url=None))
    assert result.outcome == "linked" and result.application_id == existing.id


def test_conflicting_job_id_is_never_linked(env) -> None:
    _existing(env, job_url="https://www.linkedin.com/jobs/view/4999999999/")
    [result] = _ingest(env, _obs())
    assert result.outcome == "review"
    assert len(_apps(env)) == 1


def test_ambiguous_duplicates_go_to_review_and_are_never_merged(env) -> None:
    first = _existing(env)
    second = _existing(env, role="Engineering Manager ")
    [result] = _ingest(env, _obs(job_url=None))
    assert result.outcome == "review"
    assert env.db.list_merge_operations(1, 10)[1] == 0
    assert {a.id for a in _apps(env)} == {first.id, second.id}


def test_status_without_submission_proof_is_reviewed(env) -> None:
    [result] = _ingest(env, _obs(proves_submission=False, status="interview"))
    assert result.outcome == "review" and _apps(env) == []


def test_known_item_follows_merges_to_the_survivor(env) -> None:
    _ingest(env, _obs())
    [collected] = _apps(env)
    other = _existing(env, company="Contoso Analytics", role="Staff Engineer")
    merge(env.db, [collected.id, other.id], survivor=other.id)
    [result] = _ingest(env, _obs(status="shortlisted"))
    assert result.outcome == "linked" and result.application_id == other.id


def test_human_owned_evidence_is_never_overwritten(env) -> None:
    _ingest(env, _obs(extraction="unverified"))
    evidence_id = env.db.collector_review_evidence_ids()[0]
    assert env.client.post(f"{_BASE}/evidence/{evidence_id}/dismiss").status_code == 200
    # The same observation replayed must not re-decide the person's dismissal.
    assert _ingest(env, _obs(extraction="unverified"))[0].outcome == "unchanged"
    evidence = env.db.get_evidence(evidence_id)
    assert evidence.decided_by == "human" and evidence.processing_status == "dismissed"


# ------------------------------------------------------------------ #
# Interruption and concurrency                                         #
# ------------------------------------------------------------------ #


def test_interrupted_observation_is_resumed_on_retry(env, monkeypatch) -> None:
    from backend.collection import resolution

    def crash(self, *args, **kwargs):
        raise RuntimeError("process died mid-decision")

    monkeypatch.setattr(resolution.ResolverDecider, "decide", crash)
    assert _ingest(env, _obs())[0].outcome == "error"
    monkeypatch.undo()
    # The stored observation is still pending; its evidence claim is stale after the TTL.
    from backend import config as app_config

    monkeypatch.setattr(app_config, "RESOLVER_PROCESSING_CLAIM_TTL_SECONDS", 0)
    import backend.db.data_store as data_store

    monkeypatch.setattr(data_store, "RESOLVER_PROCESSING_CLAIM_TTL_SECONDS", 0)
    assert _ingest(env, _obs())[0].outcome == "created"
    assert len(_apps(env)) == 1


def test_concurrent_submissions_of_one_new_item_create_one_application(env) -> None:
    outcomes: list[str] = []
    barrier = threading.Barrier(4)
    runs = [_run(env) for _ in range(4)]

    def submit(run) -> None:
        barrier.wait()
        outcomes.extend(r.outcome for r in _ingest(env, _obs(), run=run))

    threads = [threading.Thread(target=submit, args=(run,)) for run in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("created") == 1
    assert len(_apps(env)) == 1
