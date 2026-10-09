"""Merge preview: survivor choice, field-by-field comparison, conflicts and warnings.

Pure functions over the state DataStore.load_merge_state returns — nothing here writes. The
same state token is checked again inside the merge transaction, so a preview can only be
executed while the records still look exactly as previewed.

Rules (docs/phase-2-identity-resolution.md §12):
- Default survivor: a record with person-confirmed evidence, then the richest record (most
  filled fields), then the one with most evidence, then the oldest, then the lowest ID.
- applied_date combines to the earliest; thread IDs are unioned.
- Any other field with two or more different non-empty values is a conflict the user must
  resolve explicitly; a non-empty value is never dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend import config as app_config
from backend.db import merge_snapshot
from backend.engine.normalization import normalize_company, normalize_role

_EMPTY_VALUES: dict[str, set[Any]] = {
    "source_portal": {"Direct/Unknown", "Unknown"},
    "application_method": {"Unknown"},
}
_STATUS_RANK = {
    "Applied": 0,
    "Resume Shortlisted": 1,
    "Interview Scheduled": 2,
    "Interview In Progress": 3,
    "Offer Negotiation": 4,
    "Rejected": 4,
    "Withdrawn": 4,
    "Offer": 5,
    "Joined": 6,
}
_RICHNESS_FIELDS = (
    "company",
    "role",
    "job_url",
    "external_job_id",
    "withdraw_reason",
    "source_portal",
    "application_method",
)


class MergePlanError(ValueError):
    """Field choices do not resolve the plan's conflicts."""


@dataclass(frozen=True)
class FieldComparison:
    name: str
    values: dict[int, Any]
    proposed: Any
    proposed_from: int | None
    rule: str  # survivor_preferred | only_value | earliest | most_advanced | empty
    conflict: bool


@dataclass(frozen=True)
class MergePlan:
    application_ids: list[int]
    survivor_id: int
    default_survivor_id: int
    token: str
    applications: list[dict[str, Any]]
    fields: list[FieldComparison]
    conflicts: list[str]
    warnings: list[str]
    blocking: list[str]
    relink: dict[str, list[int]]
    superseded: dict[str, list[int]]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def safe(self) -> bool:
        return not self.blocking


def _is_empty(name: str, value: Any) -> bool:
    return value is None or value == "" or value in _EMPTY_VALUES.get(name, set())


def _summaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for app_id_str, row in sorted(state["applications"].items(), key=lambda kv: int(kv[0])):
        app_id = int(app_id_str)

        def count(kind: str, **extra: Any) -> int:
            return sum(
                1
                for r in state[kind]
                if r["application_id"] == app_id and all(r.get(k) == v for k, v in extra.items())
            )

        out.append(
            {
                "id": app_id,
                "company": row.get("company"),
                "role": row.get("role"),
                "source_portal": row.get("source_portal"),
                "application_method": row.get("application_method"),
                "current_status": row.get("current_status"),
                "applied_date": row.get("applied_date"),
                "record_state": row.get("record_state"),
                "external_job_id": row.get("external_job_id"),
                "evidence_count": count("evidence"),
                "status_history_count": count("status_history", superseded_by_merge_id=None),
                "event_count": count("events", superseded_by_merge_id=None),
                "human_confirmed": any(
                    r["application_id"] == app_id and r.get("decided_by") == "human"
                    for r in state["evidence"]
                ),
                "filled_fields": sum(
                    0 if _is_empty(name, row.get(name)) else 1 for name in _RICHNESS_FIELDS
                ),
            }
        )
    return out


def default_survivor(applications: list[dict[str, Any]]) -> int:
    best = min(
        applications,
        key=lambda a: (
            0 if a["human_confirmed"] else 1,
            -a["filled_fields"],
            -a["evidence_count"],
            str(a.get("applied_date") or ""),
            a["id"],
        ),
    )
    return int(best["id"])


def plan_merge(state: dict[str, Any], survivor_id: int | None = None) -> MergePlan:
    ids = [int(k) for k in sorted(state["applications"], key=int)]
    requested = list(state.get("application_ids") or ids)
    applications = _summaries(state)
    blocking: list[str] = []
    missing = sorted(set(requested) - set(ids))
    if missing:
        blocking.append(f"Applications not found: {missing}")
    if len(ids) < 2:
        blocking.append("Select at least two applications to merge.")
    if len(requested) > app_config.MERGE_MAX_APPLICATIONS:
        blocking.append(
            f"At most {app_config.MERGE_MAX_APPLICATIONS} applications can be merged at once."
        )
    for app in applications:
        if app["record_state"] != "active":
            blocking.append(f"Application {app['id']} is already merged into another record.")

    default_id = default_survivor(applications) if applications else 0
    chosen = survivor_id if survivor_id is not None else default_id
    if chosen not in ids:
        blocking.append(f"Survivor {chosen} is not one of the selected applications.")
        chosen = default_id
    order = [chosen, *[i for i in ids if i != chosen]]
    rows = {i: state["applications"][str(i)] for i in ids}

    fields: list[FieldComparison] = []
    for name in merge_snapshot.MERGEABLE_FIELDS:
        values = {i: rows[i].get(name) for i in order}
        non_empty = {i: v for i, v in values.items() if not _is_empty(name, v)}
        distinct = list(dict.fromkeys(non_empty.values()))
        if name == "applied_date":
            earliest = (
                min(non_empty.items(), key=lambda kv: (str(kv[1]), kv[0])) if non_empty else None
            )
            fields.append(
                FieldComparison(
                    name,
                    values,
                    earliest[1] if earliest else None,
                    earliest[0] if earliest else None,
                    "earliest",
                    False,
                )
            )
            continue
        if not distinct:
            fields.append(FieldComparison(name, values, values.get(chosen), None, "empty", False))
            continue
        if name == "current_status" and len(distinct) > 1:
            top = max(
                non_empty.items(), key=lambda kv: (_STATUS_RANK.get(kv[1], -1), -order.index(kv[0]))
            )
            fields.append(FieldComparison(name, values, top[1], top[0], "most_advanced", True))
            continue
        source = chosen if chosen in non_empty else next(i for i in order if i in non_empty)
        fields.append(
            FieldComparison(
                name,
                values,
                non_empty[source],
                source,
                "survivor_preferred" if len(distinct) > 1 else "only_value",
                len(distinct) > 1,
            )
        )

    warnings: list[str] = []
    by_name = {f.name: f for f in fields}
    job_ids = {v for v in by_name["external_job_id"].values.values() if v}
    if len(job_ids) > 1:
        warnings.append("The records have different job IDs — they may be different jobs.")
    companies = {normalize_company(v) for v in by_name["company"].values.values() if v}
    if len(companies) > 1:
        warnings.append(
            "Company names differ after normalization; check these are the same employer."
        )
    roles = {normalize_role(v) for v in by_name["role"].values.values() if v}
    if len(roles) > 1:
        warnings.append("Role titles differ; the survivor keeps only the role you choose.")
    dates = sorted(str(v) for v in by_name["applied_date"].values.values() if v)
    if len(dates) > 1:
        first = merge_snapshot.parse_datetime(dates[0])
        last = merge_snapshot.parse_datetime(dates[-1])
        if first and last and (last - first).days > app_config.RESOLVER_DATE_WINDOW_AFTER_DAYS:
            warnings.append("Applied dates are months apart — this may be a re-application.")

    sources = set(ids) - {chosen}
    relink = {
        kind: [r["id"] for r in state[kind] if r["application_id"] in sources]
        for kind in merge_snapshot.CHILD_KINDS
    }
    live_history = [r for r in state["status_history"] if r.get("superseded_by_merge_id") is None]
    superseded_history = merge_snapshot.superseded_history_ids(live_history)
    live_events = [r for r in state["events"] if r.get("superseded_by_merge_id") is None]
    superseded_events = merge_snapshot.superseded_event_ids(live_events, set(superseded_history))

    return MergePlan(
        application_ids=ids,
        survivor_id=chosen,
        default_survivor_id=default_id,
        token=state["token"],
        applications=applications,
        fields=fields,
        conflicts=[f.name for f in fields if f.conflict],
        warnings=warnings,
        blocking=blocking,
        relink=relink,
        superseded={"status_history": superseded_history, "events": superseded_events},
        counts={
            "evidence": len(state["evidence"]),
            "status_history": len(live_history),
            "events": len(live_events),
            "status_history_superseded": len(superseded_history),
            "events_superseded": len(superseded_events),
        },
    )


def resolve_field_values(plan: MergePlan, choices: dict[str, int]) -> dict[str, Any]:
    """Final survivor values: every conflict must be resolved by naming the application
    whose value to keep; other fields take the proposed value unless a choice is given."""
    unknown = set(choices) - set(merge_snapshot.MERGEABLE_FIELDS)
    if unknown:
        raise MergePlanError(f"Unknown fields: {sorted(unknown)}")
    missing = [name for name in plan.conflicts if name not in choices]
    if missing:
        raise MergePlanError(f"Choose a value for: {', '.join(missing)}")
    values: dict[str, Any] = {}
    for comparison in plan.fields:
        if comparison.name in choices:
            source = choices[comparison.name]
            if source not in comparison.values:
                raise MergePlanError(
                    f"{comparison.name}: application {source} is not part of this merge"
                )
            values[comparison.name] = comparison.values[source]
        else:
            values[comparison.name] = comparison.proposed
    return values
