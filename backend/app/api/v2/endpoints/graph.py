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

@router.post(
    "/trace",
    response_model=TraceResultV2,
    response_model_by_alias=True,
)
async def post_trace(
    request: Request,
    body: TraceRequest = Body(..., description="See plan §1.1 for the full contract."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Initial lineage trace projected to the resolved target level.

    Server-authoritative top-level by default: a request body of just
    ``{"urn":"urn:..."}`` resolves to ``targetLevel=0`` (topmost rollup) and
    returns the focus + the set of top-level entities that lineage flows
    through. Drill-down via /trace/expand.
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
    body: TraceExpandRequest = Body(..., description="Drill-down delta request."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Drill-down delta. Decreases granularity for the expanded subtree by one
    ontology level (or by ``newTargetLevel`` if explicit). The session-stored
    request body provides the trace context — clients send only the URN to
    drill into."""
    try:
        delta = await engine.get_trace_delta_v2(body)
    except KeyError:
        return _trace_error(
            code="trace_session_expired",
            message="Trace session has expired; re-trace required.",
            status_code=410,
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
