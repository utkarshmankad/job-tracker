"""Tests for backend/config.py — env-var-derived defaults.

config.py reads os.environ at import time, so these tests set env vars via
monkeypatch and reload the module rather than importing the already-loaded
singleton. Each test restores the original module afterward.
"""

from __future__ import annotations

import importlib

import backend.config as config_module


def _reload_config(monkeypatch, **env: str) -> object:
    # config.py calls load_dotenv() at import time, which would silently repopulate any
    # var this test just deleted from os.environ (e.g. LLM_PROVIDER=groq in the repo's
    # .env) — stub it out so reload() sees exactly the env this test set up.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


def test_default_llm_provider_is_ollama(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = _reload_config(monkeypatch)

    assert cfg.LLM_PROVIDER == "ollama"
    assert cfg.LLM_MODEL == "llama3.2:3b"
    assert cfg.LLM_BASE_URL == "http://localhost:11434"


def test_groq_provider_switches_model_and_base_url_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    cfg = _reload_config(monkeypatch, LLM_PROVIDER="groq")

    assert cfg.LLM_MODEL == "openai/gpt-oss-20b"
    assert cfg.LLM_BASE_URL == "https://api.groq.com/openai/v1"


def test_explicit_llm_model_overrides_provider_default(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, LLM_PROVIDER="ollama", LLM_MODEL="custom-model")
    assert cfg.LLM_MODEL == "custom-model"


def test_api_port_parses_env_var_as_int(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, API_PORT="9000")
    assert cfg.API_PORT == 9000
    assert isinstance(cfg.API_PORT, int)


def test_public_base_url_defaults_from_api_host_and_port(monkeypatch) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    cfg = _reload_config(monkeypatch, API_HOST="example.local", API_PORT="1234")
    assert cfg.PUBLIC_BASE_URL == "http://example.local:1234"


def test_public_base_url_env_override_wins(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, PUBLIC_BASE_URL="https://job-tracker.example.com")
    assert cfg.PUBLIC_BASE_URL == "https://job-tracker.example.com"


def test_cache_enabled_defaults_true(monkeypatch) -> None:
    monkeypatch.delenv("CACHE_ENABLED", raising=False)
    cfg = _reload_config(monkeypatch)
    assert cfg.CACHE_ENABLED is True


def test_cache_enabled_false_when_env_set_false(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, CACHE_ENABLED="false")
    assert cfg.CACHE_ENABLED is False


def test_admin_token_unset_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    cfg = _reload_config(monkeypatch)
    assert cfg.ADMIN_TOKEN is None


def test_job_tracker_dir_respects_env_override(monkeypatch, tmp_path) -> None:
    custom_dir = str(tmp_path / "custom-job-tracker")
    cfg = _reload_config(monkeypatch, JOB_TRACKER_DIR=custom_dir)

    assert str(cfg.JOB_TRACKER_DIR) == custom_dir
    assert cfg.DB_PATH == cfg.JOB_TRACKER_DIR / "applications.db"


def teardown_module() -> None:
    """Restore the real module state so later test modules see actual env-derived config."""
    importlib.reload(config_module)
