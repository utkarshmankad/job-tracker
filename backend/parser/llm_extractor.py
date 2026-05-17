"""LLM-based email field extractor using Ollama local inference."""

import json
from dataclasses import dataclass

import httpx
import structlog

from backend.db.models import ApplicationStatus

log = structlog.get_logger(__name__)

# Canonical status strings the LLM must use (excludes APPLIED — that's the default / None)
_STATUS_MAP: dict[str, ApplicationStatus] = {
    "RESUME_SHORTLISTED": ApplicationStatus.RESUME_SHORTLISTED,
    "INTERVIEW_SCHEDULED": ApplicationStatus.INTERVIEW_SCHEDULED,
    "INTERVIEW_IN_PROGRESS": ApplicationStatus.INTERVIEW_IN_PROGRESS,
    "OFFER_NEGOTIATION": ApplicationStatus.OFFER_NEGOTIATION,
    "OFFER": ApplicationStatus.OFFER,
    "REJECTED": ApplicationStatus.REJECTED,
    "WITHDRAWN": ApplicationStatus.WITHDRAWN,
    "JOINED": ApplicationStatus.JOINED,
}

_SYSTEM_PROMPT = """\
You are a precise job application email parser. Extract structured fields from job-related emails.

Return ONLY a valid JSON object with these exact keys:
- "company": string or null — the hiring company name (never a job portal like LinkedIn, Naukri, or Greenhouse)
- "role": string or null — the specific job title or position (e.g. "Software Engineer", "Product Manager")
- "status": one of the exact strings below, or null

Status values — pick the single best match:
- "APPLIED" — application confirmation/receipt (default; use when no advancement detected)
- "RESUME_SHORTLISTED" — profile shortlisted or selected for the next stage
- "INTERVIEW_SCHEDULED" — an interview has been arranged or is upcoming
- "INTERVIEW_IN_PROGRESS" — interview round is happening now or just happened
- "OFFER_NEGOTIATION" — compensation/offer is being discussed
- "OFFER" — a formal job offer has been made
- "REJECTED" — application rejected, not moving forward, or position filled
- "WITHDRAWN" — candidate withdrew their application
- "JOINED" — candidate has joined the company

Rules:
- Prefer null over a wrong guess for company or role.
- Do not return a job portal name (LinkedIn, Naukri, Greenhouse, etc.) as the company.
- If the email is a generic newsletter, digest, or unrelated marketing, set all fields to null.\
"""


@dataclass
class LLMExtractionResult:
    company: str | None
    role: str | None
    status: ApplicationStatus | None


class LLMExtractor:
    def __init__(self, base_url: str, model: str, timeout: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def extract(
        self,
        sender: str,
        subject: str,
        snippet: str,
        body_text: str | None = None,
    ) -> LLMExtractionResult | None:
        """Call Ollama to extract company, role, and status.

        Returns None when Ollama is unavailable or returns malformed output so the
        caller can fall back to the deterministic regex/spaCy path.
        """
        parts = [f"From: {sender}", f"Subject: {subject}"]
        if snippet:
            parts.append(f"Snippet: {snippet}")
        if body_text:
            parts.append(f"Body excerpt: {body_text[:500]}")

        try:
            response = httpx.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": "\n".join(parts)},
                    ],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.ConnectError:
            log.warning("ollama_unavailable_falling_back", base_url=self._base_url)
            return None
        except httpx.TimeoutException:
            log.warning("ollama_timeout_falling_back", timeout=self._timeout)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning("ollama_http_error_falling_back", status_code=exc.response.status_code)
            return None

        try:
            raw = response.json()["message"]["content"]
            parsed = json.loads(raw)
        except (KeyError, json.JSONDecodeError) as exc:
            log.warning("ollama_parse_error_falling_back", error=str(exc))
            return None

        status_raw = parsed.get("status")
        status = _STATUS_MAP.get(status_raw) if isinstance(status_raw, str) else None

        return LLMExtractionResult(
            company=_clean_str(parsed.get("company")),
            role=_clean_str(parsed.get("role")),
            status=status,
        )


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None
