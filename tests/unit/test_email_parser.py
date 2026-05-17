"""Tests for backend/parser/email_parser.py."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.db.models import ApplicationStatus, SuppressRule
from backend.parser.email_parser import EmailParser, ParsedApplication, RawEmail
from backend.parser.llm_extractor import LLMExtractionResult


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


# ------------------------------------------------------------------ #
# Parser resilience — spaCy unavailable or crashing                   #
# ------------------------------------------------------------------ #


def test_spacy_crash_falls_back_to_regex(parser: EmailParser) -> None:
    """When spaCy's nlp() raises, company extraction still works via regex fallback."""
    parser._nlp = MagicMock(side_effect=RuntimeError("spacy internal error"))
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    # Company may or may not be extracted by regex, but no exception should propagate
    assert result.source_portal == "Naukri"


def test_spacy_model_not_found_uses_regex_only(tmp_path) -> None:
    """Parser with _nlp=None (model not installed) still classifies emails via regex."""
    import spacy
    with patch.object(spacy, "load", side_effect=OSError("model not found")):
        p = EmailParser()
    assert p._nlp is None

    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="",
        body_text=None,
    )
    result = p.parse(email, suppress_rules=[])
    assert result is not None
    assert result.source_portal == "Naukri"
    # Regex fallback extracts company from subject
    assert result.company == "Infosys"


def test_parse_does_not_store_body_text_when_spacy_fails(parser: EmailParser) -> None:
    """body_text is always cleared even when spaCy crashes mid-extraction."""
    parser._nlp = MagicMock(side_effect=RuntimeError("boom"))
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        body_text="secret body content",
    )
    parser.parse(email, suppress_rules=[])
    assert email.body_text is None


# ------------------------------------------------------------------ #
# LLM extractor integration                                            #
# ------------------------------------------------------------------ #


def test_llm_company_and_role_used_when_available(parser: EmailParser) -> None:
    """When LLM returns company and role, those values are used in the result."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = LLMExtractionResult(
        company="DeepMind", role="Research Engineer", status=None
    )
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to DeepMind",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.company == "DeepMind"
    assert result.role == "Research Engineer"


def test_llm_status_used_over_keyword_detection(parser: EmailParser) -> None:
    """LLM-detected status takes priority over keyword-based signal detection."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = LLMExtractionResult(
        company="Stripe",
        role="Backend Engineer",
        status=ApplicationStatus.REJECTED,
    )
    # Subject matches Naukri portal but contains no rejection keywords — keyword
    # detection alone would return None; LLM correctly infers REJECTED from context.
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Stripe",
        snippet="We appreciate your interest but have decided to go in a different direction.",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.REJECTED


def test_llm_none_status_falls_back_to_keyword_detection(parser: EmailParser) -> None:
    """When LLM returns status=None, keyword detection is still applied."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = LLMExtractionResult(
        company="Infosys", role=None, status=None
    )
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your profile has been shortlisted",
        snippet="",
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.status_signal == ApplicationStatus.RESUME_SHORTLISTED


def test_llm_partial_result_regex_fills_missing_role(parser: EmailParser) -> None:
    """When LLM returns company but no role, regex extraction fills in the role."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = LLMExtractionResult(
        company="Infosys", role=None, status=None
    )
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application for Software Engineer at Infosys",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.company == "Infosys"
    assert result.role is not None  # regex extracts "Software Engineer"


def test_llm_unavailable_falls_back_to_spacy_regex(parser: EmailParser) -> None:
    """When LLM returns None (Ollama down), existing spaCy/regex extraction runs."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = None
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.company == "Infosys"


def test_llm_disabled_uses_existing_extraction(parser: EmailParser) -> None:
    """Setting _llm=None disables LLM; existing extraction still works."""
    parser._llm = None
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="",
        body_text=None,
    )
    result = parser.parse(email, suppress_rules=[])
    assert result is not None
    assert result.company == "Infosys"
    assert result.source_portal == "Naukri"


def test_body_text_cleared_even_when_llm_is_used(parser: EmailParser) -> None:
    """body_text must always be cleared after parsing, regardless of LLM path."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = LLMExtractionResult(
        company="Acme", role="SWE", status=None
    )
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Acme",
        body_text="sensitive body content",
    )
    parser.parse(email, suppress_rules=[])
    assert email.body_text is None


def test_llm_called_with_correct_arguments(parser: EmailParser) -> None:
    """LLM extractor receives sender, subject, snippet, and body_text."""
    parser._llm = MagicMock()
    parser._llm.extract.return_value = None  # fall back to regex
    email = _make_email(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="Application submitted",
        body_text="Full email body here",
    )
    parser.parse(email, suppress_rules=[])
    parser._llm.extract.assert_called_once_with(
        sender="noreply@naukri.com",
        subject="Your application to Infosys",
        snippet="Application submitted",
        body_text="Full email body here",
    )
