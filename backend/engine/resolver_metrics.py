"""Privacy-safe counters for evidence processing.

Counters only — no message IDs, addresses, subjects or snippets are kept. They reset when
the process restarts; persistent totals come from the evidence table
(DataStore.evidence_decision_counts) and are reported alongside them by the API.
"""

from __future__ import annotations

import threading
from collections import Counter

COUNTERS = (
    "evidence_processed",
    "auto_linked",
    "new_application_created",
    "sent_to_review",
    "ignored",
    "duplicate_skipped",
    "human_decision_retained",
    "concurrent_claim_skipped",
    "resolver_errors",
)


class ResolverMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in COUNTERS:
            raise ValueError(f"Unknown resolver metric: {name}")
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {name: self._counts.get(name, 0) for name in COUNTERS}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


metrics = ResolverMetrics()
