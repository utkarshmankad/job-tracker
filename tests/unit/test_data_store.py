"""Tests for backend/db/data_store.py."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from backend.db.data_store import ApplicationFilter, DataStore, is_application_stale
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


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_find_application_by_thread_id_found(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="ThreadCo")
    app.thread_ids = json.dumps(["thread-abc", "thread-xyz"])
    saved = ds.upsert_application(app)

    found = ds.find_application_by_thread_id("thread-abc")
    assert found is not None
    assert found.id == saved.id


def test_find_application_by_thread_id_not_found(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    ds.upsert_application(_make_app(company="OtherCo"))

    assert ds.find_application_by_thread_id("nonexistent-thread") is None


def test_find_application_by_thread_id_second_in_list(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="MultiThreadCo")
    app.thread_ids = json.dumps(["first-thread", "second-thread", "third-thread"])
    saved = ds.upsert_application(app)

    assert ds.find_application_by_thread_id("second-thread") is not None
    assert ds.find_application_by_thread_id("third-thread") is not None


def test_find_application_by_thread_id_does_not_match_partial(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="PartialCo")
    app.thread_ids = json.dumps(["thread-123"])
    ds.upsert_application(app)

    # "thread-12" is a substring but not a full thread_id — should not match
    assert ds.find_application_by_thread_id("thread-12") is None


def test_find_application_by_thread_id_after_append(tmp_path: Path) -> None:
    """A thread_id added to an existing application via a second upsert must be findable."""
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="AppendCo")
    app.thread_ids = json.dumps(["thread-one"])
    saved = ds.upsert_application(app)

    saved.thread_ids = json.dumps(["thread-one", "thread-two"])
    ds.upsert_application(saved)

    found = ds.find_application_by_thread_id("thread-two")
    assert found is not None
    assert found.id == saved.id


def test_thread_id_index_backfills_from_existing_json_blobs(tmp_path: Path) -> None:
    """Reopening a DB whose ApplicationThreadId rows are missing/incomplete must
    backfill them from Application.thread_ids without the caller doing anything."""
    from sqlmodel import Session, select
    from backend.db.models import ApplicationThreadId

    db_path = tmp_path / "test.db"
    ds = DataStore(db_path)
    app = _make_app(company="BackfillCo")
    app.thread_ids = json.dumps(["legacy-thread"])
    saved = ds.upsert_application(app)

    with Session(ds._engine) as session:
        for row in session.exec(select(ApplicationThreadId)).all():
            session.delete(row)
        session.commit()

    ds2 = DataStore(db_path)  # constructor runs the backfill
    found = ds2.find_application_by_thread_id("legacy-thread")
    assert found is not None
    assert found.id == saved.id


def test_get_status_history_for_apps_filters_correctly(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app1 = ds.upsert_application(_make_app(company="Alpha"))
    app2 = ds.upsert_application(_make_app(company="Beta"))

    assert app1.id is not None
    assert app2.id is not None

    ds.append_status_history(app1.id, None, ApplicationStatus.APPLIED.value, "email")
    ds.append_status_history(app1.id, ApplicationStatus.APPLIED.value, ApplicationStatus.REJECTED.value, "email")
    ds.append_status_history(app2.id, None, ApplicationStatus.APPLIED.value, "email")

    result = ds.get_status_history_for_apps({app1.id})
    assert len(result) == 2
    assert all(h.application_id == app1.id for h in result)


def test_get_status_history_for_apps_empty_set(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = ds.upsert_application(_make_app(company="SomeCo"))
    assert app.id is not None
    ds.append_status_history(app.id, None, ApplicationStatus.APPLIED.value, "email")

    assert ds.get_status_history_for_apps(set()) == []


def test_is_application_stale_old_applied(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = ds.upsert_application(_make_app(company="StaleCo"))

    with Session(ds._engine) as session:
        db_app = session.get(Application, app.id)
        db_app.updated_at = datetime.utcnow() - timedelta(days=20)
        session.add(db_app)
        session.commit()

    refreshed = ds.get_application(app.id)
    assert refreshed is not None
    assert is_application_stale(refreshed, threshold_days=14) is True


def test_is_application_stale_recent_applied(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = ds.upsert_application(_make_app(company="FreshCo"))
    assert is_application_stale(app, threshold_days=14) is False


def test_is_application_stale_non_applied_status(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    app = _make_app(company="RejectedCo", current_status=ApplicationStatus.REJECTED)
    app = ds.upsert_application(app)

    with Session(ds._engine) as session:
        db_app = session.get(Application, app.id)
        db_app.updated_at = datetime.utcnow() - timedelta(days=30)
        session.add(db_app)
        session.commit()

    refreshed = ds.get_application(app.id)
    assert refreshed is not None
    assert is_application_stale(refreshed, threshold_days=14) is False


def test_poller_state_missing_raises_runtime_error(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    # Delete the singleton PollerState row directly
    from backend.db.models import PollerState
    with Session(ds._engine) as session:
        state = session.get(PollerState, 1)
        if state:
            session.delete(state)
            session.commit()

    with pytest.raises(RuntimeError, match="PollerState row missing"):
        ds.get_poller_state()


def test_update_poller_state_missing_raises_runtime_error(tmp_path: Path) -> None:
    ds = DataStore(tmp_path / "test.db")
    from backend.db.models import PollerState
    with Session(ds._engine) as session:
        state = session.get(PollerState, 1)
        if state:
            session.delete(state)
            session.commit()

    with pytest.raises(RuntimeError, match="PollerState row missing"):
        ds.update_poller_state(status="ERROR")
