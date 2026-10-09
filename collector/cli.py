"""Collector command line (scripts/collect.py). See docs/collector-operations.md.

Commands never print the credential, cookies or page content. Human-facing output goes
through click.echo (this is the tool's UI); nothing here logs secrets.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click

from backend.collection.contract import ObservedApplication
from collector import COLLECTOR_VERSION
from collector.adapters import ADAPTERS, UnsupportedSource, adapter_for
from collector.client import ClientError, TrackerClient
from collector.config import (
    CollectorConfig,
    ConfigError,
    SourceConfig,
    config_path,
    load_config,
    state_dir,
)
from collector.core import PageState
from collector.diagnostics import DiagnosticStore
from collector.driver import DriverError, PageDriver, PlaywrightDriver
from collector.lock import LockHeld, run_lock
from collector.runner import RunOutcome, SourceRunner
from collector.secrets import CredentialMissing, load_credential, store_credential

_config_option = click.option(
    "--config",
    "config_file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Config file (default: the collector home config.toml).",
)


def _load(config_file: Path | None) -> CollectorConfig:
    try:
        return load_config(config_file)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _store(cfg: CollectorConfig) -> DiagnosticStore:
    return DiagnosticStore(
        state_dir(), snapshots_enabled=cfg.diagnostics_enabled, ttl_hours=cfg.diagnostics_ttl_hours
    )


def _client(cfg: CollectorConfig) -> TrackerClient:
    try:
        return TrackerClient(cfg.api_url, load_credential(cfg.api_url))
    except CredentialMissing as exc:
        raise click.ClickException(str(exc)) from exc


@contextmanager
def _browser(cfg: CollectorConfig) -> Iterator[PageDriver]:
    if not cfg.user_data_dir.exists():
        raise click.ClickException(
            f"Browser profile {cfg.user_data_dir} does not exist. Create it with "
            "`scripts/collect.py open-profile` and sign in to each site first."
        )
    driver = PlaywrightDriver(cfg.user_data_dir, channel=cfg.channel, headless=cfg.headless)
    try:
        yield driver
    finally:
        driver.close()


def _source(cfg: CollectorConfig, source_key: str) -> SourceConfig:
    for source in cfg.sources:
        if source.source_key == source_key:
            return source
    raise click.ClickException(f"{source_key} is not configured in [[sources]].")


def _code_suffix(code: str | None) -> str:
    return f" ({code})" if code else ""


def _print_outcome(outcome: RunOutcome) -> None:
    summary = outcome.summary()
    click.echo(
        f"{summary['source_key']}/{summary['account_label']}: {summary['status']}"
        + (f" ({summary['error_code']})" if summary["error_code"] else "")
        + f" — {summary['observations']} observation(s)"
        + (" [dry run, nothing sent]" if outcome.dry_run else "")
    )
    if outcome.server_counts:
        click.echo("  tracker: " + ", ".join(f"{k} {v}" for k, v in outcome.server_counts.items()))


def _write_pending(outcome: RunOutcome) -> Path:
    """Save a dry run's validated observations for an explicit later `submit`."""
    pending = state_dir() / "pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    path = pending / f"{outcome.source_key}-{outcome.account_label}-{outcome.run_key}.json"
    payload = {
        "contract_version": 1,
        "source_key": outcome.source_key,
        "account_label": outcome.account_label,
        "status": outcome.status,
        "error_code": outcome.error_code,
        "observations": [o.model_dump(mode="json") for o in outcome.observations],
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    return path


def _run_one(
    cfg: CollectorConfig, source: SourceConfig, driver: PageDriver, dry_run: bool
) -> RunOutcome:
    adapter = adapter_for(source.source_key, source.employer)
    runner = SourceRunner(
        adapter,
        driver,
        account_label=source.account_label,
        client=None if dry_run else _client(cfg),
        diagnostics=_store(cfg),
        max_pages=cfg.max_pages,
        min_delay=cfg.min_delay_seconds,
        max_delay=cfg.max_delay_seconds,
        batch_size=cfg.batch_size,
    )
    return runner.run(dry_run=dry_run)


@click.group()
def cli() -> None:
    """Read-only application-history collector for Job Tracker."""


@cli.command("validate-config")
@_config_option
def validate_config(config_file: Path | None) -> None:
    """Check the config file (paths, sources, limits) without opening a browser."""
    cfg = _load(config_file)
    click.echo(f"Config:   {config_file or config_path()}")
    click.echo(f"Tracker:  {cfg.api_url}")
    click.echo(
        f"Profile:  {cfg.user_data_dir} ({'exists' if cfg.user_data_dir.exists() else 'missing'})"
    )
    for source in cfg.sources:
        try:
            adapter_for(source.source_key, source.employer)
            support = "supported"
        except UnsupportedSource:
            support = "UNSUPPORTED"
        state = "enabled" if source.enabled else "disabled"
        click.echo(f"Source:   {source.source_key}/{source.account_label} ({state}, {support})")
    try:
        load_credential(cfg.api_url)
        click.echo("Credential: present in keychain")
    except CredentialMissing:
        click.echo("Credential: missing — run `enroll`")


@cli.command("adapters")
def list_adapters() -> None:
    """List supported adapters and whether their selectors were verified live."""
    for key, adapter_cls in sorted(ADAPTERS.items()):
        verified = adapter_cls.LIVE_VERIFIED or "not yet (fixture-tested only)"
        click.echo(
            f"{key:10s} v{adapter_cls.VERSION}  pagination={adapter_cls.PAGINATION:9s}  "
            f"live-verified: {verified}"
        )
    click.echo(
        "employer-<slug>  generic history table — only with an explicit "
        "[sources.employer] definition"
    )


@cli.command("enroll")
@click.option("--api-url", required=True, help="Tracker API base URL.")
@click.option("--code", default=None, help="One-time setup code (prompted if omitted).")
def enroll(api_url: str, code: str | None) -> None:
    """Exchange a one-time setup code for the collector credential (stored in the keychain)."""
    code = code or getpass.getpass("Setup code: ")
    client = TrackerClient(api_url, None)
    try:
        result = client.enroll(code.strip())
    except ClientError as exc:
        raise click.ClickException(f"Enrollment failed: {exc.code}") from exc
    finally:
        client.close()
    store_credential(api_url, result["credential"])
    click.echo(f"Enrolled collector {result['collector_id']} for: {', '.join(result['scopes'])}")
    click.echo("The credential is stored in your keychain; it is not shown.")


@cli.command("check-session")
@_config_option
@click.option("--source", "source_key", required=True)
def check_session(config_file: Path | None, source_key: str) -> None:
    """Open the source's history page and report the session state only (reads nothing)."""
    cfg = _load(config_file)
    source = _source(cfg, source_key)
    try:
        adapter = adapter_for(source.source_key, source.employer)
    except UnsupportedSource as exc:
        raise click.ClickException(f"{source_key} is not supported.") from exc
    from bs4 import BeautifulSoup

    with _browser(cfg) as driver:
        try:
            driver.goto(adapter.HISTORY_URL)
        except DriverError as exc:
            raise click.ClickException("The history page could not be opened.") from exc
        state = adapter.detect_state(
            BeautifulSoup(driver.html(), "html.parser"), driver.current_url()
        )
    click.echo(f"{source_key}: {state.value}")
    if state in (PageState.SIGNED_OUT, PageState.CHALLENGE, PageState.CONSENT):
        click.echo("Resolve this in the browser window yourself, then run again.")


def _guarded_runs(cfg: CollectorConfig, sources: list[SourceConfig], dry_run: bool) -> int:
    failures = 0
    try:
        with run_lock(state_dir() / "collector.lock"), _browser(cfg) as driver:
            for source in sources:
                try:
                    outcome = _run_one(cfg, source, driver, dry_run)
                except UnsupportedSource:
                    click.echo(f"{source.source_key}: unsupported — skipped")
                    failures += 1
                    continue
                except ClientError as exc:
                    click.echo(f"{source.source_key}: tracker error ({exc.code})")
                    failures += 1
                    continue
                _print_outcome(outcome)
                if dry_run:
                    click.echo(
                        f"  validated observations saved for review: {_write_pending(outcome)}"
                    )
                if outcome.status != "succeeded":
                    failures += 1
    except LockHeld as exc:
        raise click.ClickException(str(exc)) from exc
    return failures


@cli.command("run")
@_config_option
@click.option("--source", "source_key", required=True)
@click.option("--dry-run", is_flag=True, help="Collect and validate, but send nothing.")
def run(config_file: Path | None, source_key: str, dry_run: bool) -> None:
    """Collect one source."""
    cfg = _load(config_file)
    if _guarded_runs(cfg, [_source(cfg, source_key)], dry_run):
        sys.exit(1)


@cli.command("run-all")
@_config_option
@click.option("--dry-run", is_flag=True, help="Collect and validate, but send nothing.")
def run_all(config_file: Path | None, dry_run: bool) -> None:
    """Collect every enabled source, one after another, in one browser session."""
    cfg = _load(config_file)
    if _guarded_runs(cfg, cfg.enabled_sources(), dry_run):
        sys.exit(1)


@cli.command("submit")
@_config_option
@click.argument("pending_file", type=click.Path(path_type=Path, dir_okay=False, exists=True))
def submit(config_file: Path | None, pending_file: Path) -> None:
    """Send a dry run's saved, validated observations as one run (after you reviewed them)."""
    cfg = _load(config_file)
    data: dict[str, Any] = json.loads(pending_file.read_text())
    observations = [ObservedApplication.model_validate(o) for o in data["observations"]]
    if not observations:
        raise click.ClickException("The file has no observations.")
    adapter_version = observations[0].adapter_version
    client = _client(cfg)
    import uuid
    from datetime import UTC, datetime

    run_key = uuid.uuid4().hex
    try:
        client.start_run(
            {
                "run_key": run_key,
                "source_key": data["source_key"],
                "account_label": data["account_label"],
                "collector_version": COLLECTOR_VERSION,
                "adapter_version": adapter_version,
            }
        )
        counts: dict[str, int] = {}
        for start in range(0, len(observations), cfg.batch_size):
            chunk = observations[start : start + cfg.batch_size]
            result = client.submit_batch(
                run_key,
                {
                    "batch_key": f"{run_key}-{start // cfg.batch_size:04d}",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "observations": [o.model_dump(mode="json") for o in chunk],
                },
            )
            for name, count in (result.get("counts") or {}).items():
                counts[name] = counts.get(name, 0) + int(count)
        status = data.get("status") or "succeeded"
        client.finish_run(
            run_key,
            {
                "status": status,
                "items_seen": len(observations),
                "error_code": data.get("error_code") if status != "succeeded" else None,
                "diagnostics": {"submitted_from_dry_run": True},
            },
        )
    except ClientError as exc:
        raise click.ClickException(f"Submission failed: {exc.code}") from exc
    finally:
        client.close()
    pending_file.unlink()
    click.echo("Submitted. tracker: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


@cli.command("diagnostics")
@_config_option
@click.option(
    "--remote/--local-only", default=True, help="Also fetch this collector's server metrics."
)
def diagnostics(config_file: Path | None, remote: bool) -> None:
    """Print aggregate diagnostics (counts and codes only — no page content)."""
    cfg = _load(config_file)
    for entry in _store(cfg).recent_runs():
        click.echo(
            f"{entry.get('at', '')}  {entry['source_key']}/{entry['account_label']}  "
            f"{entry['status']}{_code_suffix(entry.get('error_code'))}  "
            f"obs={entry['observations']}  {json.dumps(entry['diagnostics'], sort_keys=True)}"
        )
    if remote:
        client = _client(cfg)
        try:
            click.echo(json.dumps(client.metrics(), indent=2, sort_keys=True))
        except ClientError as exc:
            click.echo(f"Server metrics unavailable: {exc.code}")
        finally:
            client.close()


@cli.command("clear-diagnostics")
@_config_option
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def clear_diagnostics(config_file: Path | None, yes: bool) -> None:
    """Delete local run logs, saved dry-run files, encrypted page snapshots and their key."""
    cfg = _load(config_file)
    if not yes:
        click.confirm("Delete all local collector diagnostics?", abort=True)
    removed = _store(cfg).clear_all()
    click.echo(f"Removed {removed} item(s).")


@cli.command("open-profile")
@_config_option
def open_profile(config_file: Path | None) -> None:
    """Open the configured browser profile so you can sign in to job sites yourself."""
    cfg = _load(config_file)
    cfg.user_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver = PlaywrightDriver(cfg.user_data_dir, channel=cfg.channel, headless=False)
    click.echo("Sign in to each job site in the window, then press Enter here to close it.")
    try:
        input()
    finally:
        driver.close()


def main() -> None:
    cli(prog_name="collect.py")
