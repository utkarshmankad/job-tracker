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
        ("Acme Technologies Pvt. Ltd.", "acme"),
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
        canonical_job_url("HTTPS://Jobs.Example.com/view/123/?utm_source=x&ref=y#frag")
        == "https://jobs.example.com/view/123?ref=y"
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
    for subject in ("Re: Hello  World", "FW[3]: x", "", None, "Plain"):
        assert module._normalize_subject(subject) == normalize_subject(subject)
