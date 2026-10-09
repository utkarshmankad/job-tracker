"""Normalization, identifier extraction and fingerprints for identity resolution.

Pure, deterministic functions with no database or network access; the single source of
truth for the stored identity columns, the resolver and the duplicate detector.

Rules are deliberately conservative: removing a token is only allowed when it cannot turn
two different companies or roles into the same value. Company names lose *trailing legal
entity designators* only (``Acme Pvt Ltd`` → ``acme``); words that are part of a name, such
as "Technologies", "Labs" or "Group", are kept.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NORMALIZATION_VERSION = "2"
FINGERPRINT_VERSION = "evidence-v1"
_SEPARATOR = "\x1f"  # ASCII unit separator: cannot occur in normalized text fields

# Trailing legal-entity designators, removed only at the end of a name and never when that
# would leave nothing. Multi-word forms are matched before their parts.
_LEGAL_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("private", "limited"),
    ("pvt", "ltd"),
    ("pte", "ltd"),
    ("pty", "ltd"),
    ("co", "ltd"),
    ("limited",),
    ("ltd",),
    ("private",),
    ("pvt",),
    ("incorporated",),
    ("inc",),
    ("llc",),
    ("llp",),
    ("plc",),
    ("corporation",),
    ("corp",),
    ("gmbh",),
    ("ag",),
    ("bv",),
    ("sa",),
    ("sarl",),
    ("oy",),
    ("ab",),
    ("kk",),
)
_ROLE_ABBREVIATIONS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "mgr": "manager",
    "engg": "engineering",
}
_REPLY_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|aw|tr|wg|sv|vs)\s*(\[\d+\])?\s*[:：]\s*|\[external\]\s*)+", re.IGNORECASE
)
_FORWARD_PREFIX = re.compile(
    r"^\s*(\[external\]\s*)?(fw|fwd|wg|tr)\s*(\[\d+\])?\s*[:：]", re.IGNORECASE
)
_EMAIL_IN_ANGLE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_BARE_EMAIL = re.compile(r"([^\s<>@,;\"']+@[^\s<>@,;\"']+)")

# Query parameters that only track a visit and never identify a job.
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "trk",
        "trkinfo",
        "trackingid",
        "refid",
        "lipi",
        "src",
        "source",
        "ref",
        "referrer",
        "_hsenc",
        "_hsmi",
        "si",
        "gh_src",
        "lever-source",
        "lever-origin",
    }
)

# Map portal names (portal_rules.yaml) and evidence sources to one canonical source key.
_SOURCE_ALIASES = {
    "gmail": "gmail",
    "linkedin": "linkedin",
    "naukri": "naukri",
    "indeed": "indeed",
    "instahyre": "instahyre",
    "instahire": "instahyre",
    "careernet": "careernet",
    "hirist": "hirist",
    "iimjobs": "iimjobs",
    "wellfound": "wellfound",
    "angellist": "wellfound",
    "greenhouse": "company_portal",
    "lever": "company_portal",
    "workday": "company_portal",
    "ashby": "company_portal",
    "smartrecruiters": "company_portal",
    "icims": "company_portal",
    "company_portal": "company_portal",
    "company portal": "company_portal",
    "direct consultancy": "company_portal",
    "direct unknown": "other",
    "unknown": "other",
}

# Sender domains that host many companies' mail (job boards, ATS vendors, schedulers) —
# never evidence of *which* company. Matched on the registrable domain.
VENDOR_DOMAINS = frozenset(
    {
        "linkedin.com",
        "naukri.com",
        "indeed.com",
        "indeedemail.com",
        "instahyre.com",
        "hirist.com",
        "hirist.tech",
        "iimjobs.com",
        "careernet.in",
        "wellfound.com",
        "angel.co",
        "greenhouse.io",
        "greenhouse-mail.io",
        "mygreenhouseapp.com",
        "lever.co",
        "hire.lever.co",
        "myworkday.com",
        "workday.com",
        "myworkdayjobs.com",
        "ashbyhq.com",
        "smartrecruiters.com",
        "icims.com",
        "taleo.net",
        "successfactors.com",
        "jobvite.com",
        "bamboohr.com",
        "recruitee.com",
        "workable.com",
        "teamtailor.com",
        "zohorecruit.com",
        "calendly.com",
        "hackerrank.com",
        "hackerearth.com",
        "codility.com",
        "mettl.com",
        "testgorilla.com",
    }
)
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.in",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "rediffmail.com",
        "aol.com",
    }
)
# Second-level public suffixes common in this data set (company.co.in → company.co.in).
_TWO_LABEL_SUFFIXES = frozenset(
    {"co.in", "co.uk", "com.au", "co.jp", "com.br", "com.sg", "co.nz", "net.in", "org.in", "ac.in"}
)
_SHARED_MAILBOX = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|alerts?|mailer|bounce|jobs-noreply|"
    r"careers?-?noreply|messages-noreply|inmail-hit-reply|hit-reply|updates?|news(letter)?|"
    r"talent|recruiting|recruitment|careers?|jobs?|hr|hiring|team|info|support)([+._-].*)?$"
)

# Job IDs recoverable from well-known job URLs: (source key, host pattern, path/query regex).
_JOB_ID_PATTERNS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    (
        "linkedin",
        re.compile(r"(^|\.)linkedin\.com$"),
        re.compile(r"/jobs/view/(?:[^/]*-)?(\d{6,})"),
    ),
    ("linkedin", re.compile(r"(^|\.)linkedin\.com$"), re.compile(r"[?&]currentjobid=(\d{6,})")),
    ("naukri", re.compile(r"(^|\.)naukri\.com$"), re.compile(r"-(\d{9,14})(?:[/?#]|$)")),
    ("indeed", re.compile(r"(^|\.)indeed\.com$"), re.compile(r"[?&]jk=([0-9a-f]{12,20})")),
    ("company_portal", re.compile(r"greenhouse\.io$"), re.compile(r"/jobs/(\d{5,})")),
    ("company_portal", re.compile(r".*"), re.compile(r"[?&]gh_jid=(\d{5,})")),
    (
        "company_portal",
        re.compile(r"(^|\.)lever\.co$"),
        re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    ),
    (
        "company_portal",
        re.compile(r"myworkdayjobs\.com$"),
        re.compile(r"_((?:jr|r|req)[-_]?\d{3,})(?:[/?#]|$)"),
    ),
)


def _fold(value: str | None) -> str:
    """NFKC, case-fold and strip combining accents: 'Société' and 'SOCIETE' agree."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value).casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _tokens(value: str | None) -> list[str]:
    text = _fold(value).replace("&", " and ")
    return re.sub(r"[^\w]+|_", " ", text).split()


def normalize_company(value: str | None) -> str:
    """Folded, punctuation-free, trailing legal designators removed ("" when empty)."""
    tokens = _tokens(value)
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if len(tokens) > len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                tokens = tokens[: -len(suffix)]
                changed = True
                break
    return " ".join(tokens)


def normalize_role(value: str | None) -> str:
    """Folded, punctuation-free, a few unambiguous abbreviations expanded ("" if empty)."""
    return " ".join(_ROLE_ABBREVIATIONS.get(t, t) for t in _tokens(value))


def normalize_subject(value: str | None) -> str | None:
    """Reply/forward/[EXTERNAL] prefixes stripped, folded, whitespace collapsed."""
    if not value:
        return None
    stripped = _REPLY_PREFIX.sub("", unicodedata.normalize("NFKC", value))
    text = " ".join(_fold(stripped).split())
    return text or None


def is_forwarded_subject(value: str | None) -> bool:
    return bool(value and _FORWARD_PREFIX.search(unicodedata.normalize("NFKC", value)))


def normalize_email_address(value: str | None) -> str:
    """The address part of 'Name <user@host>' or a bare address, folded ("" if none)."""
    if not value:
        return ""
    match = _EMAIL_IN_ANGLE.search(value) or _BARE_EMAIL.search(value)
    return _fold(match.group(1)) if match else " ".join(_fold(value).split())


def sender_domain(value: str | None) -> str:
    """Host part of the sender address, folded, without a leading 'www.' ("" if none)."""
    address = normalize_email_address(value)
    if "@" not in address:
        return ""
    host = address.rsplit("@", 1)[1].strip(".")
    return host[4:] if host.startswith("www.") else host


def registrable_domain(domain: str) -> str:
    """Last two labels (three for common two-label public suffixes such as co.in)."""
    labels = [label for label in domain.split(".") if label]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_vendor_domain(domain: str) -> bool:
    if not domain:
        return False
    return domain in VENDOR_DOMAINS or registrable_domain(domain) in VENDOR_DOMAINS


def is_freemail_domain(domain: str) -> bool:
    if not domain:
        return False
    return domain in FREEMAIL_DOMAINS or registrable_domain(domain) in FREEMAIL_DOMAINS


def personal_sender_address(value: str | None) -> str | None:
    """The sender address when it plausibly belongs to one person at one company: not a
    shared/no-reply mailbox, not a job board or ATS, not a free-mail account."""
    address = normalize_email_address(value)
    if "@" not in address:
        return None
    local, domain = address.rsplit("@", 1)
    if _SHARED_MAILBOX.match(local) or is_vendor_domain(domain) or is_freemail_domain(domain):
        return None
    return address


def company_domain(value: str | None) -> str | None:
    """Registrable sender domain when it can identify an employer (not vendor/free-mail)."""
    domain = sender_domain(value)
    if not domain or is_vendor_domain(domain) or is_freemail_domain(domain):
        return None
    return registrable_domain(domain)


def canonical_job_url(value: str | None) -> str | None:
    """https, host folded and without 'www.', trailing slash, fragment and tracking
    parameters dropped, remaining parameters sorted."""
    if not value or not value.strip():
        return None
    raw = unicodedata.normalize("NFKC", value.strip())
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    if not parts.netloc:
        return raw.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    return urlunsplit((scheme, host, parts.path.rstrip("/"), urlencode(kept), ""))


def normalize_external_job_id(value: str | None) -> str | None:
    """Trimmed, folded, leading '#' and 'id:' removed (None when empty)."""
    if not value:
        return None
    text = _fold(value).strip()
    text = re.sub(r"^(job\s*id|req(uisition)?\s*(id)?|id)\s*[:#]?\s*", "", text)
    text = text.lstrip("#").strip()
    return text or None


def external_job_id_from_url(value: str | None) -> tuple[str, str] | None:
    """(source key, job ID) when a well-known job URL encodes one."""
    url = canonical_job_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    target = f"{parts.path}?{parts.query}".lower()
    for source, host_pattern, id_pattern in _JOB_ID_PATTERNS:
        if host_pattern.search(parts.netloc):
            match = id_pattern.search(target)
            if match:
                job_id = normalize_external_job_id(match.group(1))
                if job_id:
                    return source, job_id
    return None


def normalize_thread_id(value: str | None) -> str | None:
    """Gmail thread IDs are hexadecimal; trimmed and lower-cased (None when empty)."""
    text = (value or "").strip().lower()
    return text or None


def normalize_source(value: str | None) -> str:
    """Canonical source key for a portal name or evidence source ('other' if unknown)."""
    key = " ".join(re.sub(r"[^a-z0-9]+", " ", _fold(value)).split())
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    compact = key.replace(" ", "")
    return _SOURCE_ALIASES.get(compact, "other")


def _utc_second(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def evidence_fingerprint(
    *,
    evidence_type: str,
    source: str,
    external_id: str | None,
    thread_id: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    subject: str | None = None,
    occurred_at: datetime | None = None,
    snippet: str | None = None,
) -> str:
    """Deterministic SHA-256 identity of one piece of evidence (format ``evidence-v1``,
    unchanged since revision 0002 so stored fingerprints stay valid).

    With an external ID the fingerprint depends only on (type, source, external ID).
    Without one it is derived from stable normalized fields. Never uses hash().
    """
    if external_id:
        parts = [FINGERPRINT_VERSION, evidence_type, source, "ext", external_id.strip()]
    else:
        parts = [
            FINGERPRINT_VERSION,
            evidence_type,
            source,
            "fields",
            (thread_id or "").strip(),
            _fingerprint_address(sender),
            _fingerprint_address(recipient),
            _fingerprint_subject(subject),
            _utc_second(occurred_at) if occurred_at else "",
            " ".join((snippet or "").split()),
        ]
    return hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


# The evidence-v1 fingerprint is frozen: it must keep using the normalization that was in
# force when revision 0002 shipped, independent of later improvements above.
_V1_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def _fingerprint_subject(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(_V1_REPLY_PREFIX.sub("", value).casefold().split())


def _fingerprint_address(value: str | None) -> str:
    if not value:
        return ""
    match = _EMAIL_IN_ANGLE.search(value) or _BARE_EMAIL.search(value)
    return match.group(1).lower() if match else " ".join(value.lower().split())
