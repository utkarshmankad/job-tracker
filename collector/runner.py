"""Run one adapter against one source, conservatively.

Stages: open the history page → detect state (stop on signed-out, challenge, consent,
rate limit or anything unknown) → read pages one at a time with human-paced pauses →
validate every row locally against the contract → send bounded, idempotent batches →
report how the run ended. A run never "succeeds" with data it could not verify was
complete: selector drift, an unreadable page, the page limit or invalid rows all end the
run as ``partial`` or ``failed`` with a fixed code, and items already read are still sent.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from backend.collection.contract import ObservedApplication
from collector import COLLECTOR_VERSION
from collector.adapters.base import Adapter
from collector.client import ClientError, TrackerClient
from collector.core import STOP_STATES, AdapterError, Diagnostics, ExtractedItem, PageState
from collector.diagnostics import DiagnosticStore
from collector.driver import DriverError, PageDriver


@dataclass
class RunOutcome:
    source_key: str
    account_label: str
    run_key: str
    dry_run: bool
    status: str
    error_code: str | None
    observations: list[ObservedApplication] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    server_counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Content-free summary for the local run log and CLI output."""
        return {
            "source_key": self.source_key,
            "account_label": self.account_label,
            "run_key": self.run_key,
            "dry_run": self.dry_run,
            "status": self.status,
            "error_code": self.error_code,
            "observations": len(self.observations),
            "diagnostics": self.diagnostics.to_payload(),
            "server_counts": self.server_counts,
        }


class SourceRunner:
    def __init__(
        self,
        adapter: Adapter,
        driver: PageDriver,
        *,
        account_label: str = "default",
        client: TrackerClient | None = None,
        diagnostics: DiagnosticStore | None = None,
        max_pages: int = 10,
        min_delay: float = 4.0,
        max_delay: float = 9.0,
        batch_size: int = 25,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ) -> None:
        self._adapter = adapter
        self._driver = driver
        self._account = account_label
        self._client = client
        self._store = diagnostics
        self._max_pages = max_pages
        self._delays = (min_delay, max_delay)
        self._batch_size = batch_size
        self._clock = clock
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------ #

    def run(self, *, dry_run: bool) -> RunOutcome:
        if not dry_run and self._client is None:
            raise ValueError("A live run needs a tracker client")
        outcome = RunOutcome(
            self._adapter.SOURCE_KEY, self._account, uuid.uuid4().hex, dry_run, "failed", None
        )
        if not dry_run:
            assert self._client is not None
            self._client.start_run(
                {
                    "run_key": outcome.run_key,
                    "source_key": self._adapter.SOURCE_KEY,
                    "account_label": self._account,
                    "collector_version": COLLECTOR_VERSION,
                    "adapter_version": self._adapter.adapter_version,
                }
            )
        status, error_code = self._collect(outcome)
        if not dry_run and outcome.observations:
            try:
                self._submit(outcome)
            except ClientError:
                status, error_code = "failed", "submission_failed"
        if status == "succeeded" and outcome.diagnostics.items_invalid:
            status, error_code = "partial", "invalid_items"
        outcome.status, outcome.error_code = status, error_code
        if not dry_run:
            assert self._client is not None
            self._client.finish_run(
                outcome.run_key,
                {
                    "status": status,
                    "items_seen": outcome.diagnostics.items_extracted,
                    "error_code": error_code,
                    "diagnostics": outcome.diagnostics.to_payload(),
                },
            )
        if self._store is not None:
            self._store.record_run({"at": self._clock().isoformat(), **outcome.summary()})
        return outcome

    # ------------------------------------------------------------------ #

    def _collect(self, outcome: RunOutcome) -> tuple[str, str | None]:
        diag = outcome.diagnostics
        seen: set[str] = set()
        try:
            self._driver.goto(self._adapter.HISTORY_URL)
        except DriverError:
            return "failed", "navigation_failed"
        for page_number in range(self._max_pages):
            url = self._driver.current_url()
            html = self._driver.html()
            try:
                result = self._adapter.parse_page(html, url, self._clock().date())
            except AdapterError as exc:
                diag.stopped_detail = exc.detail or None
                self._snapshot(exc.code, html)
                return ("partial" if outcome.observations else "failed"), exc.code
            diag.pages += 1
            if result.state in STOP_STATES:
                diag.stopped_state = result.state.value
                if result.state is PageState.UNKNOWN:
                    self._snapshot("unexpected_page", html)
                return STOP_STATES[result.state]
            if result.state is PageState.EMPTY:
                # The site's explicit "no applications" state, not a guess.
                return "succeeded", None
            if result.selector_tier != "verified":
                diag.fallback_pages += 1
            new_items = 0
            for item in result.items:
                diag.items_extracted += 1
                observed = self._validate(item, diag)
                if observed is None:
                    continue
                key = observed.item_identity()[0]
                if key in seen:
                    diag.duplicates_in_run += 1
                    continue
                seen.add(key)
                new_items += 1
                outcome.observations.append(observed)
            if not result.has_more:
                return "succeeded", None
            if page_number > 0 and new_items == 0:
                # Lazy loading / "show more" produced nothing new: stop rather than loop.
                return "succeeded", None
            self._driver.pause(self._rng.uniform(*self._delays))
            try:
                advanced = self._advance()
            except DriverError:
                return "partial", "navigation_failed"
            if not advanced:
                self._snapshot("selector_drift", html)
                diag.stopped_detail = "pagination"
                return "partial", "selector_drift"
        return "partial", "page_limit"

    def _advance(self) -> bool:
        adapter = self._adapter
        if adapter.PAGINATION == "next" and adapter.NEXT_SELECTOR:
            return self._driver.click(adapter.NEXT_SELECTOR)
        if adapter.PAGINATION == "load_more" and adapter.LOAD_MORE_SELECTOR:
            return self._driver.click(adapter.LOAD_MORE_SELECTOR)
        if adapter.PAGINATION == "scroll":
            self._driver.scroll_to_bottom()
            return True
        return False

    def _validate(self, item: ExtractedItem, diag: Diagnostics) -> ObservedApplication | None:
        try:
            observed = ObservedApplication.model_validate(
                {
                    "source_key": self._adapter.SOURCE_KEY,
                    "source_item_id": item.source_item_id,
                    "company": item.company or "",
                    "role": item.role,
                    "applied_on": item.applied_on,
                    "status": item.status,
                    "raw_status": item.raw_status,
                    "job_url": item.job_url,
                    "proves_submission": item.proves_submission,
                    "extraction": item.extraction,
                    "observed_at": self._clock(),
                    "adapter_version": self._adapter.adapter_version,
                }
            )
        except ValidationError:
            diag.items_invalid += 1
            return None
        diag.items_valid += 1
        return observed

    def _submit(self, outcome: RunOutcome) -> None:
        assert self._client is not None
        observations = outcome.observations
        for start in range(0, len(observations), self._batch_size):
            chunk = observations[start : start + self._batch_size]
            # Deterministic per run and position: a retried batch reuses its key.
            batch_key = f"{outcome.run_key}-{start // self._batch_size:04d}"
            result = self._client.submit_batch(
                outcome.run_key,
                {
                    "batch_key": batch_key,
                    "sent_at": self._clock().isoformat(),
                    "observations": [o.model_dump(mode="json") for o in chunk],
                },
            )
            outcome.diagnostics.batches_sent += 1
            for name, count in (result.get("counts") or {}).items():
                outcome.server_counts[name] = outcome.server_counts.get(name, 0) + int(count)

    def _snapshot(self, reason: str, html: str) -> None:
        if self._store is not None:
            self._store.save_snapshot(self._adapter.SOURCE_KEY, reason, html)
