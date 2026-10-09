"""Which job-site sources may be collected, and why the others may not.

This is the server's authoritative readiness record (docs/collector-operations.md, "Source
readiness"). The API rejects a collector scope for an unsupported source, and the Sources
page renders its source choices from this catalog. The local collector keeps its own
record on each adapter (``SUPPORTED``, ``UNSUPPORTED_REASON``, ``LIVE_VERIFIED`` in
collector/adapters/sites.py) because it is not deployed with the backend;
tests/unit/test_source_readiness_consistency.py fails if the two records, or the
readiness tables in the docs, disagree.

``employer-<slug>`` sources are not in the catalog: they exist only through an explicit
local definition, and their observations stay ``unverified`` (never auto-created) until
that definition records a live verification.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.collection.contract import is_valid_source_key


@dataclass(frozen=True)
class SourceReadiness:
    key: str
    label: str
    supported: bool
    live_verified: str | None  # ISO date a person confirmed the selectors in a real session
    reason: str | None  # why an unsupported source cannot be collected; None if supported


SOURCE_READINESS: dict[str, SourceReadiness] = {
    entry.key: entry
    for entry in (
        SourceReadiness("indeed", "Indeed", True, "2026-10-09", None),
        SourceReadiness(
            "linkedin",
            "LinkedIn",
            False,
            None,
            "The new Job Tracker layout lacks safe stable row boundaries.",
        ),
        SourceReadiness(
            "naukri",
            "Naukri",
            False,
            None,
            "Unsupported until item identity, applied dates and complete inner-scroll "
            "collection can be established.",
        ),
        SourceReadiness(
            "instahyre",
            "Instahyre",
            False,
            None,
            "Instahyre has no application-history page.",
        ),
        SourceReadiness(
            "careernet",
            "CareerNet",
            False,
            None,
            "CareerNet's candidate history page has not been located.",
        ),
    )
}


def is_supported_scope(source_key: str) -> bool:
    """Whether a new or rotated collector may be scoped to this source."""
    entry = SOURCE_READINESS.get(source_key)
    if entry is not None:
        return entry.supported
    return is_valid_source_key(source_key)  # employer-<slug>: explicit local definition


def unsupported_scopes(scopes: list[str]) -> list[str]:
    return [s for s in scopes if not is_supported_scope(s)]
