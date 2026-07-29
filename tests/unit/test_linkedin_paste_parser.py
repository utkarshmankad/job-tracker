"""Tests for backend/parser/linkedin_paste_parser.py."""

from __future__ import annotations

from datetime import datetime, timedelta

from backend.db.models import utc_now
from backend.parser.linkedin_paste_parser import parse_linkedin_paste


def _today() -> datetime:
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


def test_format_a_company_then_role() -> None:
    text = """
    InCommon
    Engineering Manager - Platform Engineering
    Add note
    Applied 1w ago
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].company == "InCommon"
    assert entries[0].role == "Engineering Manager - Platform Engineering"
    assert entries[0].applied_date == _today() - timedelta(weeks=1)


def test_format_b_role_then_company_bullet() -> None:
    text = """
    Add note
    Principal Engineer
    Serko · Bengaluru (Hybrid)
    Applied 2d ago
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].company == "Serko"
    assert entries[0].role == "Principal Engineer"
    assert entries[0].applied_date == _today() - timedelta(days=2)


def test_role_at_company_single_line() -> None:
    text = """
    Software Engineer at Acme Corp
    Applied today
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].role == "Software Engineer"
    assert entries[0].company == "Acme"


def test_multiple_entries_separated_by_applied_marker() -> None:
    text = """
    Alpha Inc
    Backend Engineer
    Applied 1w ago
    Beta LLC
    Frontend Engineer
    Applied 3d ago
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 2
    assert entries[0].company == "Alpha"
    assert entries[1].company == "Beta"


def test_entry_end_marker_without_date() -> None:
    text = """
    Gamma Corp
    Data Scientist
    Archived
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].company == "Gamma"
    assert entries[0].applied_date is None


def test_noise_lines_are_skipped() -> None:
    text = """
    Easy Apply
    Promoted
    Delta Inc
    QA Engineer
    123 applicants
    Applied yesterday
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].company == "Delta"
    assert entries[0].applied_date == _today() - timedelta(days=1)


def test_location_only_bullet_line_ignored() -> None:
    text = """
    Epsilon Corp
    Site Reliability Engineer
    Bengaluru, India
    Applied 3w ago
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].role == "Site Reliability Engineer"


def test_hour_shorthand_resolves_to_today() -> None:
    text = """
    Zeta Inc
    DevOps Engineer
    Applied 5h ago
    """
    entries = parse_linkedin_paste(text)
    assert entries[0].applied_date == _today()


def test_month_shorthand() -> None:
    text = """
    Theta LLC
    Staff Engineer
    Applied 2mo ago
    """
    entries = parse_linkedin_paste(text)
    assert entries[0].applied_date == _today() - timedelta(days=60)


def test_empty_input_returns_no_entries() -> None:
    assert parse_linkedin_paste("") == []
    assert parse_linkedin_paste("   \n  \n ") == []


def test_trailing_block_without_end_marker_is_flushed() -> None:
    text = """
    Iota Corp
    Product Manager
    """
    entries = parse_linkedin_paste(text)
    assert len(entries) == 1
    assert entries[0].company == "Iota"
    assert entries[0].applied_date is None


def test_company_suffix_stripped() -> None:
    text = """
    Kappa Technologies Inc.
    Backend Developer
    Applied 1d ago
    """
    entries = parse_linkedin_paste(text)
    assert entries[0].company == "Kappa Technologies"
