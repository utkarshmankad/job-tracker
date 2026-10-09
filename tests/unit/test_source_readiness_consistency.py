"""Source readiness must read the same on every operational surface.

The server catalog (backend/collection/readiness.py), the local collector's adapters
(collector/adapters/sites.py) and the readiness tables in the operations docs each say
which sources are live-verified and which are unsupported. These tests fail when one
surface presents a source as verified and another as unsupported, or when stale
"nothing is verified" / "run LinkedIn" guidance comes back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import config
from backend.collection.readiness import SOURCE_READINESS, is_supported_scope
from collector.adapters import ADAPTERS, UnsupportedSource, adapter_for

ROOT = Path(__file__).resolve().parents[2]
READINESS_DOCS = ("docs/collector-operations.md", "docs/phase-3-source-collection.md")
OPERATIONAL_TEXT = (
    "README.md",
    "CLAUDE.md",
    *READINESS_DOCS,
    "collector/cli.py",
    "collector/config.py",
    "collector/adapters/sites.py",
    "launchd/com.jobtracker.collector.plist.template",
    "tests/unit/test_collector_adapters.py",
)
STALE_PHRASES = (
    re.compile(r"no adapter is live-verified", re.I),
    re.compile(r"none of the \w+ adapters is live-verified", re.I),
    re.compile(r"every adapter is LIVE_VERIFIED\s*=\s*None", re.I),
    re.compile(r"--source\s+(linkedin|naukri|instahyre|careernet)\b", re.I),
    # a TOML config example enabling an unsupported source
    re.compile(r'^\s*source_key\s*=\s*"(linkedin|naukri|instahyre|careernet)"', re.M),
)


def _doc_table(path: str) -> dict[str, str]:
    """{source_key: readiness cell} from the table that follows the readiness marker."""
    text = (ROOT / path).read_text()
    marker = text.index("readiness-table")
    rows: dict[str, str] = {}
    for line in text[marker:].splitlines():
        match = re.match(r"\|\s*`([a-z]+)`[^|]*\|([^|]*)\|", line)
        if match:
            rows[match.group(1)] = match.group(2).strip().lower()
        elif rows and not line.startswith("|"):
            break
    return rows


def test_catalog_covers_every_known_source_and_adapter() -> None:
    assert set(SOURCE_READINESS) == set(config.COLLECTOR_SOURCES) == set(ADAPTERS)


@pytest.mark.parametrize("key", sorted(SOURCE_READINESS))
def test_adapter_agrees_with_server_catalog(key: str) -> None:
    entry, adapter = SOURCE_READINESS[key], ADAPTERS[key]
    assert adapter.SUPPORTED is entry.supported
    assert adapter.LIVE_VERIFIED == entry.live_verified
    assert (adapter.UNSUPPORTED_REASON or None) == entry.reason
    if entry.supported:
        assert entry.live_verified, "a supported site source must record its live verification"
        assert adapter_for(key).SOURCE_KEY == key
    else:
        assert entry.reason and entry.live_verified is None
        with pytest.raises(UnsupportedSource):
            adapter_for(key)
    assert is_supported_scope(key) is entry.supported


@pytest.mark.parametrize("path", READINESS_DOCS)
def test_doc_readiness_table_agrees_with_catalog(path: str) -> None:
    table = _doc_table(path)
    for key, entry in SOURCE_READINESS.items():
        assert key in table, f"{path} readiness table is missing {key}"
        cell = table[key]
        if entry.supported:
            assert f"live verified {entry.live_verified}" in cell, (path, key)
            assert "unsupported" not in cell, (path, key)
        else:
            assert "unsupported" in cell, (path, key)
            assert "verified" not in cell, (path, key)


def test_only_indeed_is_ready() -> None:
    ready = sorted(k for k, e in SOURCE_READINESS.items() if e.supported)
    assert ready == ["indeed"]
    assert SOURCE_READINESS["indeed"].live_verified == "2026-10-09"


@pytest.mark.parametrize("path", OPERATIONAL_TEXT)
def test_no_stale_readiness_guidance(path: str) -> None:
    text = (ROOT / path).read_text()
    for phrase in STALE_PHRASES:
        assert not phrase.search(text), f"{path}: stale guidance matches {phrase.pattern!r}"


def test_employer_scopes_remain_allowed_but_unknown_keys_do_not() -> None:
    assert is_supported_scope("employer-acme")
    assert not is_supported_scope("monster")
