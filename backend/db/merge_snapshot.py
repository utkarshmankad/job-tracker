"""Pure helpers for merge snapshots: serialization, checksums, state tokens, validation and
the duplicate rules applied to merged status history and milestone events.

No database access. Used by DataStore (inside the merge/undo transaction) and by the merge
planner (preview), so both always agree on the token and on what counts as a duplicate.
Snapshots hold application fields and relationship IDs only — never message content.
"""

from __future__ import annotations

import enum
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

SNAPSHOT_VERSION = 1

# Survivor fields a merge may set from any involved application.
MERGEABLE_FIELDS = (
    "company",
    "role",
    "source_portal",
    "application_method",
    "job_url",
    "external_job_id",
    "applied_date",
    "current_status",
    "withdraw_reason",
    "is_false_positive",
)
CHILD_KINDS = ("evidence", "status_history", "events", "thread_links", "prospects")

# Milestones that happen once per application: duplicates collapse regardless of date.
_ONCE_PER_APPLICATION = {"Application Submitted", "Rejected", "Offer Received"}


class SnapshotError(ValueError):
    """A stored snapshot is missing, malformed or does not match its checksum."""


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=serialize_value)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def state_token(state: dict[str, Any]) -> str:
    """Fingerprint of everything a merge reads. Any change to an involved application or to
    its linked rows changes the token, so a stale preview cannot be executed."""
    return checksum({k: v for k, v in state.items() if k != "token"})[:40]


def validate_snapshot(snapshot: Any, expected_checksum: str) -> dict[str, Any]:
    """Structural and checksum validation before any restore."""
    if not isinstance(snapshot, dict):
        raise SnapshotError("Snapshot is not an object")
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise SnapshotError(f"Unsupported snapshot version {snapshot.get('version')!r}")
    apps = snapshot.get("applications")
    if not isinstance(apps, dict) or len(apps) < 2:
        raise SnapshotError("Snapshot must describe at least two applications")
    for app_id, row in apps.items():
        if not str(app_id).isdigit() or not isinstance(row, dict) or row.get("id") != int(app_id):
            raise SnapshotError(f"Malformed application entry {app_id!r}")
    for kind in CHILD_KINDS:
        rows = snapshot.get(kind)
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), int) and "application_id" in r
            for r in rows
        ):
            raise SnapshotError(f"Malformed {kind} entries")
    if checksum(snapshot) != expected_checksum:
        raise SnapshotError("Snapshot checksum does not match; refusing to restore")
    return snapshot


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _day(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.astimezone(UTC).date().isoformat() if parsed else ""


def superseded_history_ids(rows: list[dict[str, Any]]) -> list[int]:
    """Status-history rows that repeat an earlier transition (same from → to) on the
    combined record. The earliest occurrence is kept."""
    kept: set[tuple[Any, Any]] = set()
    superseded: list[int] = []
    for row in sorted(rows, key=lambda r: (str(r.get("changed_at") or ""), r["id"])):
        key = (row.get("from_status"), row.get("to_status"))
        if key in kept:
            superseded.append(row["id"])
        else:
            kept.add(key)
    return superseded


def superseded_event_ids(rows: list[dict[str, Any]], superseded_history: set[int]) -> list[int]:
    """Milestone events duplicated on the combined record: once-per-application milestones
    collapse to the earliest; interview milestones collapse when type, round and day match.
    Events tied to a superseded status-history row are superseded with it."""
    kept: set[tuple[Any, ...]] = set()
    superseded: list[int] = []
    for row in sorted(rows, key=lambda r: (str(r.get("occurred_at") or ""), r["id"])):
        if row.get("status_history_id") in superseded_history:
            superseded.append(row["id"])
            continue
        event_type = row.get("event_type")
        if event_type in _ONCE_PER_APPLICATION:
            key: tuple[Any, ...] = (event_type,)
        else:
            key = (event_type, row.get("interview_round"), _day(row.get("occurred_at")))
        if key in kept:
            superseded.append(row["id"])
        else:
            kept.add(key)
    return superseded
