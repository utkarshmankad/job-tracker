"""Unit tests for backend.db.import_from_excel."""

from datetime import UTC, datetime

import openpyxl
import pytest

from backend.db import import_from_excel

# ------------------------------------------------------------------ #
# Pure helpers
# ------------------------------------------------------------------ #


def test_status_known_value():
    assert import_from_excel._status("Rejected") == "Rejected"


def test_status_unknown_value_defaults_to_applied(capsys):
    assert import_from_excel._status("Some Weird Status") == "Applied"
    assert "unknown status" in capsys.readouterr().out


def test_status_none_defaults_to_applied():
    assert import_from_excel._status(None) == "Applied"


def test_portal_blank_defaults_to_direct_consultancy():
    assert import_from_excel._portal(None) == "Direct/Consultancy"
    assert import_from_excel._portal("") == "Direct/Consultancy"


def test_portal_strips_whitespace():
    assert import_from_excel._portal("  Naukri  ") == "Naukri"


def test_applied_date_from_datetime_naive():
    dt = datetime(2024, 1, 5)  # noqa: DTZ001 — exercising the naive-input branch
    result = import_from_excel._applied_date(dt)
    assert result.tzinfo == UTC
    assert result.year == 2024 and result.month == 1 and result.day == 5


def test_applied_date_from_datetime_aware():
    dt = datetime(2024, 1, 5, tzinfo=UTC)
    assert import_from_excel._applied_date(dt) == dt


def test_applied_date_from_string():
    result = import_from_excel._applied_date("2024-03-15")
    assert result == datetime(2024, 3, 15, tzinfo=UTC)


def test_applied_date_invalid_raises():
    with pytest.raises(ValueError):
        import_from_excel._applied_date("not-a-date")


def test_applied_date_invalid_type_raises():
    with pytest.raises(ValueError):
        import_from_excel._applied_date(12345)


# ------------------------------------------------------------------ #
# run()
# ------------------------------------------------------------------ #


def test_run_exits_1_when_db_missing(tmp_path):
    missing_db = tmp_path / "no.db"
    xlsx_path = tmp_path / "tracker.xlsx"
    with pytest.raises(SystemExit) as exc_info:
        import_from_excel.run(xlsx_path, missing_db)
    assert exc_info.value.code == 1


def _build_workbook(tmp_path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tracker"
    for _ in range(4):
        ws.append([])
    for row in rows:
        ws.append(row)
    path = tmp_path / "tracker.xlsx"
    wb.save(path)
    return path


def test_run_imports_rows_into_db(db, tmp_path):
    db_path = tmp_path / "test.db"
    xlsx_path = _build_workbook(
        tmp_path,
        rows=[
            (1, "Acme Corp", "SWE", "J1", "Naukri", "2024-01-05", "hr@acme.com", "Applied"),
            (
                2,
                "Globex",
                "Backend Eng",
                "J2",
                "LinkedIn",
                "2024-02-10",
                "hr@globex.com",
                "Rejected",
            ),
            (None, None, None, None, None, None, None, None),
        ],
    )

    import_from_excel.run(xlsx_path, db_path)

    n_apps, _ = db.count_applications_and_processed()
    assert n_apps == 2
