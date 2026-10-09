"""Fixture-contract tests for every collector adapter.

These prove each adapter's behaviour against sanitized synthetic pages: full collection
through its pagination style, stop states, selector drift, status mapping, ID extraction,
URL redaction and contract validity. They do NOT prove the selectors match the live sites
(every adapter is LIVE_VERIFIED=None, asserted below).
"""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from backend.collection.contract import ObservedApplication
from collector.adapters import ADAPTERS, UnsupportedSource, adapter_for
from collector.adapters.base import parse_applied_date
from collector.adapters.employer import EmployerDefinitionError, build_employer_adapter
from collector.adapters.sites import (
    CareerNetAdapter,
    IndeedAdapter,
    InstahyreAdapter,
    LinkedInAdapter,
    NaukriAdapter,
)
from collector.driver import FixtureDriver
from collector.runner import SourceRunner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "collector"
NOW = datetime.now(UTC).replace(microsecond=0)
TODAY = NOW.date()

EXPECTED = {
    "linkedin": {
        "statuses": ["applied", "viewed", "in_review", "applied", "closed"],
        "ids": [str(4100000000 + i) for i in range(5)],
        "applied": [TODAY - timedelta(days=d) for d in (3, 7, 14, 30, 60)],
    },
    "naukri": {
        "statuses": ["applied", "viewed", "rejected", "shortlisted", "closed"],
        "ids": [str(9100000000 + i) for i in range(5)],
        "applied": ["2026-09-12", "2026-09-05", "2026-08-28", "2026-08-20", "2026-08-10"],
    },
    # Indeed's live page shows day and month only ("Applied on Indeed on 2 Oct").
    "indeed": {
        "statuses": ["applied", "viewed", "rejected", "interview"],
        "ids": [f"{0xA1B2C3D40000 + i:012x}" for i in range(4)],
        "applied": [
            parse_applied_date(f"on {d}", TODAY) for d in ("2 Oct", "30 Sep", "22 Sep", "15 Sep")
        ],
        "roles": [
            "Engineering Manager",
            "Senior Backend Engineer",
            "Data Platform Lead",
            "Staff Engineer",
        ],
    },
    "instahyre": {
        "statuses": ["applied", "viewed", "shortlisted", "rejected", "interview"],
        "ids": [f"IH-{5000 + i}" for i in range(5)],
        "applied": ["2026-09-25", "2026-09-18", "2026-09-02", "2026-08-21", "2026-08-11"],
    },
    "careernet": {
        "statuses": ["applied", "viewed", "in_review", "rejected", "closed"],
        "ids": [f"CN-{8000 + i}" for i in range(5)],
        "applied": ["2026-10-01", "2026-09-24", "2026-09-15", "2026-09-03", "2026-08-19"],
    },
}


def _html(source: str, name: str) -> str:
    return (FIXTURES / source / f"{name}.html").read_text()


def _page2_url(adapter) -> str:
    return adapter.HISTORY_URL.rstrip("/") + "/__page2"


def _driver(source: str) -> FixtureDriver:
    adapter = ADAPTERS[source]()
    url, page2 = adapter.HISTORY_URL, _page2_url(adapter)
    if adapter.PAGINATION == "none":
        return FixtureDriver({url: _html(source, "page1")})
    pages = {url: _html(source, "page1"), page2: _html(source, "page2")}
    if adapter.PAGINATION == "scroll":
        return FixtureDriver({url: pages[url]}, scrolls={url: [pages[page2]]})
    selector = adapter.NEXT_SELECTOR if adapter.PAGINATION == "next" else adapter.LOAD_MORE_SELECTOR
    return FixtureDriver(pages, clicks={(url, selector): page2})


def _run(adapter, driver):
    runner = SourceRunner(adapter, driver, clock=lambda: NOW, rng=random.Random(3))
    return runner.run(dry_run=True)


@pytest.mark.parametrize("source", sorted(EXPECTED))
def test_full_history_collection(source: str) -> None:
    adapter = ADAPTERS[source]()
    driver = _driver(source)
    outcome = _run(adapter, driver)
    expected = EXPECTED[source]
    assert outcome.status == "succeeded", outcome.summary()
    observations = outcome.observations
    assert [o.status.value for o in observations] == expected["statuses"]
    assert [o.source_item_id for o in observations] == expected["ids"]
    assert [o.applied_on.isoformat() for o in observations] == [
        d if isinstance(d, str) else d.isoformat() for d in expected["applied"]
    ]
    assert len({o.item_identity()[0] for o in observations}) == len(expected["statuses"])
    if "roles" in expected:
        assert [o.role for o in observations] == expected["roles"]
    assert all(o.extraction == adapter.extraction_label("primary") for o in observations)
    assert all(o.proves_submission for o in observations)
    # Lazily loaded pages repeat earlier rows; they are de-duplicated, not double-counted.
    if adapter.PAGINATION in ("load_more", "scroll"):
        assert outcome.diagnostics.duplicates_in_run == 3


@pytest.mark.parametrize("source", sorted(EXPECTED))
def test_tracking_parameters_never_survive(source: str) -> None:
    outcome = _run(ADAPTERS[source](), _driver(source))
    for observation in outcome.observations:
        assert observation.job_url is not None
        assert not re.search(r"(refId|trk|utm_|sid=|tk=|src=|ref=|from=)", observation.job_url)
        assert "SYNTH" not in observation.job_url


@pytest.mark.parametrize(
    ("page", "status", "code"),
    [
        ("signed_out", "signed_out", "signed_out"),
        ("challenge", "challenged", "challenge"),
        ("drift", "failed", "selector_drift"),
    ],
)
@pytest.mark.parametrize("source", sorted(EXPECTED))
def test_stop_states(source: str, page: str, status: str, code: str) -> None:
    adapter = ADAPTERS[source]()
    driver = FixtureDriver({adapter.HISTORY_URL: _html(source, page)})
    outcome = _run(adapter, driver)
    assert (outcome.status, outcome.error_code) == (status, code)
    assert outcome.observations == [] and driver.clicked == []


@pytest.mark.parametrize("source", sorted(EXPECTED))
def test_explicit_empty_history(source: str) -> None:
    adapter = ADAPTERS[source]()
    outcome = _run(adapter, FixtureDriver({adapter.HISTORY_URL: _html(source, "empty")}))
    assert outcome.status == "succeeded" and outcome.observations == []


@pytest.mark.parametrize("source", sorted(EXPECTED))
def test_signed_out_redirect_is_detected_by_url(source: str) -> None:
    adapter = ADAPTERS[source]()
    login_url = f"https://www.{adapter.ALLOWED_HOSTS[0]}{adapter.MARKERS.signed_out_urls[0]}"
    driver = FixtureDriver(
        {login_url: "<html><body>Welcome back</body></html>"},
        redirects={adapter.HISTORY_URL: login_url},
    )
    assert _run(adapter, driver).status == "signed_out"


def test_live_validation_status_is_honest() -> None:
    """Real-session validation (2026-10-09): Indeed rewritten from the live page; Naukri
    unverified (signed out); LinkedIn, Instahyre and CareerNet cannot be collected safely
    and must refuse rather than scrape."""
    for cls in (LinkedInAdapter, InstahyreAdapter, CareerNetAdapter):
        assert cls.SUPPORTED is False and cls.UNSUPPORTED_REASON
        assert cls.LIVE_VERIFIED is None
        with pytest.raises(UnsupportedSource, match=cls.SOURCE_KEY):
            adapter_for(cls.SOURCE_KEY)
    assert NaukriAdapter.SUPPORTED and NaukriAdapter.LIVE_VERIFIED is None
    assert NaukriAdapter().extraction_label("primary") == "unverified"
    assert IndeedAdapter.SUPPORTED and IndeedAdapter.LIVE_VERIFIED == "2026-10-09"
    assert IndeedAdapter().extraction_label("primary") == "verified"
    assert set(ADAPTERS) == {"linkedin", "naukri", "indeed", "instahyre", "careernet"}


def test_indeed_reads_the_live_structure_and_strips_hidden_text() -> None:
    adapter = IndeedAdapter()
    result = adapter.parse_page(_html("indeed", "page1"), adapter.HISTORY_URL, TODAY)
    assert result.expected_count == 4 and len(result.items) == 4
    assert all("opens in a new window" not in (i.role or "") for i in result.items)
    assert [i.company for i in result.items][:2] == ["Northwind Robotics", "Contoso Analytics"]


def test_indeed_fewer_rows_than_the_site_reports_is_partial() -> None:
    adapter = IndeedAdapter()
    outcome = _run(adapter, FixtureDriver({adapter.HISTORY_URL: _html("indeed", "incomplete")}))
    assert (outcome.status, outcome.error_code) == ("partial", "incomplete_history")
    assert len(outcome.observations) == 4 and outcome.diagnostics.expected_count == 6


def test_indeed_zero_count_is_the_verified_empty_state() -> None:
    adapter = IndeedAdapter()
    outcome = _run(adapter, FixtureDriver({adapter.HISTORY_URL: _html("indeed", "empty")}))
    assert outcome.status == "succeeded" and outcome.observations == []


def test_naukri_registration_page_means_signed_out() -> None:
    adapter = NaukriAdapter()
    driver = FixtureDriver({adapter.HISTORY_URL: _html("naukri", "register_page")})
    assert _run(adapter, driver).status == "signed_out"


def test_fixtures_are_sanitized() -> None:
    files = list(FIXTURES.rglob("*.html"))
    assert len(files) >= 30
    for path in files:
        text = path.read_text()
        assert text.startswith("<!-- SYNTHETIC FIXTURE"), path
        assert "@" not in text, path
        assert not re.search(
            r"password|cookie|sessionid|li_at|csrftoken|bearer|jtc_[0-9a-f]", text, re.I
        ), path
        for host in re.findall(r"https://([^/\"?]+)", text):
            assert host.endswith(
                (
                    "linkedin.com",
                    "naukri.com",
                    "indeed.com",
                    "instahyre.com",
                    "careernet.in",
                    "example.test",
                    "example-employer.test",
                )
            ), (path, host)


def test_observations_from_every_adapter_satisfy_the_contract() -> None:
    for source in EXPECTED:
        for observation in _run(ADAPTERS[source](), _driver(source)).observations:
            assert ObservedApplication.model_validate(observation.model_dump()) == observation


def test_relative_and_absolute_dates() -> None:
    today = TODAY
    assert parse_applied_date("Applied 3d ago", today) == (today - timedelta(days=3)).isoformat()
    assert parse_applied_date("Applied on 12 Sep 2026", today) == "2026-09-12"
    assert parse_applied_date("Sep 30, 2026", today) == "2026-09-30"
    assert parse_applied_date("2026-09-25", today) == "2026-09-25"
    assert parse_applied_date("yesterday", today) == (today - timedelta(days=1)).isoformat()
    assert parse_applied_date("sometime last spring", today) is None
    assert parse_applied_date("31 Feb 2026", today) is None
    # Day and month only: the most recent such date that is not in the future.
    assert parse_applied_date("Applied on Indeed on 2 Oct", date(2026, 10, 9)) == "2026-10-02"
    assert parse_applied_date("Applied on 15 Dec", date(2026, 10, 9)) == "2025-12-15"


# ------------------------------------------------------------------ #
# Generic employer portal                                              #
# ------------------------------------------------------------------ #

_EMPLOYER = {
    "history_url": "https://careers.example-employer.test/candidate/applications",
    "allowed_hosts": ["careers.example-employer.test"],
    "list_container": "table#applications",
    "row": "table#applications tbody tr",
    "company_name": "Example Employer",
    "role": "td.title",
    "status": "td.status",
    "applied": "td.date",
    "link": "td.title a",
    "signed_out": ["form#login"],
    "status_map": {"submitted": "applied", "not selected": "rejected"},
}


def test_unknown_portals_are_unsupported() -> None:
    with pytest.raises(UnsupportedSource):
        adapter_for("employer-unknown")
    with pytest.raises(UnsupportedSource):
        adapter_for("employer-acme", {"history_url": "https://x.example.test"})
    with pytest.raises(UnsupportedSource):
        adapter_for("facebook")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"history_url": "http://careers.example-employer.test/a"}, "https"),
        ({"allowed_hosts": []}, "allowed_hosts"),
        ({"history_url": "https://other.example.test/a"}, "on one of allowed_hosts"),
        ({"row": ""}, "row is required"),
        ({"company_name": None}, "company"),
        ({"status_map": {"x": "hired"}}, "status_map"),
        ({"javascript": "alert(1)"}, "Unknown employer adapter keys"),
    ],
)
def test_employer_definitions_are_validated(change, message) -> None:
    definition = {**_EMPLOYER, **change}
    definition = {k: v for k, v in definition.items() if v is not None}
    with pytest.raises(EmployerDefinitionError, match=message):
        build_employer_adapter("employer-acme", definition)


def test_configured_employer_portal_collects() -> None:
    adapter = adapter_for("employer-acme", _EMPLOYER)
    driver = FixtureDriver({_EMPLOYER["history_url"]: _html("employer", "page1")})
    outcome = _run(adapter, driver)
    assert outcome.status == "succeeded"
    assert [(o.company, o.role, o.status.value) for o in outcome.observations] == [
        ("Example Employer", "Engineering Manager", "applied"),
        ("Example Employer", "Senior Backend Engineer", "rejected"),
    ]
    assert all(o.source_key == "employer-acme" for o in outcome.observations)
    drift = FixtureDriver({_EMPLOYER["history_url"]: _html("employer", "drift")})
    assert _run(adapter, drift).error_code == "selector_drift"
    signed_out = FixtureDriver({_EMPLOYER["history_url"]: _html("employer", "signed_out")})
    assert _run(adapter, signed_out).status == "signed_out"
