"""Tests for backend/setup_wizard.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from backend import setup_wizard


def test_setup_exits_1_when_credentials_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    with patch.object(setup_wizard, "CREDENTIALS_PATH", tmp_path / "missing.json"):
        result = runner.invoke(setup_wizard.cli, ["setup"])
    assert result.exit_code == 1
    assert "client_secret.json not found" in result.output


def test_setup_happy_path(tmp_path: Path) -> None:
    creds_path = tmp_path / "client_secret.json"
    creds_path.write_text("{}")
    job_tracker_dir = tmp_path / "jt"
    log_dir = tmp_path / "jt" / "logs"

    runner = CliRunner()
    with (
        patch.object(setup_wizard, "CREDENTIALS_PATH", creds_path),
        patch.object(setup_wizard, "JOB_TRACKER_DIR", job_tracker_dir),
        patch.object(setup_wizard, "LOG_DIR", log_dir),
        patch.object(setup_wizard, "_run_oauth", return_value='{"token": "fake"}') as mock_oauth,
        patch.object(setup_wizard, "_save_token") as mock_save,
        patch.object(setup_wizard, "DataStore") as mock_ds,
        patch.object(setup_wizard, "_install_launchd_plists") as mock_plists,
    ):
        result = runner.invoke(setup_wizard.cli, ["setup"])

    assert result.exit_code == 0, result.output
    mock_oauth.assert_called_once()
    mock_save.assert_called_once_with('{"token": "fake"}')
    mock_ds.assert_called_once()
    mock_plists.assert_called_once()
    assert job_tracker_dir.exists()
    assert log_dir.exists()
    assert "Setup complete" in result.output


def test_reauth_runs_oauth_and_saves_token() -> None:
    runner = CliRunner()
    with (
        patch.object(setup_wizard, "_run_oauth", return_value='{"token": "fake"}') as mock_oauth,
        patch.object(setup_wizard, "_save_token") as mock_save,
    ):
        result = runner.invoke(setup_wizard.cli, ["reauth"])

    assert result.exit_code == 0
    mock_oauth.assert_called_once()
    mock_save.assert_called_once_with('{"token": "fake"}')
    assert "Re-authentication complete" in result.output


def test_run_oauth_uses_installed_app_flow() -> None:
    mock_flow = MagicMock()
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "abc"}'
    mock_flow.run_local_server.return_value = mock_creds

    with patch.object(
        setup_wizard.InstalledAppFlow, "from_client_secrets_file", return_value=mock_flow
    ) as mock_from_secrets:
        result = setup_wizard._run_oauth()

    mock_from_secrets.assert_called_once()
    mock_flow.run_local_server.assert_called_once_with(port=0)
    assert result == '{"token": "abc"}'


def test_save_token_writes_to_keyring() -> None:
    with patch.object(setup_wizard, "keyring") as mock_keyring:
        setup_wizard._save_token('{"token": "xyz"}')

    mock_keyring.set_password.assert_called_once_with(
        setup_wizard.GMAIL_KEYCHAIN_SERVICE,
        setup_wizard.GMAIL_KEYCHAIN_USERNAME,
        '{"token": "xyz"}',
    )


def test_install_launchd_plists_writes_plist_files(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    with (
        patch.object(setup_wizard, "LAUNCH_AGENTS_DIR", launch_agents_dir),
        patch("subprocess.run") as mock_run,
    ):
        setup_wizard._install_launchd_plists()

    api_plist = launch_agents_dir / "com.jobtracker.api.plist"
    poller_plist = launch_agents_dir / "com.jobtracker.poller.plist"
    assert api_plist.exists()
    assert poller_plist.exists()
    assert setup_wizard.API_HOST in api_plist.read_text()
    assert str(setup_wizard.API_PORT) in api_plist.read_text()
    assert mock_run.call_count == 2
