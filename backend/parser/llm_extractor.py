"""LLM-based email field extractor and LinkedIn paste parser using Ollama local inference."""

import json
from dataclasses import dataclass
from datetime import datetime

import httpx
import structlog

from backend.db.models import ApplicationStatus

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Email extraction
# ──────────────────────────────────────────────────────────────────────────────

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

_EMAIL_SYSTEM_PROMPT = """\
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

# ──────────────────────────────────────────────────────────────────────────────
# LinkedIn paste parser
# ──────────────────────────────────────────────────────────────────────────────

_LINKEDIN_GARBAGE_PROMPT = """\
You classify lines from a LinkedIn "My Jobs" page paste as GARBAGE (noise) or CONTENT (useful).

GARBAGE lines — put their line numbers in garbage_line_numbers:
- "Add note" (LinkedIn UI button text)
- User note text: arbitrary sentences following "Add note" (e.g. "Got it. We will check back...")
- Parenthetical metadata: "(Posted Xw ago)", "(Reposted Xw ago)", "(No longer accepting applications)"
- Location names: city, state, country combinations; "Remote", "Hybrid", "On-site"
- "Easy Apply", "Promoted", "Actively recruiting", "Be an early applicant"
- Applicant counts: "200+ applicants", "Over 100 applicants"

NOT garbage (do not include):
- Company names
- Job titles / roles
- "Applied X ago", "Applied today", "Applied yesterday"
- "Archived", "Interviewing", "In progress"
- Blank lines (ignore them; do not include in garbage_line_numbers)

Return ONLY valid JSON:
{"garbage_line_numbers": [5, 7, 8, 9, 10]}\
"""


@dataclass
class LLMExtractionResult:
    company: str | None
    role: str | None
    status: ApplicationStatus | None


@dataclass
class LinkedInGarbageResult:
    """LLM output: which lines in the paste are noise/garbage."""
    garbage_line_numbers: list[int]


class LLMExtractor:
    def __init__(
        self, base_url: str, model: str, timeout: int = 30, api_key: str | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._api_key = api_key

    # ── Email extraction ──────────────────────────────────────────────────────

    def extract(
        self,
        sender: str,
        subject: str,
        snippet: str,
        body_text: str | None = None,
    ) -> LLMExtractionResult | None:
        """Call Ollama to extract company, role, and status from a job email.

        Returns None when Ollama is unavailable or returns malformed output so the
        caller can fall back to the deterministic regex/spaCy path.
        """
        parts = [f"From: {sender}", f"Subject: {subject}"]
        if snippet:
            parts.append(f"Snippet: {snippet}")
        if body_text:
            parts.append(f"Body excerpt: {body_text[:500]}")

        raw = self._chat(_EMAIL_SYSTEM_PROMPT, "\n".join(parts), log_key="email")
        if raw is None:
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("ollama_parse_error_falling_back", error=str(exc))
            return None

        status_raw = parsed.get("status")
        status = _STATUS_MAP.get(status_raw) if isinstance(status_raw, str) else None

        return LLMExtractionResult(
            company=_clean_str(parsed.get("company")),
            role=_clean_str(parsed.get("role")),
            status=status,
        )

    # ── LinkedIn paste parser ─────────────────────────────────────────────────

    def classify_linkedin_garbage(self, text: str) -> LinkedInGarbageResult | None:
        """Ask Ollama to tag noise/garbage lines in a LinkedIn paste.

        Regex handles company/role/date extraction; LLM only handles the
        harder garbage classification (user notes, ambiguous metadata).
        Returns None when Ollama is unavailable — caller omits highlighting.
        """
        lines = text.split("\n")
        # Only send non-blank lines to save tokens; track original indices.
        indexed = [(i + 1, line) for i, line in enumerate(lines) if line.strip()]
        if not indexed:
            return LinkedInGarbageResult(garbage_line_numbers=[])

        numbered = "\n".join(f"{num}| {line}" for num, line in indexed)
        total_lines = len(lines)
        valid_nums = {num for num, _ in indexed}

        raw = self._chat(_LINKEDIN_GARBAGE_PROMPT, numbered, log_key="linkedin_garbage")
        if raw is None:
            return None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("ollama_json_error_linkedin_garbage", error=str(exc))
            return None

        raw_garbage = parsed.get("garbage_line_numbers", [])
        garbage = [n for n in raw_garbage if isinstance(n, int) and n in valid_nums]

        log.info("ollama_linkedin_garbage_ok", garbage_lines=len(garbage))
        return LinkedInGarbageResult(garbage_line_numbers=garbage)

    # ── Shared HTTP helper ────────────────────────────────────────────────────

    def _chat(self, system: str, user: str, *, log_key: str) -> str | None:
        """POST to the configured LLM backend. Returns raw message content or None on failure.

        Ollama's /api/chat and Groq's OpenAI-compatible /chat/completions have
        different request/response shapes, so branch on whether an API key is
        configured (Groq) vs local Ollama.
        """
        try:
            if self._api_key:
                response = httpx.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                    },
                    timeout=self._timeout,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]

            response = httpx.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()["message"]["content"]
        except httpx.ConnectError:
            log.warning(f"ollama_unavailable_{log_key}", base_url=self._base_url)
            return None
        except httpx.TimeoutException:
            log.warning(f"ollama_timeout_{log_key}", timeout=self._timeout)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning(f"ollama_http_error_{log_key}", status_code=exc.response.status_code)
            return None
        except (KeyError, Exception) as exc:
            log.warning(f"ollama_unexpected_{log_key}", error=str(exc))
            return None


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None
