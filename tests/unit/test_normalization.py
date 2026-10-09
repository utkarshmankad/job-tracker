"""Tests for backend/engine/normalization.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.engine.normalization import (
    canonical_job_url,
    evidence_fingerprint,
    normalize_company,
    normalize_email_address,
    normalize_role,
    normalize_subject,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0002 = REPO_ROOT / "backend/db/alembic/versions/0002_evidence_model.py"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Technologies Pvt. Ltd.", "acme technologies"),  # only legal suffixes go
        ("  GLOBEX, Inc ", "globex"),
        ("Initech LLC", "initech"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_company(raw, expected) -> None:
    assert normalize_company(raw) == expected


def test_normalize_role() -> None:
    assert (
        normalize_role("Senior  Software-Engineer (Backend)") == "senior software engineer backend"
    )
    assert normalize_role(None) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Re: RE: Fwd: Interview  Schedule", "interview schedule"),
        ("FW[2]: Next steps", "next steps"),
        ("Your Application to ACME", "your application to acme"),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_subject(raw, expected) -> None:
    assert normalize_subject(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Recruiting <Jobs@Acme.COM>", "jobs@acme.com"),
        ("jobs@acme.com", "jobs@acme.com"),
        ("No Address", "no address"),
        (None, ""),
    ],
)
def test_normalize_email_address(raw, expected) -> None:
    assert normalize_email_address(raw) == expected


def test_canonical_job_url() -> None:
    assert (
        canonical_job_url("HTTP://www.Jobs.Example.com/view/123/?utm_source=x&b=2&a=1&ref=y#frag")
        == "https://jobs.example.com/view/123?a=1&b=2"
    )
    assert canonical_job_url("  ") is None
    assert canonical_job_url(None) is None


# ------------------------------------------------------------------ #
# Fingerprints                                                         #
# ------------------------------------------------------------------ #

_WHEN = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)


def test_external_id_fingerprint_ignores_details() -> None:
    a = evidence_fingerprint(evidence_type="email", source="gmail", external_id="m1")
    b = evidence_fingerprint(
        evidence_type="email",
        source="gmail",
        external_id="m1",
        sender="x@y.z",
        subject="anything",
        occurred_at=_WHEN,
    )
    assert a == b
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)
    assert a != evidence_fingerprint(evidence_type="email", source="gmail", external_id="m2")
    assert a != evidence_fingerprint(evidence_type="email", source="linkedin", external_id="m1")
    assert a != evidence_fingerprint(evidence_type="manual", source="gmail", external_id="m1")


def _fields(**overrides):
    values = dict(
        evidence_type="portal_import",
        source="naukri",
        external_id=None,
        thread_id=None,
        sender="Naukri <alerts@naukri.com>",
        recipient=None,
        subject="Application received",
        occurred_at=_WHEN,
        snippet="Thanks  for applying",
    )
    values.update(overrides)
    return evidence_fingerprint(**values)


def test_field_fingerprint_is_stable_under_normalization() -> None:
    base = _fields()
    assert base == _fields(sender="ALERTS@naukri.com")
    assert base == _fields(subject="RE: application  RECEIVED")
    assert base == _fields(snippet="Thanks for   applying")
    assert base == _fields(occurred_at=_WHEN.replace(microsecond=999))
    assert base == _fields(occurred_at=_WHEN.astimezone(timezone(timedelta(hours=5, minutes=30))))
    assert base == _fields(occurred_at=_WHEN.replace(tzinfo=None))  # naive = UTC


@pytest.mark.parametrize(
    "change",
    [
        {"thread_id": "t1"},
        {"sender": "other@naukri.com"},
        {"recipient": "me@example.com"},
        {"subject": "Application viewed"},
        {"occurred_at": _WHEN + timedelta(seconds=1)},
        {"snippet": "Different"},
        {"source": "indeed"},
    ],
)
def test_field_fingerprint_changes_with_each_field(change) -> None:
    assert _fields() != _fields(**change)


def test_fields_cannot_bleed_into_each_other() -> None:
    assert _fields(thread_id="a", subject="b") != _fields(thread_id="a b", subject=None)


def test_fingerprint_is_identical_across_processes() -> None:
    """Python's hash() is salted per process; the fingerprint must not be."""
    code = (
        "from datetime import datetime, UTC;"
        "from backend.engine.normalization import evidence_fingerprint as f;"
        "print(f(evidence_type='email', source='gmail', external_id=None, thread_id='t',"
        " sender='a@b.c', subject='Hi', occurred_at=datetime(2026,5,1,tzinfo=UTC), snippet='s'))"
    )
    outputs = set()
    for seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=60,
            check=True,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1
    expected = evidence_fingerprint(
        evidence_type="email",
        source="gmail",
        external_id=None,
        thread_id="t",
        sender="a@b.c",
        subject="Hi",
        occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
        snippet="s",
    )
    assert outputs == {expected}


def test_migration_frozen_copies_match_runtime() -> None:
    """0002 must not import app code, so it carries copies; they must stay identical."""
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION_0002)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for message_id in ("18f0a1b2c3d4e5f6", " padded ", "x"):
        assert module._fingerprint_external("email", "gmail", message_id) == evidence_fingerprint(
            evidence_type="email", source="gmail", external_id=message_id
        )
    # 0002 stored normalized_subject with the v1 rules; they still agree on plain ASCII
    # subjects (later rules only add Unicode folding and [EXTERNAL] stripping).
    for subject in ("Re: Hello  World", "FW[3]: x", "", None, "Plain"):
        assert module._normalize_subject(subject) == normalize_subject(subject)


# ------------------------------------------------------------------ #
# Resolver v2 helpers                                                  #
# ------------------------------------------------------------------ #

from backend.engine.normalization import (  # noqa: E402
    company_domain,
    external_job_id_from_url,
    is_forwarded_subject,
    is_vendor_domain,
    normalize_external_job_id,
    normalize_source,
    normalize_thread_id,
    personal_sender_address,
    registrable_domain,
    sender_domain,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Société Générale SA", "SOCIETE GENERALE"),
        ("ＡＣＭＥ Corp", "Acme Corporation"),
        ("Acme Pvt. Ltd.", "ACME PRIVATE LIMITED"),
        ("Infosys Limited", "infosys"),
    ],
)
def test_company_variants_that_must_agree(left, right) -> None:
    assert normalize_company(left) == normalize_company(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Unity Technologies", "Unity"),
        ("Acme Labs", "Acme"),
        ("Tata Consultancy Services", "Tata Motors"),
        ("Corporate Bank", "Bank"),
        ("General Motors", "General Mills"),
    ],
)
def test_distinct_companies_stay_distinct(left, right) -> None:
    assert normalize_company(left) != normalize_company(right)


def test_suffix_only_names_are_not_emptied() -> None:
    assert normalize_company("Limited") == "limited"
    assert normalize_company("Inc") == "inc"


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        ("Sr. Software Engineer", "Senior Software Engineer", True),
        ("Jr Analyst", "junior analyst", True),
        ("Data Engineer", "Platform Engineer", False),
        ("Engineering Manager", "Engineer", False),
    ],
)
def test_role_normalization(left, right, equal) -> None:
    assert (normalize_role(left) == normalize_role(right)) is equal


def test_subject_unicode_and_tags() -> None:
    assert normalize_subject("[EXTERNAL] RE：Interview") == "interview"
    assert is_forwarded_subject("Fwd: x") and is_forwarded_subject("[EXTERNAL] FW: x")
    assert not is_forwarded_subject("Re: x")


def test_domains_and_senders() -> None:
    assert sender_domain("Jane <Jane@Mail.ACME.co.in>") == "mail.acme.co.in"
    assert registrable_domain("mail.acme.co.in") == "acme.co.in"
    assert registrable_domain("careers.acme.com") == "acme.com"
    assert is_vendor_domain("us.greenhouse-mail.io") and is_vendor_domain("hire.lever.co")
    assert company_domain("x@mail.greenhouse.io") is None
    assert company_domain("x@gmail.com") is None
    assert company_domain("Jane <jane@careers.acme.com>") == "acme.com"
    assert personal_sender_address("Jane Doe <Jane.Doe@acme.com>") == "jane.doe@acme.com"
    for shared in (
        "noreply@acme.com",
        "careers@acme.com",
        "talent+x@acme.com",
        "a@linkedin.com",
        "me@gmail.com",
    ):
        assert personal_sender_address(shared) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/jobs/view/3912345678/?trk=x", ("linkedin", "3912345678")),
        (
            "https://www.linkedin.com/jobs/collections/?currentJobId=3912345678",
            ("linkedin", "3912345678"),
        ),
        (
            "https://www.naukri.com/job-listings-sre-acme-bengaluru-061024012345",
            ("naukri", "061024012345"),
        ),
        ("https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b", ("indeed", "0a1b2c3d4e5f6a7b")),
        ("https://boards.greenhouse.io/acme/jobs/4567890", ("company_portal", "4567890")),
        ("https://acme.com/careers?gh_jid=4567890", ("company_portal", "4567890")),
        (
            "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
            ("company_portal", "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"),
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/Careers/job/Pune/SRE_R12345",
            ("company_portal", "r12345"),
        ),
        ("https://example.com/about", None),
        (None, None),
    ],
)
def test_external_job_id_from_url(url, expected) -> None:
    assert external_job_id_from_url(url) == expected


def test_identifier_normalizers() -> None:
    assert normalize_external_job_id("  Job ID: #R-123 ") == "r-123"
    assert normalize_external_job_id("") is None
    assert normalize_thread_id(" 18F0ABC ") == "18f0abc"
    assert normalize_thread_id("") is None
    assert canonical_job_url("https://jobs.example.com/1?gh_src=abc&lever-source=x") == (
        "https://jobs.example.com/1"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LinkedIn", "linkedin"),
        ("Naukri", "naukri"),
        ("Instahire", "instahyre"),
        ("Direct/Consultancy", "company_portal"),
        ("Greenhouse", "company_portal"),
        ("Direct/Unknown", "other"),
        ("Something New", "other"),
        (None, "other"),
    ],
)
def test_normalize_source(raw, expected) -> None:
    assert normalize_source(raw) == expected
