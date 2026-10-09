"""Tests for the reversible merge engine: backend/engine/merge_planner.py,
backend/db/merge_snapshot.py and the merge/undo methods of backend/db/data_store.py."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.db import merge_snapshot
from backend.db.data_store import (
    ApplicationFilter,
    DataStore,
    MergeError,
    MergeInvalidError,
    MergeStaleError,
    MergeUndoConflictError,
)
from backend.db.models import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationStatus,
    Evidence,
    InterviewRound,
    Prospect,
    utc_now,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.merge_planner import MergePlanError, plan_merge, resolve_field_values

NOW = utc_now().replace(microsecond=0)
TABLES = (
    "application",
    "statushistory",
    "applicationevent",
    "evidence",
    "applicationthreadid",
    "prospect",
)


@pytest.fixture
def db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "merge.db")


def dump(db: DataStore) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db._db_path)  # test-only raw read for exact comparison
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in TABLES}
    finally:
        conn.close()


def make_app(
    db: DataStore,
    key: str,
    *,
    company: str = "Acme",
    role: str | None = "Engineer",
    days_ago: int = 10,
    portal: str = "LinkedIn",
    method: str = "Unknown",
    job_url: str | None = None,
    status: ApplicationStatus = ApplicationStatus.APPLIED,
    interview_day: int | None = None,
    human: bool = False,
) -> Application:
    applied = NOW - timedelta(days=days_ago)
    app = db.upsert_application(
        Application(
            company=company,
            role=role,
            source_portal=portal,
            application_method=method,
            job_url=job_url,
            applied_date=applied,
            current_status=status,
            thread_ids=json.dumps([f"thread-{key}"]),
        )
    )
    assert app.id is not None
    history = db.append_status_history(app.id, None, "Applied", "email", f"msg-{key}")
    db.add_application_event(
        ApplicationEvent(
            application_id=app.id,
            event_type=ApplicationEventType.APPLICATION_SUBMITTED,
            occurred_at=applied,
            source="email",
            source_message_id=f"msg-{key}",
            status_history_id=history.id,
        )
    )
    if interview_day is not None:
        db.add_application_event(
            ApplicationEvent(
                application_id=app.id,
                event_type=ApplicationEventType.INTERVIEW_SCHEDULED,
                occurred_at=NOW - timedelta(days=interview_day),
                interview_round=InterviewRound.HIRING_MANAGER,
                source="email",
                source_message_id=f"int-{key}",
            )
        )
    evidence, _ = db.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id=f"ev-{key}",
            thread_id=f"thread-{key}",
            occurred_at=applied,
        )
    )
    db.link_evidence(
        evidence.id,
        app.id,
        "manual" if human else "thread",
        1.0,
        decided_by="human" if human else "resolver",
    )
    db.upsert_prospect(
        Prospect(
            category="recruiter_outreach",
            title=f"Prospect {key}",
            sender="Recruiter",
            received_at=applied,
            gmail_message_id=f"prospect-{key}",
            gmail_thread_id=f"pthread-{key}",
            classification_reason="x",
            application_id=app.id,
        )
    )
    return app


def preview(db: DataStore, ids: list[int], survivor: int | None = None):
    state = db.load_merge_state(ids)
    state["application_ids"] = sorted(ids)
    return plan_merge(state, survivor)


def merge(db: DataStore, ids: list[int], survivor: int | None = None, choices=None, key="k1"):
    plan = preview(db, ids, survivor)
    if choices is None:
        choices = {name: plan.survivor_id for name in plan.conflicts}
    values = resolve_field_values(plan, choices)
    return db.execute_merge(
        application_ids=ids,
        survivor_id=plan.survivor_id,
        field_values=values,
        expected_token=plan.token,
        idempotency_key=key,
        initiated_by="owner@example.com",
        reason="test",
    )


# ------------------------------------------------------------------ #
# Preview                                                              #
# ------------------------------------------------------------------ #


def test_preview_does_not_modify_anything(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b", company="Acme Corp")
    before = dump(db)
    plan = preview(db, [a.id, b.id])
    assert plan.safe and plan.token
    assert dump(db) == before
    assert db.list_merge_operations()[1] == 0


def test_default_survivor_is_deterministic(db: DataStore) -> None:
    sparse = make_app(db, "sparse", role=None, days_ago=40)
    rich = make_app(db, "rich", days_ago=5, job_url="https://jobs.acme.com/1", method="Easy Apply")
    other = make_app(db, "other", days_ago=20)
    for order in ([sparse.id, rich.id, other.id], [other.id, rich.id, sparse.id]):
        assert preview(db, order).survivor_id == rich.id  # richest record wins
    confirmed = make_app(db, "human", role=None, days_ago=1, human=True)
    assert preview(db, [rich.id, confirmed.id]).survivor_id == confirmed.id  # confirmed first


def test_tie_breaks_on_oldest_then_lowest_id(db: DataStore) -> None:
    newer = make_app(db, "newer", days_ago=3)
    older = make_app(db, "older", days_ago=30)
    assert preview(db, [newer.id, older.id]).survivor_id == older.id


def test_preview_reports_conflicts_warnings_and_counts(db: DataStore) -> None:
    a = make_app(db, "a", company="Acme", role="Engineer", interview_day=5)
    b = make_app(db, "b", company="Globex", role="Analyst", portal="Naukri", interview_day=5)
    plan = preview(db, [a.id, b.id], survivor=a.id)
    assert {"company", "role", "source_portal"} <= set(plan.conflicts)
    assert any("Company names differ" in w for w in plan.warnings)
    company = next(f for f in plan.fields if f.name == "company")
    assert company.values == {a.id: "Acme", b.id: "Globex"} and company.proposed == "Acme"
    applied = next(f for f in plan.fields if f.name == "applied_date")
    assert applied.rule == "earliest" and not applied.conflict
    assert plan.counts["evidence"] == 2
    assert plan.counts["status_history_superseded"] == 1  # both have Applied
    assert plan.counts["events_superseded"] == 2  # duplicate submission + same-day interview
    assert plan.relink["evidence"] and plan.relink["prospects"]


def test_preview_blocks_unsafe_requests(db: DataStore) -> None:
    a, b, c = make_app(db, "a"), make_app(db, "b"), make_app(db, "c")
    merge(db, [a.id, b.id], survivor=a.id)
    plan = preview(db, [b.id, c.id])
    assert not plan.safe
    assert any("already merged" in reason for reason in plan.blocking)
    assert not preview(db, [c.id, 999_999]).safe
    assert not preview(db, [c.id]).safe


def test_unresolved_conflict_is_refused(db: DataStore) -> None:
    a, b = make_app(db, "a", company="Acme"), make_app(db, "b", company="Globex")
    plan = preview(db, [a.id, b.id])
    with pytest.raises(MergePlanError, match="company"):
        resolve_field_values(plan, {})
    with pytest.raises(MergePlanError):
        resolve_field_values(plan, {name: 4242 for name in plan.conflicts})


# ------------------------------------------------------------------ #
# Merge execution                                                      #
# ------------------------------------------------------------------ #


def test_merge_two_records(db: DataStore) -> None:
    a = make_app(db, "a", days_ago=20, interview_day=5)
    b = make_app(db, "b", company="Acme Pvt Ltd", days_ago=10, interview_day=5)
    op, created = merge(db, [a.id, b.id], survivor=a.id)
    assert created and op.survivor_application_id == a.id and op.source_application_ids == [b.id]

    hidden = db.get_application(b.id)
    assert (hidden.record_state, hidden.merged_into_application_id, hidden.merge_operation_id) == (
        "merged",
        a.id,
        op.id,
    )
    survivor = db.get_application(a.id)
    assert set(json.loads(survivor.thread_ids)) == {"thread-a", "thread-b"}
    assert survivor.applied_date == NOW - timedelta(days=20)
    assert [e.external_id for e in db.get_evidence_for_application(a.id)] == ["ev-a", "ev-b"]
    assert {p.gmail_message_id for p in db.get_prospects() if p.application_id == a.id} == {
        "prospect-a",
        "prospect-b",
    }
    assert db.find_application_by_thread_id("thread-b").id == a.id

    live_history = db.get_status_history(a.id)
    assert [h.to_status for h in live_history] == ["Applied"]
    assert len(db.get_status_history(a.id, include_superseded=True)) == 2
    live_events = db.get_application_events(a.id)
    assert sorted(e.event_type.value for e in live_events) == [
        "Application Submitted",
        "Interview Scheduled",
    ]
    assert len(db.get_application_events(a.id, include_superseded=True)) == 4

    assert [x.id for x in db.get_applications(ApplicationFilter())[0]] == [a.id]
    assert {x.id for x in db.get_applications(ApplicationFilter(include_merged=True))[0]} == {
        a.id,
        b.id,
    }


def test_merge_three_with_chosen_survivor_and_conflict_choices(db: DataStore) -> None:
    a = make_app(db, "a", company="Acme", role="Engineer", portal="LinkedIn")
    b = make_app(db, "b", company="Acme India", role="Software Engineer", portal="Naukri")
    c = make_app(
        db,
        "c",
        company="Acme",
        role="Engineer",
        portal="Indeed",
        job_url="https://jobs.acme.com/77",
    )
    plan = preview(db, [a.id, b.id, c.id], survivor=c.id)
    choices = {"company": b.id, "role": b.id, "source_portal": a.id}
    for name in plan.conflicts:
        choices.setdefault(name, c.id)
    op, _ = merge(db, [a.id, b.id, c.id], survivor=c.id, choices=choices)
    survivor = db.get_application(c.id)
    assert (survivor.company, survivor.role, survivor.source_portal, survivor.job_url) == (
        "Acme India",
        "Software Engineer",
        "LinkedIn",
        "https://jobs.acme.com/77",
    )
    assert survivor.normalized_company == "acme india"  # derived fields recalculated
    assert sorted(op.source_application_ids) == sorted([a.id, b.id])
    assert {x.id for x in db.get_applications(ApplicationFilter())[0]} == {c.id}
    assert len(db.get_evidence_for_application(c.id)) == 3


def test_external_ids_are_preserved(db: DataStore) -> None:
    a = make_app(db, "a", job_url="https://www.linkedin.com/jobs/view/3912345678/")
    b = make_app(db, "b")
    assert db.get_application(a.id).external_job_id == "3912345678"
    merge(db, [a.id, b.id], survivor=b.id)
    assert db.get_application(b.id).external_job_id == "3912345678"
    assert db.get_application(b.id).canonical_job_url == "https://linkedin.com/jobs/view/3912345678"


def test_merge_is_idempotent_by_key(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    first, created = merge(db, [a.id, b.id], survivor=a.id, key="same-key")
    plan_token = first.preview_token
    again, created_again = db.execute_merge(
        application_ids=[a.id, b.id],
        survivor_id=a.id,
        field_values={},
        expected_token=plan_token,
        idempotency_key="same-key",
    )
    assert created and not created_again and again.id == first.id
    assert db.list_merge_operations()[1] == 1
    with pytest.raises(MergeInvalidError):
        db.execute_merge(
            application_ids=[a.id, b.id],
            survivor_id=b.id,
            field_values={},
            expected_token=plan_token,
            idempotency_key="same-key",
        )


def test_stale_preview_is_rejected(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    plan = preview(db, [a.id, b.id])
    changed = db.get_application(a.id)
    changed.role = "Staff Engineer"
    db.upsert_application(changed)
    before = dump(db)
    with pytest.raises(MergeStaleError, match="changed since the preview"):
        db.execute_merge(
            application_ids=[a.id, b.id],
            survivor_id=plan.survivor_id,
            field_values=resolve_field_values(plan, {n: plan.survivor_id for n in plan.conflicts}),
            expected_token=plan.token,
            idempotency_key="stale",
        )
    assert dump(db) == before


def test_new_evidence_makes_a_preview_stale(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    plan = preview(db, [a.id, b.id])
    late, _ = db.insert_evidence(
        Evidence(evidence_type="email", source="gmail", external_id="late", occurred_at=NOW)
    )
    db.link_evidence(late.id, b.id, "thread", 1.0)
    assert preview(db, [a.id, b.id]).token != plan.token


def test_concurrent_merges_of_the_same_records(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    seed = DataStore(path)
    a, b = make_app(seed, "a"), make_app(seed, "b")
    plan = preview(seed, [a.id, b.id])
    values = resolve_field_values(plan, {n: plan.survivor_id for n in plan.conflicts})
    seed.close()
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def worker(n: int) -> None:
        store = DataStore(path)
        try:
            barrier.wait()
            store.execute_merge(
                application_ids=[a.id, b.id],
                survivor_id=plan.survivor_id,
                field_values=values,
                expected_token=plan.token,
                idempotency_key=f"worker-{n}",
            )
            outcomes.append("merged")
        except MergeStaleError:
            outcomes.append("stale")
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes) == ["merged", "stale", "stale", "stale"]
    assert DataStore(path).list_merge_operations()[1] == 1


def test_injected_failure_rolls_back_everything(db: DataStore) -> None:
    a, b = make_app(db, "a", interview_day=2), make_app(db, "b", interview_day=2)
    plan = preview(db, [a.id, b.id])
    values = resolve_field_values(plan, {n: plan.survivor_id for n in plan.conflicts})
    before = dump(db)
    with patch.object(merge_snapshot, "superseded_event_ids", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            db.execute_merge(
                application_ids=[a.id, b.id],
                survivor_id=plan.survivor_id,
                field_values=values,
                expected_token=plan.token,
                idempotency_key="fails",
            )
    assert dump(db) == before
    assert db.list_merge_operations()[1] == 0


def test_invalid_requests(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    with pytest.raises(MergeInvalidError):
        db.execute_merge(
            application_ids=[a.id],
            survivor_id=a.id,
            field_values={},
            expected_token="x" * 10,
            idempotency_key="k",
        )
    with pytest.raises(MergeInvalidError):
        db.execute_merge(
            application_ids=[a.id, b.id],
            survivor_id=a.id,
            field_values={"notes": "x"},
            expected_token="x" * 10,
            idempotency_key="k",
        )


# ------------------------------------------------------------------ #
# Undo                                                                 #
# ------------------------------------------------------------------ #


def test_undo_restores_everything_exactly(db: DataStore) -> None:
    a = make_app(db, "a", interview_day=4)
    b = make_app(db, "b", company="Acme Corp", interview_day=4)
    c = make_app(db, "c", company="Acme", role="Engineering Manager")
    before = dump(db)
    op, _ = merge(db, [a.id, b.id, c.id], survivor=c.id)
    assert dump(db) != before
    undone, now_undone = db.undo_merge(op.id, undone_by="owner@example.com")
    assert now_undone and undone.undone_at is not None
    assert undone.undo_metadata["restored_applications"] == sorted([a.id, b.id, c.id])
    assert dump(db) == before
    assert {x.id for x in db.get_applications(ApplicationFilter())[0]} == {a.id, b.id, c.id}
    again, repeated = db.undo_merge(op.id)
    assert not repeated and again.undone_at == undone.undone_at


def test_records_can_be_merged_again_after_undo(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    op, _ = merge(db, [a.id, b.id], key="first")
    db.undo_merge(op.id)
    second, created = merge(db, [a.id, b.id], key="second")
    assert created and second.id != op.id


def test_items_added_after_merge_stay_with_survivor(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    op, _ = merge(db, [a.id, b.id], survivor=a.id)
    late, _ = db.insert_evidence(
        Evidence(evidence_type="email", source="gmail", external_id="late", occurred_at=NOW)
    )
    db.link_evidence(late.id, a.id, "thread", 1.0)
    undone, _ = db.undo_merge(op.id)
    assert db.get_evidence(late.id).application_id == a.id
    assert undone.undo_metadata["kept_with_survivor"]["evidence"] == [late.id]
    assert db.get_evidence_by_external_id("gmail", "ev-b").application_id == b.id


@pytest.mark.parametrize("change", ["survivor_edit", "evidence_relinked", "survivor_merged_again"])
def test_unsafe_undo_is_refused_without_changes(db: DataStore, change: str) -> None:
    a, b, other = make_app(db, "a"), make_app(db, "b"), make_app(db, "x", company="Globex")
    op, _ = merge(db, [a.id, b.id], survivor=a.id, key="first")
    if change == "survivor_edit":
        survivor = db.get_application(a.id)
        survivor.role = "Principal Engineer"
        db.upsert_application(survivor)
        expected = "role was edited after the merge"
    elif change == "evidence_relinked":
        moved = db.get_evidence_by_external_id("gmail", "ev-b")
        db.link_evidence(moved.id, other.id, "manual", 1.0, decided_by="human")
        expected = f"re-linked to application {other.id}"
    else:
        merge(db, [a.id, other.id], survivor=other.id, key="second")
        expected = "merged again later"
    before = dump(db)
    with pytest.raises(MergeUndoConflictError) as err:
        db.undo_merge(op.id)
    assert any(expected in c for c in err.value.conflicts), err.value.conflicts
    assert dump(db) == before
    assert db.get_merge_operation(op.id).undone_at is None


def test_tampered_snapshot_is_refused(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    op, _ = merge(db, [a.id, b.id])
    conn = sqlite3.connect(db._db_path)
    snapshot = json.loads(conn.execute("SELECT snapshot FROM mergeoperation").fetchone()[0])
    snapshot["applications"][str(b.id)]["company"] = "Tampered"
    conn.execute("UPDATE mergeoperation SET snapshot = ?", (json.dumps(snapshot),))
    conn.commit()
    conn.close()
    with pytest.raises(merge_snapshot.SnapshotError, match="checksum"):
        db.undo_merge(op.id)


def test_snapshot_contains_no_message_content(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    db.update_evidence_details(
        db.get_evidence_by_external_id("gmail", "ev-a").id,
        sender="Jane <jane@acme.com>",
        subject="Very private subject",
        snippet="private snippet text",
    )
    op, _ = merge(db, [a.id, b.id])
    text = json.dumps(op.snapshot) + json.dumps(op.result)
    for forbidden in ("jane@acme.com", "Very private subject", "private snippet"):
        assert forbidden not in text


# ------------------------------------------------------------------ #
# Safeguards and integration with other features                      #
# ------------------------------------------------------------------ #


def test_merged_records_cannot_be_deleted(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    merge(db, [a.id, b.id], survivor=a.id)
    for target in (a.id, b.id):
        with pytest.raises(MergeError):
            db.delete_application(target)
    assert db.get_application(b.id) is not None


def test_explicit_full_reset_still_works_after_a_merge(db: DataStore) -> None:
    """reset_for_rebackfill (an explicit wipe-everything CLI) also clears merge audit rows,
    which reference the applications it deletes."""
    a, b = make_app(db, "a"), make_app(db, "b")
    merge(db, [a.id, b.id], survivor=a.id)
    db.dismiss_duplicate(a.id, b.id)
    db.reset_for_rebackfill()
    assert dump(db)["application"] == []
    assert db.list_merge_operations(1, 10)[1] == 0
    assert db.dismissed_pair_keys() == set()


def test_duplicate_suggestions_skip_merged_and_dismissed(db: DataStore) -> None:
    a, b = make_app(db, "a"), make_app(db, "b")
    c = make_app(db, "c", company="Initech")
    d = make_app(db, "d", company="Initech")
    assert len(DuplicateDetector(db).find_candidate_pairs()) == 2
    merge(db, [a.id, b.id])
    assert [
        {p["primary"].id, p["duplicate"].id} for p in DuplicateDetector(db).find_candidate_pairs()
    ] == [{c.id, d.id}]
    first = db.dismiss_duplicate(d.id, c.id)
    again = db.dismiss_duplicate(c.id, d.id)
    assert first.pair_key == again.pair_key == f"{c.id}:{d.id}"
    assert db.dismissed_pair_keys() == {f"{c.id}:{d.id}"}


def test_dedup_rules() -> None:
    history = [
        {"id": 1, "from_status": None, "to_status": "Applied", "changed_at": "2026-01-02"},
        {"id": 2, "from_status": None, "to_status": "Applied", "changed_at": "2026-01-01"},
        {"id": 3, "from_status": "Applied", "to_status": "Rejected", "changed_at": "2026-02-01"},
    ]
    assert merge_snapshot.superseded_history_ids(history) == [1]
    events = [
        {
            "id": 10,
            "event_type": "Application Submitted",
            "occurred_at": "2026-01-02",
            "status_history_id": 1,
        },
        {
            "id": 11,
            "event_type": "Interview Scheduled",
            "interview_round": "Hiring Manager",
            "occurred_at": "2026-03-01T09:00:00+00:00",
        },
        {
            "id": 12,
            "event_type": "Interview Scheduled",
            "interview_round": "Hiring Manager",
            "occurred_at": "2026-03-01T15:00:00+00:00",
        },
        {
            "id": 13,
            "event_type": "Interview Scheduled",
            "interview_round": "Hiring Manager",
            "occurred_at": "2026-03-08T09:00:00+00:00",
        },
        {"id": 14, "event_type": "Rejected", "occurred_at": "2026-04-01"},
        {"id": 15, "event_type": "Rejected", "occurred_at": "2026-04-03"},
    ]
    assert merge_snapshot.superseded_event_ids(events, {1}) == [10, 12, 15]
