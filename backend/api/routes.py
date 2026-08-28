"""All FastAPI endpoint definitions."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

from backend import cache
from backend.config import (
    ADMIN_TOKEN,
    APPLICATIONS_CACHE_TTL_SECONDS,
    DB_PATH,
    INSIGHTS_CACHE_TTL_SECONDS,
    PUBLIC_BASE_URL,
    REEXTRACT_BATCH_LIMIT,
)
from backend.db.data_store import ApplicationFilter, DataStore, is_application_stale
from backend.db.models import Application, ApplicationStatus, utc_now
from backend.diagnostics import DiagnosticRunner
from backend.engine.insights_engine import InsightsEngine
from backend.engine.status_updater import StatusUpdater

log = structlog.get_logger(__name__)

_APPLICATIONS_CACHE_PREFIX = "applications:list:"
_INSIGHTS_CACHE_PREFIX = "insights:"


def _invalidate_applications_cache() -> None:
    cache.invalidate_prefix(_APPLICATIONS_CACHE_PREFIX)
    cache.invalidate_prefix(_INSIGHTS_CACHE_PREFIX)


router = APIRouter()


# ------------------------------------------------------------------ #
# Response models                                                      #
# ------------------------------------------------------------------ #


class StatusHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    from_status: str | None
    to_status: str
    trigger: str
    changed_at: datetime
    message_id: str | None


class ApplicationResponse(BaseModel):
    id: int
    company: str | None
    role: str | None
    source_portal: str
    job_url: str | None
    applied_date: datetime
    current_status: ApplicationStatus
    thread_ids: str
    is_false_positive: bool
    withdraw_reason: str | None
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
    last_sync_at: datetime | None
    error_message: str | None

    @field_validator("last_sync_at")
    @classmethod
    def _tag_utc(cls, v: datetime | None) -> datetime | None:
        # Stored/written as UTC (datetime.now(timezone.utc)) but SQLite drops
        # tzinfo on the round trip, so the value comes back naive. Without an
        # explicit offset, browsers parse the ISO string as local time.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


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


class DiagnosticItemResponse(BaseModel):
    name: str
    ok: bool
    detail: str
    checked_at: datetime


class DiagnosticsResponse(BaseModel):
    passed: int
    failed: int
    results: list[DiagnosticItemResponse]
    checked_at: datetime


class ReextractResponse(BaseModel):
    total: int
    updated: int
    skipped: int
    remaining: int


class SuppressRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender_pattern: str
    subject_pattern: str | None
    created_at: datetime


# ------------------------------------------------------------------ #
# Request models                                                       #
# ------------------------------------------------------------------ #


class ApplicationCreate(BaseModel):
    company: str | None = None
    role: str | None = None
    source_portal: str
    job_url: str | None = None
    applied_date: datetime
    current_status: ApplicationStatus = ApplicationStatus.APPLIED


class ApplicationPatch(BaseModel):
    current_status: ApplicationStatus | None = None
    company: str | None = None
    role: str | None = None
    job_url: str | None = None
    applied_date: datetime | None = None
    is_false_positive: bool | None = None
    withdraw_reason: str | None = None


class BulkWithdrawRequest(BaseModel):
    companies: list[str]  # company names extracted from LinkedIn paste


class BulkWithdrawResponse(BaseModel):
    updated: int
    application_ids: list[int]


class BulkStatusRequest(BaseModel):
    application_ids: list[int]
    status: ApplicationStatus


class BulkDeleteRequest(BaseModel):
    application_ids: list[int]


class BulkActionResponse(BaseModel):
    updated: int
    failed_ids: list[int]


class SuppressRuleCreate(BaseModel):
    sender_pattern: str
    subject_pattern: str | None = None


class LinkedInPreviewRequest(BaseModel):
    text: str
    mode: str  # "applied" | "archived" | "interview"


class LinkedInPreviewEntry(BaseModel):
    company: str | None
    role: str | None
    applied_date: datetime | None
    predicted_action: str  # "create" | "skip" | "update" | "no_data"
    existing_id: int | None


class LinkedInPreviewResponse(BaseModel):
    entries: list[LinkedInPreviewEntry]
    garbage_lines: list[int]  # 1-indexed line numbers the LLM tagged as noise
    llm_used: bool


class LinkedInConfirmedEntry(BaseModel):
    company: str | None = None
    role: str | None = None
    applied_date: datetime | None = None
    include: bool = True


class LinkedInImportConfirmedRequest(BaseModel):
    mode: str  # "applied" | "archived" | "interview"
    entries: list[LinkedInConfirmedEntry]


class LinkedInImportEntryResult(BaseModel):
    company: str | None
    role: str | None
    applied_date: datetime | None
    action: str  # "created" | "updated" | "skipped" | "failed"
    application_id: int | None


class LinkedInImportResponse(BaseModel):
    created: int
    updated: int
    skipped: int
    failed: int
    entries: list[LinkedInImportEntryResult]


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
        withdraw_reason=app.withdraw_reason,
        created_at=app.created_at,
        updated_at=app.updated_at,
        is_stale=is_application_stale(app),
    )


def _csv_rows(apps: list[Application]) -> str:
    output = io.StringIO()
    fields = [
        "id",
        "company",
        "role",
        "source_portal",
        "job_url",
        "applied_date",
        "current_status",
        "is_false_positive",
        "created_at",
        "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for app in apps:
        writer.writerow(
            {
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
            }
        )
    return output.getvalue()


# ------------------------------------------------------------------ #
# Applications                                                         #
# ------------------------------------------------------------------ #


@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(
    request: Request,
    status: ApplicationStatus | None = None,
    source_portal: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    is_stale: bool | None = None,
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
        is_stale=is_stale,
        page=page,
        page_size=page_size,
    )
    cache_key = (
        f"{_APPLICATIONS_CACHE_PREFIX}{status}:{source_portal}:{date_from}:{date_to}:"
        f"{search}:{is_stale}:{page}:{page_size}"
    )
    cached = cache.get_json(cache_key)
    if cached is not None:
        return ApplicationListResponse(**cached)

    items, total = db.get_applications(filters)
    response = ApplicationListResponse(
        items=[_to_response(a) for a in items],
        total=total,
        page=page,
        page_size=page_size,
    )
    cache.set_json(cache_key, response.model_dump(mode="json"), APPLICATIONS_CACHE_TTL_SECONDS)
    return response


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
    _invalidate_applications_cache()
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
        filename = f"job_applications_{utc_now().date()}.csv"
        return StreamingResponse(
            iter([_csv_rows(active)]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # JSON — return as a plain streaming response with JSON content
    import json as _json

    payload = _json.dumps([_to_response(a).model_dump(mode="json") for a in active])
    return StreamingResponse(iter([payload]), media_type="application/json")


# NOTE: /applications/reextract must be registered before /applications/{id}
# so that "reextract" is not captured as an integer id.
@router.post("/applications/reextract", response_model=ReextractResponse)
def reextract_missing_fields(request: Request, offset: int = Query(0, ge=0)) -> ReextractResponse:
    """Re-fetch and re-extract company/role for applications missing them.

    Processes at most REEXTRACT_BATCH_LIMIT per call to avoid Gmail rate limits/timeouts
    on a large backlog. Pass the previous response's `remaining` count as the next
    `offset` to page through the rest.
    """
    db: DataStore = request.app.state.db
    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Poller not running")
    poller = scheduler.poller
    if poller.service is None:
        raise HTTPException(
            status_code=503, detail="Gmail not authenticated — run setup_wizard.py reauth"
        )

    batch = db.get_applications_missing_fields(offset=offset, limit=REEXTRACT_BATCH_LIMIT)
    updated = 0
    skipped = 0

    for app in batch:
        try:
            company, role = poller.reextract_fields(app)
            changed = False
            if company is not None and app.company is None:
                app.company = company
                changed = True
            if role is not None and app.role is None:
                app.role = role
                changed = True
            if changed:
                db.upsert_application(app)
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            log.warning("reextract_failed", app_id=app.id, error=str(exc))
            skipped += 1

    remaining = db.count_applications_missing_fields()
    if updated:
        _invalidate_applications_cache()
    return ReextractResponse(
        total=len(batch), updated=updated, skipped=skipped, remaining=remaining
    )


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
        app = updater.manual_update(id, body.current_status, body.withdraw_reason)

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
    if body.applied_date is not None:
        app.applied_date = body.applied_date
        needs_upsert = True
    if body.is_false_positive is not None:
        app.is_false_positive = body.is_false_positive
        needs_upsert = True

    if needs_upsert:
        app = db.upsert_application(app)

    if needs_upsert or body.current_status is not None:
        _invalidate_applications_cache()

    return _to_response(app)


@router.post("/applications/bulk-withdraw", response_model=BulkWithdrawResponse)
async def bulk_withdraw_by_companies(
    body: BulkWithdrawRequest, request: Request
) -> BulkWithdrawResponse:
    """Mark active applications as Withdrawn (company_closed) for given company names."""
    db: DataStore = request.app.state.db
    updater: StatusUpdater = request.app.state.updater
    companies = [c.strip() for c in body.companies if c.strip()]
    apps = db.find_active_applications_by_companies(companies)
    ids: list[int] = []
    for app in apps:
        assert app.id is not None
        updater.manual_update(app.id, ApplicationStatus.WITHDRAWN, "company_closed")
        ids.append(app.id)
    if ids:
        _invalidate_applications_cache()
    return BulkWithdrawResponse(updated=len(ids), application_ids=ids)


@router.post("/applications/bulk-status", response_model=BulkActionResponse)
async def bulk_update_status(body: BulkStatusRequest, request: Request) -> BulkActionResponse:
    """Set current_status on a set of applications via the same manual-update path as
    a single-record PATCH — status transitions still go through StatusUpdater, never
    a direct field write. Non-existent ids are reported in failed_ids, not raised."""
    updater: StatusUpdater = request.app.state.updater
    updated_ids: list[int] = []
    failed_ids: list[int] = []
    for app_id in body.application_ids:
        try:
            updater.manual_update(app_id, body.status)
            updated_ids.append(app_id)
        except ValueError:
            failed_ids.append(app_id)
    if updated_ids:
        _invalidate_applications_cache()
    return BulkActionResponse(updated=len(updated_ids), failed_ids=failed_ids)


@router.post("/applications/bulk-delete", response_model=BulkActionResponse)
async def bulk_delete_applications(body: BulkDeleteRequest, request: Request) -> BulkActionResponse:
    """Delete a set of applications. Unlike the single-record DELETE endpoint, this does
    not auto-add a sender-suppress rule per row — that requires a live Gmail API call per
    application (get_thread_sender_domain), and doing that N times sequentially inside one
    HTTP request doesn't scale to a bulk operation. Use the single-delete endpoint (or a
    suppress rule added directly) if that's needed for a specific sender."""
    db: DataStore = request.app.state.db
    deleted_ids: list[int] = []
    failed_ids: list[int] = []
    for app_id in body.application_ids:
        if db.delete_application(app_id):
            deleted_ids.append(app_id)
        else:
            failed_ids.append(app_id)
    if deleted_ids:
        _invalidate_applications_cache()
    return BulkActionResponse(updated=len(deleted_ids), failed_ids=failed_ids)


_LINKEDIN_STATUS_RANK: dict[ApplicationStatus, int] = {
    ApplicationStatus.APPLIED: 0,
    ApplicationStatus.RESUME_SHORTLISTED: 1,
    ApplicationStatus.INTERVIEW_SCHEDULED: 2,
    ApplicationStatus.INTERVIEW_IN_PROGRESS: 3,
    ApplicationStatus.OFFER_NEGOTIATION: 4,
    ApplicationStatus.OFFER: 5,
    ApplicationStatus.JOINED: 6,
}

_LINKEDIN_VALID_MODES = {"applied", "archived", "interview"}


def _linkedin_predict(
    db: DataStore,
    company: str | None,
    role: str | None,
    mode: str,
) -> tuple[str, int | None]:
    """Return (predicted_action, existing_id) without touching the DB."""
    if not company and not role:
        return "no_data", None
    existing = (
        db.find_application_by_company_role(company or "", role or "")
        if (company and role)
        else None
    )
    if existing is None:
        return "create", None
    assert existing.id is not None
    if mode == "interview":
        current_rank = _LINKEDIN_STATUS_RANK.get(existing.current_status, -1)
        target_rank = _LINKEDIN_STATUS_RANK[ApplicationStatus.INTERVIEW_IN_PROGRESS]
        return ("update" if 0 <= current_rank < target_rank else "skip"), existing.id
    return "skip", existing.id


# NOTE: /applications/linkedin-import/preview and /confirmed must be registered
# before /applications/{id} (they are literal path segments, not captured as int id).


@router.post("/applications/linkedin-import/preview", response_model=LinkedInPreviewResponse)
async def linkedin_import_preview(
    body: LinkedInPreviewRequest, request: Request
) -> LinkedInPreviewResponse:
    """Parse pasted LinkedIn text with LLM (fallback: regex). No DB writes."""
    from backend.parser.linkedin_paste_parser import parse_linkedin_paste  # noqa: PLC0415
    from backend.parser.llm_extractor import LLMExtractor  # noqa: PLC0415

    if body.mode not in _LINKEDIN_VALID_MODES:
        raise HTTPException(
            status_code=400, detail="mode must be 'applied', 'archived', or 'interview'"
        )

    db: DataStore = request.app.state.db
    llm: LLMExtractor | None = getattr(request.app.state, "llm_extractor", None)

    entries: list[LinkedInPreviewEntry] = []
    garbage_lines: list[int] = []
    llm_used = False

    # Regex parser is always authoritative for company/role/date extraction.
    for e in parse_linkedin_paste(body.text):
        action, existing_id = _linkedin_predict(db, e.company, e.role, body.mode)
        entries.append(
            LinkedInPreviewEntry(
                company=e.company,
                role=e.role,
                applied_date=e.applied_date,
                predicted_action=action,
                existing_id=existing_id,
            )
        )

    # LLM classifies which lines are garbage (noise highlighting only, no DB impact).
    if llm is not None:
        llm_result = llm.classify_linkedin_garbage(body.text)
        if llm_result is not None:
            llm_used = True
            garbage_lines = llm_result.garbage_line_numbers

    return LinkedInPreviewResponse(entries=entries, garbage_lines=garbage_lines, llm_used=llm_used)


@router.post("/applications/linkedin-import/confirmed", response_model=LinkedInImportResponse)
async def linkedin_import_confirmed(
    body: LinkedInImportConfirmedRequest, request: Request
) -> LinkedInImportResponse:
    """Write user-confirmed LinkedIn entries to the database."""
    if body.mode not in _LINKEDIN_VALID_MODES:
        raise HTTPException(
            status_code=400, detail="mode must be 'applied', 'archived', or 'interview'"
        )

    db: DataStore = request.app.state.db
    updater: StatusUpdater = request.app.state.updater

    target_status = (
        ApplicationStatus.INTERVIEW_IN_PROGRESS
        if body.mode == "interview"
        else ApplicationStatus.APPLIED
    )
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)

    created = updated = skipped = failed = 0
    results: list[LinkedInImportEntryResult] = []

    for entry in body.entries:
        if not entry.include:
            continue
        try:
            applied_date = entry.applied_date or today
            existing: Application | None = None

            if entry.company and entry.role:
                existing = db.find_application_by_company_role(entry.company, entry.role)

            if existing is not None:
                assert existing.id is not None
                if body.mode == "interview":
                    current_rank = _LINKEDIN_STATUS_RANK.get(existing.current_status, -1)
                    target_rank = _LINKEDIN_STATUS_RANK[ApplicationStatus.INTERVIEW_IN_PROGRESS]
                    if 0 <= current_rank < target_rank:
                        updater.manual_update(existing.id, ApplicationStatus.INTERVIEW_IN_PROGRESS)
                        updated += 1
                        action = "updated"
                    else:
                        skipped += 1
                        action = "skipped"
                else:
                    skipped += 1
                    action = "skipped"
                results.append(
                    LinkedInImportEntryResult(
                        company=entry.company,
                        role=entry.role,
                        applied_date=applied_date,
                        action=action,
                        application_id=existing.id,
                    )
                )
            else:
                saved = updater.create_manual(
                    company=entry.company,
                    role=entry.role,
                    source_portal="LinkedIn",
                    job_url=None,
                    applied_date=applied_date,
                    target_status=target_status,
                )
                created += 1
                results.append(
                    LinkedInImportEntryResult(
                        company=entry.company,
                        role=entry.role,
                        applied_date=applied_date,
                        action="created",
                        application_id=saved.id,
                    )
                )

        except Exception as exc:
            log.warning(
                "linkedin_import_entry_failed",
                role=entry.role,
                company=entry.company,
                error=str(exc),
            )
            failed += 1
            results.append(
                LinkedInImportEntryResult(
                    company=entry.company,
                    role=entry.role,
                    applied_date=entry.applied_date,
                    action="failed",
                    application_id=None,
                )
            )

    log.info(
        "linkedin_import_confirmed",
        mode=body.mode,
        created=created,
        updated=updated,
        skipped=skipped,
        failed=failed,
    )
    if created or updated:
        _invalidate_applications_cache()
    return LinkedInImportResponse(
        created=created,
        updated=updated,
        skipped=skipped,
        failed=failed,
        entries=results,
    )


@router.delete("/applications/{id}")
def delete_application(id: int, request: Request) -> dict:
    db: DataStore = request.app.state.db
    application = db.get_application(id)
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")
    _suppress_sender_on_delete(application, db, request)
    db.delete_application(id)
    _invalidate_applications_cache()
    return {"deleted": True}


def _suppress_sender_on_delete(application: Application, db: DataStore, request: Request) -> None:
    """Look up the application's first Gmail thread's sender domain and add a suppress rule."""
    import json as _json
    import re as _re

    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        return
    poller = scheduler.poller
    if poller.service is None:
        return
    try:
        thread_ids = _json.loads(application.thread_ids or "[]")
        if not thread_ids:
            return
        domain = poller.get_thread_sender_domain(thread_ids[0])
        if not domain:
            return
        pattern = _re.escape(domain)
        existing = {r.sender_pattern for r in db.get_suppress_rules()}
        if pattern not in existing:
            db.add_suppress_rule(sender_pattern=pattern)
            log.info("suppress_rule_added_on_delete", app_id=application.id, domain=domain)
    except Exception as exc:
        log.warning("suppress_on_delete_failed", app_id=application.id, error=str(exc))


# ------------------------------------------------------------------ #
# Insights                                                             #
# ------------------------------------------------------------------ #


@router.get("/insights/flow")
async def get_insights_flow(request: Request) -> dict:
    cache_key = f"{_INSIGHTS_CACHE_PREFIX}flow"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached
    db: DataStore = request.app.state.db
    result = InsightsEngine(db).flow_data()
    cache.set_json(cache_key, result, INSIGHTS_CACHE_TTL_SECONDS)
    return result


@router.get("/insights/rejection")
async def get_rejection_data(request: Request) -> dict:
    cache_key = f"{_INSIGHTS_CACHE_PREFIX}rejection"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return cached
    db: DataStore = request.app.state.db
    result = InsightsEngine(db).rejection_data()
    cache.set_json(cache_key, result, INSIGHTS_CACHE_TTL_SECONDS)
    return result


@router.get("/insights", response_model=InsightReportResponse)
async def get_insights(request: Request) -> InsightReportResponse:
    cache_key = f"{_INSIGHTS_CACHE_PREFIX}report"
    cached = cache.get_json(cache_key)
    if cached is not None:
        return InsightReportResponse(**cached)
    db: DataStore = request.app.state.db
    report = InsightsEngine(db).generate_report()
    response = InsightReportResponse(
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
    cache.set_json(cache_key, response.model_dump(mode="json"), INSIGHTS_CACHE_TTL_SECONDS)
    return response


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


@router.post("/poller/trigger")
async def trigger_poll(request: Request) -> dict:
    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Poller not running")
    already_running = scheduler.poller.is_polling
    scheduler.trigger()
    return {"triggered": True, "skipped": already_running}


@router.post("/poller/backfill-portal")
def backfill_portal(sender_domains: list[str], request: Request) -> dict:
    """Re-scan Gmail for a portal's sender domains to pick up applications missed
    before a portal_rules.yaml rule existed for it — reclassifies threads already
    in the DB under the wrong portal, and creates any that were skipped entirely."""
    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Poller not running")
    poller = scheduler.poller
    if poller.service is None:
        raise HTTPException(
            status_code=503, detail="Gmail not authenticated — run setup_wizard.py reauth"
        )
    return poller.backfill_portal(sender_domains)


def _reauth_redirect_uri() -> str:
    return f"{PUBLIC_BASE_URL}/api/v1/poller/reauth/callback"


@router.post("/poller/reauth")
async def reauth_poller(request: Request) -> dict:
    """Mark the poller AUTH_REQUIRED and point the operator at the web re-auth flow."""
    db: DataStore = request.app.state.db
    db.update_poller_state(status="AUTH_REQUIRED", clear_error=True)
    return {
        "status": "AUTH_REQUIRED",
        "message": "Re-authentication required. Visit GET /api/v1/poller/reauth/start "
        "to begin, or run locally: python backend/setup_wizard.py reauth",
    }


@router.get("/poller/reauth/start")
async def reauth_start(request: Request, token: str | None = None) -> dict:
    """Return a Google authorization URL for the operator to open in their own browser.

    This is a redirect-based web OAuth flow, not the InstalledAppFlow local-server flow —
    it works from a remote deployment (no browser/local port needed on the backend host).
    redirect_uri must be registered on the OAuth client in Google Cloud Console.

    Gated by ADMIN_TOKEN: this endpoint mints the `state` that /callback trusts, so an
    unauthenticated caller could otherwise bind their own Gmail account into this app's
    keyring. /callback itself doesn't need the token — Google's redirect can't carry
    custom headers/params, so it's protected by the unguessable state alone, which only
    an authorized /start call can mint.
    """
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Poller not running")
    try:
        auth_url, state = scheduler.poller.build_reauth_url(_reauth_redirect_uri())
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="client_secret.json not found on server")
    request.app.state.reauth_state = state
    return {"auth_url": auth_url}


@router.get("/poller/reauth/callback")
async def reauth_callback(code: str, state: str, request: Request) -> dict:
    """Google redirects here after the operator approves access in their browser."""
    scheduler = getattr(request.app.state, "poller_scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Poller not running")
    expected_state = getattr(request.app.state, "reauth_state", None)
    if expected_state is None or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid or expired reauth state — start again")
    request.app.state.reauth_state = None

    db: DataStore = request.app.state.db
    try:
        scheduler.poller.complete_reauth(code, _reauth_redirect_uri())
    except Exception as exc:
        log.error("reauth_callback_failed", error=str(exc))
        raise HTTPException(status_code=502, detail="Re-authentication failed. Check server logs.")

    db.update_poller_state(status="RUNNING", clear_error=True)
    scheduler.trigger()
    return {"status": "RUNNING", "message": "Re-authentication complete."}


# ------------------------------------------------------------------ #
# Suppress rules                                                       #
# ------------------------------------------------------------------ #


@router.get("/suppress-rules", response_model=list[SuppressRuleResponse])
async def list_suppress_rules(request: Request) -> list[SuppressRuleResponse]:
    db: DataStore = request.app.state.db
    return [SuppressRuleResponse.model_validate(r) for r in db.get_suppress_rules()]


@router.post("/suppress-rules", response_model=SuppressRuleResponse, status_code=201)
async def create_suppress_rule(body: SuppressRuleCreate, request: Request) -> SuppressRuleResponse:
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


@router.get("/diagnostics", response_model=DiagnosticsResponse)
async def run_diagnostics(request: Request) -> DiagnosticsResponse:
    now = utc_now()
    runner = DiagnosticRunner()
    results = runner.run_all()
    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)
    return DiagnosticsResponse(
        passed=passed,
        failed=failed,
        results=[
            DiagnosticItemResponse(
                name=r.name,
                ok=r.ok,
                detail=r.detail,
                checked_at=r.checked_at,
            )
            for r in results
        ],
        checked_at=now,
    )


@router.get("/status", response_model=SystemStatusResponse)
async def get_system_status(request: Request) -> SystemStatusResponse:
    db: DataStore = request.app.state.db
    now = utc_now()
    started_at: datetime | None = getattr(request.app.state, "started_at", None)
    components: list[ComponentStatusResponse] = []

    # API Server — always operational if the request arrived
    components.append(
        ComponentStatusResponse(
            name="API Server",
            status="operational",
            description="FastAPI server is responding",
            checked_at=now,
        )
    )

    # Database
    db_total = 0
    db_size_kb = 0
    try:
        _, db_total = db.get_applications(ApplicationFilter(page_size=1))
        db_size_kb = DB_PATH.stat().st_size // 1024 if DB_PATH.exists() else 0
        components.append(
            ComponentStatusResponse(
                name="Database",
                status="operational",
                description=f"SQLite healthy — {db_total} applications, {db_size_kb} KB",
                checked_at=now,
            )
        )
    except Exception as exc:
        components.append(
            ComponentStatusResponse(
                name="Database",
                status="outage",
                description=f"Database error: {exc}",
                checked_at=now,
            )
        )

    # Gmail Poller
    poller = db.get_poller_state()
    if poller.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
        poller_status = "outage"
        poller_desc = "Authentication error — run: python backend/setup_wizard.py reauth"
    elif poller.status == "API_ERROR":
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

    components.append(
        ComponentStatusResponse(
            name="Gmail Poller",
            status=poller_status,
            description=poller_desc,
            checked_at=now,
        )
    )

    # Gmail Auth
    if poller.status in ("AUTH_ERROR", "AUTH_REQUIRED"):
        auth_status = "outage"
        auth_desc = "OAuth token invalid — re-run setup wizard"
    else:
        auth_status = "operational"
        auth_desc = "OAuth token valid"

    components.append(
        ComponentStatusResponse(
            name="Gmail Auth",
            status=auth_status,
            description=auth_desc,
            checked_at=now,
        )
    )

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
