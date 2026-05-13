"""Graph endpoints — API v2.

Smart top-level lineage trace + drill-down delta endpoints. The full
contract lives in:
  - ``TraceRequest`` / ``TraceResultV2`` / ``TraceMeta`` in
    ``backend/common/models/graph.py``
  - ``ContextEngine.get_trace_v2`` / ``get_trace_delta_v2`` in
    ``backend/app/services/context_engine.py``

Two endpoints:
  POST /api/v2/{ws_id}/graph/trace         — initial trace, projected to target level
  POST /api/v2/{ws_id}/graph/trace/expand  — drill-down delta (stateless)

Default behavior matches the user requirement: a trace from any node returns
top-level (level 0) rollup by default; drill-down uses /trace/expand to descend
through ontology levels one at a time.

This file is the v2 router. The v1 router at backend/app/api/v1/endpoints/graph.py
keeps the legacy /trace shape during the deprecation window (90 days minimum).
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.engine import get_db_session
from backend.app.providers.manager import provider_manager
from backend.app.services.context_engine import ContextEngine
from backend.common.adapters import ProviderUnavailable  # noqa: F401  (used by global handler)
from backend.common.models.graph import (
    TraceRequest,
    TraceResultV2,
    TraceExpandRequest,
    TraceExpandBatchRequest,
    TraceDelta,
    TraceMeta,
)

router = APIRouter()


# ------------------------------------------------------------------ #
# Dependency: resolve ContextEngine — same shape as v1                #
# ------------------------------------------------------------------ #

async def get_context_engine(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(
        None,
        description="Target a specific data source within a workspace.",
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ContextEngine:
    """Resolve the workspace-scoped engine. v2 drops the legacy connectionId
    fallback — workspace scope is the only supported entry point."""
    try:
        if ws_id:
            return await ContextEngine.for_workspace(
                ws_id, provider_manager, session, data_source_id=dataSourceId
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    raise HTTPException(
        status_code=400,
        detail="scope_required: ws_id is required",
    )


# ------------------------------------------------------------------ #
# Error envelope helper                                                #
# ------------------------------------------------------------------ #

def _trace_error(
    *,
    code: str,
    message: str,
    status_code: int,
    details: Optional[dict] = None,
    ontology_digest: str = "",
    trace_session_id: Optional[str] = None,
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
        "meta": {
            "ontologyDigest": ontology_digest,
            "traceSessionId": trace_session_id,
        },
    }
    return JSONResponse(status_code=status_code, content=body)


# ------------------------------------------------------------------ #
# Endpoints                                                            #
# ------------------------------------------------------------------ #


@router.post(
    "/trace",
    response_model=TraceResultV2,
    response_model_by_alias=True,
)
async def post_trace(
    request: Request,
    body: TraceRequest = Body(..., description="Skeleton-first trace request. Default level=0 returns the top-level Domain skeleton."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Initial lineage trace — skeleton-first.

    Default behavior (body of just ``{"urn":"urn:..."}``) returns the
    top-level Domain skeleton: the set of level-0 entities lineage flows
    through, plus the focus's containment ancestor chain. The response
    is server-authoritative; clients do not need to know the ontology
    depth.

    Drill-down via POST /trace/expand. Truncations (all non-fatal — they
    ride along inside the 200 response so the client can render a partial
    skeleton + a banner):
      * ``degree_cap``    — mega-node summary; meta.megaNodes populated
      * ``max_nodes``     — node budget exceeded
      * ``timeout``       — wall-clock exceeded
      * ``orphan``        — no level-0 ancestor; meta.fallbackLevel set

    Cold-start (AGGREGATED edges not level-stamped): trace still returns 200
    with correct results via a legacy label-scan fallback. Run
    ``backfill_aggregated_levels.py`` to restore the fast path.
    """
    try:
        result = await engine.get_trace_v2(body)
    except KeyError:
        return _trace_error(
            code="trace_focus_not_found",
            message=f"Focus URN not found: {body.urn}",
            status_code=404,
        )
    except ValueError as exc:
        return _trace_error(
            code="trace_invalid_request",
            message=str(exc),
            status_code=400,
        )
    return result


@router.post(
    "/trace/expand",
    response_model=TraceDelta,
    response_model_by_alias=True,
)
async def post_trace_expand(
    request: Request,
    body: TraceExpandRequest = Body(..., description="Drill-down delta request. Stateless: (sourceUrn, targetUrn, nextLevel) is sufficient."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Drill-down delta. Stateless — the (sourceUrn, targetUrn, nextLevel)
    triple uniquely identifies the aggregated edge being expanded. No
    server-side session lookup; ``traceSessionId`` is informational only
    in Phase 1.

    Response invariant: every returned node's parent is either already
    visible (from the originating /trace) or present in this response.
    Layer assignment in the canvas depends on this — do not break it."""
    try:
        delta = await engine.get_trace_delta_v2(body)
    except KeyError:
        # KeyError here is "source or target URN does not exist".
        # No session expiration semantics in Phase 1 (stateless).
        return _trace_error(
            code="trace_expand_not_found",
            message="One or both expand anchors not found in the graph.",
            status_code=404,
            trace_session_id=body.trace_session_id,
        )
    except ValueError as exc:
        return _trace_error(
            code="trace_invalid_expand",
            message=str(exc),
            status_code=400,
            trace_session_id=body.trace_session_id,
        )
    return delta


@router.post(
    "/trace/expand-batch",
    response_model=TraceDelta,
    response_model_by_alias=True,
)
async def post_trace_expand_batch(
    request: Request,
    body: TraceExpandBatchRequest = Body(
        ...,
        description="Batch drill-down. Replaces N concurrent POSTs to /trace/expand with one request; the server fans out and merges results by id.",
    ),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Batched drill-down delta.

    Accepts a list of (sourceUrn, targetUrn, nextLevel) triples that share
    the same expand configuration (lineageEdgeTypes, includeContainmentEdges).
    The server fans out via asyncio.gather, then merges results into one
    deduplicated TraceDelta. The frontend uses this on `autoDrillOnExpand`
    when a traced hub node has many incident AGGREGATED edges — previously
    that produced 20+ HTTP requests, now it produces one.

    Failure semantics: partial success is allowed. Pair-level KeyError /
    ValueError failures are swallowed so the rest of the batch still
    returns; the response carries the successful subset. Total failure
    (all pairs error) returns the first error envelope."""
    if not body.pairs:
        return TraceDelta(nodes=[], edges=[], focus={"urn": "", "displayName": ""}, effective_level=0, meta=TraceMeta(regime="expand"))

    async def run_one(pair):
        # Reconstruct a per-pair TraceExpandRequest so the engine entry
        # point stays unchanged. The batch endpoint is a transport-level
        # optimization, not a new engine semantic.
        req = TraceExpandRequest(
            source_urn=pair.source_urn,
            target_urn=pair.target_urn,
            next_level=pair.next_level,
            lineage_edge_types=body.lineage_edge_types,
            include_containment_edges=body.include_containment_edges,
            trace_session_id=body.trace_session_id,
        )
        try:
            return await engine.get_trace_delta_v2(req)
        except (KeyError, ValueError):
            return None

    results = await asyncio.gather(*(run_one(p) for p in body.pairs))
    successes = [r for r in results if r is not None]
    if not successes:
        return _trace_error(
            code="trace_expand_batch_all_failed",
            message="No pair in the batch could be expanded.",
            status_code=404,
            trace_session_id=body.trace_session_id,
        )

    # Merge by id. Last write wins for duplicates — acceptable because the
    # backend returns deterministic results for the same (s, t, lvl) triple.
    nodes_by_id = {}
    edges_by_id = {}
    containment_by_id = {}
    upstream_urns = set()
    downstream_urns = set()
    cypher_ms_total = 0
    node_count_total = 0
    edge_count_total = 0
    truncated_any = False
    truncation_reasons = set()
    focus = None
    effective_level = 0

    for delta in successes:
        for n in delta.nodes:
            nodes_by_id[n.urn] = n
        for e in delta.edges:
            edges_by_id[e.id] = e
        for ce in delta.containment_edges:
            containment_by_id[ce.id] = ce
        upstream_urns.update(delta.upstream_urns)
        downstream_urns.update(delta.downstream_urns)
        if delta.truncated:
            truncated_any = True
            if delta.truncation_reason:
                truncation_reasons.add(delta.truncation_reason)
        if focus is None:
            focus = delta.focus
            effective_level = delta.effective_level
        cypher_ms_total += delta.meta.cypher_ms
        node_count_total += delta.meta.node_count
        edge_count_total += delta.meta.edge_count

    merged_meta = TraceMeta(
        regime="expand",
        effective_level=effective_level,
        cypher_ms=cypher_ms_total,
        node_count=node_count_total,
        edge_count=edge_count_total,
        truncation_reason=";".join(sorted(truncation_reasons)) if truncation_reasons else None,
        trace_session_id=body.trace_session_id,
    )

    return TraceDelta(
        nodes=list(nodes_by_id.values()),
        edges=list(edges_by_id.values()),
        containment_edges=list(containment_by_id.values()),
        upstream_urns=upstream_urns,
        downstream_urns=downstream_urns,
        focus=focus,
        effective_level=effective_level,
        truncated=truncated_any,
        truncation_reason=";".join(sorted(truncation_reasons)) if truncation_reasons else None,
        meta=merged_meta,
    )
