"""Tests for backend/parser/email_parser.py."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.db.models import ApplicationStatus, SuppressRule
from backend.parser.email_parser import EmailParser, ParsedApplication, RawEmail


def _make_email(
    *,
    sender: str = "noreply@naukri.com",
    subject: str = "Your application to Infosys",
    snippet: str = "",
    body_text: str | None = "some body text",
    message_id: str = "msg001",
    thread_id: str = "thread001",
    date: datetime | None = None,
) -> RawEmail:
    return RawEmail(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        date=date or datetime(2024, 1, 15, 10, 0, 0),
        snippet=snippet,
        body_text=body_text,
    )


@pytest.fixture()
def parser() -> EmailParser:
    return EmailParser()


def test_naukri_application_detected(parser: EmailParser) -> None:
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.source_portal == "Naukri"
    assert result.status_signal is None  # Applied is default, no signal keyword


def test_linkedin_application_detected(parser: EmailParser) -> None:
    email = _make_email(
        sender="jobs-noreply@linkedin.com",
        subject="Your application was sent to Google",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.source_portal == "LinkedIn"


def test_direct_hr_detected(parser: EmailParser) -> None:
    email = _make_email(
        sender="hr@somecompany.com",
        subject="Job Application - Senior Engineer",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.source_portal == "Direct/Unknown"
    assert result.is_classification_confident is False


def test_shortlist_signal(parser: EmailParser) -> None:
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your profile has been shortlisted",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.RESUME_SHORTLISTED


def test_interview_signal(parser: EmailParser) -> None:
    email = _make_email(
        sender="hr@somecompany.com",
        subject="Interview Scheduled for Senior Engineer role",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.INTERVIEW_SCHEDULED


def test_rejection_signal(parser: EmailParser) -> None:
    email = _make_email(
        sender="hr@somecompany.com",
        subject="Regret to inform - your application",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.REJECTED


def test_offer_signal(parser: EmailParser) -> None:
    email = _make_email(
        sender="hr@techcorp.com",
        subject="Offer Letter from TechCorp",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.OFFER


def test_suppressed_email(parser: EmailParser) -> None:
    rule = SuppressRule(sender_pattern=r"naukri\.com", subject_pattern=None)
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
    )
    result = parser.parse(email, suppress_rules=[rule])
    assert result is None


def test_newsletter_not_classified(parser: EmailParser) -> None:
    email = _make_email(
        sender="newsletter@naukri.com",
        subject="Top 10 jobs this week",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is None


def test_body_text_cleared(parser: EmailParser) -> None:
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        body_text="Full email body with sensitive content",
    )
    parser.parse(email, suppress_rules=[])
    assert email.body_text is None


def test_body_text_cleared_on_suppression(parser: EmailParser) -> None:
    rule = SuppressRule(sender_pattern=r"naukri\.com", subject_pattern=None)
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        body_text="Full email body",
    )
    parser.parse(email, suppress_rules=[rule])
    assert email.body_text is None


def test_company_extraction_from_subject(parser: EmailParser) -> None:
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.company == "Infosys"


def test_url_extraction(parser: EmailParser) -> None:
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="View your application at https://naukri.com/job/12345",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.job_url == "https://naukri.com/job/12345"


# ------------------------------------------------------------------ #
# New tests from code review fixes                                     #
# ------------------------------------------------------------------ #


def test_invalid_regex_in_suppress_rule_does_not_crash(parser: EmailParser) -> None:
    """An invalid regex in a suppress rule must be skipped, not raise re.error."""
    bad_rule = SuppressRule(sender_pattern="[unclosed", subject_pattern=None)
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
    )
    # Should not raise — invalid rule is skipped with a warning
    result = parser.parse(email, suppress_rules=[bad_rule])
    # The email is still classified; the bad rule is ignored
    assert result is not None


def test_invalid_subject_regex_in_suppress_rule_is_skipped(parser: EmailParser) -> None:
    """Invalid subject_pattern regex skips the rule entirely."""
    bad_rule = SuppressRule(sender_pattern=r"naukri\.com", subject_pattern="[bad")
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
    )
    result = parser.parse(email, suppress_rules=[bad_rule])
    assert result is not None


def test_valid_suppress_rule_still_works_alongside_invalid(parser: EmailParser) -> None:
    """A valid rule after a bad rule still suppresses correctly."""
    bad_rule = SuppressRule(sender_pattern="[unclosed", subject_pattern=None)
    good_rule = SuppressRule(sender_pattern=r"naukri\.com", subject_pattern=None)
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
    )
    result = parser.parse(email, suppress_rules=[bad_rule, good_rule])
    assert result is None  # good_rule fires


def test_extract_sender_domain_angle_brackets() -> None:
    from backend.parser.email_parser import extract_sender_domain
    assert extract_sender_domain("Naukri <noreply@naukri.com>") == "naukri.com"


def test_extract_sender_domain_bare_email() -> None:
    from backend.parser.email_parser import extract_sender_domain
    assert extract_sender_domain("noreply@naukri.com") == "naukri.com"


def test_extract_sender_domain_empty() -> None:
    from backend.parser.email_parser import extract_sender_domain
    assert extract_sender_domain("") == ""
