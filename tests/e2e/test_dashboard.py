"""E2E tests for the Job Tracker dashboard (requires live backend + frontend)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Browser, Locator, Page, expect

# ---------------------------------------------------------------------------
# Server fixtures
# ---------------------------------------------------------------------------


def _wait_for(url: str, timeout: float = 30.0) -> None:
    """Poll a loopback URL until it answers, instead of sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=1).status_code < 500:
                return
        except requests.ConnectionError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not start within {timeout}s")


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
    url = "http://jobtracker.localhost:8001"
    _wait_for(url + "/api/v1/health")
    yield url
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
    url = "http://jobtracker.localhost:5174"
    _wait_for(url)
    yield url
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    env_file.unlink(missing_ok=True)


# Sign-in attempts are rate limited (AUTH_RATE_LIMIT_ATTEMPTS per window), so the module
# signs in once through the UI and once over HTTP, then reuses both sessions.


@pytest.fixture(scope="module")
def auth_state(browser: Browser, frontend_server: str, tmp_path_factory) -> str:
    """Sign in once with the explicit local developer button and keep the session cookie."""
    context = browser.new_context()
    page = context.new_page()
    page.goto(frontend_server)
    page.get_by_role("button", name="Continue as local developer").click()
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()
    path = str(tmp_path_factory.mktemp("e2e_auth") / "state.json")
    context.storage_state(path=path)
    context.close()
    return path


@pytest.fixture
def browser_context_args(browser_context_args: dict, auth_state: str) -> dict:
    return {**browser_context_args, "storage_state": auth_state}


@pytest.fixture(scope="module")
def api_session(backend_server: str) -> tuple[requests.Session, str]:
    """Signed-in HTTP session for seeding data: session cookie plus CSRF header."""
    session = requests.Session()
    resp = session.post(backend_server + "/api/v1/auth/local")
    resp.raise_for_status()
    session.headers["X-CSRF-Token"] = resp.json()["csrf_token"]
    return session, backend_server + "/api/v1"


def _sign_in(page: Page, frontend_server: str) -> None:
    """Open the app with the module's signed-in session."""
    page.goto(frontend_server)
    expect(page.get_by_role("button", name="Sign out")).to_be_visible()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
# The backend DB is shared by every test in this module, so each test seeds uniquely named
# companies and narrows the table with the search filter.


def _open_tab(page: Page, name: str) -> None:
    # A tab's accessible name may include a count badge (e.g. "Stale 1 stale application").
    page.get_by_role("navigation").get_by_role("button", name=re.compile(f"^{name}")).click()


def _search(page: Page, text: str) -> None:
    page.get_by_label("Search").fill(text)


def _app_rows(page: Page) -> Locator:
    """Rows of the visible applications table (the hidden Home tab has tables too)."""
    table = page.locator("table:visible", has=page.get_by_role("checkbox", name="Select all"))
    return table.locator("tbody tr")


def _seed(http: requests.Session, api: str, **fields: str) -> int:
    # Today's date keeps the row out of the Stale tab unless a test sets an older one.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    payload = {"source_portal": "LinkedIn", "applied_date": today, **fields}
    resp = http.post(api + "/applications", json=payload)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _active_total(http: requests.Session, api: str, search: str) -> int:
    resp = http.get(api + "/applications", params={"search": search})
    resp.raise_for_status()
    return int(resp.json()["total"])


def test_login_required(browser: Browser, frontend_server: str, backend_server: str) -> None:
    context = browser.new_context()  # no stored session
    page = context.new_page()
    page.goto(frontend_server)
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert requests.get(backend_server + "/api/v1/applications").status_code == 401
    assert requests.get(backend_server + "/api/v1/health").json() == {"status": "ok"}
    context.close()


def test_dashboard_loads(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    expect(page.get_by_role("heading", level=1)).to_have_text("Dashboard")


def test_navigation_tabs_visible(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    nav = page.get_by_role("navigation")
    for name in ("Home", "Applications", "Opportunities", "Data Quality", "Stale", "Status"):
        expect(nav.get_by_role("button", name=re.compile(f"^{name}")).first).to_be_visible()


def test_add_application_form(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    page.get_by_role("button", name="Add Application").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Company").fill("AddFormE2ECorp")
    dialog.get_by_label("Role").fill("Engineer")
    dialog.get_by_label("Source Portal").select_option("LinkedIn")
    dialog.get_by_role("button", name="Submit").click()
    expect(dialog).to_be_hidden()

    _open_tab(page, "Applications")
    _search(page, "AddFormE2ECorp")
    expect(_app_rows(page)).to_contain_text(["AddFormE2ECorp"])


def test_filter_by_status(page: Page, frontend_server: str, api_session) -> None:
    http, api = api_session
    # Distinct roles: creating the same company + role again updates the existing record.
    for n in range(3):
        _seed(http, api, company="FilterE2E Applied", role=f"Role {n}", current_status="Applied")
    for n in range(2):
        _seed(http, api, company="FilterE2E Rejected", role=f"Role {n}", current_status="Rejected")

    _sign_in(page, frontend_server)
    _open_tab(page, "Applications")
    _search(page, "FilterE2E")
    expect(_app_rows(page)).to_have_count(5)
    page.get_by_label("Status", exact=True).select_option("Rejected")
    expect(_app_rows(page)).to_have_count(2)


def test_stale_row_highlighted(page: Page, frontend_server: str, api_session) -> None:
    http, api = api_session
    stale_date = (datetime.now(UTC) - timedelta(days=20)).strftime("%Y-%m-%d")
    _seed(http, api, company="StaleE2ECorp", source_portal="Naukri", applied_date=stale_date)

    _sign_in(page, frontend_server)
    _open_tab(page, "Stale")
    stale_row = page.locator("tr", has_text="StaleE2ECorp")
    expect(stale_row.get_by_role("img", name=re.compile("Stale"))).to_be_visible()
    expect(stale_row.locator("td").first).to_have_class(re.compile(r".*amber.*"))


def test_home_analytics_insufficient_data(page: Page, frontend_server: str) -> None:
    # Runs before the merge test seeds more rows: fewer than 10 applications exist here.
    _sign_in(page, frontend_server)
    expect(page.locator("body")).to_contain_text("Add at least 10 applications")


def test_export_csv_download(page: Page, frontend_server: str) -> None:
    _sign_in(page, frontend_server)
    with page.expect_download() as dl:
        page.get_by_role("button", name="Export").click()
    download = dl.value
    assert download.suggested_filename.endswith(".csv")


def test_merge_three_duplicates_and_undo(page: Page, frontend_server: str, api_session) -> None:
    """Select three duplicates, preview, resolve the conflict, merge, then undo."""
    http, api = api_session
    ids = [
        _seed(http, api, company="MergeE2ECorp", role="Platform Engineer"),
        _seed(http, api, company="MergeE2ECorp", role="Platform Engineer"),
        _seed(
            http,
            api,
            company="MergeE2ECorp",
            role="Platform Engineer",
            current_status="Resume Shortlisted",
        ),
    ]
    assert _active_total(http, api, "MergeE2ECorp") == 3

    _sign_in(page, frontend_server)
    _open_tab(page, "Applications")
    _search(page, "MergeE2ECorp")
    rows = _app_rows(page)
    expect(rows).to_have_count(3)

    page.get_by_role("checkbox", name="Select all").check()
    page.get_by_role("button", name="Merge duplicates").click()

    dialog = page.get_by_role("dialog", name="Merge 3 applications")
    expect(dialog).to_contain_text("Nothing is permanently deleted")
    confirm = dialog.get_by_role("button", name="Confirm merge")
    # The differing status is a conflict: confirm stays disabled until it is resolved.
    expect(confirm).to_be_disabled()
    dialog.get_by_role("group", name="Status", exact=True).get_by_role(
        "radio", name=re.compile("Resume Shortlisted")
    ).check()
    expect(confirm).to_be_enabled()
    confirm.click()

    expect(page.get_by_role("status").filter(has_text="Merged 3 applications")).to_be_visible()
    expect(dialog).to_be_hidden()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Resume Shortlisted")
    assert _active_total(http, api, "MergeE2ECorp") == 1
    merged = http.get(
        api + "/applications", params={"search": "MergeE2ECorp", "include_merged": "true"}
    ).json()
    assert sorted(item["id"] for item in merged["items"]) == sorted(ids)

    page.get_by_role("button", name="Undo", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="Merge undone")).to_be_visible()
    expect(rows).to_have_count(3)
    assert _active_total(http, api, "MergeE2ECorp") == 3


def test_merge_dialog_fits_mobile_viewport(page: Page, frontend_server: str, api_session) -> None:
    http, api = api_session
    for _ in range(2):
        _seed(http, api, company="MobileMergeCorp", role="Designer")
    page.set_viewport_size({"width": 375, "height": 740})
    _sign_in(page, frontend_server)
    _open_tab(page, "Applications")
    _search(page, "MobileMergeCorp")
    expect(_app_rows(page)).to_have_count(2)
    page.get_by_role("checkbox", name="Select all").check()
    page.get_by_role("button", name="Merge duplicates").click()

    dialog = page.get_by_role("dialog", name="Merge 2 applications")
    confirm = dialog.get_by_role("button", name="Confirm merge")
    expect(confirm).to_be_enabled()  # identical records: no conflicts to resolve
    box = dialog.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375
    confirm.scroll_into_view_if_needed()
    expect(confirm).to_be_in_viewport()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert _active_total(http, api, "MobileMergeCorp") == 2  # cancelled: nothing merged


def _collect(http: requests.Session, api: str, base: str) -> None:
    """Enroll a collector through the real API and submit one run like the local agent."""
    setup = http.post(api + "/collectors", json={"name": "E2E laptop", "scopes": ["naukri"]})
    setup.raise_for_status()
    enrolled = requests.post(api + "/collector/enroll", json={"code": setup.json()["setup_code"]})
    enrolled.raise_for_status()
    bearer = {"Authorization": f"Bearer {enrolled.json()['credential']}"}
    run_key = uuid.uuid4().hex
    started = requests.post(
        api + "/collector/runs",
        headers=bearer,
        json={
            "run_key": run_key,
            "source_key": "naukri",
            "collector_version": "0.1.0",
            "adapter_version": "naukri/0.1.0",
        },
    )
    started.raise_for_status()
    now = datetime.now(UTC)
    observation = {
        "source_key": "naukri",
        "source_item_id": "e2e-1",
        "company": "E2E Collected Co",
        "role": "Collected Role",
        "applied_on": (now - timedelta(days=2)).date().isoformat(),
        "status": "applied",
        "raw_status": "Application Sent",
        "job_url": None,
        "proves_submission": True,
        "extraction": "unverified",
        "observed_at": now.isoformat(),
        "adapter_version": "naukri/0.1.0",
    }
    batch = {
        "batch_key": uuid.uuid4().hex,
        "sent_at": now.isoformat(),
        "observations": [observation],
    }
    requests.post(
        api + f"/collector/runs/{run_key}/observations", headers=bearer, json=batch
    ).raise_for_status()
    requests.post(
        api + f"/collector/runs/{run_key}/finish",
        headers=bearer,
        json={"status": "signed_out", "items_seen": 1, "error_code": "signed_out"},
    ).raise_for_status()


def test_sources_page_shows_collection_state(page: Page, frontend_server: str, api_session) -> None:
    http, api = api_session
    _collect(http, api, frontend_server)
    _sign_in(page, frontend_server)
    _open_tab(page, "Sources")
    expect(page.get_by_role("heading", level=1)).to_have_text("Collection Sources")
    sources = page.get_by_role("region", name="Configured sources")
    expect(sources).to_contain_text("Naukri")
    expect(sources.get_by_text("Signed out")).to_be_visible()
    expect(page.get_by_role("alert").filter(has_text="Needs your attention")).to_contain_text(
        "not signed in"
    )
    review = page.get_by_role("region", name=re.compile("Needs review"))
    expect(review).to_contain_text("E2E Collected Co")
    expect(review.get_by_role("button", name="Create application")).to_be_visible()
    runs = page.get_by_role("region", name="Recent runs")
    runs.get_by_role("button", name="Inspect").first.click()
    expect(runs).to_contain_text("Needs review")
    collectors = page.get_by_role("region", name="Collectors")
    expect(collectors).to_contain_text("E2E laptop")
    # Only a short hint is ever shown — never a full credential.
    expect(collectors).not_to_contain_text(re.compile(r"jtc_[0-9a-f]{16}\."))
    expect(collectors).to_contain_text("jtc_")


def test_sources_page_fits_mobile(page: Page, frontend_server: str) -> None:
    """No horizontal page scroll on the Sources page at phone width (with collected data)."""
    page.set_viewport_size({"width": 375, "height": 740})
    _sign_in(page, frontend_server)
    _open_tab(page, "Sources")
    expect(page.get_by_role("region", name="Totals")).to_be_visible()
    expect(page.get_by_role("region", name="Recent runs")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
