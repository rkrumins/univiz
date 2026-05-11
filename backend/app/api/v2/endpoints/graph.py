"""Graph endpoints — API v2.

Smart top-level lineage trace + drill-down delta endpoints. See plan
i-want-you-to-effervescent-glacier.md §1.1 for the full contract.

Two endpoints:
  POST /api/v2/{ws_id}/graph/trace         — initial trace, projected to target level
  POST /api/v2/{ws_id}/graph/trace/expand  — drill-down delta from a session

Default behavior matches the user requirement: a trace from any node returns
top-level (level 0) rollup by default; drill-down uses /trace/expand to descend
through ontology levels one at a time.

This file is the v2 router. The v1 router at backend/app/api/v1/endpoints/graph.py
keeps the legacy /trace shape during the deprecation window (90 days minimum).
"""
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
    TraceDelta,
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
# Error envelope helper (plan §1.1)                                   #
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

def _check_skeleton_truncation(result) -> Optional[JSONResponse]:
    """Translate truncation reasons that should be HTTP-level signals
    (503 for backfill-required) into structured error responses. Other
    truncations (degree_cap, max_nodes, timeout, orphan) are non-fatal
    and ride along inside the 200 response so the client can still render
    a partial skeleton + a banner."""
    meta = getattr(result, "meta", None)
    if meta is None:
        return None
    if meta.truncation_reason == "levels_not_backfilled":
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "30"},
            content={
                "error": {
                    "code": "levels_not_backfilled",
                    "message": (
                        "AGGREGATED edges are missing sourceLevel/targetLevel. "
                        "Run backfill_aggregated_levels.py; retrying after 30s."
                    ),
                    "details": {},
                },
                "meta": {
                    "ontologyDigest": meta.ontology_digest or "",
                    "traceSessionId": meta.trace_session_id,
                },
            },
        )
    return None


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

    Drill-down via POST /trace/expand. Truncations:
      * ``degree_cap``    — mega-node summary; meta.megaNodes populated
      * ``max_nodes``     — node budget exceeded
      * ``timeout``       — wall-clock exceeded
      * ``orphan``        — no level-0 ancestor; meta.fallbackLevel set
      * ``levels_not_backfilled`` — 503 + Retry-After (separate envelope)
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
    # Cold-start: backfill required — return 503 + Retry-After.
    err = _check_skeleton_truncation(result)
    if err is not None:
        return err
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
    err = _check_skeleton_truncation(delta)
    if err is not None:
        return err
    return delta
