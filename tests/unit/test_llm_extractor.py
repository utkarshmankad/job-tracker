"""Tests for backend/parser/llm_extractor.py."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from backend.db.models import ApplicationStatus
from backend.parser.llm_extractor import LLMExtractor, LLMExtractionResult


@pytest.fixture()
def extractor() -> LLMExtractor:
    return LLMExtractor(base_url="http://localhost:11434", model="llama3.2:3b", timeout=10)


def _ollama_response(content: dict) -> MagicMock:
    """Fake httpx.Response in Ollama /api/chat format."""
    mock = MagicMock()
    mock.raise_for_status.return_value = None
    mock.json.return_value = {"message": {"content": json.dumps(content)}}
    return mock


# ------------------------------------------------------------------ #
# Happy path                                                           #
# ------------------------------------------------------------------ #


def test_extracts_all_fields(extractor: LLMExtractor) -> None:
    payload = {"company": "Google", "role": "Software Engineer", "status": "INTERVIEW_SCHEDULED"}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@google.com", "Interview at Google", "")
    assert result is not None
    assert result.company == "Google"
    assert result.role == "Software Engineer"
    assert result.status == ApplicationStatus.INTERVIEW_SCHEDULED


def test_extracts_rejection_status(extractor: LLMExtractor) -> None:
    payload = {"company": "Stripe", "role": "Backend Engineer", "status": "REJECTED"}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("careers@stripe.com", "Update on your application", "")
    assert result is not None
    assert result.status == ApplicationStatus.REJECTED


def test_extracts_offer_status(extractor: LLMExtractor) -> None:
    payload = {"company": "Atlassian", "role": "SRE", "status": "OFFER"}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@atlassian.com", "Offer letter enclosed", "")
    assert result is not None
    assert result.status == ApplicationStatus.OFFER


def test_applied_status_maps_to_none(extractor: LLMExtractor) -> None:
    # "APPLIED" is the default — extractor returns None so downstream falls back to keyword check
    payload = {"company": "Zerodha", "role": "SWE", "status": "APPLIED"}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("noreply@naukri.com", "Application received", "")
    assert result is not None
    assert result.status is None  # APPLIED not in _STATUS_MAP → None


def test_null_fields_returned_as_none(extractor: LLMExtractor) -> None:
    payload = {"company": None, "role": None, "status": None}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@company.com", "Job application", "")
    assert result is not None
    assert result.company is None
    assert result.role is None
    assert result.status is None


def test_whitespace_only_strings_normalised_to_none(extractor: LLMExtractor) -> None:
    payload = {"company": "   ", "role": "\t", "status": None}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@company.com", "Job application", "")
    assert result is not None
    assert result.company is None
    assert result.role is None


def test_non_string_company_returns_none(extractor: LLMExtractor) -> None:
    payload = {"company": 123, "role": ["SWE"], "status": None}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@company.com", "Job application", "")
    assert result is not None
    assert result.company is None
    assert result.role is None


def test_unknown_status_string_returns_none(extractor: LLMExtractor) -> None:
    payload = {"company": "Meta", "role": "PM", "status": "PENDING_REVIEW"}
    with patch("httpx.post", return_value=_ollama_response(payload)):
        result = extractor.extract("hr@meta.com", "Application update", "")
    assert result is not None
    assert result.status is None


def test_snippet_and_body_included_in_request(extractor: LLMExtractor) -> None:
    captured: list[dict] = []

    def fake_post(url: str, json: dict, **kwargs: object) -> MagicMock:
        captured.append(json)
        return _ollama_response({"company": "Acme", "role": "Dev", "status": None})

    with patch("httpx.post", side_effect=fake_post):
        extractor.extract("hr@acme.com", "Application received", "Great news!", "Full body text here")

    user_content = captured[0]["messages"][1]["content"]
    assert "Great news!" in user_content
    assert "Full body text here" in user_content


def test_body_text_truncated_to_500_chars(extractor: LLMExtractor) -> None:
    captured: list[dict] = []

    def fake_post(url: str, json: dict, **kwargs: object) -> MagicMock:
        captured.append(json)
        return _ollama_response({"company": None, "role": None, "status": None})

    long_body = "x" * 1000
    with patch("httpx.post", side_effect=fake_post):
        extractor.extract("hr@acme.com", "Subject", "", long_body)

    user_content = captured[0]["messages"][1]["content"]
    # The body excerpt line: "Body excerpt: " + 500 chars
    assert len(user_content) < 700


def test_temperature_zero_in_request(extractor: LLMExtractor) -> None:
    captured: list[dict] = []

    def fake_post(url: str, json: dict, **kwargs: object) -> MagicMock:
        captured.append(json)
        return _ollama_response({"company": None, "role": None, "status": None})

    with patch("httpx.post", side_effect=fake_post):
        extractor.extract("hr@co.com", "Test", "")

    assert captured[0]["options"]["temperature"] == 0


# ------------------------------------------------------------------ #
# Error / fallback paths                                               #
# ------------------------------------------------------------------ #


def test_connect_error_returns_none(extractor: LLMExtractor) -> None:
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        result = extractor.extract("hr@co.com", "Test", "")
    assert result is None


def test_timeout_returns_none(extractor: LLMExtractor) -> None:
    with patch("httpx.post", side_effect=httpx.TimeoutException("timed out")):
        result = extractor.extract("hr@co.com", "Test", "")
    assert result is None


def test_http_status_error_returns_none(extractor: LLMExtractor) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    exc = httpx.HTTPStatusError("server error", request=MagicMock(), response=mock_resp)
    with patch("httpx.post", side_effect=exc):
        result = extractor.extract("hr@co.com", "Test", "")
    assert result is None


def test_invalid_json_in_content_returns_none(extractor: LLMExtractor) -> None:
    mock = MagicMock()
    mock.raise_for_status.return_value = None
    mock.json.return_value = {"message": {"content": "not-json"}}
    with patch("httpx.post", return_value=mock):
        result = extractor.extract("hr@co.com", "Test", "")
    assert result is None


def test_missing_message_key_returns_none(extractor: LLMExtractor) -> None:
    mock = MagicMock()
    mock.raise_for_status.return_value = None
    mock.json.return_value = {"unexpected": "shape"}
    with patch("httpx.post", return_value=mock):
        result = extractor.extract("hr@co.com", "Test", "")
    assert result is None


def test_all_known_status_values_map_correctly(extractor: LLMExtractor) -> None:
    expected = {
        "RESUME_SHORTLISTED": ApplicationStatus.RESUME_SHORTLISTED,
        "INTERVIEW_SCHEDULED": ApplicationStatus.INTERVIEW_SCHEDULED,
        "INTERVIEW_IN_PROGRESS": ApplicationStatus.INTERVIEW_IN_PROGRESS,
        "OFFER_NEGOTIATION": ApplicationStatus.OFFER_NEGOTIATION,
        "OFFER": ApplicationStatus.OFFER,
        "REJECTED": ApplicationStatus.REJECTED,
        "WITHDRAWN": ApplicationStatus.WITHDRAWN,
        "JOINED": ApplicationStatus.JOINED,
    }
    for status_str, expected_status in expected.items():
        payload = {"company": None, "role": None, "status": status_str}
        with patch("httpx.post", return_value=_ollama_response(payload)):
            result = extractor.extract("hr@co.com", "Test", "")
        assert result is not None
        assert result.status == expected_status, f"Failed for {status_str}"
