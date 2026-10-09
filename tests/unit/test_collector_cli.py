"""Collector CLI: enrollment into the keychain, config validation, adapter listing,
dry-run → submit, diagnostics hygiene and the run lock. No browser, network or keychain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from collector import cli as cli_module
from collector.cli import cli
from collector.client import ClientError
from collector.diagnostics import DiagnosticStore
from collector.lock import run_lock
from tests.unit.test_collector_framework import (
    MemoryKeyring,  # noqa: F401  (fixture helper)
    memory_keyring,  # noqa: F401
)

CREDENTIAL = "jtc_0123456789abcdef." + "S" * 43


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("JOB_TRACKER_COLLECTOR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.toml").write_text(
        'api_url = "https://tracker.example.test"\n'
        f'[browser]\nuser_data_dir = "{tmp_path / "profile"}"\n'
        '[[sources]]\nsource_key = "indeed"\n'
        '[[sources]]\nsource_key = "employer-unknown"\n[sources.employer]\nhistory_url = "x"\n'
    )
    return tmp_path / "home"


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, api_url, credential, **_kwargs) -> None:
        self.credential = credential
        self.calls: list[tuple[str, object]] = []
        FakeClient.instances.append(self)

    def enroll(self, code):
        if code != "good-code-abcdefghijklmnop":
            raise ClientError("rejected_payload", 400)
        return {"collector_id": 3, "credential": CREDENTIAL, "scopes": ["linkedin"]}

    def start_run(self, payload):
        self.calls.append(("start", payload))
        return {}

    def submit_batch(self, run_key, payload):
        self.calls.append(("submit", payload))
        return {"counts": {"created": len(payload["observations"])}}

    def finish_run(self, run_key, payload):
        self.calls.append(("finish", payload))
        return {}

    def metrics(self):
        return {"runs_by_status": {}}

    def close(self):
        return None


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(cli_module, "TrackerClient", FakeClient)
    return FakeClient


def test_enroll_stores_credential_without_printing_it(memory_keyring, fake_client) -> None:  # noqa: F811
    result = CliRunner().invoke(
        cli,
        [
            "enroll",
            "--api-url",
            "https://tracker.example.test",
            "--code",
            "good-code-abcdefghijklmnop",
        ],
    )
    assert result.exit_code == 0, result.output
    assert CREDENTIAL not in result.output and "S" * 10 not in result.output
    assert "stored in your keychain" in result.output
    assert memory_keyring.data[("job-tracker-collector", "tracker.example.test")] == CREDENTIAL


def test_enroll_failure_reports_a_code_only(memory_keyring, fake_client) -> None:  # noqa: F811
    result = CliRunner().invoke(
        cli,
        [
            "enroll",
            "--api-url",
            "https://tracker.example.test",
            "--code",
            "bad-code-xxxxxxxxxxxxxxxx",
        ],
    )
    assert result.exit_code != 0 and "Enrollment failed: rejected_payload" in result.output
    assert memory_keyring.data == {}


def test_validate_config_reports_support_and_credential(home, memory_keyring) -> None:  # noqa: F811
    result = CliRunner().invoke(cli, ["validate-config"])
    assert result.exit_code == 0, result.output
    assert "indeed/default (enabled, supported)" in result.output
    assert "employer-unknown/default (enabled, UNSUPPORTED)" in result.output
    assert "Credential: missing" in result.output


def test_invalid_config_is_a_clean_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JOB_TRACKER_COLLECTOR_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('api_url = "http://evil.example.test"\n')
    result = CliRunner().invoke(cli, ["validate-config"])
    assert result.exit_code != 0 and "https" in result.output


def test_adapters_are_honest_about_verification() -> None:
    result = CliRunner().invoke(cli, ["adapters"])
    assert result.exit_code == 0
    assert result.output.count("UNSUPPORTED:") == 3  # linkedin, instahyre, careernet
    assert "naukri" in result.output and "not yet (fixture-tested only)" in result.output
    assert "live-verified: 2026-10-09" in result.output  # indeed
    assert "employer-<slug>" in result.output


def test_dry_run_file_then_submit(home, memory_keyring, fake_client) -> None:  # noqa: F811
    memory_keyring.set_password("job-tracker-collector", "tracker.example.test", CREDENTIAL)
    pending = home / "state" / "pending"
    pending.mkdir(parents=True)
    path = pending / "linkedin-default-abc.json"
    observation = {
        "source_key": "linkedin",
        "source_item_id": "1",
        "company": "Northwind Robotics",
        "role": "Engineer",
        "applied_on": None,
        "status": "applied",
        "raw_status": "Applied",
        "job_url": None,
        "proves_submission": True,
        "extraction": "unverified",
        "observed_at": "2026-10-01T00:00:00+00:00",
        "adapter_version": "linkedin/0.1.0",
    }
    path.write_text(
        json.dumps(
            {
                "source_key": "linkedin",
                "account_label": "default",
                "status": "succeeded",
                "error_code": None,
                "observations": [observation],
            }
        )
    )
    result = CliRunner().invoke(cli, ["submit", str(path)])
    assert result.exit_code == 0, result.output
    client = fake_client.instances[-1]
    assert client.credential == CREDENTIAL
    assert [kind for kind, _ in client.calls] == ["start", "submit", "finish"]
    assert not path.exists()


def test_run_refuses_while_another_run_holds_the_lock(home, memory_keyring) -> None:  # noqa: F811
    state = home / "state"
    state.mkdir(exist_ok=True)
    (home.parent / "profile").mkdir()
    with run_lock(state / "collector.lock"):
        result = CliRunner().invoke(cli, ["run", "--source", "indeed", "--dry-run"])
    assert result.exit_code != 0 and "Another collection run is in progress" in result.output


def test_local_run_log_is_content_free(tmp_path, memory_keyring) -> None:  # noqa: F811
    import random

    from collector.driver import FixtureDriver
    from collector.runner import SourceRunner
    from tests.unit.test_collector_framework import HISTORY, NOW, ExampleAdapter, _page, _row

    store = DiagnosticStore(tmp_path, snapshots_enabled=False, ttl_hours=24)
    SourceRunner(
        ExampleAdapter(),
        FixtureDriver({HISTORY: _page(_row(1) + _row(2))}),
        diagnostics=store,
        clock=lambda: NOW,
        rng=random.Random(1),
    ).run(dry_run=True)
    text = (tmp_path / "runs.jsonl").read_text()
    assert "Synthetic Co" not in text and "Role 1" not in text and "jobs.example.test" not in text
    assert '"observations": 2' in text


def test_clear_diagnostics(home, memory_keyring) -> None:  # noqa: F811
    store = DiagnosticStore(home / "state", snapshots_enabled=True, ttl_hours=24)
    store.save_snapshot("linkedin", "selector_drift", "<p>x</p>")
    store.record_run({"status": "failed"})
    result = CliRunner().invoke(cli, ["clear-diagnostics", "--yes"])
    assert result.exit_code == 0 and "Removed 2 item(s)." in result.output
    assert not list((home / "state").rglob("*.bin"))
