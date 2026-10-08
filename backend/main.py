"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.auth import (
    CSRF_HEADER,
    AuthError,
    AuthService,
    load_auth_config,
    require_user,
    validate_auth_config,
)
from backend.api.routes import public_router, require_database, router
from backend.config import (
    API_HOST,
    API_PORT,
    FRONTEND_ORIGIN,
    FRONTEND_PORT,
    FRONTEND_PORT_ALT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLED,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)
from backend.db.data_store import DataStore
from backend.db.models import utc_now
from backend.db.schema import SchemaOutdatedError
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.parser.llm_extractor import LLMExtractor

log = structlog.get_logger()


def _build_cors_origins(
    frontend_port: int, frontend_port_alt: int, frontend_origin: str | None
) -> list[str]:
    origins = [
        f"http://jobtracker.localhost:{frontend_port}",
        f"http://jobtracker.localhost:{frontend_port_alt}",
    ]
    if frontend_origin:
        origins.append(frontend_origin)
    return origins


_cors_origins = _build_cors_origins(FRONTEND_PORT, FRONTEND_PORT_ALT, FRONTEND_ORIGIN)


def build_auth_service() -> AuthService:
    """Load and validate auth configuration. Raises AuthConfigError in production when
    anything required is missing, which aborts startup (fail closed)."""
    auth_config = load_auth_config(_cors_origins)
    validate_auth_config(auth_config)
    return AuthService(auth_config)


def _open_database() -> tuple[DataStore | None, str | None]:
    """Open the database, or explain why the API must stay in maintenance mode.

    Maintenance mode (no DataStore, no poller, data endpoints return 503) is entered when
    the maintenance flag file exists — the operator is migrating or restoring — or when the
    schema needs a migration that the startup policy will not run unattended (always the
    case in production). See docs/database-operations.md.
    """
    from backend import config as app_config

    if app_config.MAINTENANCE_FLAG_PATH.exists():
        return None, "maintenance flag present"
    try:
        return DataStore(app_config.DB_PATH), None
    except SchemaOutdatedError as exc:
        return None, exc.status.describe()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from backend.poller.scheduler import PollerScheduler, build_poller

    # First, before any data is opened: refuse to serve with broken production auth.
    app.state.auth = build_auth_service()
    app.state.started_at = utc_now()
    app.state.reauth_state = None

    db, maintenance_reason = _open_database()
    app.state.db = db
    app.state.maintenance_reason = maintenance_reason
    if db is None:
        # Quiesced: no writers at all, so an operator can migrate or restore safely.
        app.state.updater = None
        app.state.llm_extractor = None
        app.state.poller_scheduler = None
        log.warning("app_started_in_maintenance_mode", reason=maintenance_reason)
        yield
        log.info("app_shutdown")
        return

    app.state.updater = StatusUpdater(db, DuplicateDetector(db))
    app.state.llm_extractor = (
        LLMExtractor(LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS, LLM_API_KEY)
        if LLM_ENABLED
        else None
    )

    poller = build_poller()
    scheduler = PollerScheduler(poller)
    app.state.poller_scheduler = scheduler
    try:
        poller.authenticate()
        scheduler.start()
    except Exception as exc:
        log.error("poller_start_failed", error=str(exc))

    log.info("app_started")
    yield

    scheduler.stop()
    log.info("app_shutdown")


class _RedactQueryStringFilter(logging.Filter):
    """Strip query strings from uvicorn access-log lines. The Gmail re-auth callback
    carries an OAuth authorization code and state in its query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0] + "?[redacted]", *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactQueryStringFilter())


app = FastAPI(title="Job Tracker API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(AuthError)
async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
        headers={"Cache-Control": "no-store", **exc.headers},
    )


# Production serves the API same-origin through the Vercel /api rewrite, so CORS only
# matters for local development and a directly configured FRONTEND_ORIGIN. Origins are an
# explicit allow-list (never "*") because credentials (the session cookie) are allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", CSRF_HEADER],
)

app.include_router(public_router, prefix="/api/v1")
app.include_router(
    router,
    prefix="/api/v1",
    dependencies=[Depends(require_user), Depends(require_database)],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=API_HOST == "jobtracker.localhost",
    )
