"""FastAPI application entry point."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router
from backend.config import API_HOST, API_PORT, FRONTEND_PORT, FRONTEND_PORT_ALT
from backend.db.data_store import DataStore
from backend.engine.duplicate_detector import DuplicateDetector
from backend.engine.status_updater import StatusUpdater

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from datetime import datetime
    db = DataStore()
    app.state.db = db
    app.state.updater = StatusUpdater(db, DuplicateDetector(db))
    app.state.started_at = datetime.utcnow()
    log.info("app_started")
    yield
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
