"""Tests for the evidence repository and identity columns in backend/db/data_store.py."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.db.data_store import (
    ApplicationFilter,
    ApplicationNotFoundError,
    DataStore,
    EvidenceFilter,
    EvidenceNotFoundError,
)
from backend.db.models import Application, ApplicationStatus, Evidence, utc_now
from backend.engine.normalization import evidence_fingerprint

T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "evidence.db")


def _email(message_id: str = "m1", *, thread: str = "t1", when: datetime = T0, **extra) -> Evidence:
    return Evidence(
        evidence_type="email",
        source="gmail",
        external_id=message_id,
        thread_id=thread,
        occurred_at=when,
        **extra,
    )


def _app(store: DataStore, company: str = "Acme", **extra) -> Application:
    return store.upsert_application(
        Application(
            company=company,
            role=extra.pop("role", "Engineer"),
            source_portal="LinkedIn",
            applied_date=extra.pop("applied_date", utc_now()),
            current_status=ApplicationStatus.APPLIED,
            **extra,
        )
    )


def _raw(store_path: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(store_path)  # test-only inspection/surgery
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Idempotent insertion                                                 #
# ------------------------------------------------------------------ #


def test_insert_is_idempotent_by_external_id(store: DataStore) -> None:
    first, created = store.insert_evidence(_email())
    again, created_again = store.insert_evidence(_email(sender="changed@example.com"))
    assert created is True and created_again is False
    assert again.id == first.id
    assert first.content_fingerprint == evidence_fingerprint(
        evidence_type="email", source="gmail", external_id="m1"
    )
    assert store.list_evidence(EvidenceFilter())[1] == 1


def test_same_source_and_external_id_under_another_type_is_not_duplicated(
    store: DataStore,
) -> None:
    first, _ = store.insert_evidence(_email())
    other, created = store.insert_evidence(
        Evidence(evidence_type="manual", source="gmail", external_id="m1", occurred_at=T0)
    )
    assert created is False
    assert other.id == first.id


def test_fingerprint_dedupes_evidence_without_external_id(store: DataStore) -> None:
    def portal(when: datetime) -> Evidence:
        return Evidence(
            evidence_type="portal_import",
            source="naukri",
            sender="alerts@naukri.com",
            subject="Application received",
            occurred_at=when,
        )

    a, created_a = store.insert_evidence(portal(T0))
    b, created_b = store.insert_evidence(portal(T0.replace(microsecond=5)))
    c, created_c = store.insert_evidence(portal(T0 + timedelta(seconds=1)))
    assert (created_a, created_b, created_c) == (True, False, True)
    assert a.id == b.id != c.id


def test_insert_validates_choices_and_truncates_snippet(store: DataStore) -> None:
    with pytest.raises(ValueError, match="source"):
        store.insert_evidence(Evidence(evidence_type="email", source="myspace", occurred_at=T0))
    with pytest.raises(ValueError, match="evidence_type"):
        store.insert_evidence(Evidence(evidence_type="fax", source="gmail", occurred_at=T0))
    with pytest.raises(ValueError, match="processing_status"):
        store.insert_evidence(_email(processing_status="done"))
    stored, _ = store.insert_evidence(_email("m-long", snippet="x" * 2000, subject="Re: Hi"))
    assert len(stored.snippet) == 500
    assert stored.normalized_subject == "hi"


def test_concurrent_inserts_of_the_same_evidence_yield_one_row(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    DataStore(path).close()
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[tuple[int, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        store = DataStore(path)  # separate engine and connections per thread
        try:
            barrier.wait()
            row, created = store.insert_evidence(_email("same-message"))
            with lock:
                results.append((row.id, created))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == workers
    assert len({row_id for row_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert _raw(path, "SELECT COUNT(*) FROM evidence")[0][0] == 1


def test_database_enforces_uniqueness_even_without_the_repository(
    store: DataStore, tmp_path
) -> None:
    store.insert_evidence(_email())
    with pytest.raises(sqlite3.IntegrityError):
        _raw(
            tmp_path / "evidence.db",
            "INSERT INTO evidence (evidence_type, source, external_id, occurred_at, captured_at, "
            "raw_metadata, content_fingerprint, processing_status, created_at, updated_at) "
            "VALUES ('email', 'gmail', 'm1', '2026-01-01', '2026-01-01', '{}', 'different', "
            "'pending', '2026-01-01', '2026-01-01')",
        )


def test_lookup_by_external_id(store: DataStore) -> None:
    stored, _ = store.insert_evidence(_email("abc"))
    assert store.get_evidence_by_external_id("gmail", "abc").id == stored.id
    assert store.get_evidence_by_external_id("gmail", "zzz") is None
    assert store.get_evidence(stored.id).external_id == "abc"


# ------------------------------------------------------------------ #
# Listing and counts                                                   #
# ------------------------------------------------------------------ #


def test_list_filters_and_counts(store: DataStore) -> None:
    app = _app(store)
    linked, _ = store.insert_evidence(_email("a", when=T0))
    store.link_evidence(linked.id, app.id, "thread", 1.0)
    review, _ = store.insert_evidence(_email("b", when=T0 + timedelta(days=1)))
    store.update_evidence_processing(review.id, "needs_review", review_reason="x")
    ignored, _ = store.insert_evidence(_email("c", when=T0 + timedelta(days=2)))
    store.update_evidence_processing(ignored.id, "ignored")
    store.insert_evidence(
        Evidence(evidence_type="portal_import", source="naukri", external_id="n1", occurred_at=T0)
    )

    ids = lambda f: [e.external_id for e in store.list_evidence(f)[0]]  # noqa: E731
    # Newest first; equal occurred_at ties break by id, newest row first.
    assert ids(EvidenceFilter()) == ["b", "n1", "a"]
    assert ids(EvidenceFilter(linked=True)) == ["a"]
    assert sorted(ids(EvidenceFilter(linked=False))) == ["b", "n1"]
    assert ids(EvidenceFilter(source="naukri")) == ["n1"]
    assert ids(EvidenceFilter(evidence_type="email")) == ["b", "a"]
    assert ids(EvidenceFilter(processing_status="ignored")) == ["c"]
    assert "c" in ids(EvidenceFilter(include_ignored=True))
    assert ids(
        EvidenceFilter(date_from=T0 + timedelta(hours=1), date_to=T0 + timedelta(days=1))
    ) == ["b"]
    assert ids(EvidenceFilter(application_id=app.id)) == ["a"]
    page, total = store.list_evidence(EvidenceFilter(page=2, page_size=2))
    assert total == 3 and len(page) == 1
    assert store.count_evidence() == {"total": 3, "unlinked": 2, "needs_review": 1}


# ------------------------------------------------------------------ #
# Link / unlink / last_evidence_at                                     #
# ------------------------------------------------------------------ #


def test_link_unlink_and_last_evidence_at(store: DataStore) -> None:
    first_app = _app(store, "Acme")
    second_app = _app(store, "Globex")
    early, _ = store.insert_evidence(_email("e1", when=T0))
    late, _ = store.insert_evidence(_email("e2", when=T0 + timedelta(days=3)))

    store.link_evidence(early.id, first_app.id, "thread", 1.0)
    linked = store.link_evidence(late.id, first_app.id, "company_role", 0.9)
    assert linked.processing_status == "linked"
    assert linked.review_reason is None
    assert store.get_application(first_app.id).last_evidence_at == T0 + timedelta(days=3)
    assert [e.external_id for e in store.get_evidence_for_application(first_app.id)] == ["e1", "e2"]

    # Relinking moves it and refreshes both applications.
    store.link_evidence(late.id, second_app.id, "manual", 1.0)
    assert store.get_application(first_app.id).last_evidence_at == T0
    assert store.get_application(second_app.id).last_evidence_at == T0 + timedelta(days=3)

    unlinked = store.unlink_evidence(late.id)
    assert unlinked.application_id is None
    assert unlinked.processing_status == "needs_review"
    assert unlinked.review_reason == "manually_unlinked"
    assert unlinked.link_method is None and unlinked.link_confidence is None
    assert store.get_application(second_app.id).last_evidence_at is None


def test_link_errors(store: DataStore) -> None:
    app = _app(store)
    evidence, _ = store.insert_evidence(_email())
    with pytest.raises(EvidenceNotFoundError):
        store.link_evidence(9999, app.id, "manual", 1.0)
    with pytest.raises(ApplicationNotFoundError):
        store.link_evidence(evidence.id, 9999, "manual", 1.0)
    with pytest.raises(ValueError):
        store.link_evidence(evidence.id, app.id, "telepathy", 1.0)
    with pytest.raises(ValueError):
        store.link_evidence(evidence.id, app.id, "manual", 1.5)
    with pytest.raises(EvidenceNotFoundError):
        store.unlink_evidence(9999)
    assert store.get_evidence(evidence.id).application_id is None  # nothing half-applied


def test_failed_link_leaves_previous_link_intact(store: DataStore) -> None:
    app = _app(store)
    evidence, _ = store.insert_evidence(_email())
    store.link_evidence(evidence.id, app.id, "thread", 1.0)
    with pytest.raises(ApplicationNotFoundError):
        store.link_evidence(evidence.id, 4242, "manual", 1.0)
    assert store.get_evidence(evidence.id).application_id == app.id


def test_details_update_only_for_external_id_evidence(store: DataStore) -> None:
    gmail, _ = store.insert_evidence(_email())
    updated = store.update_evidence_details(
        gmail.id, sender="a@b.c", subject="Fwd: Hi", snippet="s", metadata={"k": 1}
    )
    assert (updated.sender, updated.normalized_subject, updated.raw_metadata) == (
        "a@b.c",
        "hi",
        {"k": 1},
    )
    assert updated.content_fingerprint == gmail.content_fingerprint
    manual, _ = store.insert_evidence(
        Evidence(evidence_type="manual", source="other", subject="note", occurred_at=T0)
    )
    with pytest.raises(ValueError, match="immutable"):
        store.update_evidence_details(manual.id, sender=None, subject="x", snippet=None)


def test_processing_update_merges_metadata(store: DataStore) -> None:
    evidence, _ = store.insert_evidence(_email(raw_metadata={"a": 1}))
    updated = store.update_evidence_processing(evidence.id, "error", metadata={"b": 2})
    assert updated.raw_metadata == {"a": 1, "b": 2}
    with pytest.raises(ValueError):
        store.update_evidence_processing(evidence.id, "finished")


def test_recompute_last_evidence_at(store: DataStore, tmp_path) -> None:
    app = _app(store)
    other = _app(store, "Globex")
    evidence, _ = store.insert_evidence(_email(when=T0))
    store.link_evidence(evidence.id, app.id, "thread", 1.0)
    _raw(
        tmp_path / "evidence.db", "UPDATE application SET last_evidence_at = '2001-01-01 00:00:00'"
    )
    assert store.recompute_last_evidence_at(app.id) == 1
    assert store.get_application(app.id).last_evidence_at == T0
    assert store.recompute_last_evidence_at() == 2
    assert store.get_application(other.id).last_evidence_at is None


# ------------------------------------------------------------------ #
# Existing operations keep evidence consistent                         #
# ------------------------------------------------------------------ #


def test_delete_application_detaches_evidence(store: DataStore) -> None:
    app = _app(store)
    evidence, _ = store.insert_evidence(_email())
    store.link_evidence(evidence.id, app.id, "thread", 1.0)
    assert store.delete_application(app.id) is True
    detached = store.get_evidence(evidence.id)
    assert detached.application_id is None
    assert detached.processing_status == "needs_review"
    assert detached.review_reason == "application_deleted"


def test_merge_moves_evidence_to_primary(store: DataStore) -> None:
    primary = _app(store, "Acme")
    duplicate = _app(store, "Acme Corp")
    evidence, _ = store.insert_evidence(_email(when=T0 + timedelta(days=2)))
    store.link_evidence(evidence.id, duplicate.id, "thread", 1.0)
    merged = store.merge_applications(primary.id, duplicate.id)
    assert store.get_evidence(evidence.id).application_id == primary.id
    assert merged.last_evidence_at == T0 + timedelta(days=2)


def test_reset_for_rebackfill_returns_evidence_to_pending(store: DataStore) -> None:
    app = _app(store)
    evidence, _ = store.insert_evidence(_email())
    store.link_evidence(evidence.id, app.id, "thread", 1.0)
    store.reset_for_rebackfill()
    reset = store.get_evidence(evidence.id)
    assert (reset.application_id, reset.processing_status) == (None, "pending")
    assert store.get_applications(ApplicationFilter())[1] == 0


# ------------------------------------------------------------------ #
# Identity columns                                                     #
# ------------------------------------------------------------------ #


def test_identity_columns_derived_on_save(store: DataStore) -> None:
    app = _app(
        store,
        "Acme Technologies Pvt Ltd",
        role="Senior Engineer",
        job_url="https://Jobs.acme.com/1/?utm_medium=x",
    )
    assert app.normalized_company == "acme"
    assert app.normalized_role == "senior engineer"
    assert app.canonical_job_url == "https://jobs.acme.com/1"
    app.company = "Globex"
    assert store.upsert_application(app).normalized_company == "globex"


def test_save_never_rolls_back_last_evidence_at(store: DataStore) -> None:
    app = _app(store)
    stale_copy = store.get_application(app.id)
    evidence, _ = store.insert_evidence(_email(when=T0))
    store.link_evidence(evidence.id, app.id, "thread", 1.0)
    stale_copy.role = "Staff Engineer"
    saved = store.upsert_application(stale_copy)  # copy still has last_evidence_at=None
    assert saved.last_evidence_at == T0


def test_runtime_maintenance_fills_identity_columns(tmp_path: Path) -> None:
    path = tmp_path / "older.db"
    store = DataStore(path)
    app = _app(store, "Initech Inc", job_url="https://x.example/job/9/")
    store.close()
    # Simulate a row written by an older release that does not know these columns.
    _raw(
        path,
        "UPDATE application SET normalized_company=NULL, normalized_role=NULL, "
        "canonical_job_url=NULL WHERE id=?",
        app.id,
    )
    reopened = DataStore(path)
    fixed = reopened.get_application(app.id)
    assert (fixed.normalized_company, fixed.normalized_role, fixed.canonical_job_url) == (
        "initech",
        "engineer",
        "https://x.example/job/9",
    )
