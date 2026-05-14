"""Email field extraction logic."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import spacy
import yaml

from backend.config import PORTAL_RULES_PATH
from backend.db.models import ApplicationStatus, SuppressRule
from backend.parser.status_signals import GLOBAL_STATUS_KEYWORDS

_SIGNAL_TO_STATUS: dict[str, ApplicationStatus] = {
    "shortlisted": ApplicationStatus.RESUME_SHORTLISTED,
    "interview_scheduled": ApplicationStatus.INTERVIEW_SCHEDULED,
    "interview_in_progress": ApplicationStatus.INTERVIEW_IN_PROGRESS,
    "offer_negotiation": ApplicationStatus.OFFER_NEGOTIATION,
    "offer": ApplicationStatus.OFFER,
    "rejected": ApplicationStatus.REJECTED,
}


@dataclass
class RawEmail:
    message_id: str
    thread_id: str
    sender: str       # full "Name <email@domain.com>" string
    subject: str
    date: datetime
    snippet: str      # Gmail snippet, max 100 chars
    body_text: str | None  # set to None after parsing


@dataclass
class ParsedApplication:
    message_id: str
    thread_id: str
    company: str | None
    role: str | None
    source_portal: str
    job_url: str | None
    applied_date: datetime
    status_signal: ApplicationStatus | None
    raw_sender: str
    raw_subject: str
    is_classification_confident: bool  # False for Direct/Unknown matches


class EmailParser:
    def __init__(self, rules_path: Path = PORTAL_RULES_PATH) -> None:
        with open(rules_path) as f:
            data = yaml.safe_load(f)
        self._portals: list[dict[str, Any]] = data.get("portals", [])
        self._nlp = spacy.load("en_core_web_sm")

    def parse(self, email: RawEmail, suppress_rules: list[SuppressRule]) -> ParsedApplication | None:
        if self._matches_suppress_rule(email.sender, email.subject, suppress_rules):
            email.body_text = None
            return None

        sender_domain = self._extract_sender_domain(email.sender)
        subject_lower = email.subject.lower()

        matched_portal: dict[str, Any] | None = None
        is_confident = True

        # Try domain match; require subject_patterns OR status_signal keyword to also match.
        # This prevents Naukri newsletter domains from matching non-job emails.
        for portal in self._portals:
            if portal.get("name") == "Direct/Unknown":
                continue
            domain_matched = False
            for domain in portal.get("sender_domains", []):
                # Handle entries like "jobs-noreply@linkedin.com" (full address in domain list)
                if "@" in domain:
                    if domain.lower() in email.sender.lower():
                        domain_matched = True
                        break
                elif sender_domain == domain.lower():
                    domain_matched = True
                    break
            if domain_matched and self._subject_or_signal_matches(subject_lower, portal):
                matched_portal = portal
                break

        # No domain match → check Direct/Unknown via subject_patterns OR status_signal keywords.
        # Unknown senders that mention interview/offer/rejection signal are still job emails.
        if not matched_portal:
            direct_portal = next((p for p in self._portals if p.get("name") == "Direct/Unknown"), None)
            if direct_portal and self._subject_or_signal_matches(subject_lower, direct_portal):
                matched_portal = direct_portal
                is_confident = False

        if not matched_portal:
            email.body_text = None
            return None

        portal_name = matched_portal["name"]
        portal_signals = matched_portal.get("status_signals", {})

        status_signal = self._detect_status_signal(
            email.subject, email.snippet, portal_name, portal_signals
        )

        snippet_for_extraction = email.snippet or ""
        body_text_for_extraction = email.body_text or ""
        combined_snippet = f"{snippet_for_extraction} {body_text_for_extraction}".strip()

        company = self._extract_company(email.subject, combined_snippet, portal_name)
        role = self._extract_role(email.subject)
        job_url = self._extract_job_url(combined_snippet)

        email.body_text = None

        return ParsedApplication(
            message_id=email.message_id,
            thread_id=email.thread_id,
            company=company,
            role=role,
            source_portal=portal_name,
            job_url=job_url,
            applied_date=email.date,
            status_signal=status_signal,
            raw_sender=email.sender,
            raw_subject=email.subject,
            is_classification_confident=is_confident,
        )

    def _subject_or_signal_matches(self, subject_lower: str, portal: dict[str, Any]) -> bool:
        """Returns True if subject matches any subject_pattern or any status_signal keyword."""
        for pattern in portal.get("subject_patterns", []):
            if pattern.lower() in subject_lower:
                return True
        for keywords in portal.get("status_signals", {}).values():
            for kw in keywords:
                if kw.lower() in subject_lower:
                    return True
        return False

    def _extract_sender_domain(self, sender: str) -> str:
        # Match "Name <user@domain.com>" or "user@domain.com"
        match = re.search(r"<[^@]+@([^>]+)>", sender)
        if match:
            return match.group(1).lower()
        match = re.search(r"[^@\s]+@([^\s>]+)", sender)
        if match:
            return match.group(1).lower()
        return ""

    def _extract_company(self, subject: str, body_snippet: str, portal_name: str) -> str | None:
        text = f"{subject} {body_snippet}"

        # spacy ORG entities
        doc = self._nlp(text)
        orgs = [ent.text.strip() for ent in doc.ents if ent.label_ == "ORG"]
        if orgs:
            return orgs[0]

        # Regex fallbacks on subject
        patterns = [
            r"(?:your application to|applied to|application to)\s+([A-Z][A-Za-z0-9 &,.']+?)(?:\s+(?:for|is|has|-)|\s*$)",
            r"(?:at|from)\s+([A-Z][A-Za-z0-9 &,.']+?)(?:\s+(?:for|is|has|-)|\s*$)",
            r"^([A-Z][A-Za-z0-9 &,.']+?)\s*[-–—]\s*(?:Job Application|Application)",
        ]
        for pattern in patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        return None

    def _extract_role(self, subject: str) -> str | None:
        patterns = [
            r"for the role of\s+(.+?)(?:\s+at\s+|\s*$)",
            r"for\s+(.+?)\s+position",
            r"^(.+?)\s*[-–—]\s*Application",
            r"application for\s+(.+?)(?:\s+at\s+|\s+position|\s*$)",
            r"applied for\s+(?:the\s+)?(?:role of\s+)?(.+?)(?:\s+at\s+|\s*$)",
            r"applied for\s+(.+?)(?:\s+at\s+|\s*$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                role = match.group(1).strip()
                if len(role) > 2:
                    return role
        return None

    def _extract_job_url(self, snippet: str) -> str | None:
        matches = re.findall(r"https?://[^\s\"'>]+", snippet)
        keywords = {"job", "position", "career", "opening", "role"}
        portal_domains = {
            "naukri.com", "linkedin.com", "hirest.tech", "instahire.in",
            "wellfound.com", "angel.co", "instahyre.com",
        }
        for url in matches:
            url_lower = url.lower()
            if any(kw in url_lower for kw in keywords):
                return url
            if any(domain in url_lower for domain in portal_domains):
                return url
        return None

    def _detect_status_signal(
        self,
        subject: str,
        snippet: str,
        portal_name: str,
        portal_signals: dict[str, list[str]],
    ) -> ApplicationStatus | None:
        combined = f"{subject} {snippet}".lower()

        # Check portal-specific signals first
        for signal_key, keywords in portal_signals.items():
            status = _SIGNAL_TO_STATUS.get(signal_key)
            if status is None:
                continue
            for kw in keywords:
                if kw.lower() in combined:
                    return status

        # Fall back to global keywords
        for status, keywords in GLOBAL_STATUS_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in combined:
                    return status

        return None

    def _matches_suppress_rule(
        self, sender: str, subject: str, rules: list[SuppressRule]
    ) -> bool:
        sender_lower = sender.lower()
        subject_lower = subject.lower()
        for rule in rules:
            pattern = rule.sender_pattern.lower()
            if re.search(pattern, sender_lower):
                if rule.subject_pattern is None:
                    return True
                if re.search(rule.subject_pattern.lower(), subject_lower):
                    return True
        return False
