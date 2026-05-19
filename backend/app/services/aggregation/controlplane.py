"""
Aggregation Control Plane — standalone FastAPI process.

Owns the aggregation API surface, job lifecycle, scheduling, crash
recovery, and drift detection. Dispatches job execution to Workers
(Data Plane) via Redis Streams.  Does NOT run any Worker logic —
heavy FalkorDB MERGE operations happen exclusively in the Workers.

Entry point:
    python -m backend.app.services.aggregation.controlplane

    or via uvicorn:
    uvicorn backend.app.services.aggregation.controlplane:app --port 8091

Architecture:
    - OWN ProviderManager with SHORT timeouts (5s) for drift checks and
      readiness probes. A slow FalkorDB returns degraded status rather
      than blocking the API.
    - OWN Postgres session factory (JOBS pool) for job state queries.
    - RedisStreamDispatcher for job dispatch to workers.
    - AggregationScheduler for periodic drift detection.
    - Crash recovery on startup (re-dispatches interrupted jobs).

Environment variables:
    MANAGEMENT_DB_URL          Postgres connection string (required)
    REDIS_URL                  Redis broker URL (default: redis://localhost:6380/0)
    FALKORDB_HOST              FalkorDB host (default: localhost)
    FALKORDB_PORT              FalkorDB port (default: 6379)
    FALKORDB_SOCKET_TIMEOUT    Socket timeout (default: 5 — short for API responsiveness)
    AGGREGATION_API_PORT       HTTP port (default: 8091)
    LOG_LEVEL                  Logging level (default: INFO)
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, Response, status,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Control Plane uses SHORT FalkorDB timeout — API responsiveness > completeness.
# Drift checks and readiness probes that hit FalkorDB will timeout quickly
# and return a degraded response rather than blocking.
if "FALKORDB_SOCKET_TIMEOUT" not in os.environ:
    os.environ["FALKORDB_SOCKET_TIMEOUT"] = "5"

# Small connection pools — Control Plane only does lightweight reads,
# not heavy MERGE operations.
if "FALKORDB_GRAPH_POOL_SIZE" not in os.environ:
    os.environ["FALKORDB_GRAPH_POOL_SIZE"] = "4"
if "FALKORDB_REDIS_POOL_SIZE" not in os.environ:
    os.environ["FALKORDB_REDIS_POOL_SIZE"] = "4"


# ── Lifespan ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Control Plane startup / shutdown lifecycle."""
    from backend.app.db.engine import close_db, get_jobs_session
    from backend.app.providers.manager import ProviderManager
    from .service import AggregationService
    from .dispatcher import RedisStreamDispatcher
    from .scheduler import AggregationScheduler
    from .redis_client import get_redis, close_redis
    from .db_init import init_aggregation_db

    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("=== Aggregation Control Plane starting ===")

    # 1. Initialize aggregation DB (schema + tables, no Alembic needed)
    await init_aggregation_db()

    # 2. ProviderManager with short timeouts for drift/readiness
    registry = ProviderManager()

    # 3. Redis dispatcher
    redis_client = get_redis()
    dispatcher = RedisStreamDispatcher(redis_client)

    # 4. Ontology service (for monolith-mode resolution during trigger)
    ontology_svc = None
    try:
        from backend.app.ontology.adapters.sqlalchemy_repo import SQLAlchemyOntologyRepository
        from backend.app.ontology.service import LocalOntologyService
        ontology_svc = LocalOntologyService(
            SQLAlchemyOntologyRepository(None)
        )
    except Exception:
        logger.warning("Ontology service not available — will use DB fallback")

    # 5. AggregationService
    svc = AggregationService(
        dispatcher=dispatcher,
        registry=registry,
        session_factory=get_jobs_session,
        ontology_service=ontology_svc,
    )

    # 6. Crash recovery — OFF the startup critical path. It re-dispatches
    # interrupted jobs with a per-job exponential backoff sleep, which can
    # take minutes. Awaiting it here would delay the lifespan `yield`, so
    # uvicorn would not serve `/health` and the Docker healthcheck would
    # never pass. Run it as a background task instead.
    async def _run_crash_recovery() -> None:
        try:
            recovered = await svc.recover_interrupted_jobs()
            if recovered:
                logger.info("Recovered %d interrupted aggregation jobs", recovered)
        except Exception as e:
            logger.warning("Crash recovery failed: %s", e)

    recovery_task = asyncio.create_task(_run_crash_recovery())

    # 7. Start scheduler (drift detection)
    scheduler = AggregationScheduler(get_jobs_session, registry)
    scheduler_task = asyncio.create_task(scheduler.start())
    logger.info("Aggregation scheduler started")

    # Store in app state
    app.state.aggregation_service = svc
    app.state.session_factory = get_jobs_session

    port = os.getenv("AGGREGATION_API_PORT", "8091")
    logger.info("=== Aggregation Control Plane ready (port %s) ===", port)

    yield

    # Shutdown
    await scheduler.stop()
    scheduler_task.cancel()
    recovery_task.cancel()
    try:
        await asyncio.wait_for(registry.evict_all(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("Provider shutdown timed out")
    await close_redis()
    await close_db()
    logger.info("=== Aggregation Control Plane stopped ===")


# ── App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Aggregation Control Plane",
    description="Aggregation job lifecycle, scheduling, and status API.",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Dependencies ────────────────────────────────────────────────────

def _get_svc(request: Request):
    """FastAPI dependency — retrieves AggregationService from app.state."""
    from .service import AggregationService
    svc = getattr(request.app.state, "aggregation_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Aggregation service is not available. The server may still be starting up.",
        )
    return svc


async def _get_session(request: Request):
    """FastAPI dependency — yields a DB session from the JOBS pool."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Database not available")
    async with session_factory() as session:
        yield session


# ── Schemas (import here to avoid circular) ─────────────────────────

from .schemas import (  # noqa: E402
    AggregationTriggerRequest,
    AggregationSkipRequest,
    AggregationScheduleRequest,
    AggregationJobResponse,
    PaginatedJobsResponse,
    DataSourceReadinessResponse,
    DriftCheckResponse,
)
from .service import ConflictError, NotFoundError  # noqa: E402


# ── Health ──────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    """Health check for K8s liveness/readiness probes."""
    return {"status": "healthy", "role": "aggregation-controlplane"}


# ── GET /aggregation/jobs/summary ───────────────────────────────────

@app.get("/aggregation/jobs/summary", summary="Job summary KPIs")
async def get_jobs_summary(
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    return await svc.get_jobs_summary(session)


# ── GET /aggregation/jobs (global, cross-workspace) ─────────────────

@app.get(
    "/aggregation/jobs",
    response_model=PaginatedJobsResponse,
    summary="List all jobs (global)",
)
async def list_jobs_global(
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
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
    return await svc.list_jobs_global(
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


# ── POST /aggregation/data-sources/{ds_id}/jobs ─────────────────────

@app.post(
    "/aggregation/data-sources/{ds_id}/jobs",
    response_model=AggregationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger aggregation",
)
async def trigger_aggregation(
    ds_id: str,
    body: AggregationTriggerRequest,
    response: Response,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
    trigger_source: str = Query("manual", alias="triggerSource"),
):
    try:
        job = await svc.trigger(ds_id, body, trigger_source, session)
        response.headers["Location"] = (
            f"/aggregation/data-sources/{ds_id}/jobs/{job.id}"
        )
        return job
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /aggregation/data-sources/{ds_id}/readiness ──────────────────

@app.get(
    "/aggregation/data-sources/{ds_id}/readiness",
    response_model=DataSourceReadinessResponse,
    summary="Get aggregation readiness",
)
async def get_readiness(
    ds_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.get_readiness(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /aggregation/data-sources/{ds_id}/jobs ───────────────────────

@app.get(
    "/aggregation/data-sources/{ds_id}/jobs",
    response_model=list[AggregationJobResponse],
    summary="List jobs for a data source",
)
async def list_jobs(
    ds_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
    job_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return await svc.list_jobs(ds_id, session, status=job_status, limit=limit)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /aggregation/data-sources/{ds_id}/jobs/{job_id} ─────────────

@app.get(
    "/aggregation/data-sources/{ds_id}/jobs/{job_id}",
    response_model=AggregationJobResponse,
    summary="Get job status",
)
async def get_job(
    ds_id: str,
    job_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.get_job(ds_id, job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── POST /aggregation/data-sources/{ds_id}/jobs/{job_id}/resume ──────

@app.post(
    "/aggregation/data-sources/{ds_id}/jobs/{job_id}/resume",
    response_model=AggregationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a failed job",
)
async def resume_job(
    ds_id: str,
    job_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.resume(ds_id, job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── POST /aggregation/data-sources/{ds_id}/jobs/{job_id}/cancel ──────

@app.post(
    "/aggregation/data-sources/{ds_id}/jobs/{job_id}/cancel",
    response_model=AggregationJobResponse,
    summary="Cancel a job",
)
async def cancel_job(
    ds_id: str,
    job_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.cancel(ds_id, job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── DELETE /aggregation/jobs/{job_id} ────────────────────────────────

@app.delete(
    "/aggregation/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a terminal job",
)
async def delete_job(
    job_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        await svc.delete_job(job_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── POST /aggregation/data-sources/{ds_id}/purge ────────────────────

@app.post(
    "/aggregation/data-sources/{ds_id}/purge",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Purge aggregated edges (asynchronous)",
)
async def purge_aggregation(
    ds_id: str,
    response: Response,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    """Claim a purge slot and hand off to the insights-service worker.
    The provider DELETE runs as a Redis Streams job with retry, DLQ,
    and crash recovery (see ``backend/insights_service/purge.py``)."""
    from backend.insights_service.enqueue import enqueue_purge_job_safe

    try:
        job = await svc.claim_purge_job(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    await enqueue_purge_job_safe(job.id, ds_id, job.workspace_id)
    response.headers["Location"] = (
        f"/aggregation/data-sources/{ds_id}/jobs/{job.id}"
    )
    return {
        "deletedEdges": 0,
        "dataSourceId": ds_id,
        "jobId": job.id,
        "status": "pending",
    }


# ── POST /aggregation/data-sources/{ds_id}/skip ─────────────────────

@app.post(
    "/aggregation/data-sources/{ds_id}/skip",
    response_model=DataSourceReadinessResponse,
    summary="Skip aggregation",
)
async def skip_aggregation(
    ds_id: str,
    body: AggregationSkipRequest,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.skip(ds_id, body, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── PUT /aggregation/data-sources/{ds_id}/schedule ───────────────────

@app.put(
    "/aggregation/data-sources/{ds_id}/schedule",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set aggregation schedule",
)
async def set_schedule(
    ds_id: str,
    body: AggregationScheduleRequest,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        await svc.set_schedule(ds_id, body.cron_expression, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── GET /aggregation/data-sources/{ds_id}/drift ──────────────────────

@app.get(
    "/aggregation/data-sources/{ds_id}/drift",
    response_model=DriftCheckResponse,
    summary="Check for graph drift",
)
async def check_drift(
    ds_id: str,
    svc=Depends(_get_svc),
    session: AsyncSession = Depends(_get_session),
):
    try:
        return await svc.check_drift(ds_id, session)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── CLI entry point ─────────────────────────────────────────────────

def _main() -> None:
    """Run the Control Plane as a standalone process."""
    import uvicorn

    port = int(os.getenv("AGGREGATION_API_PORT", "8091"))
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    uvicorn.run(
        "backend.app.services.aggregation.controlplane:app",
        host="0.0.0.0",
        port=port,
        log_level=log_level,
        access_log=True,
    )


if __name__ == "__main__":
    _main()
