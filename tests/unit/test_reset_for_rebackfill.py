"""Unit tests for backend.db.reset_for_rebackfill."""

from unittest.mock import patch

import pytest

from backend.db import reset_for_rebackfill
from backend.db.models import Application, ApplicationStatus, utc_now


def _make_app(company: str) -> Application:
    return Application(
        company=company,
        role="Engineer",
        source_portal="Naukri",
        applied_date=utc_now(),
        current_status=ApplicationStatus.APPLIED,
    )


def test_reset_exits_1_when_db_missing(tmp_path):
    missing_path = tmp_path / "does-not-exist.db"
    with pytest.raises(SystemExit) as exc_info:
        reset_for_rebackfill.reset(missing_path)
    assert exc_info.value.code == 1


def test_reset_confirmed_deletes_data(db, tmp_path, capsys):
    db.upsert_application(_make_app("Co1"))
    db.upsert_application(_make_app("Co2"))
    db_path = tmp_path / "test.db"

    with patch("backend.db.reset_for_rebackfill.input", return_value="yes"):
        reset_for_rebackfill.reset(db_path)

    n_apps, n_proc = db.count_applications_and_processed()
    assert n_apps == 0
    assert n_proc == 0
    out = capsys.readouterr().out
    assert "Deleted 2 application(s)" in out


def test_reset_aborted_when_not_confirmed(db, tmp_path, capsys):
    db.upsert_application(_make_app("Co1"))
    db_path = tmp_path / "test.db"

    with patch("backend.db.reset_for_rebackfill.input", return_value="no"):
        with pytest.raises(SystemExit) as exc_info:
            reset_for_rebackfill.reset(db_path)

    assert exc_info.value.code == 0
    n_apps, _ = db.count_applications_and_processed()
    assert n_apps == 1
    assert "Aborted." in capsys.readouterr().out
