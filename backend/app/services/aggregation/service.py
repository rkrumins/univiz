"""
AggregationService — REST API-driven aggregation orchestrator.

Owns: job lifecycle, concurrent job guards, ontology resolution,
      crash recovery, scheduling.
Does NOT own: batch materialization (that's the Worker).

This class has NO FastAPI imports. No HTTP concepts. Pure domain logic.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .dispatcher import AggregationDispatcher
from .models import AggregationJobORM
from .reservation import claim_exclusive
from .schemas import (
    AggregationJobResponse,
    AggregationSkipRequest,
    AggregationTriggerRequest,
    DataSourceReadinessResponse,
    DriftCheckResponse,
    PaginatedJobsResponse,
    ResumeOverrides,
)
from .fingerprint import compute_graph_fingerprint, fingerprints_match
from backend.app.ontology import gate as ontology_gate
from backend.app.ontology import runtime as ontology_runtime

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    import uuid
    return f"agg_{uuid.uuid4().hex[:12]}"


class AggregationService:
    """REST API-driven aggregation orchestrator.

    Dependency injection: accepts Dispatcher and ProviderRegistry protocols.
    No FastAPI imports. No HTTP concepts. Pure domain logic.

    Args:
        dispatcher: Dispatches job_id to a worker (in-process or message queue)
        registry: ProviderRegistry to look up graph providers
        session_factory: Async session context manager
        ontology_service: Direct reference to ontology service (monolith mode)
    """

    def __init__(
        self,
        dispatcher: AggregationDispatcher,
        registry: Any,
        session_factory: Any,
        ontology_service: Any = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._session_factory = session_factory
        self._ontology_service = ontology_service

    # ── Trigger (with concurrent job guard + ontology resolution) ──────

    async def trigger(
        self,
        ds_id: str,
        request: AggregationTriggerRequest,
        trigger_source: str,
        session: AsyncSession,
    ) -> AggregationJobResponse:
        """Create and dispatch an aggregation job.

        1. CHECK: Is there already an active job for this data source?
           → If yes: raise ConflictError (409)
        2. RESOLVE: Call ontology service to get containment/lineage edge types
           → If no ontology assigned: raise ValueError (422)
        3. CREATE: AggregationJobORM with frozen edge types
        4. DISPATCH: dispatcher.dispatch(job_id)
        5. RETURN: AggregationJobResponse
        """
        from .models import AggregationDataSourceStateORM

        # ── Phase 2.2 — idempotency early-return (read-only fast path) ──
        # Two POSTs sharing a key for the same data source within 60 min
        # collapse to the original job (200 with the existing job ID).
        # Done outside the transaction so non-key-bearing callers don't
        # pay a lookup cost.
        #
        # The replay is ontology-aware. The prior job freezes
        # ``containment_edge_types`` / ``lineage_edge_types`` at trigger
        # time, so an unconditional replay would mask edits the user made
        # to the ontology between runs (the original "edits not picked
        # up on re-run" bug). Replay only short-circuits when the prior
        # job's ``ontology_fingerprint`` matches the current ontology —
        # otherwise the prior job is treated as stale and we fall
        # through to a fresh resolve.
        idem_key = request.idempotency_key
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(minutes=60)
        ).isoformat()
        if idem_key:
            existing_idem = (
                await session.execute(
                    select(AggregationJobORM)
                    .where(AggregationJobORM.data_source_id == ds_id)
                    .where(AggregationJobORM.idempotency_key == idem_key)
                    .where(AggregationJobORM.created_at >= cutoff_iso)
                    .order_by(AggregationJobORM.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_idem is not None:
                if await self._replay_fingerprint_matches(
                    existing_idem, ds_id, session,
                ):
                    logger.info(
                        "Idempotent replay for data source %s — returning job %s "
                        "(idempotency_key=%s)",
                        ds_id, existing_idem.id, idem_key,
                    )
                    return self._to_response(existing_idem)
                logger.info(
                    "Skipping idempotent replay for data source %s job %s — "
                    "ontology fingerprint changed since first trigger; "
                    "running a fresh resolve",
                    ds_id, existing_idem.id,
                )

        # ── Atomic claim (Phase 2 §2.1) ────────────────────────────
        # `claim_exclusive` combines a pg_try_advisory_xact_lock with
        # SELECT ... FOR UPDATE SKIP LOCKED so concurrent triggers across
        # uvicorn workers / replicas serialize cleanly: at most one caller
        # ever observes "no active job" for a given data_source_id.
        async with session.begin():
            if not await claim_exclusive(session, ds_id):
                if idem_key:
                    replay = (
                        await session.execute(
                            select(AggregationJobORM)
                            .where(AggregationJobORM.data_source_id == ds_id)
                            .where(AggregationJobORM.idempotency_key == idem_key)
                            .where(AggregationJobORM.created_at >= cutoff_iso)
                            .order_by(AggregationJobORM.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if replay is not None and await self._replay_fingerprint_matches(
                        replay, ds_id, session,
                    ):
                        return self._to_response(replay)
                raise ConflictError(
                    "An aggregation job is already active for this data source"
                )

            # Resolve ontology via direct service call (CRIT-5)
            ontology_data = await self._resolve_ontology(ds_id, session)

            # ── Graph-level guard ──────────────────────────────────
            # Multiple data sources can point to the same FalkorDB graph.
            # Running parallel jobs on the same graph is wasteful: the
            # Redis idempotency sets (keyed by graph_name) mean only the
            # first job creates AGGREGATED edges — subsequent jobs process
            # edges but produce nothing because SADD returns 0.
            # Block the trigger if another data source on the same graph
            # already has an active job.
            graph_name = ontology_data.get("graph_name")
            if graph_name:
                conflicting_job = (
                    await session.execute(
                        select(AggregationJobORM)
                        .where(AggregationJobORM.graph_name == graph_name)
                        .where(AggregationJobORM.data_source_id != ds_id)
                        .where(AggregationJobORM.status.in_(["pending", "running"]))
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if conflicting_job:
                    raise ConflictError(
                        f"Another aggregation job ({conflicting_job.id}) is already "
                        f"active on graph '{graph_name}' via data source "
                        f"{conflicting_job.data_source_id}. Wait for it to complete "
                        f"or cancel it first."
                    )
            containment_types = ontology_data.get("containment_edge_types", [])
            lineage_types = ontology_data.get("lineage_edge_types", [])
            # Empty-lineage is no longer possible here — the gate's
            # ``no_lineage`` blocking reason already raised
            # OntologyResolutionError inside ``_resolve_ontology``. The
            # frozen list still gets persisted for the worker.

            # Create job with frozen edge types + denormalized graph metadata
            job_kwargs = dict(
                id=_generate_id(),
                data_source_id=ds_id,
                workspace_id=ontology_data.get("workspace_id"),
                provider_id=ontology_data.get("provider_id"),
                graph_name=ontology_data.get("graph_name"),
                data_source_label=ontology_data.get("data_source_label"),
                ontology_id=ontology_data.get("ontology_id"),
                ontology_fingerprint=ontology_data.get("ontology_fingerprint"),
                projection_mode=request.projection_mode,
                containment_edge_types=json.dumps(containment_types),
                lineage_edge_types=json.dumps(lineage_types),
                status="pending",
                trigger_source=trigger_source,
                batch_size=request.batch_size,
                idempotency_key=idem_key,
                # Per-job overrides: when None, the worker / ORM defaults
                # apply (timeout_secs → _JOB_TIMEOUT_SECS env, max_retries → 3).
                timeout_secs=request.timeout_secs,
                created_at=_now(),
            )
            # Only set max_retries when caller supplied one, so the ORM
            # column default (3) is honoured for backwards-compat callers.
            if request.max_retries is not None:
                job_kwargs["max_retries"] = request.max_retries
            job = AggregationJobORM(**job_kwargs)
            session.add(job)

            # Update aggregation-owned state table
            await self._upsert_ds_state(session, ds_id, aggregation_status="pending")
            # session.begin() commits on context exit

        # Dispatch AFTER commit so the worker sees the persisted row
        await self._dispatcher.dispatch(job.id)

        logger.info(
            "Aggregation job %s created for data source %s (trigger: %s, edges: %s)",
            job.id, ds_id, trigger_source, lineage_types,
        )

        return self._to_response(job)

    # ── Skip (user opts out with double confirmation) ─────────────────

    async def skip(
        self,
        ds_id: str,
        request: AggregationSkipRequest,
        session: AsyncSession,
    ) -> DataSourceReadinessResponse:
        """Mark data source as 'skipped' — views can be created but without aggregated edges."""
        if not request.confirmed:
            raise ValueError(
                "You must confirm skipping aggregation by setting confirmed=true"
            )

        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if not state:
            raise NotFoundError(f"Data source {ds_id} not found in aggregation state")

        state.aggregation_status = "skipped"
        await session.commit()

        logger.info("Aggregation skipped for data source %s", ds_id)

        return await self.get_readiness(ds_id, session)

    # ── Status ────────────────────────────────────────────────────────

    async def get_readiness(
        self, ds_id: str, session: AsyncSession
    ) -> DataSourceReadinessResponse:
        """Get the aggregation readiness status for a data source."""
        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if not state:
            # No state row yet — data source exists but aggregation never configured
            return DataSourceReadinessResponse(
                data_source_id=ds_id,
                is_ready=False,
                aggregation_status="none",
                can_create_views=False,
                active_job=None,
                drift_detected=False,
                last_aggregated_at=None,
                aggregation_edge_count=0,
                message="Aggregation has not been configured. Aggregate or skip to create views.",
            )

        # Find active job (if any)
        active_result = await session.execute(
            select(AggregationJobORM)
            .where(AggregationJobORM.data_source_id == ds_id)
            .where(AggregationJobORM.status.in_(["pending", "running"]))
            .order_by(AggregationJobORM.created_at.desc())
        )
        active_job = active_result.scalar_one_or_none()

        status = state.aggregation_status or "none"

        can_create = status in ("ready", "skipped")
        is_ready = status == "ready"

        # Check for drift using the aggregation-owned fingerprint
        drift = False
        _DRIFT_TIMEOUT = float(__import__("os").getenv("SCHEDULER_DRIFT_CHECK_TIMEOUT", "5"))
        if is_ready and state.graph_fingerprint:
            try:
                provider = await asyncio.wait_for(
                    self._registry.get_provider_for_workspace(
                        state.workspace_id, session, data_source_id=ds_id,
                    ),
                    timeout=_DRIFT_TIMEOUT,
                )
                current_fp = await asyncio.wait_for(
                    compute_graph_fingerprint(provider),
                    timeout=_DRIFT_TIMEOUT,
                )
                drift = not fingerprints_match(state.graph_fingerprint, current_fp)
            except Exception:
                pass  # Can't check drift — don't block

        messages = {
            "none": "Aggregation has not been configured. Aggregate or skip to create views.",
            "pending": "Aggregation is queued and will start shortly.",
            "running": "Aggregation is in progress.",
            "ready": "Aggregation complete. Views can be created.",
            "failed": "Aggregation failed. You can retry or skip.",
            "skipped": "Aggregation was skipped. Views can be created without aggregated edges.",
        }

        return DataSourceReadinessResponse(
            data_source_id=ds_id,
            is_ready=is_ready,
            aggregation_status=status,
            can_create_views=can_create,
            active_job=self._to_response(active_job) if active_job else None,
            drift_detected=drift,
            last_aggregated_at=state.last_aggregated_at,
            aggregation_edge_count=state.aggregation_edge_count or 0,
            message=messages.get(status, "Unknown status."),
        )

    async def get_job(
        self, ds_id: str, job_id: str, session: AsyncSession
    ) -> AggregationJobResponse:
        """Get a specific aggregation job."""
        job = await session.get(AggregationJobORM, job_id)
        if not job or job.data_source_id != ds_id:
            raise NotFoundError(f"Aggregation job {job_id} not found")
        return self._to_response(job)

    async def list_jobs(
        self,
        ds_id: str,
        session: AsyncSession,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[AggregationJobResponse]:
        """List aggregation jobs for a data source."""
        query = (
            select(AggregationJobORM)
            .where(AggregationJobORM.data_source_id == ds_id)
            .order_by(AggregationJobORM.created_at.desc())
            .limit(limit)
        )
        if status:
            query = query.where(AggregationJobORM.status == status)

        result = await session.execute(query)
        return [self._to_response(j) for j in result.scalars()]

    # ── Global summary (KPI stats) ─────────────────────────────────

    async def get_jobs_summary(self, session: AsyncSession) -> dict:
        """Return aggregate KPI stats across all aggregation jobs."""
        # Count by status
        status_q = (
            select(
                AggregationJobORM.status,
                func.count(AggregationJobORM.id),
            )
            .group_by(AggregationJobORM.status)
        )
        status_rows = (await session.execute(status_q)).all()
        by_status = {row[0]: row[1] for row in status_rows}
        total = sum(by_status.values())

        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        denominator = completed + failed
        success_rate = round(completed / denominator * 100, 1) if denominator > 0 else None

        # Average duration for completed jobs (from started_at to completed_at)
        dur_q = (
            select(
                AggregationJobORM.started_at,
                AggregationJobORM.completed_at,
            )
            .where(AggregationJobORM.status == "completed")
            .where(AggregationJobORM.started_at.isnot(None))
            .where(AggregationJobORM.completed_at.isnot(None))
            .order_by(AggregationJobORM.created_at.desc())
            .limit(50)  # last 50 completed jobs
        )
        dur_rows = (await session.execute(dur_q)).all()
        durations = []
        for started_at, completed_at in dur_rows:
            try:
                s = datetime.fromisoformat(started_at)
                e = datetime.fromisoformat(completed_at)
                durations.append((e - s).total_seconds())
            except Exception:
                pass
        avg_duration = round(sum(durations) / len(durations), 1) if durations else None

        return {
            "total": total,
            "byStatus": by_status,
            "successRate": success_rate,
            "avgDurationSeconds": avg_duration,
        }

    # ── Global listing (cross-workspace) ─────────────────────────────

    async def list_jobs_global(
        self,
        session: AsyncSession,
        *,
        status: Optional[List[str]] = None,
        workspace_id: Optional[str] = None,
        data_source_ids: Optional[List[str]] = None,
        projection_mode: Optional[str] = None,
        trigger_source: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> PaginatedJobsResponse:
        """List aggregation jobs across all data sources and workspaces.

        Uses denormalized columns on AggregationJobORM (workspace_id,
        data_source_label) instead of JOINing to the public schema.
        No cross-schema dependency.
        """
        # Base query: AggregationJobORM only (denormalized columns)
        base = select(AggregationJobORM)

        # Apply filters conditionally
        if status:
            base = base.where(AggregationJobORM.status.in_(status))
        if workspace_id:
            base = base.where(AggregationJobORM.workspace_id == workspace_id)
        if data_source_ids:
            base = base.where(AggregationJobORM.data_source_id.in_(data_source_ids))
        if projection_mode:
            base = base.where(AggregationJobORM.projection_mode == projection_mode)
        if trigger_source:
            base = base.where(AggregationJobORM.trigger_source == trigger_source)
        if date_from:
            base = base.where(AggregationJobORM.created_at >= date_from)
        if date_to:
            base = base.where(AggregationJobORM.created_at <= date_to + "T23:59:59.999999")
        if search:
            pattern = f"%{search}%"
            base = base.where(or_(
                AggregationJobORM.id.ilike(pattern),
                AggregationJobORM.error_message.ilike(pattern),
                AggregationJobORM.data_source_label.ilike(pattern),
            ))

        # Count total matching rows
        count_q = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_q)).scalar() or 0

        # Fetch paginated results
        rows_q = (
            base
            .order_by(AggregationJobORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(rows_q)
        jobs = result.scalars().all()

        items = [
            self._to_global_response(job, job.workspace_id, None, job.data_source_label)
            for job in jobs
        ]

        return PaginatedJobsResponse(items=items, total=total, limit=limit, offset=offset)

    @staticmethod
    def _to_global_response(
        job: AggregationJobORM,
        workspace_id: str,
        workspace_name: str,
        data_source_label: Optional[str],
    ) -> AggregationJobResponse:
        """Convert ORM to enriched response for the global listing."""
        # Compute duration
        duration = None
        if job.started_at:
            try:
                started = datetime.fromisoformat(job.started_at)
                if job.completed_at:
                    ended = datetime.fromisoformat(job.completed_at)
                else:
                    ended = datetime.now(timezone.utc)
                duration = round((ended - started).total_seconds(), 1)
            except Exception:
                pass

        # Compute coverage
        coverage = None
        if job.total_edges and job.total_edges > 0:
            coverage = round(job.processed_edges / job.total_edges * 100, 1)

        # Estimate completion (same logic as _to_response)
        estimated = None
        if job.status == "running" and job.processed_edges > 0 and job.total_edges > 0:
            if job.started_at:
                try:
                    started = datetime.fromisoformat(job.started_at)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    rate = job.processed_edges / elapsed if elapsed > 0 else 0
                    remaining = job.total_edges - job.processed_edges
                    if rate > 0:
                        eta = datetime.now(timezone.utc) + timedelta(seconds=remaining / rate)
                        estimated = eta.isoformat()
                except Exception:
                    pass

        return AggregationJobResponse(
            id=job.id,
            data_source_id=job.data_source_id,
            status=job.status,
            trigger_source=job.trigger_source,
            progress=job.progress,
            total_edges=job.total_edges,
            processed_edges=job.processed_edges,
            created_edges=job.created_edges,
            batch_size=job.batch_size,
            last_checkpoint_at=job.last_checkpoint_at,
            resumable=job.status == "failed" and job.retry_count < job.max_retries,
            retry_count=job.retry_count,
            error_message=job.error_message,
            estimated_completion_at=estimated,
            started_at=job.started_at,
            completed_at=job.completed_at,
            updated_at=job.updated_at,
            created_at=job.created_at,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            data_source_label=data_source_label,
            projection_mode=job.projection_mode,
            duration_seconds=duration,
            edge_coverage_pct=coverage,
            last_cursor=job.last_cursor,
            max_retries=job.max_retries,
            timeout_secs=job.timeout_secs,
        )

    # ── Resume ────────────────────────────────────────────────────────

    async def resume(
        self,
        ds_id: str,
        job_id: str,
        session: AsyncSession,
        overrides: Optional[ResumeOverrides] = None,
    ) -> AggregationJobResponse:
        """Resume a failed aggregation job from its last checkpoint.

        Optional `overrides` lets the caller bump per-job parameters
        (timeout_secs, batch_size, projection_mode, max_retries) before
        the worker picks the job back up. The checkpoint (`last_cursor`)
        is intentionally preserved — that's the whole point of resume.
        """
        job = await session.get(AggregationJobORM, job_id)
        if not job or job.data_source_id != ds_id:
            raise NotFoundError(f"Aggregation job {job_id} not found")

        if job.status not in ("failed", "cancelled"):
            raise ValueError(
                f"Job {job_id} is not resumable (current status: {job.status}; "
                f"resume requires 'failed' or 'cancelled')"
            )

        # Apply overrides BEFORE the max_retries gate so a caller who
        # raises max_retries can resume a job that exhausted the old cap.
        if overrides is not None:
            if overrides.timeout_secs is not None:
                job.timeout_secs = overrides.timeout_secs
            if overrides.batch_size is not None:
                job.batch_size = overrides.batch_size
            if overrides.projection_mode is not None:
                job.projection_mode = overrides.projection_mode
            if overrides.max_retries is not None:
                job.max_retries = overrides.max_retries

        if job.retry_count >= job.max_retries:
            raise ValueError(f"Job {job_id} has exceeded max retries ({job.max_retries})")

        job.status = "pending"
        job.retry_count += 1
        job.error_message = None
        job.updated_at = _now()
        await session.commit()

        await self._dispatcher.dispatch(job.id)

        logger.info("Aggregation job %s resumed (retry %d/%d)", job_id, job.retry_count, job.max_retries)

        return self._to_response(job)

    # ── Cancel ────────────────────────────────────────────────────────

    async def cancel(
        self, ds_id: str, job_id: str, session: AsyncSession
    ) -> AggregationJobResponse:
        """Cancel a pending or running aggregation job.

        Cooperative-first: requests the worker to exit at the next safe
        boundary (between MERGE sub-batches or at an outer-batch
        commit), so the in-flight FalkorDB MERGE completes cleanly and
        ``last_cursor`` reflects durably-committed work. The worker
        itself flips status='cancelled' when it observes the request,
        emits the ``job_cancelled`` event, and unregisters.

        For pending jobs (no worker is running), we mark cancelled
        directly and emit the event — there's nothing to coordinate
        with. For running jobs we keep the dispatcher's
        ``cancel_task`` as a hard fallback that fires after a grace
        period, but the cooperative path should win in practice.
        """
        from .cancel import request_cancel as request_cooperative_cancel

        job = await session.get(AggregationJobORM, job_id)
        if not job or job.data_source_id != ds_id:
            raise NotFoundError(f"Aggregation job {job_id} not found")

        if job.status not in ("pending", "running"):
            raise ValueError(f"Job {job_id} cannot be cancelled (status: {job.status})")

        # Cooperative cancel — fire on TWO paths in parallel:
        #
        #   1. Local CancelRegistry (in-process). Immediate effect for
        #      jobs hosted in this same process (web tier under
        #      InProcessDispatcher, or — when this code runs in the
        #      worker process via a callback — for jobs that worker
        #      owns). No Redis round-trip; the event flips
        #      synchronously.
        #
        #   2. Redis Pub/Sub broadcast. Reaches every other process
        #      that has a CancelListener subscribed (aggregation
        #      worker process, insights worker process, additional
        #      web-tier replicas). Each listener forwards to its own
        #      local registry, so the worker hosting the job sees its
        #      event get set even though the request landed somewhere
        #      else.
        #
        # We always publish to Redis even when the local registry
        # claims the job — broadcasts are cheap, and the alternative
        # (skip publish on local hit) breaks if a duplicate
        # registration ever happens cross-process. The listener's
        # local-registry check is idempotent.
        if job.status == "running":
            local_hit = request_cooperative_cancel(job_id)
            try:
                from .redis_client import get_redis
                from .cancel import publish_cancel
                await publish_cancel(get_redis(), job_id)
            except Exception as exc:
                # Pub/Sub publish failed; fall back to direct DB write
                # below. Not fatal — local cancel may still have
                # landed, and the dispatcher's hard task.cancel()
                # remains as the last-resort escape hatch.
                logger.warning(
                    "Aggregation job %s: cross-process cancel publish failed "
                    "(falling back to local + direct DB write): %s",
                    job_id, exc,
                )
                local_hit = local_hit  # no change; DB-write path follows

            if local_hit:
                logger.info(
                    "Aggregation job %s: cooperative cancel requested locally "
                    "+ broadcast; worker will transition at next safe boundary",
                    job_id,
                )
                await session.refresh(job)
                return self._to_response(job)
            # No local hit — the worker is in another process. The
            # broadcast we just fired should reach it; flip the DB
            # row to ``cancelled`` now so the UI reflects the user's
            # intent immediately, and the dispatcher's hard
            # task.cancel() fallback fires below as a belt-and-braces
            # path for the very unlikely case where pub/sub silently
            # dropped the message AND the worker keeps running.
            logger.info(
                "Aggregation job %s: no local registry hit; cross-process "
                "cancel broadcast + direct DB write",
                job_id,
            )

        # Pending job, or no worker registered (e.g. the worker process
        # crashed and the row is orphaned). Mark cancelled directly.
        job.status = "cancelled"
        job.completed_at = _now()
        job.updated_at = _now()
        await session.commit()

        # Hard fallback for the orphaned-row case where there's no
        # cooperative consumer to honour the event.
        if hasattr(self._dispatcher, "cancel_task"):
            self._dispatcher.cancel_task(job_id)

        logger.info("Aggregation job %s cancelled (direct path)", job_id)

        return self._to_response(job)

    # ── Delete job record ───────────────────────────────────────────

    async def delete_job(self, job_id: str, session: AsyncSession) -> None:
        """Delete a terminal aggregation job record from history."""
        job = await session.get(AggregationJobORM, job_id)
        if not job:
            raise NotFoundError(f"Aggregation job {job_id} not found")

        if job.status in ("pending", "running"):
            raise ValueError(
                f"Cannot delete job {job_id} while it is {job.status}. Cancel it first."
            )

        await session.delete(job)
        await session.commit()
        logger.info("Aggregation job %s deleted from history", job_id)

    # ── Purge ─────────────────────────────────────────────────────────
    #
    # Purge is a long-running provider operation (``MATCH ... DELETE``
    # against millions of aggregated edges) that runs as a regular
    # insights-service Redis Streams job — see
    # ``backend/insights_service/purge.py``. The web tier only claims a
    # ``pending`` row in ``aggregation_jobs`` and hands the job off; it
    # never blocks on the provider DELETE.
    #
    # That gets us, for free: retry on transient errors via
    # delivery-count + DLQ; crash recovery via XAUTOCLAIM at worker
    # startup; the soft-retry path when the provider is throttled
    # (no DLQ cascade); the per-graph asyncio.Semaphore so concurrent
    # purges of the same graph serialise.

    async def claim_purge_job(
        self, ds_id: str, session: AsyncSession,
    ) -> AggregationJobORM:
        """Reserve a ``pending`` purge slot in ``aggregation_jobs``.

        Raises ``NotFoundError`` if the data source has no aggregation
        state, ``ConflictError`` if another aggregation/purge job is
        already in flight for this data source. The caller hands the
        returned ``job.id`` to the insights-service worker via
        ``enqueue_purge_job_safe`` — see ``aggregation.py`` /
        ``controlplane.py`` purge endpoints.
        """
        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if not state:
            raise NotFoundError(f"Data source {ds_id} not found in aggregation state")

        # Resolve the canonical workspace_id from the source-of-truth
        # row (``workspace_data_sources.workspace_id`` is nullable=False)
        # so we can backfill the aggregation state cache when it has
        # drifted. Without this fall-back, a state row that was written
        # before workspace_id was being set would block every purge
        # forever, and the insights enqueue layer's envelope guard
        # would silently swallow the request anyway.
        from backend.app.db.models import WorkspaceDataSourceORM
        ds_orm = await session.get(WorkspaceDataSourceORM, ds_id)
        if not state.workspace_id:
            canonical_ws = ds_orm.workspace_id if ds_orm else None
            if not canonical_ws:
                raise NotFoundError(
                    f"Data source {ds_id} has no workspace assignment in "
                    "either aggregation_data_source_state or "
                    "workspace_data_sources. Assign the data source to a "
                    "workspace before purging."
                )
            logger.warning(
                "claim_purge_job: backfilling empty workspace_id for ds=%s "
                "from workspace_data_sources (canonical_ws=%s)",
                ds_id, canonical_ws,
            )
            state.workspace_id = canonical_ws

        active = (
            await session.execute(
                select(AggregationJobORM)
                .where(AggregationJobORM.data_source_id == ds_id)
                .where(AggregationJobORM.status.in_(["pending", "running"]))
            )
        ).scalars().first()
        if active:
            raise ConflictError(
                f"Cannot purge while aggregation job {active.id} is active"
            )

        actual_mode = (ds_orm.projection_mode if ds_orm else None) or "in_source"

        now = _now()
        # Purge jobs don't use ``batch_size`` semantically — DELETE
        # queries don't iterate in batches the way aggregation INSERTs
        # do — but the field is non-null on the ORM model and the
        # ``AggregationTriggerRequest`` schema enforces ``ge=100``.
        # Storing 0 broke any downstream code that read the row back
        # through that schema (e.g. the Retrigger dialog reading
        # ``job.batchSize`` from history). Use the same default as
        # ``AggregationTriggerRequest.batch_size`` so a re-read always
        # validates.
        purge_job = AggregationJobORM(
            id=_generate_id(),
            data_source_id=ds_id,
            workspace_id=state.workspace_id,
            status="pending",
            trigger_source="purge",
            projection_mode=actual_mode,
            progress=0,
            total_edges=0,
            processed_edges=0,
            created_edges=0,
            batch_size=5000,
            retry_count=0,
            max_retries=0,
            created_at=now,
            updated_at=now,
        )
        session.add(purge_job)
        await session.commit()
        await session.refresh(purge_job)

        logger.info(
            "Purge job %s claimed for ds=%s (mode=%s) — handing off to insights worker",
            purge_job.id, ds_id, actual_mode,
        )
        return purge_job

    # ── Schedule ──────────────────────────────────────────────────────

    async def set_schedule(
        self, ds_id: str, cron: Optional[str], session: AsyncSession
    ) -> None:
        """Set or clear the aggregation schedule for a data source."""
        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if not state:
            raise NotFoundError(f"Data source {ds_id} not found in aggregation state")

        state.aggregation_schedule = cron
        await session.commit()

        logger.info("Aggregation schedule %s for data source %s", cron or "cleared", ds_id)

    # ── Change Detection ──────────────────────────────────────────────

    async def check_drift(
        self, ds_id: str, session: AsyncSession
    ) -> DriftCheckResponse:
        """Check if the underlying graph has changed since last aggregation."""
        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if not state:
            raise NotFoundError(f"Data source {ds_id} not found in aggregation state")

        _DRIFT_TIMEOUT = float(__import__("os").getenv("SCHEDULER_DRIFT_CHECK_TIMEOUT", "5"))
        try:
            provider = await asyncio.wait_for(
                self._registry.get_provider_for_workspace(
                    state.workspace_id, session, data_source_id=ds_id,
                ),
                timeout=_DRIFT_TIMEOUT,
            )
            current_fp = await asyncio.wait_for(
                compute_graph_fingerprint(provider),
                timeout=_DRIFT_TIMEOUT,
            )
        except Exception as e:
            logger.warning("Failed to compute fingerprint for drift check: %s", e)
            return DriftCheckResponse(
                drift_detected=False,
                current_fingerprint=None,
                stored_fingerprint=state.graph_fingerprint,
                last_checked_at=_now(),
            )

        drift = not fingerprints_match(state.graph_fingerprint, current_fp)

        return DriftCheckResponse(
            drift_detected=drift,
            current_fingerprint=current_fp,
            stored_fingerprint=state.graph_fingerprint,
            last_checked_at=_now(),
        )

    # ── Startup Recovery (CRIT-4: lives here, NOT on Worker) ─────────

    async def recover_interrupted_jobs(self) -> int:
        """Called once on application startup.

        Scans for jobs stuck in 'pending' or 'running' (interrupted by crash).
        Re-dispatches via the configured dispatcher.
        The worker resumes from the stored last_cursor checkpoint.

        Recovery distinguishes two cases:
        - A *running* job with a checkpoint (last_cursor) was actively making
          progress when the process died.  This is a clean crash recovery —
          re-dispatch from the checkpoint **without** incrementing retry_count.
        - A job with no checkpoint (never ran, or stuck in pending) counts as
          a genuine retry and increments retry_count.
        """
        async with self._session_factory() as session:
            stale_jobs = await session.execute(
                select(AggregationJobORM).where(
                    AggregationJobORM.status.in_(["pending", "running"])
                )
            )
            count = 0
            for job in stale_jobs.scalars():
                # Purge jobs are recovered by the insights-service worker
                # via XAUTOCLAIM on the ``insights.jobs.purge`` stream —
                # the message stays in PEL across restarts and gets
                # redelivered on the next worker boot. No action needed
                # here. We don't even mark them failed: the worker will
                # do that on real failure (e.g. provider permanently
                # gone) via the standard delivery-attempt path.
                if job.trigger_source == "purge":
                    continue

                was_making_progress = (
                    job.status == "running"
                    and job.last_cursor is not None
                )

                if was_making_progress:
                    # Clean crash recovery — resume from checkpoint, no penalty
                    job.status = "pending"
                    job.error_message = None
                    job.updated_at = _now()
                    await session.commit()
                    # Before dispatching, add delay based on retry count to prevent rapid-fire
                    # re-dispatch when the provider is still down
                    if job.retry_count and job.retry_count > 0:
                        delay = min(5.0 * (2 ** (job.retry_count - 1)), 120.0)
                        logger.info("Delaying recovery dispatch for job %s by %.0fs (retry_count=%d)", job.id, delay, job.retry_count)
                        await asyncio.sleep(delay)
                    await self._dispatcher.dispatch(job.id)
                    count += 1
                    logger.info(
                        "Crash-recovered aggregation job %s from checkpoint "
                        "(cursor: %s, retry_count unchanged at %d)",
                        job.id, job.last_cursor, job.retry_count,
                    )
                elif job.retry_count < job.max_retries:
                    # No progress checkpoint — count as a genuine retry
                    job.retry_count += 1
                    job.status = "pending"
                    job.updated_at = _now()
                    await session.commit()
                    # Before dispatching, add delay based on retry count to prevent rapid-fire
                    # re-dispatch when the provider is still down
                    if job.retry_count and job.retry_count > 0:
                        delay = min(5.0 * (2 ** (job.retry_count - 1)), 120.0)
                        logger.info("Delaying recovery dispatch for job %s by %.0fs (retry_count=%d)", job.id, delay, job.retry_count)
                        await asyncio.sleep(delay)
                    await self._dispatcher.dispatch(job.id)
                    count += 1
                    logger.info(
                        "Retried aggregation job %s (retry %d/%d)",
                        job.id, job.retry_count, job.max_retries,
                    )
                else:
                    job.status = "failed"
                    job.error_message = "Max retries exceeded after crash recovery"
                    job.updated_at = _now()
                    await session.commit()
                    logger.warning(
                        "Aggregation job %s permanently failed after %d retries",
                        job.id, job.max_retries,
                    )
            return count

    # ── Ontology Resolution ──────────────────────────────────────────

    async def _resolve_ontology(self, ds_id: str, session: AsyncSession) -> dict:
        """Resolve ontology edge types for a data source and run the
        ontology-resolution gate.

        Delegates to ``ontology_runtime.build_resolution_report`` for
        the load + gate evaluation; translates its typed exceptions
        into the service's domain errors so the endpoint adapter can
        keep its existing exception → HTTP mapping. Raises:

            - NotFoundError  — DS row or ontology row missing
            - ValueError     — DS exists but ontology_id is null
            - OntologyResolutionError — gate failed (any of:
              missing_entity_types, missing_edge_types,
              unclassified_relationships, no_lineage)

        Returns dict with:
            ontology_id, ontology_fingerprint, workspace_id, provider_id,
            graph_name, data_source_label, containment_edge_types,
            lineage_edge_types
        """
        try:
            report = await ontology_runtime.build_resolution_report(session, ds_id)
        except ontology_runtime.DataSourceMissing:
            raise NotFoundError(f"Data source {ds_id} not found")
        except ontology_runtime.OntologyNotAssigned:
            raise ValueError(
                "Aggregation requires an assigned ontology. "
                "Please configure an ontology for this data source first."
            )
        except ontology_runtime.OntologyMissing as exc:
            raise NotFoundError(f"Ontology {str(exc)} not found")

        if not report.resolved:
            raise OntologyResolutionError(report)

        # Gate passed — derive the flat edge-type lists from the same
        # ontology row the runtime helper just used. We re-load the row
        # here only to avoid threading it back through the helper API;
        # one extra ORM lookup is cheap relative to the gate eval.
        from backend.app.db.models import OntologyORM, WorkspaceDataSourceORM
        from backend.app.ontology.resolver import (
            parse_entity_definitions,
            parse_relationship_definitions,
            derive_flat_lists,
        )

        ds = await session.get(WorkspaceDataSourceORM, ds_id)
        ontology_orm = await session.get(OntologyORM, ds.ontology_id)
        entity_defs = parse_entity_definitions(
            json.loads(ontology_orm.entity_type_definitions or "{}")
        )
        rel_defs = parse_relationship_definitions(
            json.loads(ontology_orm.relationship_type_definitions or "{}")
        )
        flat = derive_flat_lists(entity_defs, rel_defs)

        return {
            "ontology_id": ds.ontology_id,
            "ontology_fingerprint": report.fingerprint,
            "workspace_id": ds.workspace_id,
            "provider_id": ds.provider_id,
            "graph_name": ds.graph_name,
            "data_source_label": getattr(ds, "label", None),
            "containment_edge_types": flat.containment_edge_types,
            "lineage_edge_types": flat.lineage_edge_types,
        }

    async def _replay_fingerprint_matches(
        self,
        existing_job: AggregationJobORM,
        ds_id: str,
        session: AsyncSession,
    ) -> bool:
        """Return True if it's safe to replay ``existing_job`` for the
        current trigger.

        The replay is only safe when the ontology hasn't materially
        changed since ``existing_job`` was created. We compare the
        prior job's frozen ``ontology_fingerprint`` to a freshly
        computed fingerprint over the current OntologyORM row. Any
        mismatch (including either side being NULL) returns False so
        the caller falls through to a fresh resolve.

        Tolerates missing data sources / ontologies (returns False) so
        a stale prior job never wins by accident; the surrounding
        gate-fail path will produce a clearer 422 below.
        """
        if not existing_job.ontology_fingerprint:
            return False
        try:
            from backend.app.db.models import WorkspaceDataSourceORM, OntologyORM
        except ImportError:
            return False
        ds = await session.get(WorkspaceDataSourceORM, ds_id)
        if ds is None or not ds.ontology_id:
            return False
        # If the assigned ontology changed entirely, the prior fingerprint
        # belongs to a different ontology — never safe to replay.
        if ds.ontology_id != existing_job.ontology_id:
            return False
        ontology_orm = await session.get(OntologyORM, ds.ontology_id)
        if ontology_orm is None:
            return False
        current_fp = ontology_gate.compute_fingerprint_from_ontology_orm(ontology_orm)
        return current_fp == existing_job.ontology_fingerprint

    # ── State Management Helpers ────────────────────────────────────────

    async def _upsert_ds_state(
        self, session: AsyncSession, ds_id: str, **fields,
    ) -> None:
        """Create or update the aggregation-owned data source state row.

        Uses AggregationDataSourceStateORM in the aggregation schema.
        Creates the row if it doesn't exist (e.g., first trigger for a DS).
        """
        from .models import AggregationDataSourceStateORM

        state = await session.get(AggregationDataSourceStateORM, ds_id)
        if state is None:
            state = AggregationDataSourceStateORM(
                data_source_id=ds_id,
                workspace_id=fields.pop("workspace_id", ""),
            )
            session.add(state)
        for key, value in fields.items():
            if hasattr(state, key):
                setattr(state, key, value)

    # ── Response Helpers ─────────────────────────────────────────────

    @staticmethod
    def _to_response(job: AggregationJobORM) -> AggregationJobResponse:
        """Convert ORM to response model."""
        # Estimate completion time
        estimated = None
        if job.status == "running" and job.processed_edges > 0 and job.total_edges > 0:
            if job.started_at:
                try:
                    started = datetime.fromisoformat(job.started_at)
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    rate = job.processed_edges / elapsed if elapsed > 0 else 0
                    remaining = job.total_edges - job.processed_edges
                    if rate > 0:
                        eta_seconds = remaining / rate
                        eta = datetime.now(timezone.utc) + timedelta(seconds=eta_seconds)
                        estimated = eta.isoformat()
                except Exception:
                    pass

        return AggregationJobResponse(
            id=job.id,
            data_source_id=job.data_source_id,
            status=job.status,
            trigger_source=job.trigger_source,
            progress=job.progress,
            total_edges=job.total_edges,
            processed_edges=job.processed_edges,
            created_edges=job.created_edges,
            batch_size=job.batch_size,
            last_checkpoint_at=job.last_checkpoint_at,
            resumable=job.status == "failed" and job.retry_count < job.max_retries,
            retry_count=job.retry_count,
            error_message=job.error_message,
            estimated_completion_at=estimated,
            started_at=job.started_at,
            completed_at=job.completed_at,
            updated_at=job.updated_at,
            created_at=job.created_at,
            edge_coverage_pct=(
                round(job.processed_edges / job.total_edges * 100, 1)
                if job.total_edges and job.total_edges > 0 else None
            ),
            last_cursor=job.last_cursor,
            max_retries=job.max_retries,
            timeout_secs=job.timeout_secs,
            projection_mode=job.projection_mode,
        )


# ── Custom Exception Classes ────────────────────────────────────────


class ConflictError(Exception):
    """409 — resource conflict (e.g., duplicate active job)."""
    pass


class NotFoundError(Exception):
    """404 — resource not found."""
    pass


class OntologyResolutionError(Exception):
    """422 — the assigned ontology fails the resolution gate.

    Carries the full ``ResolutionReport`` so the endpoint can surface
    blocking_reasons + per-relationship gaps to the wizard without
    re-running the gate. Distinct from a generic ``ValueError`` so
    the trigger endpoint can attach the structured detail body
    cleanly.
    """

    def __init__(self, report):
        self.report = report
        super().__init__(
            "Ontology resolution gate failed: " + ", ".join(report.blocking_reasons)
        )
