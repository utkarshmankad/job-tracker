"""Tests for backend/engine/insights_engine.py."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.engine.insights_engine import ChannelStat, InsightsEngine

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_db(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "test.db")


def _seed_app(
    db: DataStore,
    *,
    source_portal: str = "LinkedIn",
    status: ApplicationStatus = ApplicationStatus.APPLIED,
) -> Application:
    app = Application(
        company="TestCo",
        role="Engineer",
        source_portal=source_portal,
        applied_date=utc_now(),
        current_status=status,
    )
    return db.upsert_application(app)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_insufficient_data_flag(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(9):
        _seed_app(db)

    report = InsightsEngine(db).generate_report()

    assert report.insufficient_data is True
    assert report.total_applications == 9
    assert report.channels == []
    assert report.insights == []


def test_funnel_counts_correct(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    distribution = [
        ApplicationStatus.APPLIED,
        ApplicationStatus.APPLIED,
        ApplicationStatus.APPLIED,
        ApplicationStatus.RESUME_SHORTLISTED,
        ApplicationStatus.RESUME_SHORTLISTED,
        ApplicationStatus.INTERVIEW_SCHEDULED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.WITHDRAWN,
    ]
    for status in distribution:
        _seed_app(db, status=status)

    report = InsightsEngine(db).generate_report()

    assert report.insufficient_data is False
    assert report.total_applications == 10
    assert report.funnel[ApplicationStatus.APPLIED.value] == 3
    assert report.funnel[ApplicationStatus.RESUME_SHORTLISTED.value] == 2
    assert report.funnel[ApplicationStatus.INTERVIEW_SCHEDULED.value] == 1
    assert report.funnel[ApplicationStatus.REJECTED.value] == 2
    assert report.funnel[ApplicationStatus.OFFER.value] == 1
    assert report.funnel[ApplicationStatus.WITHDRAWN.value] == 1
    assert report.funnel[ApplicationStatus.INTERVIEW_IN_PROGRESS.value] == 0


def test_red_flag_channel(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(15):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)

    report = InsightsEngine(db).generate_report()

    linkedin = next(i for i in report.insights if i.source == "LinkedIn")
    assert linkedin.flag == "red"
    assert "LinkedIn" in linkedin.message
    assert "15" in linkedin.message


def test_green_flag_channel(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(11):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)
    for _ in range(4):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.INTERVIEW_SCHEDULED)

    report = InsightsEngine(db).generate_report()

    linkedin = next(i for i in report.insights if i.source == "LinkedIn")
    assert linkedin.flag == "green"
    assert "LinkedIn" in linkedin.message
    assert "27%" in linkedin.message


def test_no_division_by_zero(tmp_path: Path) -> None:
    # Direct ChannelStat property guard
    stat = ChannelStat(source="X", total=0, shortlisted=0, interviewed=0, offered=0)
    assert stat.interview_rate() == 0.0
    assert stat.offer_rate() == 0.0

    # Engine must not raise even with an all-zero channel that somehow gets through
    db = _make_db(tmp_path)
    for _ in range(15):
        _seed_app(db, source_portal="Naukri", status=ApplicationStatus.APPLIED)

    report = InsightsEngine(db).generate_report()
    assert report.total_applications == 15
    naukri = next(i for i in report.insights if i.source == "Naukri")
    assert naukri.flag == "red"  # 15 apps, 0 interviews


def test_channel_stats_include_response_efficiency_and_confidence(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(14):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)
    interviewed = _seed_app(
        db, source_portal="LinkedIn", status=ApplicationStatus.INTERVIEW_SCHEDULED
    )
    assert interviewed.id is not None
    db.append_status_history(
        interviewed.id,
        ApplicationStatus.APPLIED.value,
        ApplicationStatus.INTERVIEW_SCHEDULED.value,
        "email",
    )

    report = InsightsEngine(db).generate_report()
    linkedin = next(channel for channel in report.channels if channel.source == "LinkedIn")

    assert linkedin.responded == 1
    assert linkedin.response_rate() == pytest.approx(1 / 15)
    assert linkedin.applications_per_interview() == 15.0
    assert linkedin.confidence() == "medium"
    assert report.methods[0].source == "Unknown"


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_generate_report_single_db_fetch(tmp_path: Path) -> None:
    """generate_report must call _fetch_active_apps once, not once per sub-method."""
    db = _make_db(tmp_path)
    for _ in range(10):
        _seed_app(db)

    engine = InsightsEngine(db)
    call_count = 0
    original_fetch = engine._fetch_active_apps

    def counted_fetch():
        nonlocal call_count
        call_count += 1
        return original_fetch()

    engine._fetch_active_apps = counted_fetch  # type: ignore[method-assign]
    engine.generate_report()

    assert call_count == 1, f"Expected 1 fetch, got {call_count}"


def test_flow_data_filters_history_to_active_apps(tmp_path: Path) -> None:
    """flow_data must not load history for false-positive applications."""
    db = _make_db(tmp_path)

    active = _seed_app(db, status=ApplicationStatus.INTERVIEW_SCHEDULED)
    false_pos = _seed_app(db, status=ApplicationStatus.REJECTED)
    assert active.id is not None and false_pos.id is not None

    # Mark false_pos as false positive
    false_pos.is_false_positive = True
    db.upsert_application(false_pos)

    # Add history to both
    db.append_status_history(active.id, None, "Applied", "email")
    db.append_status_history(false_pos.id, None, "Applied", "email")

    engine = InsightsEngine(db)
    app_ids_fetched: list[set] = []
    original = db.get_status_history_for_apps

    def spy(app_ids):
        app_ids_fetched.append(set(app_ids))
        return original(app_ids)

    db.get_status_history_for_apps = spy  # type: ignore[method-assign]
    engine.flow_data()

    assert app_ids_fetched, "get_status_history_for_apps was never called"
    fetched = app_ids_fetched[0]
    assert active.id in fetched
    assert false_pos.id not in fetched


def test_shortlisted_values_derived_from_stages() -> None:
    """_SHORTLISTED_VALUES must match _SHORTLISTED_STAGES.value for every member."""
    from backend.engine.insights_engine import _SHORTLISTED_STAGES, InsightsEngine

    expected = frozenset(s.value for s in _SHORTLISTED_STAGES)
    assert InsightsEngine._SHORTLISTED_VALUES == expected


def test_interviewed_values_derived_from_stages() -> None:
    from backend.engine.insights_engine import _INTERVIEW_STAGES, InsightsEngine

    expected = frozenset(s.value for s in _INTERVIEW_STAGES)
    assert InsightsEngine._INTERVIEWED_VALUES == expected


def test_offered_values_derived_from_stages() -> None:
    from backend.engine.insights_engine import _OFFER_STAGES, InsightsEngine

    expected = frozenset(s.value for s in _OFFER_STAGES)
    assert InsightsEngine._OFFERED_VALUES == expected


def test_flow_data_empty_db(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = InsightsEngine(db).flow_data()
    assert result["insufficient_data"] is True
    assert result["kpis"]["total"] == 0


def test_flow_data_with_history(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(11):
        app = _seed_app(db, status=ApplicationStatus.REJECTED)
        assert app.id is not None
        db.append_status_history(app.id, None, "Applied", "email")
        db.append_status_history(app.id, "Applied", "Rejected", "email")

    result = InsightsEngine(db).flow_data()
    assert result["insufficient_data"] is False
    assert result["kpis"]["total"] == 11


def test_search_pulse_separates_recent_activity_from_matured_conversion(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    now = utc_now()
    recent = _seed_app(db, status=ApplicationStatus.INTERVIEW_SCHEDULED)
    recent.applied_date = now - timedelta(days=3)
    recent.created_at = now - timedelta(days=2)
    db.upsert_application(recent)
    matured = _seed_app(db, status=ApplicationStatus.REJECTED)
    matured.applied_date = now - timedelta(days=20)
    matured.created_at = now - timedelta(days=20)
    db.upsert_application(matured)

    result = InsightsEngine(db).search_pulse(window_days=28)

    assert result["recent"]["applications"] == 2
    assert result["recent"]["interviews"] == 1
    assert result["matured_cohort"]["applications"] == 1
    assert result["matured_cohort"]["responses"] == 1
    assert result["matured_cohort"]["response_rate"] == 1.0


def test_search_pulse_reports_late_imports_and_both_activity_dates(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    now = utc_now()
    app = _seed_app(db)
    app.applied_date = now - timedelta(days=20)
    app.created_at = now - timedelta(days=2)
    db.upsert_application(app)

    result = InsightsEngine(db).search_pulse(window_days=28)

    assert result["recent"]["late_imports"] == 1
    assert sum(week["applied"] for week in result["activity"]) == 1
    assert sum(week["captured"] for week in result["activity"]) == 1


def test_search_pulse_does_not_count_withdrawal_as_employer_response(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    app = _seed_app(db, status=ApplicationStatus.WITHDRAWN)
    app.applied_date = utc_now() - timedelta(days=20)
    db.upsert_application(app)

    result = InsightsEngine(db).search_pulse(window_days=28)

    assert result["matured_cohort"]["applications"] == 1
    assert result["matured_cohort"]["responses"] == 0
    assert result["matured_cohort"]["response_rate"] == 0.0


def test_search_pulse_rejects_unsupported_window(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    try:
        InsightsEngine(db).search_pulse(window_days=14)
    except ValueError as exc:
        assert "7, 28, or 90" in str(exc)
    else:
        raise AssertionError("Expected unsupported window to be rejected")


# --------------------------------------------------------------------------- #
# rejection_data                                                               #
# --------------------------------------------------------------------------- #


def test_rejection_data_empty_db(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = InsightsEngine(db).rejection_data()
    assert result["insufficient_data"] is True
    assert result["total"] == 0
    assert result["rejection_rate"] == 0.0
    assert result["stage_breakdown"] == {"rejection": {}, "withdrawal": {}}


def test_rejection_data_basic_counts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(5):
        _seed_app(db, status=ApplicationStatus.REJECTED)
    for _ in range(2):
        _seed_app(db, status=ApplicationStatus.WITHDRAWN)
    for _ in range(3):
        _seed_app(db, status=ApplicationStatus.OFFER)
    for _ in range(4):
        _seed_app(db, status=ApplicationStatus.APPLIED)

    result = InsightsEngine(db).rejection_data()

    assert result["total"] == 14
    assert result["rejected"] == 5
    assert result["withdrawn"] == 2
    assert result["resolved"] == 10  # 5 rejected + 2 withdrawn + 3 offered
    assert result["rejection_rate"] == 0.5
    assert result["withdrawal_rate"] == round(2 / 14, 4)
    assert result["non_offer_rate"] == round(7 / 10, 4)
    assert result["insufficient_data"] is False


def test_rejection_data_stage_breakdown_by_history(tmp_path: Path) -> None:
    db = _make_db(tmp_path)

    # Rejected straight from Applied — bucket "Applied"
    app1 = _seed_app(db, status=ApplicationStatus.REJECTED)
    db.append_status_history(app1.id, None, "Applied", "email")
    db.append_status_history(app1.id, "Applied", "Rejected", "email")

    # Rejected after being shortlisted — bucket "Shortlisted"
    app2 = _seed_app(db, status=ApplicationStatus.REJECTED)
    db.append_status_history(app2.id, None, "Applied", "email")
    db.append_status_history(app2.id, "Applied", "Resume Shortlisted", "email")
    db.append_status_history(app2.id, "Resume Shortlisted", "Rejected", "email")

    # Withdrawn after interview — bucket "Interview"
    app3 = _seed_app(db, status=ApplicationStatus.WITHDRAWN)
    db.append_status_history(app3.id, None, "Applied", "email")
    db.append_status_history(app3.id, "Applied", "Interview Scheduled", "email")
    db.append_status_history(app3.id, "Interview Scheduled", "Withdrawn", "manual")

    result = InsightsEngine(db).rejection_data()

    assert result["stage_breakdown"]["rejection"]["Applied"] == 1
    assert result["stage_breakdown"]["rejection"]["Shortlisted"] == 1
    assert result["stage_breakdown"]["withdrawal"]["Interview"] == 1


def test_rejection_data_portal_breakdown(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.REJECTED)
    _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)
    _seed_app(db, source_portal="Naukri", status=ApplicationStatus.WITHDRAWN)

    result = InsightsEngine(db).rejection_data()
    portals = {p["portal"]: p for p in result["portal_breakdown"]}

    assert portals["LinkedIn"]["total"] == 2
    assert portals["LinkedIn"]["rejected"] == 1
    assert portals["LinkedIn"]["rejection_rate"] == 1.0
    assert portals["Naukri"]["withdrawn"] == 1
    assert portals["Naukri"]["rejection_rate"] == 0.0  # resolved (withdrawn) but not rejected


def test_rejection_data_monthly_trend_has_six_months(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_app(db, status=ApplicationStatus.REJECTED)

    result = InsightsEngine(db).rejection_data()

    assert len(result["monthly_trend"]) == 6
    for entry in result["monthly_trend"]:
        assert set(entry.keys()) == {"month", "label", "rejected", "withdrawn"}
