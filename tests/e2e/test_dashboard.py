"""E2E tests for the Job Tracker dashboard (requires live backend + frontend)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_server(tmp_path_factory):
    test_dir = tmp_path_factory.mktemp("e2e_db")
    # Authentication stays on: E2E uses the explicit loopback-only local sign-in mode.
    env = {
        **os.environ,
        "JOB_TRACKER_DIR": str(test_dir),
        "APP_ENV": "development",
        "AUTH_MODE": "local",
    }
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "backend.main:app", "--port", "8001"],
        cwd=".",
        env=env,
        start_new_session=True,
    )
    time.sleep(3)
    yield "http://jobtracker.localhost:8001"
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


@pytest.fixture(scope="module")
def frontend_server(backend_server):
    env_file = Path("frontend/.env.local")
    env_file.write_text(f"VITE_API_BASE={backend_server}\n")
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", "5174"],
        cwd="frontend",
        start_new_session=True,
    )
    time.sleep(5)
    yield "http://jobtracker.localhost:5174"
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    env_file.unlink(missing_ok=True)


def _sign_in(page: Page, frontend_server: str) -> None:
    """Open the app and use the explicit local developer sign-in."""
    page.goto(frontend_server)
    page.get_by_role("button", name="Continue as local developer").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


def _api_session(backend_server: str) -> tuple[requests.Session, str]:
    """Signed-in HTTP session for seeding data: session cookie plus CSRF header."""
    session = requests.Session()
    resp = session.post(backend_server + "/api/v1/auth/local")
    resp.raise_for_status()
    session.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
    return session, backend_server + "/api/v1"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_login_required(page: Page, frontend_server: str, backend_server: str) -> None:
    page.goto(frontend_server)
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert requests.get(backend_server + "/api/v1/applications").status_code == 401
    assert requests.get(backend_server + "/api/v1/health").json() == {"status": "ok"}


def test_dashboard_loads(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    expect(page.locator("h1")).to_contain_text("Job Application Tracker")


def test_applications_tab_visible(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    expect(page.get_by_role("button", name="Applications")).to_be_visible()
    expect(page.get_by_role("button", name="Analytics")).to_be_visible()


def test_add_application_form(page: Page, frontend_server: str, backend_server: str) -> None:
    _sign_in(page, frontend_server)
    page.get_by_role("button", name="Add Application").click()

    page.get_by_label("Company").fill("TestCorp")
    page.get_by_label("Role").fill("Engineer")
    page.get_by_label("Source Portal").select_option("LinkedIn")
    page.get_by_role("button", name="Submit").click()

    expect(page.locator("table")).to_contain_text("TestCorp")


def test_filter_by_status(page: Page, frontend_server: str, backend_server: str) -> None:
    http, api = _api_session(backend_server)
    base_payload = {
        "source_portal": "LinkedIn",
        "applied_date": "2024-06-01",
    }
    for _ in range(3):
        http.post(
            api + "/applications",
            json={**base_payload, "current_status": "Applied", "company": "AppCo"},
        )
    for _ in range(2):
        http.post(
            api + "/applications",
            json={**base_payload, "current_status": "Rejected", "company": "RejCo"},
        )

    _sign_in(page, frontend_server)
    page.get_by_label("Status").select_option("Rejected")

    rows = page.locator("table tbody tr")
    expect(rows).to_have_count(2)


def test_stale_row_highlighted(page: Page, frontend_server: str, backend_server: str) -> None:
    from datetime import UTC, datetime, timedelta

    http, api = _api_session(backend_server)
    stale_date = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%d")
    resp = http.post(
        api + "/applications",
        json={
            "source_portal": "Naukri",
            "applied_date": stale_date,
            "current_status": "Applied",
            "company": "StaleE2ECorp",
        },
    )
    assert resp.status_code == 201

    _sign_in(page, frontend_server)
    stale_row = page.locator("tr", has_text="StaleE2ECorp")
    expect(stale_row).to_have_class(re.compile(r".*amber.*"))


def test_analytics_tab_insufficient_data(
    page: Page, frontend_server: str, backend_server: str
) -> None:
    _sign_in(page, frontend_server)
    page.get_by_role("button", name="Analytics").click()
    expect(page.locator("body")).to_contain_text("Add at least 10 applications")


def test_export_csv_download(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    with page.expect_download() as dl:
        page.get_by_role("button", name="Export").click()
    download = dl.value
    assert download.suggested_filename.endswith(".csv")
