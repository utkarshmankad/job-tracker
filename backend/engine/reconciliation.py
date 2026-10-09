"""Phase 2 reconciliation: a read-only audit of what the resolver, duplicate detection and
merge workflow would do to an existing database (docs/phase-2-reconciliation-report.md).

Everything here reads through DataStore and the same resolver/merge code the API uses. The
only writing helpers (`apply_evidence_links`, `simulate_merge_and_undo`) are called by the
CLI on disposable copies, or — for evidence links only — behind its explicit apply flags.
No function here merges records on the database it is auditing, creates applications or
changes application status.

Output never contains company names, roles, addresses, subjects, snippets, message or
thread IDs: records are referred to by stable anonymized IDs (`Anonymizer`), and only
aggregate signal names, scores, reasons and counts are reported.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from backend import config as app_config
from backend.db.data_store import (
    ApplicationFilter,
    DataStore,
    EvidenceFilter,
    MergeError,
    is_application_stale,
)
from backend.db.models import (
    Application,
    ApplicationStatus,
    DecisionSource,
    Evidence,
    EvidenceStatus,
    RecordState,
)
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.identity_resolver import (
    EvidenceSignals,
    IdentityResolver,
    MessageKind,
    Outcome,
    ResolutionResult,
    company_relation,
    compare_applications,
    role_relation,
    signals_from_evidence,
)
from backend.engine.insights_engine import InsightsEngine
from backend.engine.merge_planner import MergePlanError, plan_merge, resolve_field_values
from backend.engine.normalization import (
    canonical_job_url,
    external_job_id_from_url,
    normalize_company,
    normalize_external_job_id,
    normalize_role,
    normalize_source,
    normalize_thread_id,
)

RECONCILIATION_VERSION = "1.0.0"

# Evidence the resolver may still decide. Anything a person decided, and anything already
# classified as not about an application, is reported but never re-resolved.
_RESOLVABLE = {
    EvidenceStatus.PENDING.value,
    EvidenceStatus.PROCESSING.value,
    EvidenceStatus.NEEDS_REVIEW.value,
    EvidenceStatus.ERROR.value,
}
_ALREADY_LINKED = {EvidenceStatus.LINKED.value, EvidenceStatus.CREATED_APPLICATION.value}
_NOT_APPLICATION = {
    EvidenceStatus.IGNORED.value,
    EvidenceStatus.INFORMATIONAL.value,
    EvidenceStatus.DISMISSED.value,
}
_UNKNOWN_SOURCES = {"other", "company_portal", "unknown", "direct"}
_INTERVIEW_STATUSES = {
    ApplicationStatus.INTERVIEW_SCHEDULED.value,
    ApplicationStatus.INTERVIEW_IN_PROGRESS.value,
    ApplicationStatus.OFFER_NEGOTIATION.value,
    ApplicationStatus.OFFER.value,
    ApplicationStatus.JOINED.value,
}
_OFFER_STATUSES = {
    ApplicationStatus.OFFER_NEGOTIATION.value,
    ApplicationStatus.OFFER.value,
    ApplicationStatus.JOINED.value,
}
_RESOLVER_ERRORS = (ValueError, LookupError, RuntimeError, TypeError, SQLAlchemyError)


# ------------------------------------------------------------------ #
# Configuration                                                        #
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class Thresholds:
    auto_link_score: int
    auto_link_margin: int
    review_score: int
    duplicate_score: int

    @classmethod
    def from_config(cls) -> Thresholds:
        return cls(
            app_config.RESOLVER_AUTO_LINK_SCORE,
            app_config.RESOLVER_AUTO_LINK_MARGIN,
            app_config.RESOLVER_REVIEW_SCORE,
            app_config.DUPLICATE_SUGGESTION_SCORE,
        )

    def weaker_than_config(self) -> list[str]:
        """Names of thresholds set below the configured values. Reconciliation may tighten
        thresholds to see what changes, never loosen them."""
        base = Thresholds.from_config()
        return [
            name
            for name in ("auto_link_score", "auto_link_margin", "review_score", "duplicate_score")
            if getattr(self, name) < getattr(base, name)
        ]


@contextmanager
def applied_thresholds(thresholds: Thresholds) -> Iterator[None]:
    """Run the resolver with these thresholds (read from config at call time), restoring
    the configured values afterwards."""
    names = {
        "RESOLVER_AUTO_LINK_SCORE": thresholds.auto_link_score,
        "RESOLVER_AUTO_LINK_MARGIN": thresholds.auto_link_margin,
        "RESOLVER_REVIEW_SCORE": thresholds.review_score,
    }
    saved = {name: getattr(app_config, name) for name in names}
    try:
        for name, value in names.items():
            setattr(app_config, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(app_config, name, value)


class Anonymizer:
    """Stable pseudonymous IDs: the same seed and record give the same label in every run,
    and labels reveal nothing about the record."""

    _PREFIX = {"application": "APP", "evidence": "EV", "group": "GRP"}

    def __init__(self, seed: str) -> None:
        self._seed = seed
        self.mapping: dict[str, int] = {}

    def __call__(self, kind: str, record_id: int) -> str:
        digest = hashlib.sha256(f"{self._seed}\x1f{kind}\x1f{record_id}".encode()).hexdigest()
        label = f"{self._PREFIX.get(kind, kind.upper())}-{digest[:10]}"
        self.mapping[label] = record_id
        return label


def confidence_band(value: float | None) -> str:
    if value is None:
        return "none"
    if value >= 0.9:
        return "0.90-1.00"
    if value >= 0.67:
        return "0.67-0.89"
    if value >= 0.33:
        return "0.33-0.66"
    return "0.00-0.32"


def age_band(occurred_at: datetime | None, as_of: datetime) -> str:
    if occurred_at is None:
        return "unknown"
    days = (as_of - _aware(occurred_at)).days
    for limit, label in ((7, "0-6d"), (30, "7-29d"), (90, "30-89d"), (180, "90-179d")):
        if days < limit:
            return label
    return "180d+"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _counter(values: Iterator[str] | list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


# ------------------------------------------------------------------ #
# Proposed-link verification                                           #
# ------------------------------------------------------------------ #


def _app_identity(app: Application) -> dict[str, Any]:
    url = app.canonical_job_url or canonical_job_url(app.job_url)
    extracted = external_job_id_from_url(url)
    return {
        "company": app.normalized_company or normalize_company(app.company),
        "role": app.normalized_role or normalize_role(app.role),
        "url": url,
        "job_id": normalize_external_job_id(app.external_job_id)
        or (extracted[1] if extracted else None),
        "source": normalize_source(app.source_portal),
    }


def verify_link(signals: EvidenceSignals, app: Application | None) -> dict[str, bool]:
    """Independent checks on a proposed automatic link. Every value must be True for the
    link to be proposed; a False makes it a review item (release blocker if the resolver
    itself proposed it)."""
    if app is None:
        return {"target_exists": False}
    ident = _app_identity(app)
    delta = _aware(signals.occurred_at) - _aware(app.applied_date)
    sources = {signals.source, ident["source"]} - _UNKNOWN_SOURCES
    return {
        "target_exists": True,
        "target_active": app.record_state == RecordState.ACTIVE.value,
        "target_not_merged": app.merged_into_application_id is None,
        "no_external_job_id_conflict": not (
            signals.external_job_id
            and ident["job_id"]
            and signals.external_job_id != ident["job_id"]
        ),
        "no_canonical_url_conflict": not (
            signals.canonical_url and ident["url"] and signals.canonical_url != ident["url"]
        ),
        "company_compatible": company_relation(signals.company, ident["company"]) != "conflict",
        "role_compatible": role_relation(signals.role, ident["role"]) != "conflict",
        "date_compatible": timedelta(days=-app_config.RESOLVER_DATE_WINDOW_BEFORE_DAYS)
        <= delta
        <= timedelta(days=app_config.RESOLVER_DATE_WINDOW_AFTER_DAYS),
        "source_compatible": len(sources) <= 1,
    }


def _signal_split(result: ResolutionResult, app_id: int | None) -> tuple[list[str], list[str]]:
    chosen = next((c for c in result.candidates if c.application_id == app_id), None)
    if chosen is None and result.candidates:
        chosen = result.candidates[0]
    if chosen is None:
        return [], []
    positive = sorted(s.name for s in chosen.signals if s.weight > 0)
    negative = sorted(s.name for s in chosen.signals if s.weight < 0)
    return positive, negative


# ------------------------------------------------------------------ #
# Evidence reconciliation                                              #
# ------------------------------------------------------------------ #


def evidence_kind(evidence: Evidence) -> MessageKind:
    """The message kind recorded when the evidence was first resolved; otherwise a status
    signal makes it a status update, and anything else is treated as a follow-up, which
    can never create an application (precision over recall)."""
    recorded = (evidence.resolver_result or {}).get("message_kind")
    if recorded in {k.value for k in MessageKind}:
        return MessageKind(recorded)
    parser = (evidence.raw_metadata or {}).get("parser") or {}
    if isinstance(parser, dict) and parser.get("status_signal"):
        return MessageKind.STATUS_UPDATE
    return MessageKind.FOLLOW_UP


def _iter_evidence(store: DataStore, max_records: int | None) -> Iterator[Evidence]:
    page, seen = 1, 0
    while True:
        items, _ = store.list_evidence(
            EvidenceFilter(include_ignored=True, page=page, page_size=500)
        )
        if not items:
            return
        for item in sorted(items, key=lambda e: e.id or 0):
            if max_records is not None and seen >= max_records:
                return
            seen += 1
            yield item
        page += 1


def reconcile_evidence(
    store: DataStore, anon: Anonymizer, as_of: datetime, max_records: int | None = None
) -> dict[str, Any]:
    """Resolve every eligible evidence row without writing anything."""
    resolver = IdentityResolver(store)
    rows: list[dict[str, Any]] = []
    for evidence in _iter_evidence(store, max_records):
        assert evidence.id is not None
        row: dict[str, Any] = {
            "evidence": anon("evidence", evidence.id),
            "evidence_type": evidence.evidence_type,
            "source": evidence.source,
            "status_before": evidence.processing_status,
            "link_method_before": evidence.link_method,
            "age_band": age_band(evidence.occurred_at, as_of),
            "linked_application": anon("application", evidence.application_id)
            if evidence.application_id
            else None,
        }
        if evidence.decided_by == DecisionSource.HUMAN.value:
            rows.append({**row, "category": "human_decision_preserved", "outcome": "skipped"})
            continue
        if evidence.processing_status in _NOT_APPLICATION:
            rows.append({**row, "category": "ignored_or_irrelevant", "outcome": "skipped"})
            continue
        kind = evidence_kind(evidence)
        signals = signals_from_evidence(evidence, kind)
        try:
            result = resolver.resolve(signals)
        except _RESOLVER_ERRORS as exc:
            rows.append(
                {
                    **row,
                    "category": "resolver_error",
                    "outcome": "error",
                    "error": type(exc).__name__,
                }
            )
            continue
        target = result.application_id
        positive, negative = _signal_split(result, target)
        row.update(
            outcome=result.outcome.value,
            reason=result.reason,
            confidence=result.confidence,
            confidence_band=confidence_band(result.confidence),
            message_kind=kind.value,
            link_method=result.link_method,
            positive_signals=positive,
            negative_signals=negative,
            proposed_application=anon("application", target) if target else None,
            candidates=[
                {"application": anon("application", c.application_id), "score": c.score}
                for c in result.candidates[:5]
            ],
        )
        if evidence.processing_status in _ALREADY_LINKED:
            agrees = result.outcome is Outcome.LINKED and target == evidence.application_id
            row["category"] = "already_linked_confirmed" if agrees else "already_linked_disagrees"
        elif result.outcome is Outcome.LINKED:
            checks = verify_link(signals, store.get_application(target) if target else None)
            row["checks"] = checks
            row["category"] = "auto_link" if all(checks.values()) else "auto_link_failed_checks"
        elif result.outcome is Outcome.NEW_APPLICATION:
            row["category"] = "likely_new_application"
        elif result.reason in {
            "conflicting_strong_identifiers",
            "strong_identifier_conflict",
            "strong_identifier_disagrees_with_text",
        }:
            row["category"] = "conflicting_strong_identifiers"
        elif result.outcome is Outcome.IGNORED:
            row["category"] = "ignored_or_irrelevant"
        else:
            row["category"] = "review_required"
        rows.append(row)

    resolved = [r for r in rows if r["outcome"] not in {"skipped", "error"}]
    counts = Counter(r["category"] for r in rows)
    return {
        "rows": rows,
        "totals": {
            "total_evidence": len(rows),
            "already_linked": sum(1 for r in rows if r["status_before"] in _ALREADY_LINKED),
            "unlinked": sum(1 for r in rows if r["linked_application"] is None),
            "automatically_linkable": counts["auto_link"],
            "auto_link_failed_checks": counts["auto_link_failed_checks"],
            "likely_new_applications": counts["likely_new_application"],
            "review_required": counts["review_required"],
            "ignored_or_irrelevant": counts["ignored_or_irrelevant"],
            "conflicting_strong_identifiers": counts["conflicting_strong_identifiers"],
            "human_confirmed_preserved": counts["human_decision_preserved"],
            "already_linked_confirmed": counts["already_linked_confirmed"],
            "already_linked_disagrees": counts["already_linked_disagrees"],
            # Evidence is unique by fingerprint and (source, external_id), so a replayed
            # message can never appear twice here; counted to make that visible.
            "idempotent_duplicates_skipped": 0,
            "resolver_errors": counts["resolver_error"],
        },
        "breakdowns": {
            "evidence_type": _counter(r["evidence_type"] for r in rows),
            "source": _counter(r["source"] for r in rows),
            "outcome": _counter(r["outcome"] for r in rows),
            "category": _counter(r["category"] for r in rows),
            "reason": _counter(r["reason"] for r in resolved),
            "confidence_band": _counter(r["confidence_band"] for r in resolved),
            "link_method": _counter(r["link_method"] or "none" for r in resolved),
            "age_band": _counter(r["age_band"] for r in rows),
        },
    }


# ------------------------------------------------------------------ #
# Resolver replay of application identities (simulation)               #
# ------------------------------------------------------------------ #


class _ExcludingResolver(IdentityResolver):
    """The production resolver with one application hidden from its candidates."""

    def __init__(self, db: DataStore, exclude_id: int) -> None:
        super().__init__(db)
        self._exclude = exclude_id

    def _candidates(self, signals: EvidenceSignals) -> list[Application]:
        return [a for a in super()._candidates(signals) if a.id != self._exclude]


def application_signals(app: Application) -> EvidenceSignals:
    """What the application's original acknowledgement would have looked like to the
    resolver: its identifiers, first thread, applied date and source."""
    ident = _app_identity(app)
    try:
        threads = [t for t in json.loads(app.thread_ids or "[]") if isinstance(t, str)]
    except (json.JSONDecodeError, TypeError):
        threads = []
    url = ident["url"]
    extracted = external_job_id_from_url(url)
    return EvidenceSignals(
        occurred_at=_aware(app.applied_date),
        kind=MessageKind.ACKNOWLEDGEMENT,
        thread_id=normalize_thread_id(threads[0]) if threads else None,
        external_job_id=ident["job_id"],
        external_job_source=extracted[0] if extracted else ident["source"],
        canonical_url=url,
        company=ident["company"],
        role=ident["role"],
        source=ident["source"],
        classification_confident=True,
    )


def replay_applications(
    store: DataStore, anon: Anonymizer, max_records: int | None = None
) -> dict[str, Any]:
    """For each active application, ask the resolver which *other* application it would
    attach that application's acknowledgement to. A confident answer means the two
    records describe the same application — an independent cross-check of duplicate
    detection. Nothing is written."""
    apps, _ = store.get_applications(ApplicationFilter(page_size=1_000_000))
    apps = sorted(apps, key=lambda a: a.id or 0)[:max_records]
    rows: list[dict[str, Any]] = []
    for app in apps:
        assert app.id is not None
        signals = application_signals(app)
        try:
            result = _ExcludingResolver(store, app.id).resolve(signals)
        except _RESOLVER_ERRORS as exc:
            rows.append(
                {
                    "application": anon("application", app.id),
                    "outcome": "error",
                    "error": type(exc).__name__,
                }
            )
            continue
        target = result.application_id
        positive, negative = _signal_split(result, target)
        row: dict[str, Any] = {
            "application": anon("application", app.id),
            "outcome": result.outcome.value,
            "reason": result.reason,
            "confidence": result.confidence,
            "confidence_band": confidence_band(result.confidence),
            "proposed_application": anon("application", target) if target else None,
            "positive_signals": positive,
            "negative_signals": negative,
            "_pair": sorted([app.id, target]) if target else None,
        }
        if target is None and result.outcome is Outcome.REVIEW_REQUIRED and result.candidates:
            # The closest other record: a possible duplicate or re-application to check.
            top = result.candidates[0].application_id
            row["top_candidate"] = anon("application", top)
            row["top_candidate_score"] = result.candidates[0].score
            row["_pair"] = sorted([app.id, top])
        if result.outcome is Outcome.LINKED:
            row["checks"] = verify_link(signals, store.get_application(target) if target else None)
        rows.append(row)
    return {
        "rows": rows,
        "outcomes": _counter(r["outcome"] for r in rows),
        "reasons": _counter(r.get("reason", "error") for r in rows),
        "confidence_bands": _counter(r.get("confidence_band", "none") for r in rows),
    }


# ------------------------------------------------------------------ #
# Duplicate reconciliation                                             #
# ------------------------------------------------------------------ #


def _groups(ids: set[int], pairs: list[tuple[int, int]]) -> list[list[int]]:
    parent = {i: i for i in ids}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    out: dict[int, list[int]] = {}
    for i in sorted(ids):
        out.setdefault(find(i), []).append(i)
    return sorted((g for g in out.values() if len(g) > 1), key=lambda g: g[0])


def classify_group(features: dict[str, Any], duplicate_score: int) -> str:
    """do_not_merge > high > medium > insufficient. No category is ever merged
    automatically; the category only orders the human review queue."""
    if (
        features["conflicting_external_ids"]
        or features["conflicting_canonical_urls"]
        or features["blocking"]
    ):
        return "do_not_merge_strong_conflict"
    if features["min_pair_score"] < duplicate_score:
        return "insufficient_evidence"
    if (
        features["min_pair_score"] >= app_config.RESOLVER_AUTO_LINK_SCORE
        and not features["different_roles"]
        and not features["large_date_span"]
    ):
        return "high_confidence_review"
    return "medium_confidence_review"


def reconcile_duplicates(
    store: DataStore, anon: Anonymizer, duplicate_score: int
) -> dict[str, Any]:
    """Candidate pairs, connected groups and a merge *preview* per group. Never merges."""
    detector = DuplicateDetector(store, threshold=duplicate_score)
    pairs = detector.find_candidate_pairs()
    dismissed = store.dismissed_pair_keys()
    kept: list[tuple[int, int, float, list[str]]] = []
    dismissed_count = 0
    for pair in pairs:
        a, b = pair["primary"].id, pair["duplicate"].id
        if store.pair_key(a, b) in dismissed:
            dismissed_count += 1
            continue
        kept.append((a, b, pair["score"], pair["reasons"]))
    ids = {i for a, b, *_ in kept for i in (a, b)}
    groups = _groups(ids, [(a, b) for a, b, *_ in kept])
    span_limit = app_config.RESOLVER_DATE_WINDOW_AFTER_DAYS

    out_groups: list[dict[str, Any]] = []
    for members in groups:
        apps = {a.id: a for a in store.get_applications_by_ids(members)}
        pair_scores = []
        signals: Counter[str] = Counter()
        conflicting_ids = False
        for left, right in itertools.combinations(members, 2):
            score, reasons = compare_applications(apps[left], apps[right])
            pair_scores.append(score)
            signals.update(reasons)
            if "Different job IDs on the same source" in reasons:
                conflicting_ids = True
        idents = [_app_identity(apps[i]) for i in members]
        urls = {i["url"] for i in idents if i["url"]}
        roles = {i["role"] for i in idents if i["role"]}
        dates = sorted(_aware(apps[i].applied_date) for i in members)
        state = store.load_merge_state(members)
        state["application_ids"] = members
        plan = plan_merge(state)
        features = {
            "size": len(members),
            "min_pair_score": min(pair_scores),
            "max_pair_score": max(pair_scores),
            "conflicting_external_ids": conflicting_ids,
            "conflicting_canonical_urls": len(urls) > 1,
            "different_roles": len(roles) > 1,
            "date_span_days": (dates[-1] - dates[0]).days,
            "large_date_span": (dates[-1] - dates[0]).days > span_limit,
            "field_conflicts": sorted(plan.conflicts),
            "warnings": len(plan.warnings),
            "blocking": bool(plan.blocking),
            "preview_safe": plan.safe,
        }
        category = classify_group(features, duplicate_score)
        out_groups.append(
            {
                "group": anon("group", members[0]),
                "applications": [anon("application", i) for i in members],
                "proposed_survivor": anon("application", plan.survivor_id),
                "category": category,
                "signals": dict(sorted(signals.items())),
                "counts": plan.counts,
                "requires_field_choices": bool(plan.conflicts),
                "auto_merge": False,
                **features,
                "_members": members,
            }
        )
    categories = Counter(g["category"] for g in out_groups)
    return {
        "candidate_pairs": len(kept),
        "dismissed_pairs": dismissed_count,
        "groups": out_groups,
        "summary": {
            "groups": len(out_groups),
            "group_sizes": _counter(str(g["size"]) for g in out_groups),
            "pair_score_distribution": _counter(_score_band(int(p[2])) for p in kept),
            "signal_distribution": dict(
                sorted(Counter(r for *_, reasons in kept for r in reasons).items())
            ),
            "categories": {
                name: categories.get(name, 0)
                for name in (
                    "high_confidence_review",
                    "medium_confidence_review",
                    "insufficient_evidence",
                    "do_not_merge_strong_conflict",
                )
            },
            "conflicting_external_ids": sum(g["conflicting_external_ids"] for g in out_groups),
            "conflicting_canonical_urls": sum(g["conflicting_canonical_urls"] for g in out_groups),
            "different_roles": sum(g["different_roles"] for g in out_groups),
            "large_date_span": sum(g["large_date_span"] for g in out_groups),
            "require_field_choices": sum(g["requires_field_choices"] for g in out_groups),
            "previewable_never_auto_merged": sum(
                1
                for g in out_groups
                if g["preview_safe"] and g["category"] != "do_not_merge_strong_conflict"
            ),
        },
    }


def _score_band(score: int) -> str:
    for limit, label in ((80, "70-79"), (100, "80-99"), (150, "100-149")):
        if score < limit:
            return label
    return "150+"


# ------------------------------------------------------------------ #
# Data quality                                                         #
# ------------------------------------------------------------------ #

# Role values that read like message text rather than a job title (greetings, pronouns,
# scheduling phrases) — a Phase 1 extraction artefact. Counted, never printed.
_SENTENCE_ROLE = re.compile(
    r"\b(hi|hello|dear|thanks?|thank you|your|you|we|our|availability|interest|regarding|please)\b",
    re.IGNORECASE,
)


def data_quality(store: DataStore) -> dict[str, int]:
    """Aggregate identity-quality signals on active applications. Noisy or empty roles
    make the resolver score role conflicts, which sends mail to review (never to a wrong
    automatic link) and hides real duplicates below the suggestion threshold."""
    apps, total = store.get_applications(ApplicationFilter(page_size=1_000_000))
    threads: Counter[str] = Counter()
    for app in apps:
        try:
            values = json.loads(app.thread_ids or "[]")
        except (json.JSONDecodeError, TypeError):
            values = []
        for value in {normalize_thread_id(v) for v in values if isinstance(v, str)} - {None}:
            threads[str(value)] += 1
    return {
        "active_applications": total,
        "empty_role": sum(1 for a in apps if not (a.role or "").strip()),
        "sentence_like_role": sum(
            1 for a in apps if a.role and (_SENTENCE_ROLE.search(a.role) or len(a.role) > 70)
        ),
        "without_thread": sum(1 for a in apps if not json.loads(a.thread_ids or "[]")),
        "with_job_url": sum(1 for a in apps if a.job_url),
        "with_external_job_id": sum(1 for a in apps if _app_identity(a)["job_id"]),
        "thread_ids_on_multiple_applications": sum(1 for n in threads.values() if n > 1),
    }


# ------------------------------------------------------------------ #
# Analytics                                                            #
# ------------------------------------------------------------------ #


def analytics_snapshot(store: DataStore, as_of: datetime) -> dict[str, Any]:
    """The figures the dashboard shows, computed by the same engine with a fixed clock."""
    engine = InsightsEngine(store, clock=lambda: as_of)
    apps, total = store.get_applications(ApplicationFilter(page_size=1_000_000))
    report = engine.generate_report()
    conversions = engine.conversion_data(6)
    pulse = engine.search_pulse(window_days=28)
    statuses = Counter(str(_value(a.current_status)) for a in apps)
    interviews = sum(1 for a in apps if _value(a.current_status) in _INTERVIEW_STATUSES)
    offers = sum(1 for a in apps if _value(a.current_status) in _OFFER_STATUSES)
    six_months = as_of - timedelta(days=180)
    return {
        "total_active_applications": total,
        "status_distribution": dict(sorted(statuses.items())),
        "source_distribution": _counter(a.source_portal or "Unknown" for a in apps),
        "interview_count": interviews,
        "offer_count": offers,
        "interview_conversion": round(interviews / total, 4) if total else 0.0,
        "offer_conversion": round(offers / total, 4) if total else 0.0,
        "six_month_opportunities": sum(1 for a in apps if _aware(a.applied_date) >= six_months),
        "stale_count": sum(1 for a in apps if is_application_stale(a, now=as_of)),
        "funnel": report.funnel,
        "channel_performance": [
            {
                "source": c.source,
                "total": c.total,
                "interviewed": c.interviewed,
                "offered": c.offered,
                "response_rate": round(c.response_rate(), 4),
            }
            for c in report.channels
        ],
        "application_rate_series": pulse.get("activity", []),
        "conversions_6m": conversions,
        "flow": engine.flow_data(),
    }


def _value(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def analytics_digest(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def analytics_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Scalar figures that differ (before → after) and which composite sections changed."""
    out: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        out[key] = {"before": a, "after": b} if not isinstance(a, list | dict) else "changed"
    return out


# ------------------------------------------------------------------ #
# Writes for disposable copies / explicit apply                        #
# ------------------------------------------------------------------ #


def apply_evidence_links(
    store: DataStore, plan_rows: list[dict[str, Any]], anon: Anonymizer
) -> int:
    """Record the verified automatic links (and nothing else): evidence ownership only —
    no application is created, merged or changed in status. Human-owned evidence is
    refused by DataStore.record_resolution. Returns the number of links recorded."""
    resolver = IdentityResolver(store)
    applied = 0
    for row in plan_rows:
        if row.get("category") != "auto_link":
            continue
        evidence = store.get_evidence(anon.mapping[row["evidence"]])
        if evidence is None or evidence.processing_status not in _RESOLVABLE:
            continue
        # Re-resolve at apply time: the plan is advisory, the current data decides.
        signals = signals_from_evidence(evidence, evidence_kind(evidence))
        result = resolver.resolve(signals)
        target = result.application_id
        if result.outcome is not Outcome.LINKED or target is None:
            continue
        if not all(verify_link(signals, store.get_application(target)).values()):
            continue
        assert evidence.id is not None
        if store.record_resolution(
            evidence.id,
            resolution=result.to_json(),
            status=EvidenceStatus.LINKED.value,
            application_id=target,
            link_method=result.link_method,
            link_confidence=result.confidence,
        ):
            applied += 1
    return applied


@dataclass
class SimulationResult:
    group: str
    category: str
    size: int
    merged: bool
    undone: bool
    active_before: int
    active_after_merge: int
    active_after_undo: int
    ownership_moved: dict[str, int] = field(default_factory=dict)
    snapshot_checksum_valid: bool = False
    restored_exactly: bool = False
    foreign_key_violations: int = 0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def simulate_merge_and_undo(
    store: DataStore,
    db_digests: Callable[[], dict[str, Any]],
    fk_violations: Callable[[], int],
    group: dict[str, Any],
    key: str,
) -> SimulationResult:
    """Merge one group on a DISPOSABLE copy, check ownership and integrity, undo, and
    prove the data is logically identical to before. Field conflicts take the proposed
    survivor's values (a simulation choice, not a recommendation)."""
    from backend.db import merge_snapshot

    members: list[int] = group["_members"]

    def total() -> int:
        return store.get_applications(ApplicationFilter(page_size=1))[1]

    before = db_digests()
    active_before = total()
    result = SimulationResult(
        group["group"],
        group["category"],
        len(members),
        False,
        False,
        active_before,
        active_before,
        active_before,
    )
    try:
        state = store.load_merge_state(members)
        state["application_ids"] = members
        plan = plan_merge(state)
        values = resolve_field_values(plan, {name: plan.survivor_id for name in plan.conflicts})
        operation, _ = store.execute_merge(
            application_ids=members,
            survivor_id=plan.survivor_id,
            field_values=values,
            expected_token=plan.token,
            idempotency_key=f"reconcile-sim-{key}",
            initiated_by="reconciliation-simulation",
            reason="disposable-copy simulation",
        )
        result.merged = True
        result.active_after_merge = total()
        after_state = store.load_merge_state([plan.survivor_id])
        moved = (operation.result or {}).get("moved") or {}
        result.ownership_moved = {
            kind: len(moved.get(kind) or []) for kind in merge_snapshot.CHILD_KINDS
        }
        result.ownership_moved["survivor_children_after"] = sum(
            len(after_state[kind]) for kind in merge_snapshot.CHILD_KINDS
        )
        merge_snapshot.validate_snapshot(operation.snapshot, operation.snapshot_checksum)
        result.snapshot_checksum_valid = True
        result.foreign_key_violations += fk_violations()
        assert operation.id is not None
        store.undo_merge(operation.id, undone_by="reconciliation-simulation")
        result.undone = True
        result.active_after_undo = total()
        result.foreign_key_violations += fk_violations()
        after = db_digests()
        result.restored_exactly = business_tables_equal(before, after)
    except (MergeError, MergePlanError, ValueError, LookupError) as exc:
        result.error = type(exc).__name__
    return result


# The audit trail of the simulation itself is expected to remain after undo.
_AUDIT_TABLES = {"mergeoperation"}


def business_tables_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = (set(before) | set(after)) - _AUDIT_TABLES
    return all(before.get(k) == after.get(k) for k in keys)
