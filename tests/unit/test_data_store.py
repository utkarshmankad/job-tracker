"""Tests for backend/db/data_store.py."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from backend.db.data_store import ApplicationFilter, DataStore
from backend.db.models import Application, ApplicationStatus, StatusHistory


def _make_app(**kwargs) -> Application:
    defaults = dict(source_portal="LinkedIn", applied_date=datetime.utcnow())
    defaults.update(kwargs)
    return Application(**defaults)


# --------------------------------------------------------------------------- #


def test_create_and_retrieve_application(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="Acme", role="Engineer")
    saved = ds.upsert_application(app)

    assert saved.id is not None
    fetched = ds.get_application(saved.id)
    assert fetched is not None
    assert fetched.company == "Acme"
    assert fetched.role == "Engineer"


def test_filter_by_status(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    ds.upsert_application(_make_app(company="A", current_status=ApplicationStatus.APPLIED))
    ds.upsert_application(_make_app(company="B", current_status=ApplicationStatus.REJECTED))

    items, total = ds.get_applications(ApplicationFilter(status=ApplicationStatus.REJECTED))
    assert total == 1
    assert items[0].company == "B"


def test_filter_by_search_term(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    ds.upsert_application(_make_app(company="Google LLC", role="SWE"))
    ds.upsert_application(_make_app(company="Meta", role="Product Manager"))

    items, total = ds.get_applications(ApplicationFilter(search="goog"))
    assert total == 1
    assert items[0].company == "Google LLC"

    # Role match
    items2, total2 = ds.get_applications(ApplicationFilter(search="product"))
    assert total2 == 1
    assert items2[0].company == "Meta"


def test_pagination(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    for i in range(5):
        ds.upsert_application(_make_app(company=f"Co{i}"))

    page1, total = ds.get_applications(ApplicationFilter(page=1, page_size=3))
    assert total == 5
    assert len(page1) == 3

    page2, _ = ds.get_applications(ApplicationFilter(page=2, page_size=3))
    assert len(page2) == 2

    ids_p1 = {a.id for a in page1}
    ids_p2 = {a.id for a in page2}
    assert ids_p1.isdisjoint(ids_p2)


def test_append_status_history(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    saved = ds.upsert_application(_make_app(company="HistoryCo"))

    ds.append_status_history(saved.id, None, ApplicationStatus.APPLIED.value, "manual")
    ds.append_status_history(
        saved.id,
        ApplicationStatus.APPLIED.value,
        ApplicationStatus.REJECTED.value,
        "email",
        message_id="msg-001",
    )

    history = ds.get_status_history(saved.id)
    assert len(history) == 2
    assert history[0].from_status is None
    assert history[0].to_status == ApplicationStatus.APPLIED.value
    assert history[1].to_status == ApplicationStatus.REJECTED.value
    assert history[1].message_id == "msg-001"
    assert history[1].trigger == "email"


def test_suppress_rule_crud(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")

    rule = ds.add_suppress_rule("noreply@spam.com", "Job Alert")
    assert rule.id is not None
    assert rule.sender_pattern == "noreply@spam.com"

    rules = ds.get_suppress_rules()
    assert len(rules) == 1

    assert ds.delete_suppress_rule(rule.id) is True
    assert ds.get_suppress_rules() == []
    assert ds.delete_suppress_rule(999) is False


def test_mark_and_check_processed(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")

    assert ds.is_processed("msg-xyz") is False
    ds.mark_processed("msg-xyz", "applied")
    assert ds.is_processed("msg-xyz") is True


def test_stale_applications(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    fresh = ds.upsert_application(_make_app(company="FreshCo"))
    stale_candidate = ds.upsert_application(_make_app(company="StaleCo"))

    # Backdate updated_at via a direct session write (bypasses upsert_application's now() logic)
    with Session(ds._engine) as session:
        db_app = session.get(Application, stale_candidate.id)
        db_app.updated_at = datetime.utcnow() - timedelta(days=20)
        session.add(db_app)
        session.commit()

    stale = ds.get_stale_applications(threshold_days=14)
    stale_ids = {a.id for a in stale}
    assert stale_candidate.id in stale_ids
    assert fresh.id not in stale_ids


def test_db_missing_file_handled(tmp_path: Path) -> None:
    new_db = tmp_path / "brand_new.db"
    assert not new_db.exists()

    ds = DataStore(new_db)

    assert new_db.exists()
    _, total = ds.get_applications(ApplicationFilter())
    assert total == 0


def test_duplicate_upsert_updates_timestamp(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    saved = ds.upsert_application(_make_app(company="Dup Corp"))
    first_updated_at = saved.updated_at

    time.sleep(0.05)

    saved.company = "Dup Corp (updated)"
    updated = ds.upsert_application(saved)

    _, total = ds.get_applications(ApplicationFilter())
    assert total == 1  # no duplicate row
    assert updated.company == "Dup Corp (updated)"
    assert updated.updated_at >= first_updated_at
