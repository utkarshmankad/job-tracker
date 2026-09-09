"""Tests for automatic high-signal LinkedIn opportunity classification."""

from datetime import UTC, datetime

from backend.parser.email_parser import RawEmail
from backend.parser.prospect_parser import parse_linkedin_prospect


def _email(
    subject: str,
    snippet: str = "",
    sender: str = "LinkedIn <messages-noreply@linkedin.com>",
) -> RawEmail:
    return RawEmail(
        message_id="m1",
        thread_id="t1",
        sender=sender,
        subject=subject,
        date=datetime(2026, 9, 10, tzinfo=UTC),
        snippet=snippet,
        body_text=None,
    )


def test_detects_interview_and_scheduling_request() -> None:
    result = parse_linkedin_prospect(_email("A recruiter wants to schedule a call"))
    assert result is not None
    assert result.category == "meeting"


def test_detects_linkedin_recruiter_message() -> None:
    result = parse_linkedin_prospect(_email("Priya sent you a message", "Career opportunity"))
    assert result is not None
    assert result.category == "recruiter_outreach"


def test_detects_explicit_profile_interest() -> None:
    result = parse_linkedin_prospect(_email("A recruiter is interested in your profile"))
    assert result is not None
    assert result.category == "profile_interest"


def test_ignores_recommendation_digest() -> None:
    assert parse_linkedin_prospect(_email("10 jobs you may be interested in")) is None


def test_ignores_same_words_from_non_linkedin_sender() -> None:
    assert parse_linkedin_prospect(_email("Schedule a call", sender="unknown@example.com")) is None
