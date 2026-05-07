"""
Thin FastAPI adapter for the aggregation service.

Supports two modes controlled by ``AGGREGATION_PROXY_ENABLED`` env var:

  - **Direct mode** (default, ``false``):
    Calls AggregationService in-process. Used for dev / single-process mode.

  - **Proxy mode** (``true``):
    Forwards all requests to the Aggregation Control Plane via HTTP.
    The viz-service becomes a transparent proxy — the Control Plane owns
    all job lifecycle logic.  This is the production deployment model.

This is the ONLY monolith file that imports FROM the aggregation package.
"""
import asyncio
import json as _json
import logging
import os
from typing import List, Optional

import httpx
from fastapi import (
    APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.engine import get_db_session
from backend.app.ontology import gate as ontology_gate
from backend.app.services.aggregation.schemas import ResumeOverrides
from backend.common.models.management import (
    OntologyResolutionResponse,
    OntologyResolutionRelGap,
    OntologyResolutionHierarchyGap,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Feature flag ────────────────────────────────────────────────────

_PROXY_ENABLED = os.getenv("AGGREGATION_PROXY_ENABLED", "false").lower() == "true"
_PROXY_BASE_URL = os.getenv("AGGREGATION_SERVICE_URL", "http://localhost:8091")

# ── Proxy client (lazy singleton) ──────────────────────────────────

_httpx_client: httpx.AsyncClient | None = None


def _get_proxy_client() -> httpx.AsyncClient:
    """Return a reusable httpx.AsyncClient pointed at the Control Plane."""
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(
            base_url=_PROXY_BASE_URL,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
    return _httpx_client


async def _proxy(method: str, path: str, request: Request, body: bytes | None = None) -> Response:
    """Forward a request to the Control Plane and return its response."""
    client = _get_proxy_client()
    try:
        # Forward query params as-is
        url = httpx.URL(path, params=dict(request.query_params))
        resp = await client.request(
            method,
            str(url),
            content=body,
            headers={"content-type": "application/json"} if body else {},
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="Aggregation Control Plane is unreachable. It may still be starting up.",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Aggregation Control Plane request timed out.",
        )


# ── Direct-mode dependencies (only imported when proxy is disabled) ─

def _get_svc(request: Request):
    """FastAPI dependency — retrieves AggregationService from app.state.

    In proxy mode, returns None (the endpoint short-circuits to the proxy
    before using svc). In direct mode, raises 503 if not yet initialized.
    """
    if _PROXY_ENABLED:
        return None
    svc = getattr(request.app.state, "aggregation_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Aggregation service is not available. The server may still be starting up.",
        )
    return svc


# ── Lazy imports for direct mode (avoid importing if proxy-only) ────

def _direct_imports():
    from backend.app.services.aggregation import (
        AggregationTriggerRequest,
        AggregationSkipRequest,
        AggregationScheduleRequest,
    )
    from backend.app.services.aggregation.service import ConflictError, NotFoundError
    return AggregationTriggerRequest, AggregationSkipRequest, AggregationScheduleRequest, ConflictError, NotFoundError


# ── Path mapping: viz-service paths -> Control Plane paths ──────────
# Viz-service mounts this router at /admin, so full paths are like:
#   /api/v1/admin/aggregation-jobs/summary
# The Control Plane uses:
#   /aggregation/jobs/summary


# ── GET /aggregation-jobs/summary ───────────────────────────────────

@router.get("/aggregation-jobs/summary", summary="Get aggregation job summary stats (KPIs)")
async def get_jobs_summary(
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", "/aggregation/jobs/summary", request)
    return await svc.get_jobs_summary(session)


# ── GET /aggregation-jobs (global) ──────────────────────────────────

@router.get("/aggregation-jobs", summary="List all aggregation jobs (global)")
async def list_jobs_global(
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
    job_status: Optional[List[str]] = Query(None, alias="status"),
    workspace_id: Optional[str] = Query(None, alias="workspaceId"),
    data_source_id: Optional[List[str]] = Query(None, alias="dataSourceId"),
    projection_mode: Optional[str] = Query(None, alias="projectionMode"),
    trigger_source: Optional[str] = Query(None, alias="triggerSource"),
    date_from: Optional[str] = Query(None, alias="dateFrom"),
    date_to: Optional[str] = Query(None, alias="dateTo"),
    search: Optional[str] = Query(None, alias="search"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", "/aggregation/jobs", request)
    paginated = await svc.list_jobs_global(
        session,
        status=job_status,
        workspace_id=workspace_id,
        data_source_ids=data_source_id,
        projection_mode=projection_mode,
        trigger_source=trigger_source,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=limit,
        offset=offset,
    )

    # Overlay live Redis HSET state on the running rows. Without this,
    # Job History (which calls this list endpoint at the relaxed 10s
    # cadence) sees the frozen DB values for any job currently inside
    # an outer batch — the durable counters only advance at outer-
    # batch boundaries by design. Per-row JobRow components also open
    # their own SSE for sub-second updates, but the list response is
    # the first paint and the polling-fallback source.
    #
    # Cost analysis: only running/pending rows hit Redis. Terminal
    # rows fall through to the durable DB values directly. Even at
    # the limit=100 cap, in practice the active subset is small
    # (operators don't run more than ~10 jobs concurrently). HSET
    # reads pipelined for cardinality-resilience.
    try:
        active_items = [
            it for it in paginated.items
            if it.status in ("running", "pending")
        ]
        if active_items:
            from backend.app.jobs import get_state_store
            store = get_state_store()
            for it in active_items:
                snap = await store.get(it.id)
                if not snap:
                    continue
                for field in (
                    "processed_edges", "total_edges", "created_edges", "progress",
                ):
                    raw = snap.get(field)
                    if raw is None:
                        continue
                    try:
                        parsed = int(raw)
                    except (TypeError, ValueError):
                        continue
                    current = getattr(it, field, 0) or 0
                    if parsed > current:
                        setattr(it, field, parsed)
                last_heartbeat = snap.get("last_heartbeat_at")
                if last_heartbeat and not it.last_checkpoint_at:
                    it.last_checkpoint_at = last_heartbeat
    except Exception as exc:
        # List-endpoint overlay must never fail the request. Per-row
        # SSE remains the primary path; this is best-effort enrichment.
        logger.debug(
            "list_jobs_global: live-state overlay failed (DB rows only): %s",
            exc,
        )

    return paginated


# ── POST /data-sources/{ds_id}/aggregation-jobs ─────────────────────

@router.post(
    "/data-sources/{ds_id}/aggregation-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger aggregation for a data source",
)
async def trigger_aggregation(
    ds_id: str,
    request: Request,
    response: Response,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
    trigger_source: str = Query("manual", alias="triggerSource"),
):
    # Resolution gate runs BEFORE the proxy short-circuit. The Control
    # Plane has no access to viz-service ontology tables, so the gate
    # must enforce here in both modes.
    #
    # Read-only path: the helper does ``session.get`` / ``session.execute``,
    # both of which trigger SQLAlchemy autobegin. We close that
    # transaction with ``rollback`` immediately so ``svc.trigger`` (which
    # opens its own ``session.begin()``) doesn't fail with
    # "A transaction is already begun on this Session".
    preflight = await _build_resolution_report(ds_id, session)
    await session.rollback()
    if preflight.kind == "ds_missing":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "data_source_not_found",
                "message": (
                    f"Data source {ds_id!r} was not found. The id may be from "
                    "a stale tab — refresh the workspace and try again."
                ),
            },
        )
    if preflight.kind == "ontology_unassigned":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ontology_not_assigned",
                "message": (
                    f"Data source {ds_id!r} has no ontology assigned. Configure "
                    "an ontology for this data source first."
                ),
                "resolution": _report_to_response(
                    ontology_gate.ResolutionReport(
                        resolved=False,
                        ontology_id=None,
                        ontology_version=None,
                        ontology_is_published=False,
                        blocking_reasons=["ontology_not_assigned"],
                    )
                ).model_dump(by_alias=True),
            },
        )
    if preflight.kind == "ontology_missing":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ontology_missing",
                "message": (
                    f"Data source {ds_id!r} references ontology "
                    f"{preflight.ontology_id!r} but no such ontology exists. "
                    "Reassign a valid ontology and retry."
                ),
            },
        )
    if preflight.kind != "ok" or preflight.report is None:
        # Defensive: any new kind we forgot to wire surfaces as a 500
        # rather than silently passing.
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected pre-flight result: {preflight.kind!r}",
        )
    pre_report = preflight.report
    if not pre_report.resolved:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ontology_unresolved",
                "message": (
                    "Ontology resolution gate failed: "
                    + ", ".join(pre_report.blocking_reasons)
                ),
                "resolution": _report_to_response(pre_report).model_dump(by_alias=True),
            },
        )

    if _PROXY_ENABLED:
        body = await request.body()
        return await _proxy(
            "POST",
            f"/aggregation/data-sources/{ds_id}/jobs?triggerSource={trigger_source}",
            request,
            body=body,
        )

    AggregationTriggerRequest, _, _, ConflictError, NotFoundError = _direct_imports()
    from backend.app.services.aggregation.service import OntologyResolutionError

    body_data = _json.loads(await request.body())
    body = AggregationTriggerRequest(**body_data)
    try:
        job = await svc.trigger(ds_id, body, trigger_source, session)
        response.headers["Location"] = (
            f"/api/v1/admin/data-sources/{ds_id}/aggregation-jobs/{job.id}"
        )
        return job
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except OntologyResolutionError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ontology_unresolved",
                "message": str(e),
                "resolution": _report_to_response(e.report).model_dump(by_alias=True),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Resolution gate (shared helper) ─────────────────────────────────


class _PreflightResult:
    """Outcome of the trigger-time pre-flight gate.

    Distinguishes between the three "no report" reasons so the
    endpoint can return a precise 422 instead of the catch-all
    "ontology not assigned" message that masked real bugs (deleted
    ontology rows, dangling ds_ids, mismatched IDs from the wizard,
    etc.).
    """
    __slots__ = ("kind", "report", "ds_id", "ontology_id")

    def __init__(self, *, kind: str, report=None, ds_id=None, ontology_id=None):
        self.kind = kind
        self.report = report
        self.ds_id = ds_id
        self.ontology_id = ontology_id


async def _build_resolution_report(
    ds_id: str, session: AsyncSession,
) -> _PreflightResult:
    """Run the ontology-resolution gate against the data source's
    assigned ontology and the cached graph schema stats.

    Returns a tagged ``_PreflightResult``:

    - ``kind="ok"`` — gate evaluated; ``report`` holds the result.
    - ``kind="ds_missing"`` — no ``WorkspaceDataSourceORM`` row for ``ds_id``.
    - ``kind="ontology_unassigned"`` — DS exists but ``ontology_id`` is null.
    - ``kind="ontology_missing"`` — ``ontology_id`` set but no row in
      ``ontologies`` (dangling FK, e.g. ontology hard-deleted while a
      data source still references it).

    Reads only viz-service-owned tables, so the same code runs in
    direct and proxy modes.
    """
    from backend.app.db.models import OntologyORM, WorkspaceDataSourceORM
    from backend.app.db.repositories.stats_repo import get_data_source_stats

    ds = await session.get(WorkspaceDataSourceORM, ds_id)
    if ds is None:
        logger.warning(
            "trigger_aggregation: WorkspaceDataSourceORM not found for ds_id=%s",
            ds_id,
        )
        return _PreflightResult(kind="ds_missing", ds_id=ds_id)
    if not ds.ontology_id:
        logger.info(
            "trigger_aggregation: ds_id=%s exists but has no ontology_id",
            ds_id,
        )
        return _PreflightResult(kind="ontology_unassigned", ds_id=ds_id)
    orm = await session.get(OntologyORM, ds.ontology_id)
    if orm is None:
        logger.warning(
            "trigger_aggregation: ds_id=%s references ontology_id=%s "
            "but the ontology row is missing (dangling FK)",
            ds_id, ds.ontology_id,
        )
        return _PreflightResult(
            kind="ontology_missing", ds_id=ds_id, ontology_id=ds.ontology_id,
        )

    stats_row = await get_data_source_stats(session, ds_id)
    entity_ids: List[str] = []
    edge_ids: List[str] = []
    if stats_row and stats_row.schema_stats:
        try:
            payload = _json.loads(stats_row.schema_stats)
            entity_stats = payload.get("entityTypeStats") or payload.get("entity_type_stats") or []
            edge_stats = payload.get("edgeTypeStats") or payload.get("edge_type_stats") or []
            entity_ids = [
                e.get("id") for e in entity_stats if isinstance(e, dict) and e.get("id")
            ]
            edge_ids = [
                e.get("id") for e in edge_stats if isinstance(e, dict) and e.get("id")
            ]
        except (TypeError, ValueError):
            pass

    report = ontology_gate.check_resolution(
        ontology_id=orm.id,
        ontology_version=orm.version,
        ontology_is_published=bool(orm.is_published),
        ontology_revision=getattr(orm, "revision", 0) or 0,
        entity_type_definitions_raw=_json.loads(orm.entity_type_definitions or "{}"),
        relationship_type_definitions_raw=_json.loads(orm.relationship_type_definitions or "{}"),
        introspected_entity_ids=entity_ids,
        introspected_edge_ids=edge_ids,
    )
    return _PreflightResult(kind="ok", report=report, ds_id=ds_id, ontology_id=orm.id)


def _report_to_response(
    report: ontology_gate.ResolutionReport,
) -> OntologyResolutionResponse:
    return OntologyResolutionResponse(
        resolved=report.resolved,
        ontologyId=report.ontology_id,
        ontologyVersion=report.ontology_version,
        ontologyIsPublished=report.ontology_is_published,
        missingEntityTypes=report.missing_entity_types,
        missingEdgeTypes=report.missing_edge_types,
        unclassifiedRelationships=[
            OntologyResolutionRelGap(
                id=g.id,
                name=g.name,
                isContainment=g.is_containment,
                isLineage=g.is_lineage,
            )
            for g in report.unclassified_relationships
        ],
        hasLineage=report.has_lineage,
        hasContainment=report.has_containment,
        hierarchyWarnings=[
            OntologyResolutionHierarchyGap(
                entityType=g.entity_type,
                missingField=g.missing_field,
            )
            for g in report.hierarchy_warnings
        ],
        advisoryWarnings=report.advisory_warnings,
        blockingReasons=report.blocking_reasons,
        fingerprint=report.fingerprint,
    )


# ── GET /data-sources/{ds_id}/ontology-resolution ───────────────────


@router.get(
    "/data-sources/{ds_id}/ontology-resolution",
    response_model=OntologyResolutionResponse,
    summary="Inspect the ontology-resolution gate for a data source",
)
async def get_ontology_resolution(
    ds_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Run the ontology-resolution gate against the assigned ontology
    and return the report. Drives the wizard's SchemaReviewStep and
    the SemanticStep warning banner.

    Always reads from the viz-service ontology DB even in proxy mode
    (the Control Plane never sees ontology rows), so this endpoint is
    not proxied.
    """
    preflight = await _build_resolution_report(ds_id, session)
    if preflight.kind == "ds_missing":
        raise HTTPException(status_code=404, detail=f"Data source {ds_id!r} not found")
    if preflight.kind == "ontology_unassigned":
        return OntologyResolutionResponse(
            resolved=False,
            ontologyId=None,
            ontologyVersion=None,
            ontologyIsPublished=False,
            blockingReasons=["ontology_not_assigned"],
        )
    if preflight.kind == "ontology_missing":
        return OntologyResolutionResponse(
            resolved=False,
            ontologyId=preflight.ontology_id,
            ontologyVersion=None,
            ontologyIsPublished=False,
            blockingReasons=["ontology_missing"],
        )
    if preflight.kind == "ok" and preflight.report is not None:
        return _report_to_response(preflight.report)
    raise HTTPException(
        status_code=500,
        detail=f"Unexpected pre-flight result: {preflight.kind!r}",
    )


# ── GET /data-sources/{ds_id}/readiness ─────────────────────────────

@router.get("/data-sources/{ds_id}/readiness", summary="Get aggregation readiness")
async def get_readiness(
    ds_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", f"/aggregation/data-sources/{ds_id}/readiness", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        return await svc.get_readiness(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /data-sources/{ds_id}/aggregation-jobs ──────────────────────

@router.get("/data-sources/{ds_id}/aggregation-jobs", summary="List aggregation jobs")
async def list_jobs(
    ds_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
    job_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", f"/aggregation/data-sources/{ds_id}/jobs", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        return await svc.list_jobs(ds_id, session, status=job_status, limit=limit)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /data-sources/{ds_id}/aggregation-jobs/{job_id} ─────────────

@router.get("/data-sources/{ds_id}/aggregation-jobs/{job_id}", summary="Get job status")
async def get_job(
    ds_id: str,
    job_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", f"/aggregation/data-sources/{ds_id}/jobs/{job_id}", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        response = await svc.get_job(ds_id, job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Overlay live state from Redis HSET. Between outer-batch PG
    # commits the DB row is "stale by design" (the platform's
    # liveness/durability split puts mid-batch counters in Redis).
    # Polling clients that hit this endpoint between outer-batch
    # boundaries get the live ``processed_edges`` / ``created_edges``
    # / ``progress`` from the HSET, falling through to the DB
    # values when the snapshot is absent (job already terminal,
    # TTL expired, or Redis down).
    try:
        from backend.app.jobs import get_state_store
        snapshot = await get_state_store().get(job_id)
        if snapshot:
            for field in (
                "processed_edges", "total_edges", "created_edges", "progress",
            ):
                raw = snapshot.get(field)
                if raw is None:
                    continue
                try:
                    parsed = int(raw)
                except (TypeError, ValueError):
                    continue
                # Only overlay when the live value is *ahead* of the
                # durable one. For terminal jobs, the DB row is the
                # source of truth and we don't want a stale Redis
                # snapshot to walk back the final number.
                current = getattr(response, field, 0) or 0
                if parsed > current:
                    setattr(response, field, parsed)
            last_heartbeat = snapshot.get("last_heartbeat_at")
            if last_heartbeat and not response.last_checkpoint_at:
                response.last_checkpoint_at = last_heartbeat
    except Exception as exc:
        # Live overlay must never fail a GET. Log + return the DB row.
        logger.debug(
            "get_job: live-state overlay failed (returning DB row only): %s",
            exc,
        )

    return response


# ── SSE: GET /data-sources/{ds_id}/aggregation-jobs/{job_id}/events ──


@router.get(
    "/data-sources/{ds_id}/aggregation-jobs/{job_id}/events",
    summary="Server-Sent Events stream of job progress",
)
async def stream_job_events(
    ds_id: str,
    job_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    """Server-Sent Events stream of platform ``JobEvent``s for one
    aggregation/purge job.

    Backed by ``JobEventConsumer``: backfills via the broker's
    replay path from ``Last-Event-ID`` (the consumer-emitted
    sequence number) then live-tails. The connection closes
    automatically when a ``terminal`` event lands.

    No authentication beyond the existing ``/api/v1/admin``
    middleware. Frame format follows the SSE spec: each event
    has ``id:`` (sequence), ``event:`` (type), and ``data:`` (JSON
    envelope) lines, terminated by a blank line.
    """
    # The consumer subscribes via ``broker.JobScope`` (single-job
    # scope) — separate type from the ``schemas.JobScope`` that
    # carries workspace_id/data_source_id on event envelopes. They
    # share a name in their respective namespaces by intent; import
    # by alias to disambiguate.
    from backend.app.jobs import get_consumer
    from backend.app.jobs.broker import JobScope as BrokerJobScope
    consumer = get_consumer()
    broker_scope = BrokerJobScope(job_id=job_id)

    from_seq: Optional[int]
    if last_event_id:
        try:
            from_seq = int(last_event_id)
        except ValueError:
            from_seq = 0
    else:
        from_seq = None

    async def _frames():
        try:
            async for event in consumer.stream(broker_scope, from_seq):
                if await request.is_disconnected():
                    return
                payload = event.model_dump_json()
                # SSE frame format. ``id`` is the sequence so a
                # client reconnecting via Last-Event-ID can resume
                # from where it left off. ``event`` is the type
                # (state/progress/phase/terminal/resync).
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.type}\n"
                    f"data: {payload}\n\n"
                ).encode("utf-8")
        except Exception as exc:
            logger.warning(
                "SSE stream %s: error during streaming: %s",
                job_id, exc, exc_info=True,
            )
            # Send a final ``error`` frame so the client surfaces
            # something rather than appearing to hang.
            yield (
                f"event: error\n"
                f"data: {{\"detail\": \"stream error\"}}\n\n"
            ).encode("utf-8")

    return StreamingResponse(
        _frames(),
        media_type="text/event-stream",
        headers={
            # ``no-cache`` is mandatory on SSE; nginx and friends will
            # otherwise buffer the entire response. ``X-Accel-Buffering``
            # disables Nginx's proxy_buffering specifically so frames
            # flow within sub-second latency.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── POST .../resume ─────────────────────────────────────────────────

@router.post(
    "/data-sources/{ds_id}/aggregation-jobs/{job_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a failed aggregation job",
)
async def resume_job(
    ds_id: str,
    job_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
    overrides: ResumeOverrides | None = Body(default=None),
):
    if _PROXY_ENABLED:
        # Forward the (possibly empty) body so the Control Plane can
        # apply the same overrides. body() returns b"" when no body
        # was sent — _proxy treats that as no content.
        body = await request.body()
        return await _proxy(
            "POST",
            f"/aggregation/data-sources/{ds_id}/jobs/{job_id}/resume",
            request,
            body=body if body else None,
        )
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        return await svc.resume(ds_id, job_id, session, overrides=overrides)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── POST .../cancel ─────────────────────────────────────────────────

@router.post(
    "/data-sources/{ds_id}/aggregation-jobs/{job_id}/cancel",
    summary="Cancel an aggregation job",
)
async def cancel_job(
    ds_id: str,
    job_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("POST", f"/aggregation/data-sources/{ds_id}/jobs/{job_id}/cancel", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        return await svc.cancel(ds_id, job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── DELETE /aggregation-jobs/{job_id} ───────────────────────────────

@router.delete(
    "/aggregation-jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a terminal aggregation job",
)
async def delete_job(
    job_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("DELETE", f"/aggregation/jobs/{job_id}", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        await svc.delete_job(job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── POST /data-sources/{ds_id}/purge-aggregation ───────────────────

@router.post(
    "/data-sources/{ds_id}/purge-aggregation",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Purge aggregated edges (asynchronous)",
)
async def purge_aggregation(
    ds_id: str,
    request: Request,
    response: Response,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    """Queue a purge job. Returns 202 with the job row immediately; the
    actual ``MATCH ... DELETE`` runs as a regular insights-service
    Redis Streams job, which gets us retry, DLQ, and crash recovery
    via XAUTOCLAIM (a FastAPI ``BackgroundTasks`` would die on
    rolling restart and leave the job stuck in ``running``).

    Frontend polls the returned ``jobId`` via the standard
    aggregation-jobs endpoints (Job History UI handles this).
    """
    if _PROXY_ENABLED:
        return await _proxy("POST", f"/aggregation/data-sources/{ds_id}/purge", request)

    from backend.insights_service.enqueue import enqueue_purge_job_force

    _, _, _, ConflictError, NotFoundError = _direct_imports()
    try:
        job = await svc.claim_purge_job(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Validate envelope fields before touching Redis. Each falsy here
    # would make ``enqueue_purge_job_safe`` silently return None and the
    # operator would see a generic 500. Surface the actual missing
    # field instead — the helper's silent guards stay in place as
    # defense-in-depth for any other accidental caller.
    from datetime import datetime, timezone

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    missing = [
        name
        for name, val in (
            ("job.id", job.id),
            ("data_source_id", ds_id),
            ("workspace_id", job.workspace_id),
        )
        if not val
    ]
    if missing:
        logger.error(
            "purge_aggregation: envelope rejected — empty field(s) %s "
            "(job.id=%r ds_id=%r workspace_id=%r)",
            missing, job.id, ds_id, job.workspace_id,
        )
        job.status = "failed"
        job.error_message = (
            f"Purge envelope rejected: missing required field(s): "
            f"{', '.join(missing)}"
        )
        now_iso = _now_iso()
        job.completed_at = now_iso
        job.updated_at = now_iso
        await session.commit()
        raise HTTPException(
            status_code=500,
            detail=(
                f"Purge could not be queued: required field(s) empty: "
                f"{', '.join(missing)}. Check the data source's workspace "
                f"assignment in aggregation_data_source_state."
            ),
        )

    # Hand the job off to the insights worker. The ``force`` variant
    # drops any stale dedup claim before XADD — duplicate-purge guard
    # is enforced at the DB layer via ``claim_purge_job`` above, so
    # the Redis claim is just a worker-side optimization we don't need
    # to defer to.
    msg_id = await enqueue_purge_job_force(job.id, ds_id, job.workspace_id)
    if msg_id is None:
        # Inputs were just validated above, so a None now is either
        # Redis-down or an XADD exception caught by enqueue_job_safe's
        # broad handler. Distinguish via PING and surface accordingly.
        from backend.app.services.aggregation.redis_client import get_redis as _get_redis_for_ping
        redis_reachable = False
        try:
            await asyncio.wait_for(_get_redis_for_ping().ping(), timeout=2.0)
            redis_reachable = True
        except Exception as ping_exc:
            logger.warning(
                "purge_aggregation: Redis PING failed for ds=%s: %s",
                ds_id, ping_exc,
            )

        # Mark the row failed so the user sees a terminal state in
        # Job History instead of a phantom "pending" they can never
        # cancel out of.
        job.status = "failed"
        if redis_reachable:
            # PING worked but enqueue still failed. The likely cause is
            # an XADD exception that ``enqueue_job_safe`` swallowed —
            # check its ``logger.exception`` line for the underlying
            # error.
            logger.error(
                "purge_aggregation: enqueue returned None despite Redis OK "
                "and validated envelope (job.id=%r ds_id=%r workspace_id=%r) — "
                "check enqueue_job_safe exception log for XADD failure",
                job.id, ds_id, job.workspace_id,
            )
            job.error_message = (
                "Failed to enqueue purge job: insights stream rejected the XADD "
                "(see backend logs)."
            )
            http_status = 500
            user_detail = (
                "Purge could not be queued: the insights stream rejected the "
                "write. Check backend logs for the underlying error."
            )
        else:
            job.error_message = (
                "Failed to enqueue purge job: insights queue unreachable"
            )
            http_status = 503
            user_detail = (
                "Purge could not be queued: insights worker queue is "
                "unavailable. The job has been recorded as failed; retry "
                "once Redis is reachable."
            )
        now_iso = _now_iso()
        job.completed_at = now_iso
        job.updated_at = now_iso
        await session.commit()
        raise HTTPException(status_code=http_status, detail=user_detail)

    response.headers["Location"] = (
        f"/api/v1/admin/data-sources/{ds_id}/aggregation-jobs/{job.id}"
    )
    return {
        "deletedEdges": 0,
        "dataSourceId": ds_id,
        "jobId": job.id,
        "status": "pending",
    }


# ── POST /data-sources/{ds_id}/skip-aggregation ────────────────────

@router.post("/data-sources/{ds_id}/skip-aggregation", summary="Skip aggregation")
async def skip_aggregation(
    ds_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        body = await request.body()
        return await _proxy("POST", f"/aggregation/data-sources/{ds_id}/skip", request, body=body)
    AggregationTriggerRequest, AggregationSkipRequest, _, _, NotFoundError = _direct_imports()
    import json
    body_data = json.loads(await request.body())
    body = AggregationSkipRequest(**body_data)
    try:
        return await svc.skip(ds_id, body, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── PUT /data-sources/{ds_id}/aggregation-schedule ──────────────────

@router.put(
    "/data-sources/{ds_id}/aggregation-schedule",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set aggregation schedule",
)
async def set_schedule(
    ds_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        body = await request.body()
        return await _proxy("PUT", f"/aggregation/data-sources/{ds_id}/schedule", request, body=body)
    _, _, AggregationScheduleRequest, _, NotFoundError = _direct_imports()
    import json
    body_data = json.loads(await request.body())
    body = AggregationScheduleRequest(**body_data)
    try:
        await svc.set_schedule(ds_id, body.cron_expression, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /data-sources/{ds_id}/check-drift ──────────────────────────

@router.get("/data-sources/{ds_id}/check-drift", summary="Check for graph drift")
async def check_drift(
    ds_id: str,
    request: Request,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(get_db_session),
):
    if _PROXY_ENABLED:
        return await _proxy("GET", f"/aggregation/data-sources/{ds_id}/drift", request)
    _, _, _, _, NotFoundError = _direct_imports()
    try:
        return await svc.check_drift(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
