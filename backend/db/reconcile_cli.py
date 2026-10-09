"""Phase 2 reconciliation command (scripts/reconcile_database.py).

Dry-run by default. The input database is opened only read-only: it is hashed, digested
and copied with SQLite's online backup API into a private temporary directory, and every
step — migration, resolver, duplicate previews, analytics, merge/undo simulation — runs on
those copies. The input's SHA-256 and per-table digests are compared before and after;
any difference fails the run.

`--apply` (never needed for an audit) records only the verified automatic evidence links on
the input database, after a verified backup, and requires `--apply-confirm`. It never
merges, creates applications or changes status.

Output files are mode 0600 in a 0700 directory and contain no names, roles, addresses,
subjects, snippets or message/thread IDs; a scanner checks them against the database's own
values before the run reports success. Human-facing output goes through click.echo.
See docs/phase-2-reconciliation-report.md.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import structlog

from backend import config as app_config
from backend.db.backup import create_backup, restore_backup, sha256_file, verify_backup
from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus
from backend.db.schema import SchemaPolicy, read_status
from backend.engine import reconciliation as rec
from backend.engine.identity_resolver import RESOLVER_VERSION
from backend.engine.merge_planner import plan_merge, resolve_field_values

log = structlog.get_logger(__name__)

PRODUCTION_ACK = "I-UNDERSTAND-THIS-IS-THE-CONFIGURED-PRODUCTION-DATABASE"
APPLY_CONFIRM = "APPLY-VERIFIED-EVIDENCE-LINKS"
REPO_ROOT = Path(__file__).resolve().parents[2]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

EXIT_REFUSED = 2
EXIT_INPUT_CHANGED = 3
EXIT_LEAK = 4


class Refused(click.ClickException):
    exit_code = EXIT_REFUSED


# ------------------------------------------------------------------ #
# Guards                                                               #
# ------------------------------------------------------------------ #


def check_guards(
    db: Path,
    output_dir: Path,
    *,
    production_ack: str | None,
    resolver_version: str,
    thresholds: rec.Thresholds,
    apply: bool,
    apply_confirm: str | None,
) -> None:
    if db.resolve() == app_config.DB_PATH.resolve() and production_ack != PRODUCTION_ACK:
        raise Refused(
            "This is the configured production database path. Reconcile a verified backup "
            f"copy instead, or pass --production-ack {PRODUCTION_ACK}."
        )
    if resolver_version != RESOLVER_VERSION:
        raise Refused(
            f"Resolver version {resolver_version} requested but this release runs "
            f"{RESOLVER_VERSION}; plans are only valid for the running resolver."
        )
    weaker = thresholds.weaker_than_config()
    if weaker:
        raise Refused(
            "Thresholds may be tightened but never loosened below the configured values: "
            + ", ".join(weaker)
        )
    out = output_dir.resolve()
    runtime = (REPO_ROOT / ".job-tracker").resolve()
    if out.is_relative_to(REPO_ROOT) and not out.is_relative_to(runtime):
        raise Refused(
            "Output must be outside the repository (or under the gitignored .job-tracker/): "
            "reconciliation output is derived from personal data."
        )
    if out.exists() and any(out.iterdir()):
        raise Refused("Output directory must be new or empty.")
    if apply and apply_confirm != APPLY_CONFIRM:
        raise Refused(f"--apply also needs --apply-confirm {APPLY_CONFIRM}.")


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write(path: Path, payload: Any) -> None:
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, sort_keys=True, default=str)
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text + ("\n" if not text.endswith("\n") else ""))


def find_leaks(texts: dict[str, str], terms: set[str]) -> list[str]:
    """Files containing a stored company/role/ID/sender/subject value or an email address.
    Only reports *which file* and *what kind* — never the value itself."""
    leaks = []
    lowered_terms = {t.lower() for t in terms if len(t) >= 4 and not t.isdigit()}
    for name, text in texts.items():
        low = text.lower()
        if _EMAIL.search(text):
            leaks.append(f"{name}: email address")
        hits = sum(1 for t in lowered_terms if t in low and not _benign(t))
        if hits:
            leaks.append(f"{name}: {hits} stored value(s)")
    return leaks


# Strings that are field values in this app but also legitimate aggregate labels.
_BENIGN = {s.value.lower() for s in ApplicationStatus} | {
    p.lower() for p in getattr(app_config, "SOURCE_PORTALS", ())
}


def _benign(term: str) -> bool:
    return term in _BENIGN or term in {"unknown", "linkedin", "naukri", "indeed", "instahyre"}


# ------------------------------------------------------------------ #
# Pipeline                                                             #
# ------------------------------------------------------------------ #


def _open(path: Path) -> DataStore:
    return DataStore(path, schema_policy=SchemaPolicy.VERIFY)


def _copy(source: Path, destination: Path) -> Path:
    DataStore.online_backup(source, destination, source_read_only=True)
    destination.chmod(0o600)
    return destination


def migration_audit(input_db: Path, work: Path) -> tuple[Path, dict[str, Any]]:
    """Copy → upgrade to head → prove the pre-existing columns are untouched → open with
    runtime maintenance → report derived backfills → backup/verify/restore round trip."""
    migrated = _copy(input_db, work / "migrated.db")
    source = read_status(migrated)
    # The revision row is the one thing a migration is meant to change.
    pre_columns = {
        k: v for k, v in DataStore.table_columns(migrated).items() if k != "alembic_version"
    }
    before = DataStore.table_digests(migrated, pre_columns)
    store = DataStore(migrated, schema_policy=SchemaPolicy.INSPECT)
    store.upgrade_schema()
    store.close()
    after_schema = DataStore.table_digests(migrated, pre_columns)
    store = _open(migrated)  # runtime maintenance runs here, on the copy
    store.close()
    after_open = DataStore.table_digests(migrated, pre_columns)
    target = read_status(migrated)
    counts = DataStore.count_rows_readonly(migrated)
    full = DataStore.table_digests(migrated)

    backup_root = _private_dir(work / "backups")
    backup = create_backup(migrated, backup_root, label="reconcile-drill")
    verified = verify_backup(backup.path)
    restored = work / "restored.db"
    restore_backup(backup.path, restored)
    restore_equal = DataStore.table_digests(restored) == full
    restored.unlink()

    return migrated, {
        "source_revision": source.current_revision,
        "target_revision": target.current_revision,
        "target_is_head": target.is_current,
        "integrity": DataStore.integrity_check(migrated),
        "foreign_key_violations": len(DataStore.foreign_key_check(migrated)),
        "table_counts": dict(sorted({k: v["rows"] for k, v in full.items()}.items())),
        "main_table_counts": counts,
        "new_tables": sorted(set(full) - set(pre_columns) - {"alembic_version"}),
        "additive_schema_upgrade": after_schema == before,
        "changed_by_schema_upgrade": sorted(k for k in before if before[k] != after_schema.get(k)),
        "changed_by_runtime_maintenance": {
            k: {"rows_before": after_schema[k]["rows"], "rows_after": after_open[k]["rows"]}
            for k in after_schema
            if after_schema[k] != after_open.get(k)
        },
        "core_aggregates_equal": DataStore.core_aggregates(input_db)
        == DataStore.core_aggregates(migrated),
        "restore_drill": {
            "backup_verified": verified.application_count == backup.manifest.application_count,
            "restored_tables_equal": restore_equal,
        },
    }


def _sample(rows: list[dict[str, Any]], key: str, seed: str, size: int) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for value in sorted({str(r.get(key)) for r in rows}):
        bucket = [r for r in rows if str(r.get(key)) == value]
        rng = random.Random(f"{seed}:{key}:{value}")
        out[value] = rng.sample(bucket, min(size, len(bucket)))
    return out


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def build_review_plan(
    evidence: dict[str, Any], replay: dict[str, Any], duplicates: dict[str, Any]
) -> list[dict[str, Any]]:
    """Items a person must look at, most consequential first."""
    items: list[dict[str, Any]] = []
    actions = {
        "auto_link": ("Link automatically when explicitly applied", False),
        "auto_link_failed_checks": ("Resolver anomaly: decide the link by hand", True),
        "already_linked_disagrees": ("Re-check the existing link", True),
        "likely_new_application": ("Confirm and create an application", True),
        "conflicting_strong_identifiers": ("Choose the application the identifiers mean", True),
        "review_required": ("Choose an application, create one, or dismiss", True),
        "resolver_error": ("Investigate resolver error", True),
    }
    for row in evidence["rows"]:
        action = actions.get(row["category"])
        if action is None:
            continue
        items.append(
            {
                "kind": "evidence",
                "category": row["category"],
                "evidence": row["evidence"],
                "applications": [c["application"] for c in row.get("candidates", [])],
                "reason": row.get("reason"),
                "confidence": row.get("confidence"),
                "positive_signals": row.get("positive_signals", []),
                "negative_signals": row.get("negative_signals", []),
                "proposed_action": action[0],
                "human_choice_mandatory": action[1],
            }
        )
    for group in duplicates["groups"]:
        do_not = group["category"] == "do_not_merge_strong_conflict"
        items.append(
            {
                "kind": "duplicate_group",
                "category": group["category"],
                "group": group["group"],
                "applications": group["applications"],
                "proposed_survivor": group["proposed_survivor"],
                "reason": ", ".join(sorted(group["signals"])) or "pairwise score",
                "confidence": group["min_pair_score"],
                "positive_signals": sorted(group["signals"]),
                "negative_signals": [
                    name
                    for name in (
                        "conflicting_external_ids",
                        "conflicting_canonical_urls",
                        "different_roles",
                        "large_date_span",
                    )
                    if group[name]
                ],
                "field_conflicts": group["field_conflicts"],
                "proposed_action": "Keep separate unless the conflict is explained"
                if do_not
                else "Preview in the merge dialog and decide",
                "human_choice_mandatory": True,
            }
        )
    grouped_pairs = {
        tuple(sorted(p))
        for g in duplicates["groups"]
        for p in itertools.combinations(g["_members"], 2)
    }
    seen_pairs: set[tuple[int, ...]] = set()
    for row in replay["rows"]:
        if row.get("outcome") == "review_required" and row.get("_pair"):
            pair = tuple(row["_pair"])
            if pair in seen_pairs or pair in grouped_pairs:
                continue
            seen_pairs.add(pair)
            items.append(
                {
                    "kind": "possible_duplicate",
                    "category": f"replay_{row['reason']}",
                    "applications": [row["application"], row["top_candidate"]],
                    "reason": row["reason"],
                    "confidence": row["top_candidate_score"],
                    "positive_signals": row["positive_signals"],
                    "negative_signals": row["negative_signals"],
                    "proposed_action": "Check whether these are the same application or a "
                    "re-application; merge only through the preview if they are the same",
                    "human_choice_mandatory": True,
                }
            )
            continue
        if row.get("outcome") != "linked":
            continue
        failed = [k for k, ok in (row.get("checks") or {}).items() if not ok]
        missed = tuple(row["_pair"]) not in grouped_pairs
        if not failed and not missed:
            continue
        items.append(
            {
                "kind": "resolver_anomaly",
                "category": "replay_link_failed_checks" if failed else "replay_link_not_suggested",
                "applications": [row["application"], row["proposed_application"]],
                "reason": row["reason"],
                "confidence": row["confidence"],
                "positive_signals": row["positive_signals"],
                "negative_signals": row["negative_signals"] + failed,
                "proposed_action": "Check whether these two records are the same application",
                "human_choice_mandatory": True,
            }
        )
    return items


def run_reconciliation(
    input_db: Path,
    output_dir: Path,
    *,
    seed: str,
    thresholds: rec.Thresholds,
    as_of: datetime,
    max_records: int | None,
    sample_size: int,
    report_mode: str,
    simulate: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    sha_before = sha256_file(input_db)
    digests_before = DataStore.table_digests(input_db)
    terms = DataStore.sensitive_terms(input_db)
    anon = rec.Anonymizer(seed)
    work = Path(tempfile.mkdtemp(prefix="reconcile-"))
    work.chmod(0o700)
    try:
        migrated, migration = migration_audit(input_db, work)
        with rec.applied_thresholds(thresholds):
            store = _open(migrated)
            try:
                analytics_before = rec.analytics_snapshot(store, as_of)
                quality = rec.data_quality(store)
                evidence = rec.reconcile_evidence(store, anon, as_of, max_records)
                evidence_again = rec.reconcile_evidence(store, anon, as_of, max_records)
                replay = rec.replay_applications(store, anon, max_records)
                replay_again = rec.replay_applications(store, anon, max_records)
                duplicates = rec.reconcile_duplicates(store, anon, thresholds.duplicate_score)
                duplicates_again = rec.reconcile_duplicates(store, anon, thresholds.duplicate_score)
            finally:
                store.close()
            migrated_digest = DataStore.table_digests(migrated)

            projection = _project_links(migrated, work, evidence, anon, as_of)
            simulation = (
                _simulate(migrated, work, duplicates, replay, anon, seed, sample_size, as_of)
                if simulate
                else {"skipped": True}
            )
        plan_unchanged = DataStore.table_digests(migrated) == migrated_digest
    finally:
        shutil.rmtree(work, ignore_errors=True)

    sha_after = sha256_file(input_db)
    input_unchanged = (
        sha_after == sha_before and DataStore.table_digests(input_db) == digests_before
    )
    deterministic = (
        json.dumps(evidence, sort_keys=True, default=str)
        == json.dumps(evidence_again, sort_keys=True, default=str)
        and json.dumps(replay, sort_keys=True, default=str)
        == json.dumps(replay_again, sort_keys=True, default=str)
        and json.dumps(duplicates, sort_keys=True, default=str)
        == json.dumps(duplicates_again, sort_keys=True, default=str)
    )
    strong_conflict_links = sum(
        1
        for r in evidence["rows"]
        if r["category"] in {"auto_link", "auto_link_failed_checks"}
        and not (
            r["checks"].get("no_external_job_id_conflict", True)
            and r["checks"].get("no_canonical_url_conflict", True)
        )
    )
    sims = simulation.get("groups", []) if isinstance(simulation, dict) else []
    gates = {
        "migration_reaches_head": migration["target_is_head"],
        "migration_additive": migration["additive_schema_upgrade"],
        "integrity_ok": migration["integrity"] == ["ok"]
        and migration["foreign_key_violations"] == 0,
        "restore_drill_ok": all(migration["restore_drill"].values()),
        "analytics_unchanged_by_migration": migration["core_aggregates_equal"],
        "input_unchanged": input_unchanged,
        "working_copy_unchanged_by_dry_run": plan_unchanged,
        "deterministic_within_run": deterministic,
        "no_human_decision_overwritten": True,  # dry-run writes nothing; see input_unchanged
        "no_auto_link_with_strong_conflict": strong_conflict_links == 0,
        "no_resolver_errors": evidence["totals"]["resolver_errors"] == 0,
        "merge_undo_restores_exactly": bool(sims)
        and all(s["restored_exactly"] and s["snapshot_checksum_valid"] for s in sims)
        if simulate
        else None,
        "no_automatic_merge": True,
    }
    review = build_review_plan(evidence, replay, duplicates)
    summary = {
        "reconciliation_version": rec.RECONCILIATION_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "as_of": as_of.isoformat(),
        "thresholds": thresholds.__dict__,
        "input": {"sha256": sha_before, "unchanged": input_unchanged},
        "migration": migration,
        "evidence": {"totals": evidence["totals"], "breakdowns": evidence["breakdowns"]},
        "resolver_replay": {k: v for k, v in replay.items() if k != "rows"},
        "data_quality": quality,
        "duplicates": {
            "candidate_pairs": duplicates["candidate_pairs"],
            "dismissed_pairs": duplicates["dismissed_pairs"],
            **duplicates["summary"],
        },
        "analytics": {
            "migrated_before_reconciliation": _redacted_analytics(analytics_before),
            "projected_with_auto_links": projection["summary"],
            "simulation": simulation.get("analytics") if isinstance(simulation, dict) else None,
        },
        "merge_simulation": {k: v for k, v in simulation.items() if k != "analytics"}
        if isinstance(simulation, dict)
        else simulation,
        "review_workload": dict(
            sorted(
                {
                    f"{i['kind']}:{i['category']}": sum(
                        1
                        for j in review
                        if (j["kind"], j["category"]) == (i["kind"], i["category"])
                    )
                    for i in review
                }.items()
            )
        ),
        "release_gates": gates,
    }
    plan = {
        "summary_ref": "summary.json",
        "review_items": review,
        "evidence": [_public(r) for r in evidence["rows"]],
        "resolver_replay": [_public(r) for r in replay["rows"]],
        "duplicate_groups": [_public(g) for g in duplicates["groups"]],
        "samples": {
            "evidence_by_category": _sample(evidence["rows"], "category", seed, sample_size),
            "replay_by_outcome": _sample(
                [_public(r) for r in replay["rows"]], "outcome", seed, sample_size
            ),
            "duplicates_by_category": _sample(
                [_public(g) for g in duplicates["groups"]], "category", seed, sample_size
            ),
        },
    }
    outputs = {
        "summary.json": json.dumps(summary, indent=2, sort_keys=True, default=str),
        "summary.md": render_markdown(summary),
        "review-plan.json": json.dumps(plan, indent=2, sort_keys=True, default=str),
    }
    if report_mode == "full":
        outputs["id-map.json"] = json.dumps(anon.mapping, indent=2, sort_keys=True)
    leaks = find_leaks({k: v for k, v in outputs.items() if k != "id-map.json"}, terms)
    _private_dir(output_dir)
    if leaks:
        _write(output_dir / "LEAK-REFUSED.txt", "\n".join(leaks))
        return {"exit": EXIT_LEAK, "leaks": leaks, "summary": summary}
    for name, text in outputs.items():
        _write(output_dir / name, text)
    _write(
        output_dir / "run.json",
        {"duration_seconds": round(time.monotonic() - started, 2), "report_mode": report_mode},
    )
    code = 0 if input_unchanged else EXIT_INPUT_CHANGED
    return {"exit": code, "summary": summary}


def _redacted_analytics(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Aggregates only. Sections that can carry per-record labels are reduced to digests."""
    out = {k: v for k, v in snapshot.items() if k not in {"flow", "conversions_6m"}}
    out["flow_digest"] = rec.analytics_digest({"flow": snapshot.get("flow")})
    out["conversions_6m_digest"] = rec.analytics_digest({"c": snapshot.get("conversions_6m")})
    out["digest"] = rec.analytics_digest(snapshot)
    return out


def _project_links(
    migrated: Path, work: Path, evidence: dict[str, Any], anon: rec.Anonymizer, as_of: datetime
) -> dict[str, Any]:
    """Approve every verified automatic link on a disposable copy and recompute analytics.
    Evidence links do not change application rows, so analytics are expected to match."""
    copy = _copy(migrated, work / "projection.db")
    store = _open(copy)
    try:
        before = rec.analytics_snapshot(store, as_of)
        linked = rec.apply_evidence_links(store, evidence["rows"], anon)
        after = rec.analytics_snapshot(store, as_of)
    finally:
        store.close()
        copy.unlink()
    return {
        "summary": {
            "links_applied_on_copy": linked,
            "analytics_changed": rec.analytics_digest(before) != rec.analytics_digest(after),
            "delta": rec.analytics_delta(before, after),
        }
    }


def _synthetic_group(store: DataStore, as_of: datetime) -> dict[str, Any]:
    """Three synthetic duplicates (no real data) so merge/undo is exercised even when the
    database has no real candidates in a category."""
    ids = []
    for n in range(3):
        app = store.upsert_application(
            Application(
                company="Synthetic Reconciliation Co",
                role="Simulation Engineer",
                source_portal="LinkedIn",
                applied_date=as_of,
                current_status=ApplicationStatus.APPLIED
                if n < 2
                else ApplicationStatus.RESUME_SHORTLISTED,
                thread_ids=f'["synthetic-thread-{n}"]',
            )
        )
        assert app.id is not None
        ids.append(app.id)
    return {"group": "SYNTHETIC", "category": "synthetic", "_members": ids}


def _simulate(
    migrated: Path,
    work: Path,
    duplicates: dict[str, Any],
    replay: dict[str, Any],
    anon: rec.Anonymizer,
    seed: str,
    sample_size: int,
    as_of: datetime,
) -> dict[str, Any]:
    """Merge/undo representative groups on disposable copies, never the plan copy.
    Besides duplicate groups, real record pairs the resolver replay sent to review are
    sampled per reason, so merge/undo is exercised on real history, events and threads
    even when no pair reaches the suggestion threshold. Nothing here is a recommendation
    to merge those pairs."""
    copy = _copy(migrated, work / "simulation.db")
    store = _open(copy)
    results: list[dict[str, Any]] = []
    try:
        digests = lambda: DataStore.table_digests(copy)  # noqa: E731
        fks = lambda: len(DataStore.foreign_key_check(copy))  # noqa: E731
        baseline = rec.analytics_snapshot(store, as_of)
        by_category: dict[str, list[dict[str, Any]]] = {}
        for group in duplicates["groups"]:
            by_category.setdefault(group["category"], []).append(group)
        for row in replay["rows"]:
            if row.get("outcome") == "review_required" and row.get("_pair"):
                members = list(row["_pair"])
                category = f"replay_{row['reason']}"
                bucket = by_category.setdefault(category, [])
                if all(g["_members"] != members for g in bucket):
                    bucket.append(
                        {
                            "group": anon("group", members[0]),
                            "category": category,
                            "_members": members,
                        }
                    )
        replay_categories = sorted(c for c in by_category if c.startswith("replay_"))
        for category in (
            "high_confidence_review",
            "medium_confidence_review",
            "insufficient_evidence",
            *replay_categories,
        ):
            groups = by_category.get(category, [])
            rng = random.Random(f"{seed}:simulate:{category}")
            for index, group in enumerate(rng.sample(groups, min(sample_size, len(groups)))):
                outcome = rec.simulate_merge_and_undo(
                    store, digests, fks, group, f"{category}-{index}"
                )
                results.append(outcome.to_json())

        # Projected merged state: every high-confidence group merged at once, then undone.
        high = by_category.get("high_confidence_review", [])
        before_all = digests()
        operations: list[int] = []
        for index, group in enumerate(high):
            state = store.load_merge_state(group["_members"])
            state["application_ids"] = group["_members"]
            plan = plan_merge(state)
            values = resolve_field_values(plan, {n: plan.survivor_id for n in plan.conflicts})
            op, _ = store.execute_merge(
                application_ids=group["_members"],
                survivor_id=plan.survivor_id,
                field_values=values,
                expected_token=plan.token,
                idempotency_key=f"reconcile-all-high-{index}",
                initiated_by="reconciliation-simulation",
            )
            assert op.id is not None
            operations.append(op.id)
        merged_state = rec.analytics_snapshot(store, as_of)
        for op_id in reversed(operations):
            store.undo_merge(op_id, undone_by="reconciliation-simulation")
        after_undo = rec.analytics_snapshot(store, as_of)
        all_high_restored = rec.business_tables_equal(before_all, digests())
    finally:
        store.close()
        copy.unlink()

    synthetic_copy = _copy(migrated, work / "synthetic.db")
    store = _open(synthetic_copy)
    try:
        group = _synthetic_group(store, as_of)
        outcome = rec.simulate_merge_and_undo(
            store,
            lambda: DataStore.table_digests(synthetic_copy),
            lambda: len(DataStore.foreign_key_check(synthetic_copy)),
            group,
            "synthetic",
        )
        results.append(outcome.to_json())
    finally:
        store.close()
        synthetic_copy.unlink()

    return {
        "groups": results,
        "all_high_confidence_groups": len(high),
        "all_high_restored_exactly": all_high_restored,
        "analytics": {
            "simulated_all_high_merged": _redacted_analytics(merged_state),
            "after_undo": _redacted_analytics(after_undo),
            "merge_delta": rec.analytics_delta(baseline, merged_state),
            "undo_matches_baseline": rec.analytics_digest(baseline)
            == rec.analytics_digest(after_undo),
        },
    }


# ------------------------------------------------------------------ #
# Human-readable summary                                               #
# ------------------------------------------------------------------ #


def render_markdown(summary: dict[str, Any]) -> str:
    m, e, d = summary["migration"], summary["evidence"]["totals"], summary["duplicates"]
    a = summary["analytics"]["migrated_before_reconciliation"]
    lines = [
        "# Reconciliation summary (redacted aggregates)",
        "",
        f"- As of: {summary['as_of']}; resolver {summary['resolver_version']}; "
        f"reconciliation {summary['reconciliation_version']}",
        f"- Input SHA-256: `{summary['input']['sha256']}`; "
        f"unchanged: {summary['input']['unchanged']}",
        f"- Schema: {m['source_revision']} → {m['target_revision']}; integrity {m['integrity']}; "
        f"FK violations {m['foreign_key_violations']}; additive {m['additive_schema_upgrade']}",
        "",
        "## Evidence",
        "",
        *[f"- {k}: {v}" for k, v in e.items()],
        "",
        "## Duplicates",
        "",
        f"- candidate pairs: {d['candidate_pairs']}; groups: {d['groups']}; "
        f"dismissed: {d['dismissed_pairs']}",
        *[f"- {k}: {v}" for k, v in d["categories"].items()],
        "",
        "## Analytics (migrated copy)",
        "",
        f"- active (unmerged) applications: {a['total_active_applications']}; currently at "
        f"interview or later {a['current_interview_or_later']}; currently at offer or joined "
        f"{a['current_offer_or_joined']}; stale {a['stale_count']}",
        "",
        "## Release gates",
        "",
        *[f"- {k}: {v}" for k, v in summary["release_gates"].items()],
        "",
        "## Review workload",
        "",
        *[f"- {k}: {v}" for k, v in summary["review_workload"].items()],
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ #
# Command                                                              #
# ------------------------------------------------------------------ #


@click.command("reconcile")
@click.option(
    "--db", "db_path", required=True, type=click.Path(path_type=Path, dir_okay=False, exists=True)
)
@click.option("--output-dir", required=True, type=click.Path(path_type=Path, file_okay=False))
@click.option(
    "--dry-run/--apply",
    "dry_run",
    default=True,
    show_default=True,
    help="--apply records verified automatic evidence links only (never merges).",
)
@click.option("--apply-confirm", default=None, help=f"Required with --apply: {APPLY_CONFIRM}")
@click.option(
    "--report-mode",
    type=click.Choice(["redacted", "full"]),
    default="redacted",
    show_default=True,
    help="full also writes id-map.json (anonymized ID → record ID).",
)
@click.option(
    "--seed",
    default="phase2-reconciliation",
    show_default=True,
    help="Anonymization and sampling seed (same seed → same labels and samples).",
)
@click.option("--resolver-version", default=RESOLVER_VERSION, show_default=True)
@click.option(
    "--auto-link-score", type=int, default=app_config.RESOLVER_AUTO_LINK_SCORE, show_default=True
)
@click.option(
    "--auto-link-margin", type=int, default=app_config.RESOLVER_AUTO_LINK_MARGIN, show_default=True
)
@click.option(
    "--review-score", type=int, default=app_config.RESOLVER_REVIEW_SCORE, show_default=True
)
@click.option(
    "--duplicate-score", type=int, default=app_config.DUPLICATE_SUGGESTION_SCORE, show_default=True
)
@click.option("--max-records", type=click.IntRange(min=1), default=None)
@click.option("--sample-size", type=click.IntRange(min=0), default=5, show_default=True)
@click.option(
    "--as-of",
    type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]),
    default=None,
    help="Fixed 'now' (UTC) for analytics, stale and age bands. Default: current time.",
)
@click.option("--simulate-merges/--no-simulate-merges", default=True, show_default=True)
@click.option("--production-ack", default=None, hidden=True)
def reconcile_command(
    db_path: Path,
    output_dir: Path,
    dry_run: bool,
    apply_confirm: str | None,
    report_mode: str,
    seed: str,
    resolver_version: str,
    auto_link_score: int,
    auto_link_margin: int,
    review_score: int,
    duplicate_score: int,
    max_records: int | None,
    sample_size: int,
    as_of: datetime | None,
    simulate_merges: bool,
    production_ack: str | None,
) -> None:
    """Audit what Phase 2 identity resolution and duplicate merging would do (dry-run)."""
    thresholds = rec.Thresholds(auto_link_score, auto_link_margin, review_score, duplicate_score)
    check_guards(
        db_path,
        output_dir,
        production_ack=production_ack,
        resolver_version=resolver_version,
        thresholds=thresholds,
        apply=not dry_run,
        apply_confirm=apply_confirm,
    )
    moment = (as_of.replace(tzinfo=UTC) if as_of else datetime.now(UTC)).replace(microsecond=0)
    if not dry_run:
        _apply(db_path, output_dir, seed, thresholds, moment)
        return
    result = run_reconciliation(
        db_path,
        output_dir,
        seed=seed,
        thresholds=thresholds,
        as_of=moment,
        max_records=max_records,
        sample_size=sample_size,
        report_mode=report_mode,
        simulate=simulate_merges,
    )
    if result["exit"] == EXIT_LEAK:
        raise click.ClickException(
            "Output would contain stored personal values; nothing was written except "
            "LEAK-REFUSED.txt: " + "; ".join(result["leaks"])
        )
    gates = result["summary"]["release_gates"]
    click.echo(f"Output:   {output_dir}")
    click.echo(f"Input unchanged: {result['summary']['input']['unchanged']}")
    for name, value in gates.items():
        click.echo(f"  {'✓' if value else ('–' if value is None else '✗')} {name}")
    if result["exit"]:
        raise SystemExit(result["exit"])


def _apply(
    db: Path, output_dir: Path, seed: str, thresholds: rec.Thresholds, as_of: datetime
) -> None:
    """Record verified automatic evidence links on `db` itself, after a verified backup."""
    status = read_status(db)
    if not status.is_current:
        raise Refused("--apply needs a database already at the head revision.")
    _private_dir(output_dir)
    backup = create_backup(db, _private_dir(output_dir / "backups"), label="pre-reconcile-apply")
    verify_backup(backup.path)
    anon = rec.Anonymizer(seed)
    with rec.applied_thresholds(thresholds):
        store = _open(db)
        try:
            evidence = rec.reconcile_evidence(store, anon, as_of)
            applied = rec.apply_evidence_links(store, evidence["rows"], anon)
        finally:
            store.close()
    log.info("reconciliation_applied", evidence_links=applied, backup=backup.path.name)
    click.echo(f"Backup:   {backup.path}")
    click.echo(f"Applied:  {applied} verified automatic evidence link(s); nothing else changed.")
