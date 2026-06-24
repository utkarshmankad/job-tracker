"""FastAPI application entry point."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router
from backend.config import (
    API_HOST, API_PORT, FRONTEND_PORT, FRONTEND_PORT_ALT,
    LLM_ENABLED, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS,
)
from backend.db.data_store import DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater
from backend.parser.llm_extractor import LLMExtractor

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from datetime import datetime
    from backend.poller.scheduler import build_poller, PollerScheduler

    db = DataStore()
    app.state.db = db
    app.state.updater = StatusUpdater(db, DuplicateDetector(db))
    app.state.started_at = datetime.utcnow()
    app.state.llm_extractor = (
        LLMExtractor(LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT_SECONDS) if LLM_ENABLED else None
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


app = FastAPI(title="Job Tracker API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://jobtracker.localhost:{FRONTEND_PORT}",
        f"http://jobtracker.localhost:{FRONTEND_PORT_ALT}",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=API_HOST, port=API_PORT, reload=True)
