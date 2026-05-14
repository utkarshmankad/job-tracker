"""Tests for backend/engine/insights_engine.py."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from backend.db.data_store import DataStore
from backend.db.models import Application, ApplicationStatus
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
        applied_date=datetime.utcnow(),
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
    for _ in range(10):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)

    report = InsightsEngine(db).generate_report()

    linkedin = next(i for i in report.insights if i.source == "LinkedIn")
    assert linkedin.flag == "red"
    assert "LinkedIn" in linkedin.message
    assert "10" in linkedin.message


def test_green_flag_channel(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    for _ in range(7):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.APPLIED)
    for _ in range(3):
        _seed_app(db, source_portal="LinkedIn", status=ApplicationStatus.INTERVIEW_SCHEDULED)

    report = InsightsEngine(db).generate_report()

    linkedin = next(i for i in report.insights if i.source == "LinkedIn")
    assert linkedin.flag == "green"
    assert "LinkedIn" in linkedin.message
    assert "30%" in linkedin.message


def test_no_division_by_zero(tmp_path: Path) -> None:
    # Direct ChannelStat property guard
    stat = ChannelStat(source="X", total=0, shortlisted=0, interviewed=0, offered=0)
    assert stat.interview_rate() == 0.0
    assert stat.offer_rate() == 0.0

    # Engine must not raise even with an all-zero channel that somehow gets through
    db = _make_db(tmp_path)
    for _ in range(10):
        _seed_app(db, source_portal="Naukri", status=ApplicationStatus.APPLIED)

    report = InsightsEngine(db).generate_report()
    assert report.total_applications == 10
    naukri = next(i for i in report.insights if i.source == "Naukri")
    assert naukri.flag == "red"  # 10 apps, 0 interviews
