"""Local collector framework: config safety, lock, keychain secrets, client retries,
encrypted diagnostics and the conservative runner. Synthetic pages only."""

from __future__ import annotations

import json
import random
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from collector.adapters.base import Adapter, SelectorSet, StateMarkers
from collector.client import ClientError, TrackerClient
from collector.config import ConfigError, parse_config
from collector.core import Diagnostics
from collector.diagnostics import DiagnosticStore
from collector.driver import FixtureDriver
from collector.lock import LockHeld, run_lock
from collector.runner import SourceRunner
from collector.secrets import CredentialMissing, load_credential, store_credential

# The contract refuses future observation times, so tests observe "now".
NOW = datetime.now(UTC).replace(microsecond=0)
HISTORY = "https://jobs.example.test/history"


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


# ------------------------------------------------------------------ #
# A tiny synthetic adapter for runner tests                            #
# ------------------------------------------------------------------ #


class ExampleAdapter(Adapter):
    SOURCE_KEY = "linkedin"
    VERSION = "test.1"
    HISTORY_URL = HISTORY
    ALLOWED_HOSTS = ("jobs.example.test",)
    PAGINATION = "next"
    NEXT_SELECTOR = "a.next"
    SELECTORS = (
        SelectorSet(
            "primary",
            row="li.app",
            company=".co",
            role=".role",
            status=".st",
            applied=".when",
            link="a.job",
            item_id_attr="data-id",
        ),
        SelectorSet("fallback", row="tr.app", company="td.co", role="td.role"),
    )
    MARKERS = StateMarkers(
        signed_out=("form#login",),
        empty=("p.no-apps",),
        list_container=("ul.apps", "table.apps"),
    )
    STATUS_MAP = {"applied": "applied", "not selected": "rejected", "selected": "shortlisted"}


def _row(n: int, status: str = "Applied") -> str:
    return (
        f'<li class="app" data-id="item-{n}"><span class="co">Synthetic Co {n}</span>'
        f'<span class="role">Role {n}</span><span class="st">{status}</span>'
        f'<span class="when">Applied 3 days ago</span>'
        f'<a class="job" href="/jobs/view/{1000 + n}">view</a></li>'
    )


def _page(rows: str, more: bool = False) -> str:
    nxt = '<a class="next" href="#">Next</a>' if more else ""
    return f'<html><body><ul class="apps">{rows}</ul>{nxt}</body></html>'


def _runner(driver, **kwargs) -> SourceRunner:
    defaults = dict(clock=lambda: NOW, rng=random.Random(7), max_pages=5, min_delay=4, max_delay=9)
    return SourceRunner(ExampleAdapter(), driver, **{**defaults, **kwargs})


class RecordingClient:
    def __init__(self, fail_submits: int = 0) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_submits = fail_submits

    def start_run(self, payload):
        self.calls.append(("start", payload))
        return {}

    def submit_batch(self, run_key, payload):
        if self.fail_submits:
            self.fail_submits -= 1
            raise ClientError("network_error")
        self.calls.append(("submit", payload))
        return {"counts": {"pending": len(payload["observations"])}}

    def finish_run(self, run_key, payload):
        self.calls.append(("finish", payload))
        return {}


# ------------------------------------------------------------------ #
# Config                                                               #
# ------------------------------------------------------------------ #


def _config(tmp_path: Path, **overrides) -> dict:
    data = {
        "api_url": "https://tracker.example.test",
        "browser": {"user_data_dir": str(tmp_path / "profile")},
        "sources": [{"source_key": "linkedin"}],
    }
    data.update(overrides)
    return data


def test_valid_config(tmp_path: Path) -> None:
    cfg = parse_config(_config(tmp_path))
    assert cfg.api_url == "https://tracker.example.test"
    assert cfg.headless is False and cfg.diagnostics_enabled is False
    assert [s.source_key for s in cfg.enabled_sources()] == ["linkedin"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_url": "http://tracker.example.test"}, "https"),
        ({"browser": {}}, "user_data_dir is required"),
        (
            {"browser": {"user_data_dir": "~/Library/Application Support/Google/Chrome/Default"}},
            "everyday browser profile",
        ),
        ({"limits": {"min_delay_seconds": 0.5}}, "human-paced"),
        ({"limits": {"max_pages": 500}}, "max_pages"),
        ({"sources": [{"source_key": "facebook"}]}, "Unknown source_key"),
        ({"sources": [{"source_key": "employer-acme"}]}, "needs an"),
        ({"sources": [{"source_key": "linkedin"}, {"source_key": "linkedin"}]}, "Duplicate"),
        ({"surprise": 1}, "Unknown config keys"),
    ],
)
def test_unsafe_config_is_refused(tmp_path: Path, overrides, message) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(_config(tmp_path, **overrides))


def test_profile_inside_repository_is_refused() -> None:
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(ConfigError, match="inside the repository"):
        parse_config(
            {
                "api_url": "https://t.example.test",
                "browser": {"user_data_dir": str(repo / "profile")},
            }
        )


def test_localhost_http_is_allowed(tmp_path: Path) -> None:
    assert parse_config(_config(tmp_path, api_url="http://jobtracker.localhost:8000")).api_url


# ------------------------------------------------------------------ #
# Lock, secrets, client                                                #
# ------------------------------------------------------------------ #


def test_runs_cannot_overlap(tmp_path: Path) -> None:
    lock = tmp_path / "collector.lock"
    with run_lock(lock):
        with pytest.raises(LockHeld):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released after the first run
        pass


def test_credential_lives_in_the_keychain(memory_keyring) -> None:
    with pytest.raises(CredentialMissing):
        load_credential("https://tracker.example.test")
    store_credential("https://tracker.example.test", "jtc_x.y")
    assert load_credential("https://tracker.example.test") == "jtc_x.y"
    assert memory_keyring.data == {("job-tracker-collector", "tracker.example.test"): "jtc_x.y"}


def test_client_sends_bearer_only_and_retries_with_same_body() -> None:
    seen: list[httpx.Request] = []
    failures = iter([True, False])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if next(failures, False):
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"counts": {"pending": 1}})

    client = TrackerClient(
        "https://tracker.example.test",
        "jtc_abc.def",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    client.submit_batch("run-1", {"batch_key": "k-0000", "observations": []})
    assert len(seen) == 2 and seen[0].content == seen[1].content
    assert seen[0].headers["authorization"] == "Bearer jtc_abc.def"
    assert "cookie" not in seen[0].headers


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "forbidden_scope"),
        (409, "conflict"),
        (422, "rejected_payload"),
        (429, "rate_limited"),
        (500, "server_error"),
    ],
)
def test_client_errors_are_codes_not_bodies(status, code) -> None:
    client = TrackerClient(
        "https://tracker.example.test",
        "jtc_a.b",
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="secret page body")),
        sleep=lambda _s: None,
        retries=0,
    )
    with pytest.raises(ClientError) as exc:
        client.me()
    assert exc.value.code == code and "secret page body" not in str(exc.value)


# ------------------------------------------------------------------ #
# Diagnostics                                                          #
# ------------------------------------------------------------------ #


def test_snapshots_are_off_by_default(tmp_path: Path, memory_keyring) -> None:
    store = DiagnosticStore(tmp_path, snapshots_enabled=False, ttl_hours=24)
    assert store.save_snapshot("linkedin", "selector_drift", "<p>Private</p>") is None
    assert not (tmp_path / "snapshots").exists()


def test_snapshots_are_encrypted_private_and_expire(tmp_path: Path, memory_keyring) -> None:
    store = DiagnosticStore(tmp_path, snapshots_enabled=True, ttl_hours=1)
    path = store.save_snapshot("linkedin", "selector_drift", "<p>Jane Private Doe</p>")
    assert path is not None and b"Jane" not in path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read_snapshot(path) == "<p>Jane Private Doe</p>"
    assert store.purge_expired(now=path.stat().st_mtime + 7200) == 1
    store.record_run({"status": "succeeded"})
    assert store.clear_all() == 1 and store.recent_runs() == []


# ------------------------------------------------------------------ #
# Runner                                                               #
# ------------------------------------------------------------------ #


def test_paginated_collection_succeeds_and_paces_itself() -> None:
    page2 = "https://jobs.example.test/history?page=2"
    driver = FixtureDriver(
        {HISTORY: _page(_row(1) + _row(2), more=True), page2: _page(_row(3))},
        clicks={(HISTORY, "a.next"): page2},
    )
    outcome = _runner(driver).run(dry_run=True)
    assert outcome.status == "succeeded" and outcome.error_code is None
    assert [o.source_item_id for o in outcome.observations] == ["item-1", "item-2", "item-3"]
    assert outcome.observations[0].applied_on == NOW.date() - timedelta(days=3)
    assert outcome.diagnostics.pages == 2 and 4 <= driver.paused <= 9


def test_lazy_loading_stops_when_nothing_new_appears() -> None:
    class ScrollAdapter(ExampleAdapter):
        PAGINATION = "scroll"
        NEXT_SELECTOR = None
        LOAD_MORE_SELECTOR = "button.more"

    first = _page(_row(1)).replace("</ul>", '</ul><button class="more">More</button>')
    second = _page(_row(1) + _row(2)).replace("</ul>", '</ul><button class="more">More</button>')
    driver = FixtureDriver({HISTORY: first}, scrolls={HISTORY: [second, second]})
    runner = SourceRunner(ScrollAdapter(), driver, clock=lambda: NOW, rng=random.Random(1))
    outcome = runner.run(dry_run=True)
    assert outcome.status == "succeeded"
    assert len(outcome.observations) == 2 and outcome.diagnostics.duplicates_in_run >= 1


@pytest.mark.parametrize(
    ("html", "url", "status", "code"),
    [
        ('<html><body><form id="login"></form></body></html>', HISTORY, "signed_out", "signed_out"),
        (
            '<html><body><iframe src="https://x/recaptcha"></iframe></body></html>',
            HISTORY,
            "challenged",
            "challenge",
        ),
        (
            "<html><body>Please verify you are human</body></html>",
            HISTORY,
            "challenged",
            "challenge",
        ),
        ("<html><body>Too many requests</body></html>", HISTORY, "challenged", "rate_limited"),
        (
            "<html><body><h1>Welcome to our new homepage</h1></body></html>",
            HISTORY,
            "failed",
            "unexpected_page",
        ),
    ],
)
def test_stop_states_end_the_run_without_guessing(html, url, status, code) -> None:
    driver = FixtureDriver({url: html})
    outcome = _runner(driver).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == (status, code)
    assert outcome.observations == [] and driver.clicked == []


def test_redirect_to_another_host_is_unexpected() -> None:
    driver = FixtureDriver(
        {"https://evil.example.org/history": _page(_row(1))},
        redirects={HISTORY: "https://evil.example.org/history"},
    )
    outcome = _runner(driver).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("failed", "unexpected_page")


def test_explicit_empty_state_is_a_verified_success() -> None:
    driver = FixtureDriver({HISTORY: '<html><body><p class="no-apps">None yet</p></body></html>'})
    outcome = _runner(driver).run(dry_run=True)
    assert outcome.status == "succeeded" and outcome.observations == []


def test_selector_drift_on_first_page_fails_loudly() -> None:
    driver = FixtureDriver(
        {HISTORY: '<html><body><ul class="apps"><div>new layout</div></ul></body></html>'}
    )
    outcome = _runner(driver).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("failed", "selector_drift")


def test_selector_drift_mid_run_is_partial_and_keeps_items() -> None:
    page2 = "https://jobs.example.test/history?page=2"
    driver = FixtureDriver(
        {
            HISTORY: _page(_row(1), more=True),
            page2: '<html><body><ul class="apps"><b>?</b></ul></body></html>',
        },
        clicks={(HISTORY, "a.next"): page2},
    )
    outcome = _runner(driver).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("partial", "selector_drift")
    assert len(outcome.observations) == 1


def test_missing_pagination_control_is_drift_not_success() -> None:
    driver = FixtureDriver({HISTORY: _page(_row(1), more=True)})  # "Next" visible, click fails
    outcome = _runner(driver).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("partial", "selector_drift")


def test_page_limit_is_reported_as_partial() -> None:
    pages = {HISTORY: _page(_row(0), more=True)}
    clicks = {}
    previous = HISTORY
    for n in range(1, 6):
        url = f"{HISTORY}?page={n}"
        pages[url] = _page(_row(n), more=True)
        clicks[(previous, "a.next")] = url
        previous = url
    outcome = _runner(FixtureDriver(pages, clicks=clicks), max_pages=3).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("partial", "page_limit")
    assert len(outcome.observations) == 3


def test_fallback_selectors_are_labelled() -> None:
    html = (
        '<html><body><table class="apps"><tr class="app"><td class="co">Synthetic Co</td>'
        '<td class="role">Analyst</td></tr></table></body></html>'
    )
    outcome = _runner(FixtureDriver({HISTORY: html})).run(dry_run=True)
    assert outcome.observations[0].extraction == "fallback"
    assert outcome.diagnostics.fallback_pages == 1


def test_invalid_rows_make_the_run_partial() -> None:
    bad = _row(1).replace("Synthetic Co 1", "jane@example.com")  # redacts to nothing usable
    outcome = _runner(FixtureDriver({HISTORY: _page(bad + _row(2))})).run(dry_run=True)
    assert (outcome.status, outcome.error_code) == ("partial", "invalid_items")
    assert len(outcome.observations) == 1


def test_dry_run_sends_nothing_and_live_run_is_idempotent() -> None:
    driver = FixtureDriver({HISTORY: _page("".join(_row(n) for n in range(5)))})
    client = RecordingClient()
    outcome = _runner(driver, client=client, batch_size=2).run(dry_run=False)
    kinds = [kind for kind, _ in client.calls]
    assert kinds == ["start", "submit", "submit", "submit", "finish"]
    keys = [payload["batch_key"] for kind, payload in client.calls if kind == "submit"]
    assert keys == [f"{outcome.run_key}-000{n}" for n in range(3)]
    finish = client.calls[-1][1]
    assert finish["status"] == "succeeded" and finish["items_seen"] == 5
    assert outcome.server_counts == {"pending": 5}
    json.dumps(finish)  # the finish payload is plain counters and codes


def test_submission_failure_is_reported() -> None:
    driver = FixtureDriver({HISTORY: _page(_row(1))})
    client = RecordingClient(fail_submits=1)
    outcome = _runner(driver, client=client).run(dry_run=False)
    assert (outcome.status, outcome.error_code) == ("failed", "submission_failed")
    assert client.calls[-1][1]["error_code"] == "submission_failed"


def test_status_labels_map_longest_first() -> None:
    adapter = ExampleAdapter()
    assert adapter.map_status("Not selected") == "rejected"
    assert adapter.map_status("Selected for next round") == "shortlisted"
    assert adapter.map_status("Something new") == "unknown"


def test_diagnostics_payload_has_no_free_text() -> None:
    payload = Diagnostics(
        pages=2, stopped_state="signed_out", stopped_detail="no_rows"
    ).to_payload()
    assert all(isinstance(v, int | str) for v in payload.values())
    assert set(payload) >= {"pages", "items_extracted", "stopped_state"}
