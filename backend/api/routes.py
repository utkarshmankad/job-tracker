"""All FastAPI endpoint definitions."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from backend.config import DB_PATH
from backend.db.data_store import ApplicationFilter, DataStore, is_application_stale
from backend.db.models import Application, ApplicationStatus
from backend.engine.insights_engine import InsightsEngine
from backend.engine.status_updater import StatusUpdater

router = APIRouter()


# ------------------------------------------------------------------ #
# Response models                                                      #
# ------------------------------------------------------------------ #


class StatusHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    from_status: Optional[str]
    to_status: str
    trigger: str
    changed_at: datetime
    message_id: Optional[str]


class ApplicationResponse(BaseModel):
    id: int
    company: Optional[str]
    role: Optional[str]
    source_portal: str
    job_url: Optional[str]
    applied_date: datetime
    current_status: ApplicationStatus
    thread_ids: str
    is_false_positive: bool
    created_at: datetime
    updated_at: datetime
    is_stale: bool


class ApplicationDetailResponse(ApplicationResponse):
    status_history: list[StatusHistoryResponse]


class ApplicationListResponse(BaseModel):
    items: list[ApplicationResponse]
    total: int
    page: int
    page_size: int


class ChannelStatResponse(BaseModel):
    source: str
    total: int
    shortlisted: int
    interviewed: int
    offered: int


class ChannelInsightResponse(BaseModel):
    source: str
    flag: str
    message: str


class InsightReportResponse(BaseModel):
    funnel: dict[str, int]
    channels: list[ChannelStatResponse]
    insights: list[ChannelInsightResponse]
    total_applications: int
    insufficient_data: bool
    generated_at: datetime


class PollerStatusResponse(BaseModel):
    status: str
    last_sync_at: Optional[datetime]
    error_message: Optional[str]


class ComponentStatusResponse(BaseModel):
    name: str
    status: str  # "operational" | "degraded" | "outage"
    description: str
    checked_at: datetime


class SystemStatusResponse(BaseModel):
    overall: str  # "operational" | "degraded" | "outage"
    components: list[ComponentStatusResponse]
    stats: dict
    checked_at: datetime


class SuppressRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_pattern: str
    subject_pattern: Optional[str]
    created_at: datetime


# ------------------------------------------------------------------ #
# Request models                                                       #
# ------------------------------------------------------------------ #


class ApplicationCreate(BaseModel):
    company: Optional[str] = None
    role: Optional[str] = None
    source_portal: str
    job_url: Optional[str] = None
    applied_date: datetime
    current_status: ApplicationStatus = ApplicationStatus.APPLIED


class ApplicationPatch(BaseModel):
    current_status: Optional[ApplicationStatus] = None
    company: Optional[str] = None
    role: Optional[str] = None
    job_url: Optional[str] = None
    is_false_positive: Optional[bool] = None


class SuppressRuleCreate(BaseModel):
    sender_pattern: str
    subject_pattern: Optional[str] = None


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _to_response(app: Application) -> ApplicationResponse:
    assert app.id is not None, "Application must be persisted before serializing"
    return ApplicationResponse(
        id=app.id,
        company=app.company,
        role=app.role,
        source_portal=app.source_portal,
        job_url=app.job_url,
        applied_date=app.applied_date,
        current_status=app.current_status,
        thread_ids=app.thread_ids,
        is_false_positive=app.is_false_positive,
        created_at=app.created_at,
        updated_at=app.updated_at,
        is_stale=is_application_stale(app),
    )


def _csv_rows(apps: list[Application]) -> str:
    output = io.StringIO()
    fields = [
        "id", "company", "role", "source_portal", "job_url",
        "applied_date", "current_status", "is_false_positive",
        "created_at", "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for app in apps:
        writer.writerow({
            "id": app.id,
            "company": app.company or "",
            "role": app.role or "",
            "source_portal": app.source_portal,
            "job_url": app.job_url or "",
            "applied_date": app.applied_date.isoformat(),
            "current_status": app.current_status.value,
            "is_false_positive": app.is_false_positive,
            "created_at": app.created_at.isoformat(),
            "updated_at": app.updated_at.isoformat(),
        })
    return output.getvalue()


# ------------------------------------------------------------------ #
# Applications                                                         #
# ------------------------------------------------------------------ #


@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(
    request: Request,
    status: Optional[ApplicationStatus] = None,
    source_portal: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> ApplicationListResponse:
    db: DataStore = request.app.state.db
    try:
        date_from_dt = datetime.fromisoformat(date_from) if date_from else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date_from — expected ISO 8601")
    try:
        date_to_dt = datetime.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date_to — expected ISO 8601")
    filters = ApplicationFilter(
        status=status,
        source_portal=source_portal,
        date_from=date_from_dt,
        date_to=date_to_dt,
        search=search,
        page=page,
        page_size=page_size,
    )
    items, total = db.get_applications(filters)
    return ApplicationListResponse(
        items=[_to_response(a) for a in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/applications", response_model=ApplicationResponse, status_code=201)
async def create_application(body: ApplicationCreate, request: Request) -> ApplicationResponse:
    db: DataStore = request.app.state.db
    app = Application(
        company=body.company,
        role=body.role,
        source_portal=body.source_portal,
        job_url=body.job_url,
        applied_date=body.applied_date,
        current_status=body.current_status,
        updated_at=body.applied_date,
    )
    saved = db.upsert_application(app)
    return _to_response(saved)


# NOTE: /applications/export must be registered before /applications/{id}
# so that the literal path segment "export" is not captured as an id.
@router.get("/applications/export")
async def export_applications(
    request: Request,
    format: str = Query(default="json", pattern="^(csv|json)$"),
) -> StreamingResponse:
    db: DataStore = request.app.state.db
    items, _ = db.get_applications(ApplicationFilter(page_size=10_000))
    active = [a for a in items if not a.is_false_positive]

    if format == "csv":
        filename = f"job_applications_{date.today()}.csv"
        return StreamingResponse(
            iter([_csv_rows(active)]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # JSON — return as a plain streaming response with JSON content
    import json as _json
    payload = _json.dumps([_to_response(a).model_dump(mode="json") for a in active])
    return StreamingResponse(iter([payload]), media_type="application/json")


@router.get("/applications/{id}", response_model=ApplicationDetailResponse)
async def get_application(id: int, request: Request) -> ApplicationDetailResponse:
    db: DataStore = request.app.state.db
    app = db.get_application(id)
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    history = db.get_status_history(id)
    return ApplicationDetailResponse(
        **_to_response(app).model_dump(),
        status_history=[StatusHistoryResponse.model_validate(h) for h in history],
    )


@router.patch("/applications/{id}", response_model=ApplicationResponse)
async def patch_application(
    id: int, body: ApplicationPatch, request: Request
) -> ApplicationResponse:
    db: DataStore = request.app.state.db
    app = db.get_application(id)
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")

    if body.current_status is not None:
        updater: StatusUpdater = request.app.state.updater
        app = updater.manual_update(id, body.current_status)

    needs_upsert = False
    if body.company is not None:
        app.company = body.company
        needs_upsert = True
    if body.role is not None:
        app.role = body.role
        needs_upsert = True
    if body.job_url is not None:
        app.job_url = body.job_url
        needs_upsert = True
    if body.is_false_positive is not None:
        app.is_false_positive = body.is_false_positive
        needs_upsert = True

    if needs_upsert:
        app = db.upsert_application(app)

    return _to_response(app)


@router.delete("/applications/{id}")
async def delete_application(id: int, request: Request) -> dict:
    db: DataStore = request.app.state.db
    deleted = db.delete_application(id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Application not found")
    return {"deleted": True}


# ------------------------------------------------------------------ #
# Insights                                                             #
# ------------------------------------------------------------------ #


@router.get("/insights/flow")
async def get_insights_flow(request: Request) -> dict:
    db: DataStore = request.app.state.db
    return InsightsEngine(db).flow_data()


@router.get("/insights", response_model=InsightReportResponse)
async def get_insights(request: Request) -> InsightReportResponse:
    db: DataStore = request.app.state.db
    report = InsightsEngine(db).generate_report()
    return InsightReportResponse(
        funnel=report.funnel,
        channels=[
            ChannelStatResponse(
                source=c.source,
                total=c.total,
                shortlisted=c.shortlisted,
                interviewed=c.interviewed,
                offered=c.offered,
            )
            for c in report.channels
        ],
        insights=[
            ChannelInsightResponse(source=i.source, flag=i.flag, message=i.message)
            for i in report.insights
        ],
        total_applications=report.total_applications,
        insufficient_data=report.insufficient_data,
        generated_at=report.generated_at,
    )


# ------------------------------------------------------------------ #
# Poller                                                               #
# ------------------------------------------------------------------ #


@router.get("/poller/status", response_model=PollerStatusResponse)
async def get_poller_status(request: Request) -> PollerStatusResponse:
    db: DataStore = request.app.state.db
    state = db.get_poller_state()
    return PollerStatusResponse(
        status=state.status,
        last_sync_at=state.last_sync_at,
        error_message=state.error_message,
    )


@router.post("/poller/reauth")
async def reauth_poller(request: Request) -> dict:
    db: DataStore = request.app.state.db
    db.update_poller_state(status="AUTH_REQUIRED")
    return {"message": "Re-authentication required. Run: python backend/setup_wizard.py reauth"}


# ------------------------------------------------------------------ #
# Suppress rules                                                       #
# ------------------------------------------------------------------ #


@router.get("/suppress-rules", response_model=list[SuppressRuleResponse])
async def list_suppress_rules(request: Request) -> list[SuppressRuleResponse]:
    db: DataStore = request.app.state.db
    return [SuppressRuleResponse.model_validate(r) for r in db.get_suppress_rules()]


@router.post("/suppress-rules", response_model=SuppressRuleResponse, status_code=201)
async def create_suppress_rule(
    body: SuppressRuleCreate, request: Request
) -> SuppressRuleResponse:
    try:
        re.compile(body.sender_pattern)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid sender_pattern regex: {exc}")
    if body.subject_pattern is not None:
        try:
            re.compile(body.subject_pattern)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid subject_pattern regex: {exc}")
    db: DataStore = request.app.state.db
    rule = db.add_suppress_rule(
        sender_pattern=body.sender_pattern,
        subject_pattern=body.subject_pattern,
    )
    return SuppressRuleResponse.model_validate(rule)


@router.delete("/suppress-rules/{id}")
async def delete_suppress_rule(id: int, request: Request) -> dict:
    db: DataStore = request.app.state.db
    deleted = db.delete_suppress_rule(id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Suppress rule not found")
    return {"deleted": True}


# ------------------------------------------------------------------ #
# System status                                                         #
# ------------------------------------------------------------------ #


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(request: Request) -> SystemStatusResponse:
    db: DataStore = request.app.state.db
    now = datetime.utcnow()
    started_at: Optional[datetime] = getattr(request.app.state, "started_at", None)
    components: list[ComponentStatusResponse] = []

    # API Server — always operational if the request arrived
    components.append(ComponentStatusResponse(
        name="API Server",
        status="operational",
        description="FastAPI server is responding",
        checked_at=now,
    ))

    # Database
    db_total = 0
    db_size_kb = 0
    try:
        _, db_total = db.get_applications(ApplicationFilter(page_size=1))
        db_size_kb = DB_PATH.stat().st_size // 1024 if DB_PATH.exists() else 0
        components.append(ComponentStatusResponse(
            name="Database",
            status="operational",
            description=f"SQLite healthy — {db_total} applications, {db_size_kb} KB",
            checked_at=now,
        ))
    except Exception as exc:
        components.append(ComponentStatusResponse(
            name="Database",
            status="outage",
            description=f"Database error: {exc}",
            checked_at=now,
        ))

    # Gmail Poller
    poller = db.get_poller_state()
    if poller.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
        poller_status = "outage"
        poller_desc = "Authentication error — run: python backend/setup_wizard.py reauth"
    elif poller.status == "ERROR":
        poller_status = "degraded"
        poller_desc = f"Poller error: {poller.error_message or 'unknown'}"
    elif poller.last_sync_at:
        minutes_ago = int((now - poller.last_sync_at).total_seconds() / 60)
        if minutes_ago > 15:
            poller_status = "degraded"
            poller_desc = f"Last synced {minutes_ago} min ago — poller may be stopped"
        else:
            poller_status = "operational"
            poller_desc = f"Last synced {minutes_ago} min ago"
    else:
        poller_status = "degraded"
        poller_desc = "Never synced — start the scheduler to begin polling"

    components.append(ComponentStatusResponse(
        name="Gmail Poller",
        status=poller_status,
        description=poller_desc,
        checked_at=now,
    ))

    # Gmail Auth
    if poller.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
        auth_status = "outage"
        auth_desc = "OAuth token invalid — re-run setup wizard"
    else:
        auth_status = "operational"
        auth_desc = "OAuth token valid"

    components.append(ComponentStatusResponse(
        name="Gmail Auth",
        status=auth_status,
        description=auth_desc,
        checked_at=now,
    ))

    # Overall
    statuses = {c.status for c in components}
    if "outage" in statuses:
        overall = "outage"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "operational"

    uptime_seconds = int((now - started_at).total_seconds()) if started_at else None

    return SystemStatusResponse(
        overall=overall,
        components=components,
        stats={
            "total_applications": db_total,
            "db_size_kb": db_size_kb,
            "last_poll_at": poller.last_sync_at.isoformat() if poller.last_sync_at else None,
            "uptime_seconds": uptime_seconds,
        },
        checked_at=now,
    )
