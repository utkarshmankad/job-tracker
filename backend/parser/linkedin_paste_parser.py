"""Parser for text pasted from LinkedIn My Jobs page (Applied / Archived / In Progress).

LinkedIn paste format (real-world):
  - Blank lines appear between EVERY element, not just between job cards
  - Date shorthand: "1w ago", "2d ago", "20h ago", "3mo ago"
  - Order within a card: Company → Role → [noise] → "Applied X ago" / status marker
  - Entry boundary = the "Applied X ago", "Archived", "Interviewing", or "In progress" line
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog

from backend.db.models import utc_now

log = structlog.get_logger(__name__)


@dataclass
class LinkedInParsedEntry:
    company: Optional[str] = None
    role: Optional[str] = None
    applied_date: Optional[datetime] = None
    raw_lines: list[str] = field(default_factory=list)


# Matches LinkedIn's shorthand AND written-out date formats
_APPLIED_DATE_RE = re.compile(
    r"Applied\s+"
    r"(?:"
    r"(\d+)\s*(h|hr|hour|d|day|w|week|mo|month)s?\s*ago"
    r"|"
    r"(today|yesterday)"
    r")",
    re.IGNORECASE,
)

# Lines that mark the END of a LinkedIn job card
_ENTRY_END_RE = re.compile(
    r"^(Archived|Interviewing|Interview\s+in\s+progress|In\s+progress|Withdrawn|Closed|Saved)$",
    re.IGNORECASE,
)

# Parenthetical metadata lines, e.g. "(Posted 3w ago)", "(Reposted 1w ago)", "(No longer accepting...)"
_PAREN_META_RE = re.compile(r"^\(.+\)$")

_NOISE_EXACT = frozenset({
    "add note", "easy apply", "promoted", "actively recruiting", "be an early applicant",
    "saved", "new", "viewed", "see all jobs", "show more", "show all", "load more",
    "jobs", "applied jobs", "my jobs", "linkedin", "all filters", "date posted",
    "experience level", "company", "job type", "remote", "easy apply filter",
})

_NOISE_PREFIX_RE = re.compile(
    r"^(promoted|easy apply|be an early|actively recruit|reposted\s+\d|"
    r"show more|see all|load more|got it\.|we will|following)",
    re.IGNORECASE,
)

_APPLICANT_COUNT_RE = re.compile(
    r"^\d+[\+\s]*(?:applicant|view|people|connection|follower|over)",
    re.IGNORECASE,
)

_JOB_TITLE_WORDS = frozenset({
    "engineer", "engineering", "manager", "management", "director", "lead", "senior",
    "junior", "principal", "staff", "analyst", "developer", "designer", "architect",
    "consultant", "specialist", "officer", "head", "vp", "president", "associate",
    "product", "software", "data", "platform", "backend", "frontend", "fullstack",
    "ml", "ai", "devops", "sre", "qa", "tech", "technical", "research", "scientist",
})

_LOCATION_KEYWORDS = frozenset({
    "hybrid", "on-site", "onsite", "on site",
    "india", "usa", "u.s.a.", "u.s.", "uk", "u.k.", "canada", "australia",
    "bengaluru", "bangalore", "mumbai", "delhi", "new delhi", "chennai",
    "hyderabad", "pune", "noida", "gurugram", "gurgaon", "kolkata",
    "new york", "san francisco", "seattle", "austin", "chicago", "boston",
    "london", "singapore", "dubai", "toronto", "sydney", "berlin", "amsterdam",
    "california", "texas", "washington", "nyc", "sf", "bay area",
    "greater", "metropolitan", "worldwide", "global", "work from home",
})

_COMPANY_SUFFIX_RE = re.compile(
    r"\s*,?\s*\b(Inc\.?|LLC|Ltd\.?|Corp\.?|Limited|GmbH|S\.A\.|AG|Pvt\.?)\s*$",
    re.IGNORECASE,
)


def _parse_applied_date(line: str) -> Optional[datetime]:
    m = _APPLIED_DATE_RE.search(line)
    if not m:
        return None
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    if m.group(3):
        return today if m.group(3).lower() == "today" else today - timedelta(days=1)
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("h", "hr", "hour"):
        return today  # same day
    elif unit in ("d", "day"):
        return today - timedelta(days=n)
    elif unit in ("w", "week"):
        return today - timedelta(weeks=n)
    else:  # mo / month
        return today - timedelta(days=n * 30)


def _is_noise(line: str) -> bool:
    lower = line.lower().strip()
    if lower in _NOISE_EXACT:
        return True
    if _APPLICANT_COUNT_RE.match(lower):
        return True
    if _NOISE_PREFIX_RE.match(lower):
        return True
    if _PAREN_META_RE.match(line):
        return True
    if len(lower) <= 2:
        return True
    if re.match(r"^\d+$", lower):
        return True
    return False


def _is_location(line: str) -> bool:
    """Return True when the line is almost certainly a geographic location, not a company/role."""
    if "·" in line:
        return False  # handled at call site
    lower = line.lower()

    if "," in line:
        parts = [p.strip() for p in line.split(",")]
        if all(len(p) < 40 for p in parts):
            if any(kw in lower for kw in _LOCATION_KEYWORDS):
                return True
            # "City, State" heuristic — but NOT if line contains job-title words
            if len(parts) >= 2 and all(1 <= len(p.split()) <= 4 for p in parts):
                if not re.search(r"\b(Inc|LLC|Ltd|Corp|Limited|GmbH|Pvt)\b", line, re.IGNORECASE):
                    line_words = set(re.findall(r"\b\w+\b", lower))
                    if not (line_words & _JOB_TITLE_WORDS):
                        return True

    words = set(re.findall(r"\b\w+\b", lower))
    if words & _LOCATION_KEYWORDS and len(line) < 60:
        return True

    return False


def _build_entry(
    content_lines: list[str],
    applied_date: Optional[datetime],
    company_from_bullet: Optional[str] = None,
) -> Optional[LinkedInParsedEntry]:
    """Build a parsed entry from the collected content lines.

    Two LinkedIn formats exist:
    A) Company on its own line, then Role:  InCommon / Engineering Manager
    B) Role on its own line, Company in "Company · Location" bullet:  Principal Engineer / Serko · Bengaluru

    When company_from_bullet is set (Format B), it takes precedence and
    all content_lines are treated as role candidates.
    """
    if not content_lines and not company_from_bullet:
        return None

    company: Optional[str] = company_from_bullet
    role: Optional[str] = None

    if content_lines:
        first = content_lines[0]

        if company is not None:
            # Format B: company already known from bullet line → first content line = role
            role = first
        else:
            # "Role at Company" on a single line
            at_match = re.match(r"^(.+?)\s+at\s+(.+)$", first, re.IGNORECASE)
            if at_match and len(at_match.group(1)) > 2 and len(at_match.group(2)) > 2:
                role = at_match.group(1).strip()
                company = at_match.group(2).strip()
            elif len(content_lines) >= 2:
                # Format A: Company first, Role second
                company = content_lines[0]
                role = content_lines[1]
            else:
                role = content_lines[0]

    if company:
        company = _COMPANY_SUFFIX_RE.sub("", company).strip() or None

    return LinkedInParsedEntry(
        company=company or None,
        role=role or None,
        applied_date=applied_date,
        raw_lines=content_lines,
    )


def parse_linkedin_paste(text: str) -> list[LinkedInParsedEntry]:
    """Parse text pasted from LinkedIn My Jobs page.

    Strategy: flatten all lines (blank lines between every element in real pastes),
    then treat "Applied X ago" / status markers as entry-end signals.

    Two LinkedIn paste formats are handled:
    Format A — Company on its own line, then Role:
        InCommon / Engineering Manager - Platform Engineering / Add note / Applied 1w ago
    Format B — Role on its own line, then Company·Location on a bullet line:
        Add note / Principal Engineer / Serko · Bengaluru (Hybrid) / Applied 1w ago
    """
    all_lines = [l.strip() for l in text.split("\n") if l.strip()]

    entries: list[LinkedInParsedEntry] = []
    content_lines: list[str] = []
    company_from_bullet: Optional[str] = None
    current_date: Optional[datetime] = None

    def _flush(date: Optional[datetime]) -> None:
        entry = _build_entry(content_lines, date, company_from_bullet)
        if entry and (entry.company or entry.role):
            entries.append(entry)

    for line in all_lines:
        # Applied date → end of entry
        date_val = _parse_applied_date(line)
        if date_val is not None:
            _flush(date_val)
            content_lines = []
            company_from_bullet = None
            current_date = None
            continue

        # Status marker (Archived, Interviewing, etc.) → end of entry, no date
        if _ENTRY_END_RE.match(line):
            _flush(None)
            content_lines = []
            company_from_bullet = None
            current_date = None
            continue

        # Noise → skip
        if _is_noise(line):
            continue

        # "·" bullet line: "Company · City · Work-type"
        # The first non-location part is always the company (Format B signal).
        if "·" in line:
            parts = [p.strip() for p in line.split("·") if p.strip()]
            non_loc = [p for p in parts if not _is_noise(p) and not _is_location(p)]
            if non_loc and company_from_bullet is None:
                company_from_bullet = non_loc[0]
            # Don't add to content_lines — company captured separately
            continue

        # Pure location → skip
        if _is_location(line):
            continue

        content_lines.append(line)

    # Trailing block with no end marker
    if content_lines or company_from_bullet:
        _flush(current_date)

    return entries
