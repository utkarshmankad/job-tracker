"""Tests for backend/main.py — app wiring not already exercised by integration tests."""

from __future__ import annotations

from backend.main import _build_cors_origins, app


def test_cors_origins_include_frontend_ports() -> None:
    origins = _build_cors_origins(5173, 5174, None)
    assert origins == [
        "http://jobtracker.localhost:5173",
        "http://jobtracker.localhost:5174",
    ]


def test_cors_origins_include_frontend_origin_when_set() -> None:
    origins = _build_cors_origins(5173, 5174, "https://job-tracker-three-green.vercel.app")
    assert origins[-1] == "https://job-tracker-three-green.vercel.app"


def test_cors_origins_omit_frontend_origin_when_unset() -> None:
    origins = _build_cors_origins(5173, 5174, None)
    assert len(origins) == 2


def test_app_has_expected_metadata() -> None:
    assert app.title == "Job Tracker API"
    assert app.version == "1.0.0"


def test_router_mounted_under_api_v1() -> None:
    paths = app.openapi()["paths"].keys()
    assert any(p.startswith("/api/v1") for p in paths)
