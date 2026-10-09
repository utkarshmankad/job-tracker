"""Phase 3 collection endpoints (docs/phase-3-source-collection.md §4).

Two routers with deliberately different authentication:

``collector_router`` — used by the local collection agent. Every endpoint except
``/collector/enroll`` requires ``Authorization: Bearer jtc_<id>.<secret>``. The credential
can only start/finish runs, submit observation batches for sources in its scope and read
its own run status and metrics. It is never accepted by any other endpoint: the rest of
the API authenticates the browser session cookie only, so a leaked collector credential
cannot read applications, delete, merge, decide evidence or manage collectors.

``admin_router`` — mounted on the session-cookie + CSRF protected API like every other
user endpoint. A signed-in person creates, rotates and revokes collectors and inspects
sources, runs, metrics and the collector review queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend import config as app_config
from backend.api.auth import (
    AuthError,
    client_address,
    get_auth_service,
    require_user,
    sensitive_rate_limit,
)
from backend.api.routes import EvidenceDetailResponse, _evidence_detail, require_database
from backend.collection import credentials
from backend.collection.contract import (
    ObservationBatch,
    RunFinish,
    RunStart,
    error_message,
    is_valid_source_key,
)
from backend.collection.ingest import ObservationIngestor, RunNotOpenError
from backend.collection.resolution import collection_decider
from backend.db.collection_store import CollectionConflictError
from backend.db.data_store import DataStore
from backend.db.models import CollectionRun, CollectionSource, Collector, utc_now

log = structlog.get_logger(__name__)

collector_router = APIRouter()
admin_router = APIRouter()

_INVALID_CREDENTIAL = "Invalid or revoked collector credential."


# ------------------------------------------------------------------ #
# Collector authentication                                             #
# ------------------------------------------------------------------ #


def _db(request: Request) -> DataStore:
    require_database(request)
    db: DataStore = request.app.state.db
    return db


def _unauthorized() -> AuthError:
    return AuthError(
        401,
        "collector_unauthorized",
        _INVALID_CREDENTIAL,
        headers={"WWW-Authenticate": 'Bearer realm="job-tracker-collector"'},
    )


def require_collector(request: Request) -> Collector:
    """Authenticate the bearer credential. Never logs or echoes the secret; every failure
    returns the same 401 so a probe cannot tell unknown, rotated and revoked tokens apart."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    parsed = credentials.parse_credential(value) if scheme.lower() == "bearer" else None
    if parsed is None:
        raise _unauthorized()
    token_id, secret = parsed
    service = get_auth_service(request)
    service.enforce_rate_limit(
        "collector",
        token_id,
        app_config.COLLECTOR_RATE_LIMIT_REQUESTS,
        app_config.COLLECTOR_RATE_LIMIT_WINDOW_SECONDS,
    )
    db = _db(request)
    collector = db.get_collector_by_token_id(token_id)
    if (
        collector is None
        or collector.revoked_at is not None
        or not credentials.secret_matches(secret, collector.token_hash)
    ):
        log.warning("collector_auth_failed", token_id=token_id)
        raise _unauthorized()
    assert collector.id is not None
    db.touch_collector(collector.id)
    return collector


def _require_scope(collector: Collector, source_key: str) -> None:
    if source_key not in collector.scopes:
        log.warning("collector_scope_denied", collector_id=collector.id, source_key=source_key)
        raise HTTPException(status_code=403, detail="Source is not in this collector's scope.")


async def limit_body(request: Request) -> None:
    """Reject oversized payloads before they are parsed (Content-Length and actual size)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > app_config.COLLECTOR_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large.")
    if len(await request.body()) > app_config.COLLECTOR_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large.")


def _own_run(db: DataStore, collector: Collector, run_key: str) -> CollectionRun:
    run = db.get_collection_run_by_key(run_key)
    if run is None or run.collector_id != collector.id:
        # Another collector's run is indistinguishable from a missing one.
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


# ------------------------------------------------------------------ #
# Response models                                                      #
# ------------------------------------------------------------------ #


class RunResponse(BaseModel):
    id: int
    run_key: str
    source_key: str
    account_label: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None
    collector_version: str
    adapter_version: str
    items_seen: int
    observations_received: int
    created_count: int
    linked_count: int
    review_count: int
    unchanged_count: int
    error_count: int
    error_code: str | None
    error_message: str | None
    diagnostics: dict[str, Any]


def _run_response(run: CollectionRun, account_label: str | None = None) -> RunResponse:
    assert run.id is not None
    return RunResponse(
        id=run.id,
        run_key=run.run_key,
        source_key=run.source_key,
        account_label=account_label,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        collector_version=run.collector_version,
        adapter_version=run.adapter_version,
        items_seen=run.items_seen,
        observations_received=run.observations_received,
        created_count=run.created_count,
        linked_count=run.linked_count,
        review_count=run.review_count,
        unchanged_count=run.unchanged_count,
        error_count=run.error_count,
        error_code=run.error_code,
        error_message=run.error_message,
        diagnostics=run.diagnostics or {},
    )


class CollectorResponse(BaseModel):
    id: int
    name: str
    token_hint: str  # "jtc_<first 4 of token id>…" — never the secret
    scopes: list[str]
    state: str  # pending_enrollment | active | revoked
    created_at: datetime
    enrolled_at: datetime | None
    last_used_at: datetime | None
    rotated_at: datetime | None
    revoked_at: datetime | None


def _collector_response(collector: Collector) -> CollectorResponse:
    assert collector.id is not None
    if collector.revoked_at is not None:
        state = "revoked"
    elif collector.token_hash:
        state = "active"
    else:
        state = "pending_enrollment"
    return CollectorResponse(
        id=collector.id,
        name=collector.name,
        token_hint=f"{credentials.TOKEN_PREFIX}{collector.token_id[:4]}…",
        scopes=list(collector.scopes),
        state=state,
        created_at=collector.created_at,
        enrolled_at=collector.enrolled_at,
        last_used_at=collector.last_used_at,
        rotated_at=collector.rotated_at,
        revoked_at=collector.revoked_at,
    )


# ------------------------------------------------------------------ #
# Collector endpoints (bearer credential)                              #
# ------------------------------------------------------------------ #


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=20, max_length=64)


class EnrollResponse(BaseModel):
    collector_id: int
    credential: str
    scopes: list[str]
    contract_version: int


@collector_router.post("/collector/enroll", response_model=EnrollResponse)
async def enroll(body: EnrollRequest, request: Request, response: Response) -> EnrollResponse:
    """Exchange a single-use setup code for the collector credential. The credential is
    returned exactly once, over TLS, to the CLI that stores it in the keychain."""
    service = get_auth_service(request)
    service.enforce_rate_limit(
        "collector-enroll",
        client_address(request),
        app_config.COLLECTOR_ENROLL_RATE_LIMIT_ATTEMPTS,
        app_config.COLLECTOR_ENROLL_RATE_LIMIT_WINDOW_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"
    if not credentials.valid_code_format(body.code):
        raise HTTPException(status_code=400, detail="Invalid or expired setup code.")
    db = _db(request)
    secret = credentials.new_secret()
    collector = db.redeem_collector_enrollment(
        credentials.code_hash(body.code), credentials.secret_hash(secret)
    )
    if collector is None:
        log.warning("collector_enroll_failed")
        raise HTTPException(status_code=400, detail="Invalid or expired setup code.")
    assert collector.id is not None
    log.info("collector_enrolled", collector_id=collector.id, token_id=collector.token_id)
    return EnrollResponse(
        collector_id=collector.id,
        credential=credentials.IssuedToken(collector.token_id, secret).credential,
        scopes=list(collector.scopes),
        contract_version=app_config.COLLECTOR_CONTRACT_VERSION,
    )


@collector_router.get("/collector/me")
async def collector_me(collector: Collector = Depends(require_collector)) -> dict[str, Any]:
    return {"collector_id": collector.id, "name": collector.name, "scopes": collector.scopes}


@collector_router.post(
    "/collector/runs", response_model=RunResponse, dependencies=[Depends(limit_body)]
)
async def start_run(
    body: RunStart, request: Request, collector: Collector = Depends(require_collector)
) -> RunResponse:
    """Start (or, with the same run_key, resume) a collection run for one source."""
    _require_scope(collector, body.source_key)
    db = _db(request)
    assert collector.id is not None
    try:
        run, created = db.start_collection_run(
            run_key=body.run_key,
            collector_id=collector.id,
            source_key=body.source_key,
            account_label=body.account_label,
            collector_version=body.collector_version,
            adapter_version=body.adapter_version,
        )
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail="Run key already used.") from exc
    if created:
        log.info(
            "collector_run_started",
            collector_id=collector.id,
            run_id=run.id,
            source_key=body.source_key,
        )
    return _run_response(run, body.account_label)


@collector_router.post("/collector/runs/{run_key}/observations", dependencies=[Depends(limit_body)])
async def submit_observations(
    run_key: str,
    request: Request,
    response: Response,
    collector: Collector = Depends(require_collector),
) -> dict[str, Any]:
    """Submit one idempotent batch. Replaying a batch key returns the stored result without
    reprocessing; a batch key never seen before must be recent (replay window)."""
    db = _db(request)
    run = _own_run(db, collector, run_key)
    _require_scope(collector, run.source_key)
    try:
        batch = ObservationBatch.model_validate_json(await request.body())
    except ValidationError as exc:
        # Report field locations and error types only — never the submitted values.
        raise HTTPException(
            status_code=422,
            detail=[{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()][:20],
        ) from exc
    assert run.id is not None
    stored = db.get_collection_batch(run.id, batch.batch_key)
    if stored is not None:
        response.headers["Idempotent-Replay"] = "true"
        return stored.result
    age = abs((utc_now() - batch.sent_at).total_seconds())
    if age > app_config.COLLECTOR_BATCH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=422, detail="Batch is outside the replay window.")
    try:
        result = ObservationIngestor(db, collection_decider(db)).ingest_batch(run, batch)
    except RunNotOpenError as exc:
        raise HTTPException(status_code=409, detail="This run has already finished.") from exc
    saved = db.save_collection_batch(
        run.id, batch.batch_key, len(batch.observations), result.to_json()
    )
    return saved.result


@collector_router.post(
    "/collector/runs/{run_key}/finish",
    response_model=RunResponse,
    dependencies=[Depends(limit_body)],
)
async def finish_run(
    run_key: str,
    body: RunFinish,
    request: Request,
    collector: Collector = Depends(require_collector),
) -> RunResponse:
    db = _db(request)
    run = _own_run(db, collector, run_key)
    _require_scope(collector, run.source_key)
    if body.status != "succeeded" and body.error_code is None:
        raise HTTPException(status_code=422, detail="error_code is required for this status.")
    try:
        finished = db.finish_collection_run(
            run_key,
            status=body.status,
            items_seen=body.items_seen,
            error_code=body.error_code,
            error_message=error_message(body.error_code),
            diagnostics=dict(body.diagnostics),
        )
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail="Run already finished differently.") from exc
    log.info(
        "collector_run_finished",
        collector_id=collector.id,
        run_id=finished.id,
        status=finished.status,
        error_code=finished.error_code,
    )
    return _run_response(finished)


@collector_router.get("/collector/runs/{run_key}", response_model=RunResponse)
async def collector_run_status(
    run_key: str, request: Request, collector: Collector = Depends(require_collector)
) -> RunResponse:
    return _run_response(_own_run(_db(request), collector, run_key))


@collector_router.get("/collector/metrics")
async def collector_metrics(
    request: Request, collector: Collector = Depends(require_collector)
) -> dict[str, Any]:
    return _db(request).collection_metrics(collector.id)


# ------------------------------------------------------------------ #
# Admin endpoints (session cookie + CSRF, via the protected router)    #
# ------------------------------------------------------------------ #


class CollectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9 ._-]+$")
    scopes: list[str] = Field(min_length=1, max_length=20)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        bad = [s for s in value if not is_valid_source_key(s)]
        if bad:
            raise ValueError("unknown source key")
        return sorted(set(value))


class CollectorSetupResponse(BaseModel):
    collector: CollectorResponse
    setup_code: str
    expires_at: datetime
    command: str


def _issue_setup(db: DataStore, collector: Collector) -> CollectorSetupResponse:
    assert collector.id is not None
    code = credentials.new_enrollment_code()
    expires_at = utc_now() + timedelta(seconds=app_config.COLLECTOR_ENROLLMENT_TTL_SECONDS)
    db.add_collector_enrollment(collector.id, credentials.code_hash(code), expires_at)
    api_url = app_config.PUBLIC_BASE_URL.rstrip("/")
    return CollectorSetupResponse(
        collector=_collector_response(collector),
        setup_code=code,
        expires_at=expires_at,
        command=f"python scripts/collect.py enroll --api-url {api_url} --code {code}",
    )


@admin_router.get("/collectors", response_model=list[CollectorResponse])
async def list_collectors(request: Request) -> list[CollectorResponse]:
    return [_collector_response(c) for c in _db(request).list_collectors()]


@admin_router.post(
    "/collectors",
    response_model=CollectorSetupResponse,
    dependencies=[Depends(sensitive_rate_limit)],
)
async def create_collector(
    body: CollectorCreate, request: Request, response: Response
) -> CollectorSetupResponse:
    """Create a collector and a one-time setup code (shown once; expires in minutes)."""
    user = require_user(request)
    db = _db(request)
    collector = db.create_collector(
        name=body.name,
        token_id=credentials.new_token_id(),
        scopes=body.scopes,
        created_by=user.email,
    )
    response.headers["Cache-Control"] = "no-store"
    log.info("collector_created", collector_id=collector.id, scopes=collector.scopes)
    return _issue_setup(db, collector)


@admin_router.post(
    "/collectors/{collector_id}/rotate",
    response_model=CollectorSetupResponse,
    dependencies=[Depends(sensitive_rate_limit)],
)
async def rotate_collector(
    collector_id: int, request: Request, response: Response
) -> CollectorSetupResponse:
    """Invalidate the current credential immediately and issue a new setup code."""
    db = _db(request)
    try:
        collector = db.rotate_collector(collector_id, token_id=credentials.new_token_id())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Collector not found.") from exc
    except CollectionConflictError as exc:
        raise HTTPException(
            status_code=409, detail="A revoked collector cannot be rotated."
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    log.info("collector_rotated", collector_id=collector.id)
    return _issue_setup(db, collector)


@admin_router.post(
    "/collectors/{collector_id}/revoke",
    response_model=CollectorResponse,
    dependencies=[Depends(sensitive_rate_limit)],
)
async def revoke_collector(collector_id: int, request: Request) -> CollectorResponse:
    user = require_user(request)
    try:
        collector = _db(request).revoke_collector(collector_id, revoked_by=user.email)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Collector not found.") from exc
    log.info("collector_revoked", collector_id=collector.id)
    return _collector_response(collector)


class SourceResponse(BaseModel):
    id: int
    source_key: str
    account_label: str
    collector_id: int | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_status: str | None
    needs_attention: bool
    attention_reason: str | None
    attention_message: str | None
    items: int


def _source_response(source: CollectionSource, items: int) -> SourceResponse:
    assert source.id is not None
    return SourceResponse(
        id=source.id,
        source_key=source.source_key,
        account_label=source.account_label,
        collector_id=source.collector_id,
        last_attempt_at=source.last_attempt_at,
        last_success_at=source.last_success_at,
        last_status=source.last_status,
        needs_attention=source.needs_attention,
        attention_reason=source.attention_reason,
        attention_message=error_message(source.attention_reason)
        if source.needs_attention
        else None,
        items=items,
    )


@admin_router.get("/collection/sources", response_model=list[SourceResponse])
async def list_sources(request: Request) -> list[SourceResponse]:
    db = _db(request)
    items = db.collection_metrics()["items_by_source"]
    return [_source_response(s, items.get(s.source_key, 0)) for s in db.list_collection_sources()]


@admin_router.get("/collection/runs", response_model=list[RunResponse])
async def list_runs(
    request: Request, source_key: str | None = None, limit: int = 50
) -> list[RunResponse]:
    if source_key is not None and not is_valid_source_key(source_key):
        raise HTTPException(status_code=422, detail="Unknown source key.")
    limit = max(1, min(limit, 200))
    return [
        _run_response(r)
        for r in _db(request).list_collection_runs(source_key=source_key, limit=limit)
    ]


class ObservationSummary(BaseModel):
    id: int
    item_id: int
    observed_at: datetime
    extraction: str
    decision: str
    decision_reason: str | None
    confidence: float | None
    evidence_id: int | None
    company: str | None
    role: str | None
    status: str | None
    application_id: int | None


class RunDetailResponse(BaseModel):
    run: RunResponse
    observations: list[ObservationSummary]


@admin_router.get("/collection/runs/{run_id}", response_model=RunDetailResponse)
async def run_detail(run_id: int, request: Request) -> RunDetailResponse:
    db = _db(request)
    run = db.get_collection_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    observations = []
    for obs in db.list_run_observations(run_id):
        assert obs.id is not None
        item = db.get_source_item(obs.source_item_id)
        observations.append(
            ObservationSummary(
                id=obs.id,
                item_id=obs.source_item_id,
                observed_at=obs.observed_at,
                extraction=obs.extraction,
                decision=obs.decision,
                decision_reason=obs.decision_reason,
                confidence=obs.confidence,
                evidence_id=obs.evidence_id,
                company=obs.payload.get("company"),
                role=obs.payload.get("role"),
                status=obs.payload.get("status"),
                application_id=item.application_id if item else None,
            )
        )
    return RunDetailResponse(run=_run_response(run), observations=observations)


@admin_router.get("/collection/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    return _db(request).collection_metrics()


class CollectionReviewItem(BaseModel):
    evidence: EvidenceDetailResponse
    source_key: str
    company: str | None
    role: str | None
    status: str | None
    raw_status: str | None
    applied_on: str | None
    observed_at: datetime
    extraction: str


@admin_router.get("/collection/review", response_model=list[CollectionReviewItem])
async def collection_review(request: Request, limit: int = 100) -> list[CollectionReviewItem]:
    """Collected observations waiting for a person, with the minimal fields the collector
    sent. Decide them with /evidence/{id}/accept, /create-application or /dismiss."""
    db = _db(request)
    out = []
    for obs in db.collector_review_observations(max(1, min(limit, 200))):
        evidence = db.get_evidence(obs.evidence_id) if obs.evidence_id else None
        if evidence is None:
            continue
        payload = obs.payload or {}
        out.append(
            CollectionReviewItem(
                evidence=_evidence_detail(db, evidence),
                source_key=obs.source_key,
                company=payload.get("company"),
                role=payload.get("role"),
                status=payload.get("status"),
                raw_status=payload.get("raw_status"),
                applied_on=payload.get("applied_on"),
                observed_at=obs.observed_at,
                extraction=obs.extraction,
            )
        )
    return out
