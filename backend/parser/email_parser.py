"""Email field extraction logic."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import spacy
import structlog
import yaml

from backend.config import PORTAL_RULES_PATH
from backend.db.models import ApplicationStatus, SuppressRule
from backend.parser.status_signals import GLOBAL_STATUS_KEYWORDS

log = structlog.get_logger(__name__)

def extract_sender_domain(sender: str) -> str:
    """Extract the domain portion from a 'Name <user@domain.com>' or 'user@domain.com' string."""
    match = re.search(r"<[^@]+@([^>]+)>", sender)
    if match:
        return match.group(1).lower()
    match = re.search(r"[^@\s]+@([^\s>]+)", sender)
    if match:
        return match.group(1).lower()
    return ""


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

        sender_domain = extract_sender_domain(email.sender)
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
                # Suppress job-count digest emails ("applied for 2 jobs today") which match
                # subject_patterns but are not individual job applications.
                if re.search(r"applied for \d+ job", email.subject, re.IGNORECASE):
                    break
                matched_portal = portal
                break

        # No domain match → check Direct/Unknown via subject_patterns ONLY.
        # Status-signal keywords alone (e.g. "shortlisted") are too broad without a domain
        # anchor and cause certification-program marketing emails to be classified as job apps.
        if not matched_portal:
            direct_portal = next((p for p in self._portals if p.get("name") == "Direct/Unknown"), None)
            if direct_portal:
                subject_hit = any(
                    pat.lower() in subject_lower
                    for pat in direct_portal.get("subject_patterns", [])
                )
                if subject_hit:
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

        company = self._extract_company(email.subject, combined_snippet, portal_name, sender_domain)
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

    def refine_company(self, extra_text: str, portal_name: str, sender_domain: str) -> str | None:
        """Re-run company extraction on arbitrary text (e.g. email body fetched lazily)."""
        return self._extract_company("", extra_text[:3000], portal_name, sender_domain)

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

    # Words that indicate an extracted "company" is actually a program, event, or boilerplate.
    _REJECT_COMPANY_WORDS: frozenset[str] = frozenset([
        "programme", "program", "course", "certification", "certificate",
        "training", "bootcamp", "highlights", "compile", "view",
        "event", "regards", "please", "ctc",
        "application",  # prevents "Meesho Application" type extractions
    ])
    # Names that start with these words are almost never real company names.
    _REJECT_COMPANY_LEADING: frozenset[str] = frozenset([
        "the", "a", "an", "your", "our", "this", "please", "regards",
        "received", "shortlisted", "selected", "confirm",
        # Unambiguous job-title words — companies never start with these
        "director", "manager", "senior", "sr.", "vp", "vice", "chief", "head",
        "lead", "principal", "junior", "jr.", "associate", "staff", "fellow",
    ])
    # Greeting words that precede a person's name in email text and get fused into ORG entity
    _GREETING_PATTERN = re.compile(r"\s*\b(?:hi|hello|dear|hey)\b\s*$", re.IGNORECASE)
    # If an ORG entity ends with one of these, it's most likely a job title, not a company.
    _REJECT_COMPANY_TRAILING: frozenset[str] = frozenset([
        "engineer", "developer", "analyst", "manager", "director",
        "scientist", "architect", "designer", "consultant", "officer",
        "development", "engineering", "technical", "officer",
    ])

    def _is_valid_company(self, name: str) -> bool:
        if not name or len(name) > 50:
            return False
        words = name.split()
        if len(words) > 5:
            return False
        if words[0].lower() in self._REJECT_COMPANY_LEADING:
            return False
        if words[-1].lower() in self._REJECT_COMPANY_TRAILING:
            return False
        name_lower = name.lower()
        return not any(w in name_lower for w in self._REJECT_COMPANY_WORDS)

    def _extract_company(
        self, subject: str, body_snippet: str, portal_name: str, sender_domain: str = ""
    ) -> str | None:
        text = f"{subject} {body_snippet}"

        doc = self._nlp(text)

        # Build a domain-based company hint (e.g. "hilabs.com" → "hilabs") to resolve
        # ORG+person fusions when spacy's English NER misses Indian person names.
        domain_hint = sender_domain.split(".")[0].lower() if sender_domain else ""

        # Index PERSON spans so we can detect company+person fusions (e.g. "HiLabs Ujwala Kudur")
        person_spans = [(e.start_char, e.end_char) for e in doc.ents if e.label_ == "PERSON"]

        for ent in doc.ents:
            if ent.label_ != "ORG":
                continue
            org_text = ent.text.strip()

            # Skip entities with unclosed brackets — spacy sometimes captures partial text.
            if "(" in org_text and ")" not in org_text:
                org_text = org_text.split("(")[0].strip()

            # If sender domain hint matches a word inside a multi-word ORG entity, use just
            # that word (e.g. "HiLabs" from "hilabs.com" within "HiLabs Ujwala Kudur").
            if domain_hint and len(org_text.split()) > 1:
                for word in org_text.split():
                    if word.lower() == domain_hint:
                        org_text = word
                        break

            # If a PERSON span starts inside this ORG span, truncate just before it
            # and strip any trailing greeting word (Hi, Hello, Dear, Hey).
            for p_start, _ in person_spans:
                if ent.start_char < p_start < ent.end_char:
                    org_text = text[ent.start_char:p_start].strip(" -–—,")
                    org_text = self._GREETING_PATTERN.sub("", org_text).strip()
                    break

            if self._is_valid_company(org_text):
                return org_text

        # Regex fallbacks on subject
        patterns = [
            r"(?:your application to|applied to|application to)\s+([A-Z][A-Za-z0-9 &,.']+?)(?:\s+(?:for|is|has|-)|\s*$)",
            r"(?:at|from)\s+([A-Z][A-Za-z0-9 &,.']+?)(?:\s+(?:for|is|has|-)|\s*$)",
            r"^([A-Z][A-Za-z0-9 &,.']+?)\s*[-–—]\s*(?:(?:your\s+)?(?:job )?application)",
            # "Job Offer - Company", "Interview Invitation - Company", "Your Application - Company"
            r"(?:job offer|employment offer|interview invitation|opportunity|your application|application received)\s*[-–—:]\s*([A-Z][A-Za-z0-9 &,.']+?)\s*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if self._is_valid_company(candidate):
                    return candidate

        return None

    _ROLE_LEADING_STRIP = re.compile(
        r"^(?:our|the|a|an|this|your)\s+", re.IGNORECASE
    )

    def _extract_role(self, subject: str) -> str | None:
        patterns = [
            r"for the role of\s+(.+?)(?:\s+at\s+|\s*$)",
            r"for\s+(.+?)\s+position",
            r"^(.+?)\s*[-–—]\s*Application",
            r"application for\s+(.+?)(?:\s+at\s+|\s+position|\s*$)",
            r"applied for the (?:role|position) of\s+(.+?)(?:\s+at\s+|\s*$)",
            r"applied for the (?:role|position)\s+(.+?)(?:\s+at\s+|\s*$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                role = self._ROLE_LEADING_STRIP.sub("", match.group(1).strip())
                # Reject if it looks like a digest ("2 jobs on …") or too short
                if len(role) > 2 and not re.match(r"^\d+\s+job", role, re.IGNORECASE):
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
            try:
                if not re.search(rule.sender_pattern.lower(), sender_lower):
                    continue
                if rule.subject_pattern is None:
                    return True
                if re.search(rule.subject_pattern.lower(), subject_lower):
                    return True
            except re.error:
                log.warning(
                    "invalid_suppress_rule_regex",
                    rule_id=rule.id,
                    sender_pattern=rule.sender_pattern,
                    subject_pattern=rule.subject_pattern,
                )
        return False
