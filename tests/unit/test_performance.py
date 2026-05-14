"""Performance tests for DataStore query operations."""

from __future__ import annotations

import time
from datetime import datetime

from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus


def test_list_500_apps_under_300ms(tmp_path) -> None:
    db = DataStore(db_path=tmp_path / "perf.db")
    for i in range(500):
        db.upsert_application(Application(
            company=f"Company{i}",
            role="Engineer",
            source_portal=["Naukri", "LinkedIn", "Direct/Unknown"][i % 3],
            applied_date=datetime.utcnow(),
            current_status=ApplicationStatus.APPLIED,
        ))

    start = time.perf_counter()
    results, total = db.get_applications(ApplicationFilter(page=1, page_size=50))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.300, f"Query took {elapsed:.3f}s (limit: 0.300s)"
    assert total == 500


def test_filter_query_under_100ms(tmp_path) -> None:
    db = DataStore(db_path=tmp_path / "perf2.db")
    for i in range(500):
        db.upsert_application(Application(
            company=f"Company{i}",
            role="Engineer",
            source_portal="LinkedIn" if i % 2 == 0 else "Naukri",
            applied_date=datetime.utcnow(),
            current_status=ApplicationStatus.APPLIED,
        ))

    start = time.perf_counter()
    results, total = db.get_applications(ApplicationFilter(source_portal="LinkedIn"))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.100, f"Filter took {elapsed:.3f}s (limit: 0.100s)"
