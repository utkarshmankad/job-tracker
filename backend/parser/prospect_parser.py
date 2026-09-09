"""Conservative classifier for high-signal LinkedIn recruiting emails.

This intentionally excludes recommendation digests. The goal is an actionable inbox,
not a second copy of LinkedIn's job feed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.parser.email_parser import RawEmail, extract_sender_domain


@dataclass(frozen=True)
class ParsedProspect:
    category: str
    title: str
    sender: str
    snippet: str
    message_id: str
    thread_id: str
    classification_reason: str


_LINKEDIN_DOMAINS = {"linkedin.com", "email.linkedin.com", "e.linkedin.com"}

_SIGNALS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "meeting",
        re.compile(
            r"\b(interview|schedule(?:d| a)? (?:call|chat|interview|meeting)|"
            r"calendar invite|meeting request|availability for|book a time|connect for a call)\b",
            re.IGNORECASE,
        ),
        "A meeting, interview, or scheduling signal was detected.",
    ),
    (
        "profile_interest",
        re.compile(
            r"\b(interested in your profile|viewed your profile|wants to connect|"
            r"would like to connect|your profile caught|impressed by your profile)\b",
            re.IGNORECASE,
        ),
        "A recruiter or hiring contact showed explicit profile interest.",
    ),
    (
        "recruiter_outreach",
        re.compile(
            r"\b(new (?:inmail|message) from|sent you (?:an inmail|a message)|"
            r"recruiter (?:reached out|message)|hiring for|career opportunity|job opportunity)\b",
            re.IGNORECASE,
        ),
        "Direct recruiter outreach or a specific opportunity was detected.",
    ),
)

_DIGEST_NOISE = re.compile(
    r"\b(jobs? you may be interested in|recommended jobs?|top job picks|jobs matching your profile|"
    r"new jobs? for you|daily job alert|weekly job alert)\b",
    re.IGNORECASE,
)


def parse_linkedin_prospect(email: RawEmail) -> ParsedProspect | None:
    """Return an actionable LinkedIn prospect, or None for applications/digests/noise."""
    domain = extract_sender_domain(email.sender)
    if domain not in _LINKEDIN_DOMAINS and "@linkedin.com" not in email.sender.lower():
        return None

    text = f"{email.subject} {email.snippet or ''}".strip()
    if _DIGEST_NOISE.search(text):
        return None

    for category, pattern, reason in _SIGNALS:
        if pattern.search(text):
            return ParsedProspect(
                category=category,
                title=email.subject.strip() or "LinkedIn recruiting activity",
                sender=email.sender.strip(),
                snippet=(email.snippet or "").strip()[:500],
                message_id=email.message_id,
                thread_id=email.thread_id,
                classification_reason=reason,
            )
    return None
