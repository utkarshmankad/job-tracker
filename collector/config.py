"""Collector configuration (TOML) and local paths.

Default location: ``~/Library/Application Support/JobTrackerCollector/config.toml`` on
macOS (``$XDG_CONFIG_HOME/job-tracker-collector`` elsewhere), overridable with
``JOB_TRACKER_COLLECTOR_HOME``. The config holds no secrets: the collector credential lives
in the keychain, and the browser profile directory is referenced by path, never copied.

Example::

    api_url = "https://job-tracker-api-verdant-haze-8797.fly.dev"

    [browser]
    # A dedicated profile you sign in to once per site (recommended). Using your everyday
    # Chrome profile requires allow_primary_profile = true and Chrome must be closed.
    user_data_dir = "~/Library/Application Support/JobTrackerCollector/browser-profile"
    channel = "chrome"

    [limits]
    max_pages = 10
    min_delay_seconds = 4
    max_delay_seconds = 9

    [[sources]]
    source_key = "indeed"  # the only live-verified source (docs/collector-operations.md)
    account_label = "default"
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from backend.collection.contract import is_valid_source_key

_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRIMARY_PROFILE_MARKERS = (
    "Google/Chrome/Default",
    "Google/Chrome/Profile",
    "google-chrome/Default",
    "Chromium/Default",
)


class ConfigError(ValueError):
    pass


def home_dir() -> Path:
    override = os.environ.get("JOB_TRACKER_COLLECTOR_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JobTrackerCollector"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "job-tracker-collector"


def state_dir() -> Path:
    path = home_dir() / "state"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass(frozen=True)
class SourceConfig:
    source_key: str
    account_label: str = "default"
    enabled: bool = True
    # Employer portals only: the explicitly configured generic history-table adapter.
    employer: dict[str, object] | None = None


@dataclass(frozen=True)
class CollectorConfig:
    api_url: str
    user_data_dir: Path
    channel: str | None = "chrome"
    # A Chromium-based browser binary to drive instead of a Playwright channel (e.g. Brave).
    executable_path: Path | None = None
    headless: bool = False
    allow_primary_profile: bool = False
    max_pages: int = 10
    min_delay_seconds: float = 4.0
    max_delay_seconds: float = 9.0
    batch_size: int = 25
    diagnostics_enabled: bool = False
    diagnostics_ttl_hours: int = 24
    sources: tuple[SourceConfig, ...] = field(default_factory=tuple)

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def config_path() -> Path:
    return home_dir() / "config.toml"


def load_config(path: Path | None = None) -> CollectorConfig:
    path = path or config_path()
    if not path.is_file():
        raise ConfigError(f"No collector config at {path}. See docs/collector-operations.md.")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config is not valid TOML: {exc}") from exc
    return parse_config(data)


def parse_config(data: dict[str, object]) -> CollectorConfig:
    allowed_top = {"api_url", "browser", "limits", "diagnostics", "sources"}
    unknown = set(data) - allowed_top
    if unknown:
        raise ConfigError(f"Unknown config keys: {sorted(unknown)}")
    api_url = str(data.get("api_url") or "").rstrip("/")
    parts = urlsplit(api_url)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"} or (parts.hostname or "").endswith(
        ".localhost"
    )
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ConfigError("api_url must be https (http is allowed only for localhost).")
    browser = _table(data, "browser")
    limits = _table(data, "limits")
    diagnostics = _table(data, "diagnostics")
    raw_dir = browser.get("user_data_dir")
    if not raw_dir:
        raise ConfigError("browser.user_data_dir is required: choose a browser profile explicitly.")
    user_data_dir = Path(str(raw_dir)).expanduser().resolve()
    if user_data_dir.is_relative_to(_REPO_ROOT):
        raise ConfigError("The browser profile must not be inside the repository.")
    allow_primary = bool(browser.get("allow_primary_profile", False))
    if not allow_primary and any(m in str(user_data_dir) for m in _PRIMARY_PROFILE_MARKERS):
        raise ConfigError(
            "That is an everyday browser profile. Use a dedicated profile, or set "
            "browser.allow_primary_profile = true (and close that browser first)."
        )
    sources = []
    seen = set()
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ConfigError("[[sources]] must be an array of tables.")
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise ConfigError("Each [[sources]] entry must be a table.")
        key = str(entry.get("source_key", ""))
        label = str(entry.get("account_label", "default"))
        if not is_valid_source_key(key):
            raise ConfigError(f"Unknown source_key {key!r}.")
        if not _LABEL.match(label):
            raise ConfigError("account_label must be lowercase letters, digits, - or _.")
        if (key, label) in seen:
            raise ConfigError(f"Duplicate source {key}/{label}.")
        seen.add((key, label))
        employer = entry.get("employer")
        if key.startswith("employer-") and not isinstance(employer, dict):
            raise ConfigError(f"{key} needs an [sources.employer] adapter definition.")
        sources.append(
            SourceConfig(
                source_key=key,
                account_label=label,
                enabled=bool(entry.get("enabled", True)),
                employer=employer if isinstance(employer, dict) else None,
            )
        )
    min_delay = float(limits.get("min_delay_seconds", 4.0))  # type: ignore[arg-type]
    max_delay = float(limits.get("max_delay_seconds", 9.0))  # type: ignore[arg-type]
    if min_delay < 2 or max_delay < min_delay:
        raise ConfigError("Delays must be human-paced: min_delay_seconds >= 2 and max >= min.")
    max_pages = int(limits.get("max_pages", 10))  # type: ignore[call-overload]
    if not 1 <= max_pages <= 50:
        raise ConfigError("limits.max_pages must be between 1 and 50.")
    executable = browser.get("executable_path")
    executable_path = Path(str(executable)).expanduser() if executable else None
    if executable_path is not None and not executable_path.is_file():
        raise ConfigError("browser.executable_path must point to a browser binary.")
    return CollectorConfig(
        api_url=api_url,
        user_data_dir=user_data_dir,
        executable_path=executable_path,
        channel=(str(browser["channel"]) if browser.get("channel") else None),
        headless=bool(browser.get("headless", False)),
        allow_primary_profile=allow_primary,
        max_pages=max_pages,
        min_delay_seconds=min_delay,
        max_delay_seconds=max_delay,
        batch_size=min(int(limits.get("batch_size", 25)), 100),  # type: ignore[call-overload]
        diagnostics_enabled=bool(diagnostics.get("enabled", False)),
        diagnostics_ttl_hours=min(int(diagnostics.get("ttl_hours", 24)), 72),  # type: ignore[call-overload]
        sources=tuple(sources),
    )


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table.")
    return value
