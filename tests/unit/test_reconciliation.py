"""Tests for the Phase 2 reconciliation audit (backend/engine/reconciliation.py and
scripts/reconcile_database.py). Synthetic data and temporary databases only."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from backend import config as app_config
from backend.db import reconcile_cli
from backend.db.backup import sha256_file
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, Evidence
from backend.db.reconcile_cli import (
    APPLY_CONFIRM,
    PRODUCTION_ACK,
    Refused,
    check_guards,
    find_leaks,
    reconcile_command,
    run_reconciliation,
)
from backend.engine import reconciliation as rec

AS_OF = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
APPLIED = AS_OF - timedelta(days=20)
COMPANIES = ("Quuxwidget Labs", "Zorblat Systems", "Frobnicate Analytics", "Plughat Retail")


def _app(store: DataStore, company: str, role: str, **extra) -> Application:
    return store.upsert_application(
        Application(
            company=company,
            role=role,
            source_portal=extra.pop("source_portal", "LinkedIn"),
            applied_date=extra.pop("applied_date", APPLIED),
            current_status=extra.pop("current_status", ApplicationStatus.APPLIED),
            updated_at=APPLIED,
            **extra,
        )
    )


@pytest.fixture
def synthetic_db(tmp_path: Path) -> Path:
    path = tmp_path / "input" / "applications.db"
    path.parent.mkdir()
    store = DataStore(path)
    # A clear duplicate pair, a distinct record with a mail thread, and a pair that looks
    # alike but points at two different job pages (must never be merged).
    _app(store, COMPANIES[0], "Platform Engineer", thread_ids='["thread-dup-a"]')
    _app(store, COMPANIES[0], "Platform Engineer", thread_ids='["thread-dup-b"]')
    third = _app(store, COMPANIES[1], "Data Scientist", thread_ids='["thread-known"]')
    _app(store, COMPANIES[2], "ML Engineer", job_url="https://careers.example.org/a/one")
    _app(store, COMPANIES[2], "ML Engineer", job_url="https://careers.example.org/b/two")
    human_target = _app(store, COMPANIES[3], "Store Analyst")

    pending, _ = store.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="msg-thread-match",
            thread_id="thread-known",
            sender="recruiting@zorblat-systems.example",
            subject="Your application",
            occurred_at=APPLIED + timedelta(days=3),
            raw_metadata={"parser": {"company": COMPANIES[1], "role": "Data Scientist"}},
        )
    )
    store.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="msg-unclear",
            occurred_at=APPLIED,
            raw_metadata={"parser": {"company": "Unrelated Gizmo Works"}},
        )
    )
    human, _ = store.insert_evidence(
        Evidence(
            evidence_type="email",
            source="gmail",
            external_id="msg-human",
            occurred_at=APPLIED,
            raw_metadata={"parser": {"company": COMPANIES[0]}},
        )
    )
    assert human.id is not None and human_target.id is not None
    store.link_evidence(human.id, human_target.id, "manual", 1.0, decided_by="human")
    assert pending.id is not None and third.id is not None
    store.close()
    return path


def _run(db: Path, out: Path, **kwargs):
    defaults = dict(
        seed="test-seed",
        thresholds=rec.Thresholds.from_config(),
        as_of=AS_OF,
        max_records=None,
        sample_size=3,
        report_mode="redacted",
        simulate=True,
    )
    return run_reconciliation(db, out, **{**defaults, **kwargs})


# ------------------------------------------------------------------ #
# Immutability and determinism                                         #
# ------------------------------------------------------------------ #


def test_dry_run_leaves_input_byte_identical(synthetic_db: Path, tmp_path: Path) -> None:
    sha = sha256_file(synthetic_db)
    digests = DataStore.table_digests(synthetic_db)
    siblings = sorted(p.name for p in synthetic_db.parent.iterdir())
    mtime = synthetic_db.stat().st_mtime_ns

    result = _run(synthetic_db, tmp_path / "out")

    assert result["exit"] == 0
    assert sha256_file(synthetic_db) == sha
    assert DataStore.table_digests(synthetic_db) == digests
    assert synthetic_db.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in synthetic_db.parent.iterdir()) == siblings  # no -wal/-shm
    assert result["summary"]["release_gates"]["input_unchanged"] is True
    assert result["summary"]["release_gates"]["working_copy_unchanged_by_dry_run"] is True


def test_two_runs_are_deterministic(synthetic_db: Path, tmp_path: Path) -> None:
    _run(synthetic_db, tmp_path / "one")
    _run(synthetic_db, tmp_path / "two")
    for name in ("summary.json", "review-plan.json", "summary.md"):
        assert (tmp_path / "one" / name).read_text() == (tmp_path / "two" / name).read_text()


def test_outputs_are_private_and_contain_no_personal_values(
    synthetic_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _run(synthetic_db, out, report_mode="full")
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    terms = DataStore.sensitive_terms(synthetic_db)
    assert COMPANIES[0] in terms and "thread-known" in terms
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text()
        for term in (*COMPANIES, "thread-known", "msg-human", "zorblat-systems.example"):
            assert term.lower() not in text.lower(), (path.name, term)
    assert set(json.loads((out / "id-map.json").read_text()).values()) >= {1, 2}


def test_leak_scanner_refuses_output_with_stored_values() -> None:
    assert find_leaks({"a.json": '{"x": 1}'}, {"Quuxwidget Labs"}) == []
    assert find_leaks({"a.json": "quuxwidget labs"}, {"Quuxwidget Labs"}) == [
        "a.json: 1 stored value(s)"
    ]
    assert find_leaks({"a.md": "ping someone@example.com"}, set()) == ["a.md: email address"]
    # Status and portal labels are aggregate vocabulary, not personal data.
    assert find_leaks({"a.md": "Applied LinkedIn"}, {"Applied", "LinkedIn"}) == []


# ------------------------------------------------------------------ #
# Findings                                                             #
# ------------------------------------------------------------------ #


def test_evidence_categories(synthetic_db: Path, tmp_path: Path) -> None:
    summary = _run(synthetic_db, tmp_path / "out")["summary"]
    totals = summary["evidence"]["totals"]
    assert totals["total_evidence"] == 3
    assert totals["automatically_linkable"] == 1  # same thread, same company and role
    assert totals["human_confirmed_preserved"] == 1
    assert totals["resolver_errors"] == 0
    assert totals["review_required"] + totals["likely_new_applications"] == 1
    plan = json.loads((tmp_path / "out" / "review-plan.json").read_text())
    auto = [r for r in plan["evidence"] if r["category"] == "auto_link"]
    assert all(all(r["checks"].values()) for r in auto)
    assert summary["release_gates"]["no_auto_link_with_strong_conflict"] is True


def test_duplicate_groups_are_classified_and_never_auto_merged(
    synthetic_db: Path, tmp_path: Path
) -> None:
    summary = _run(synthetic_db, tmp_path / "out")["summary"]
    categories = summary["duplicates"]["categories"]
    assert categories["high_confidence_review"] == 1
    assert categories["do_not_merge_strong_conflict"] == 1  # different job pages
    plan = json.loads((tmp_path / "out" / "review-plan.json").read_text())
    assert all(g["auto_merge"] is False for g in plan["duplicate_groups"])
    assert all(i["human_choice_mandatory"] for i in plan["review_items"] if i["kind"] != "evidence")
    store = DataStore(synthetic_db)
    assert store.list_merge_operations(1, 10)[1] == 0  # nothing merged on the input
    store.close()


def test_merge_simulation_restores_exactly(synthetic_db: Path, tmp_path: Path) -> None:
    summary = _run(synthetic_db, tmp_path / "out")["summary"]
    sim = summary["merge_simulation"]
    categories = {g["category"] for g in sim["groups"]}
    assert {"high_confidence_review", "synthetic"} <= categories
    for group in sim["groups"]:
        assert group["merged"] and group["undone"] and group["error"] is None
        assert group["active_after_merge"] == group["active_before"] - (group["size"] - 1)
        assert group["active_after_undo"] == group["active_before"]
        assert group["snapshot_checksum_valid"] and group["restored_exactly"]
        assert group["foreign_key_violations"] == 0
    analytics = summary["analytics"]["simulation"]
    assert analytics["undo_matches_baseline"] is True
    assert analytics["merge_delta"]["total_active_applications"] == {"before": 6, "after": 5}
    assert summary["release_gates"]["merge_undo_restores_exactly"] is True


def test_migration_audit_on_an_older_schema(tmp_path: Path) -> None:
    from tests.conftest import build_legacy_database, seed_legacy_rows

    legacy = build_legacy_database(tmp_path / "phase1.db", "phase1_schema.sql")
    seed_legacy_rows(legacy)
    sha = sha256_file(legacy)
    summary = _run(legacy, tmp_path / "out")["summary"]
    migration = summary["migration"]
    assert migration["source_revision"] is None or migration["source_revision"] != "0004"
    assert migration["target_is_head"] and migration["additive_schema_upgrade"]
    assert migration["core_aggregates_equal"] and migration["integrity"] == ["ok"]
    assert "mergeoperation" in migration["new_tables"]
    assert all(migration["restore_drill"].values())
    assert sha256_file(legacy) == sha


def test_thresholds_are_restored_after_use() -> None:
    before = rec.Thresholds.from_config()
    with rec.applied_thresholds(rec.Thresholds(200, 50, 90, 70)):
        assert app_config.RESOLVER_AUTO_LINK_SCORE == 200
    assert rec.Thresholds.from_config() == before


def test_anonymizer_is_stable_and_seeded() -> None:
    one, two, other = rec.Anonymizer("s"), rec.Anonymizer("s"), rec.Anonymizer("t")
    assert one("application", 7) == two("application", 7) != other("application", 7)
    assert one("application", 7).startswith("APP-") and "7" not in one("application", 7)[4:5]


# ------------------------------------------------------------------ #
# Guards and CLI                                                       #
# ------------------------------------------------------------------ #


def _guard(db: Path, out: Path, **overrides) -> None:
    args = dict(
        production_ack=None,
        resolver_version=rec.IdentityResolver.__module__ and reconcile_cli.RESOLVER_VERSION,
        thresholds=rec.Thresholds.from_config(),
        apply=False,
        apply_confirm=None,
    )
    check_guards(db, out, **{**args, **overrides})


def test_refuses_configured_production_path_without_ack(
    synthetic_db: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", synthetic_db)
    with pytest.raises(Refused, match="production"):
        _guard(synthetic_db, tmp_path / "out")
    _guard(synthetic_db, tmp_path / "out", production_ack=PRODUCTION_ACK)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"resolver_version": "0.0.1"}, "Resolver version"),
        ({"thresholds": rec.Thresholds(60, 25, 40, 70)}, "never loosened"),
        ({"apply": True}, "apply-confirm"),
    ],
)
def test_guards_refuse_unsafe_options(
    synthetic_db: Path, tmp_path: Path, overrides, message
) -> None:
    with pytest.raises(Refused, match=message):
        _guard(synthetic_db, tmp_path / "out", **overrides)


def test_refuses_output_inside_repository_or_non_empty(synthetic_db: Path, tmp_path: Path) -> None:
    with pytest.raises(Refused, match="outside the repository"):
        _guard(synthetic_db, reconcile_cli.REPO_ROOT / "docs" / "reconcile-out")
    _guard(synthetic_db, reconcile_cli.REPO_ROOT / ".job-tracker" / "reconcile-out")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    with pytest.raises(Refused, match="new or empty"):
        _guard(synthetic_db, busy)


def test_cli_dry_run_is_the_default(synthetic_db: Path, tmp_path: Path) -> None:
    sha = sha256_file(synthetic_db)
    out = tmp_path / "cli-out"
    result = CliRunner().invoke(
        reconcile_command,
        ["--db", str(synthetic_db), "--output-dir", str(out), "--as-of", "2026-10-09T12:00:00"],
    )
    assert result.exit_code == 0, result.output
    assert "Input unchanged: True" in result.output
    assert {p.name for p in out.iterdir()} == {
        "summary.json",
        "summary.md",
        "review-plan.json",
        "run.json",
    }
    assert sha256_file(synthetic_db) == sha


def test_cli_apply_records_only_verified_links(synthetic_db: Path, tmp_path: Path) -> None:
    before = DataStore.table_digests(synthetic_db)
    business_before = DataStore.table_digests(
        synthetic_db,
        {
            "application": [
                c
                for c in DataStore.table_columns(synthetic_db)["application"]
                if c != "last_evidence_at"
            ]
        },
    )
    refused = CliRunner().invoke(
        reconcile_command,
        ["--db", str(synthetic_db), "--output-dir", str(tmp_path / "a"), "--apply"],
    )
    assert refused.exit_code == 2 and DataStore.table_digests(synthetic_db) == before

    result = CliRunner().invoke(
        reconcile_command,
        [
            "--db",
            str(synthetic_db),
            "--output-dir",
            str(tmp_path / "b"),
            "--apply",
            "--apply-confirm",
            APPLY_CONFIRM,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Applied:  1 verified" in result.output
    after = DataStore.table_digests(synthetic_db)
    changed = {name for name in after if after[name] != before[name]}
    # Evidence ownership changes; on application only the derived last_evidence_at moves.
    assert changed == {"evidence", "application"}
    columns = {
        "application": [
            c
            for c in DataStore.table_columns(synthetic_db)["application"]
            if c != "last_evidence_at"
        ]
    }
    assert DataStore.table_digests(synthetic_db, columns) == business_before
    assert (
        after["mergeoperation"]["rows"] == 0 and after["statushistory"] == before["statushistory"]
    )
    assert any((tmp_path / "b" / "backups").iterdir())
    store = DataStore(synthetic_db)
    linked = store.get_evidence_by_external_id("gmail", "msg-thread-match")
    human = store.get_evidence_by_external_id("gmail", "msg-human")
    assert linked is not None and linked.processing_status == "linked"
    assert human is not None and human.decided_by == "human"
    store.close()


def test_data_quality_counts_noisy_roles_and_shared_threads(tmp_path: Path) -> None:
    store = DataStore(tmp_path / "dq.db")
    _app(store, COMPANIES[0], "Hi there, thanks for your interest", thread_ids='["t-shared"]')
    _app(store, COMPANIES[1], "", thread_ids='["T-SHARED"]')
    _app(
        store,
        COMPANIES[2],
        "Staff Engineer",
        job_url="https://www.linkedin.com/jobs/view/3912345678",
    )
    quality = rec.data_quality(store)
    store.close()
    assert quality["sentence_like_role"] == 1
    assert quality["empty_role"] == 1
    assert quality["without_thread"] == 1
    assert quality["thread_ids_on_multiple_applications"] == 1
    assert quality["with_external_job_id"] == 1
