from typing import List, Optional
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.graph import (
    GraphNode, GraphEdge,
    NodeQuery, EdgeQuery,
    AggregatedEdgeRequest, AggregatedEdgeResult,
    CreateNodeRequest, CreateNodeResult,
    CreateEdgeRequest, UpdateEdgeRequest, EdgeMutationResult,
    BatchCommandRequest, BatchCommandResult, BatchResponse,
    ChildrenWithEdgesResult, TopLevelNodesResult,
    DescendantPreviewQuery, DescendantPreviewResult,
    TraceRequest, TraceResult, ExpandRequest,
)
from backend.common.interfaces.provider import ProviderConfigurationError
from backend.app.services.context_engine import ContextEngine
from backend.app.services.fair_share import get_fair_share
from backend.app.services.graph_cache import (
    CacheScope,
    ENDPOINT_AGGREGATED,
    ENDPOINT_CHILDREN,
    get_graph_cache,
)
from backend.app.services.stats_cache import (
    CacheMiss, SYNTHETIC_SCHEMA_MISSING_FIELDS,
    build_computing_envelope, build_envelope, build_error_envelope, build_meta,
    build_synthetic_schema, read_stats_cache,
)
from backend.app.db.engine import get_db_session
from backend.app.providers.manager import provider_manager
from backend.insights_service.enqueue import enqueue_stats_job_safe
from sqlalchemy.exc import OperationalError, SQLAlchemyError

router = APIRouter()


# ------------------------------------------------------------------ #
# Dependency: resolve ContextEngine for the active connection         #
# ------------------------------------------------------------------ #

async def get_context_engine(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None, description="Target a specific data source within a workspace."),
    connectionId: Optional[str] = Query(None, description="Legacy connection ID. Prefer workspace-scoped routes."),
    session: AsyncSession = Depends(get_db_session),
) -> ContextEngine:
    """
    FastAPI dependency that resolves the appropriate ContextEngine.

    Priority:
    - `ws_id` (path param from /v1/{ws_id}/graph routes) → workspace-scoped engine
      - `dataSourceId` (optional query param) → targets specific data source within workspace
    - `connectionId` (query param, legacy) → connection-scoped engine
    - Neither → rejected; graph scope must be explicit

    Error boundary: ContextEngine.for_workspace/for_connection normalize
    all provider connectivity errors to ProviderUnavailable, which the
    global exception handler at main.py converts to HTTP 503 with
    Retry-After. KeyError (data source not found) becomes HTTP 404.
    """
    try:
        if ws_id:
            return await ContextEngine.for_workspace(
                ws_id, provider_manager, session, data_source_id=dataSourceId
            )
        if connectionId:
            return await ContextEngine.for_connection(connectionId, provider_manager, session)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # ProviderUnavailable propagates to FastAPI exception handler → 503
    raise HTTPException(
        status_code=400,
        detail="scope_required: workspace_id or connection_id is required",
    )


# ------------------------------------------------------------------ #
# Helper: resolve data source ID from workspace (DB-only, no provider)#
# ------------------------------------------------------------------ #

def _cache_scope(engine: ContextEngine) -> Optional[CacheScope]:
    """Derive the (workspace, data source) scope for cache keys.

    Returns None when the engine has no workspace context (legacy
    connection-scoped path). Connection-scoped reads bypass the cache —
    they're vanishingly rare in production and not worth the extra key
    plumbing.
    """
    ws = getattr(engine, "_workspace_id", None)
    if not ws:
        return None
    ds = getattr(engine, "_data_source_id", None) or ""
    return CacheScope(workspace_id=ws, data_source_id=ds)


async def _invalidate_cache(engine: ContextEngine) -> None:
    """Bump the generation counter for the engine's scope, invalidating
    every cached entry under (workspace, data_source).

    Safe to call after any write: missing scope is a no-op, Redis errors
    are swallowed by the cache layer. Never raises — invalidation
    failures must never fail the user's write."""
    scope = _cache_scope(engine)
    if scope is None:
        return
    await get_graph_cache().bump_generation(scope)


async def _enforce_fair_share(engine: ContextEngine, endpoint: str) -> None:
    """Charge one token against the workspace's per-endpoint bucket.

    Raises :class:`ProviderBusy` (mapped to 429+Retry-After in main.py)
    when the bucket is empty. No-op when the fair-share feature flag is
    off OR the engine has no workspace context."""
    bucket = get_fair_share()
    if not bucket.is_enabled():
        return
    ws = getattr(engine, "_workspace_id", None)
    if not ws:
        return
    await bucket.enforce(endpoint, ws)


async def _resolve_data_source_id(
    session: AsyncSession,
    ws_id: Optional[str],
    data_source_id: Optional[str],
) -> Optional[str]:
    """Resolve the data source ID for a workspace without touching the provider.
    Returns the explicit data_source_id if given, otherwise looks up the primary
    data source for the workspace.  Returns None if nothing can be resolved.
    """
    if data_source_id:
        return data_source_id
    if not ws_id:
        return None
    from backend.app.db.repositories.data_source_repo import get_primary_data_source
    ds = await get_primary_data_source(session, ws_id)
    return ds.id if ds else None


# ------------------------------------------------------------------ #
# Graph endpoints                                                     #
# ------------------------------------------------------------------ #

# V1 trace sunset date — 2 weeks from the cutover. Update if the
# deprecation window changes. RFC 8594 Sunset header format.
_V1_TRACE_SUNSET = "Mon, 25 May 2026 00:00:00 GMT"


@router.post("/trace", response_model=None, response_model_by_alias=False, deprecated=True)
async def get_lineage_trace_deprecated(request: Request):
    """**REMOVED — V1 trace is no longer served.**

    The legacy ``/api/v1/{ws}/graph/trace`` endpoint backed by
    ``engine.get_lineage()`` was the slow path that timed out on 100k+
    node graphs. Skeleton-first replacement lives at
    ``POST /api/v2/{ws}/graph/trace`` and serves the top-level Domain
    skeleton in <100 ms.

    Clients during the 2-week deprecation window receive HTTP 410 with
    a ``Sunset`` header and a migration pointer. After the window the
    route is removed entirely.
    """
    client_host = request.client.host if request.client else "?"
    logger.warning(
        "v1_trace_deprecated called from %s — clients must migrate to "
        "POST /api/v2/{ws}/graph/trace",
        client_host,
    )
    return JSONResponse(
        status_code=410,
        headers={
            "Sunset": _V1_TRACE_SUNSET,
            "Deprecation": "true",
            "Link": '</api/v2/{ws_id}/graph/trace>; rel="successor-version"',
        },
        content={
            "error": {
                "code": "v1_trace_deprecated",
                "message": (
                    "POST /api/v1/{ws}/graph/trace has been removed. "
                    "Use POST /api/v2/{ws}/graph/trace — the skeleton-first "
                    "trace returns the top-level Domain skeleton by default "
                    "and supports lazy drill-down via /trace/expand."
                ),
                "details": {
                    "successor": "POST /api/v2/{ws_id}/graph/trace",
                    "sunset": _V1_TRACE_SUNSET,
                },
            },
        },
    )


# ----------------------------------------------------------------------------- #
# Trace v2 — Cypher-native, ontology-aware lineage                             #
#                                                                               #
# Companion to the legacy /trace endpoint above. Pushes all traversal +        #
# aggregation work into Cypher (per-hop set-based BFS), returns nodes already  #
# at the requested hierarchy level, supports drill-down via /trace/expand.     #
# Cost is proportional to result size, not graph size — safe for million-node  #
# graphs. See plan: /Users/.../plans/i-want-you-to-fluttering-badger.md         #
# ----------------------------------------------------------------------------- #


@router.post("/trace/v2", response_model=TraceResult, response_model_by_alias=True)
async def trace_v2(
    request: TraceRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
) -> TraceResult:
    """Trace lineage at a hierarchy level using AGGREGATED edges.

    Returns nodes already at the requested level (peer rollup) plus the
    AGGREGATED edges between them. Filters by ``s.level``/``t.level`` at
    the database — never explodes a Domain-level trace down to Columns.

    Hard caps: ``TRACE_MAX_NODES`` (default 2000) nodes,
    ``TRACE_TIMEOUT_SECS`` (default 60 s) outer budget — both server
    config, not per-request. See ``app/config/resilience.py``. On trip,
    returns ``truncated: true`` with ``truncationReason``. Always HTTP
    200 unless input is malformed.
    """
    return await engine.trace(request)


@router.post("/trace/expand", response_model=TraceResult, response_model_by_alias=True)
async def trace_expand(
    request: ExpandRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
) -> TraceResult:
    """Drill into an AGGREGATED edge: return finer-level nodes + edges
    within (source-subtree × target-subtree) at ``nextLevel``.

    Set-based, no Cartesian. When ``nextLevel`` is the finest level in
    the ontology, the engine bypasses AGGREGATED and reads raw lineage
    edges directly.
    """
    return await engine.expand_aggregated_edge(request)


class _TraceExpandPair(BaseModel):
    """One aggregated-edge identifier in a batch expand. Aliases match the
    frontend payload (sourceUrn / targetUrn / nextLevel)."""
    source_urn: str = Field(alias="sourceUrn")
    target_urn: str = Field(alias="targetUrn")
    next_level: int | str = Field(alias="nextLevel")

    class Config:
        populate_by_name = True


class _TraceExpandBatchRequest(BaseModel):
    """Body for /trace/expand-batch — N edges share the same config."""
    pairs: List[_TraceExpandPair]
    lineage_edge_types: Optional[List[str]] = Field(None, alias="lineageEdgeTypes")
    include_containment_edges: bool = Field(True, alias="includeContainmentEdges")

    class Config:
        populate_by_name = True


@router.post("/trace/expand-batch", response_model=TraceResult, response_model_by_alias=True)
async def trace_expand_batch(
    request: _TraceExpandBatchRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
) -> TraceResult:
    """Batched drill-down. Replaces N concurrent POSTs to /trace/expand with
    one request — the frontend's ``autoDrillOnExpand`` collects every
    aggregated edge incident to an expanding traced node and ships them
    together. The server fans out via asyncio.gather and merges results by id.

    Partial-success: pair-level failures are swallowed (with a logged warning)
    so the rest of the batch returns; total failure returns 404 with the
    list of pair-level error messages in the response body. Shape matches
    /trace/expand so the frontend's normalizeTraceV2 handles either."""
    import asyncio
    if not request.pairs:
        # Empty batch — return an empty payload. Use the first pair's URN as
        # a placeholder focus; never reached because empty pairs short-circuit.
        raise HTTPException(status_code=400, detail="No pairs provided.")

    pair_errors: List[str] = []

    async def run_one(p: _TraceExpandPair):
        req = ExpandRequest(
            source_urn=p.source_urn,
            target_urn=p.target_urn,
            next_level=p.next_level,
            lineage_edge_types=request.lineage_edge_types,
            include_containment_edges=request.include_containment_edges,
        )
        try:
            return await engine.expand_aggregated_edge(req)
        except Exception as exc:
            # Catch ALL exceptions per pair — provider unavailability, value
            # errors, missing URNs, etc. Surface to the response body so the
            # frontend can render a partial result with the failure list.
            msg = f"{p.source_urn} → {p.target_urn} @ {p.next_level}: {type(exc).__name__}: {exc}"
            pair_errors.append(msg)
            logger.warning("trace/expand-batch pair failed: %s", msg, exc_info=False)
            return None

    results = await asyncio.gather(*(run_one(p) for p in request.pairs))
    successes = [r for r in results if r is not None]
    if not successes:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "trace_expand_batch_all_failed",
                "message": "No pair in the batch could be expanded.",
                "errors": pair_errors[:20],  # cap to keep response readable
            },
        )

    # Merge by id. Identical (s, t, lvl) triples produce deterministic results,
    # so last-write-wins is safe.
    nodes_by_id: dict = {}
    edges_by_id: dict = {}
    containment_by_id: dict = {}
    upstream_urns: set = set()
    downstream_urns: set = set()
    truncated_any = False
    focus = None
    effective_level = 0
    for r in successes:
        for n in r.nodes: nodes_by_id[n.urn] = n
        for e in r.edges: edges_by_id[e.id] = e
        for ce in r.containment_edges: containment_by_id[ce.id] = ce
        upstream_urns.update(r.upstream_urns)
        downstream_urns.update(r.downstream_urns)
        if r.truncated: truncated_any = True
        if focus is None:
            focus = r.focus
            effective_level = r.effective_level

    if pair_errors:
        logger.info(
            "trace/expand-batch partial success: %d/%d pairs succeeded",
            len(successes), len(request.pairs),
        )

    return TraceResult(
        nodes=list(nodes_by_id.values()),
        edges=list(edges_by_id.values()),
        containment_edges=list(containment_by_id.values()),
        upstream_urns=upstream_urns,
        downstream_urns=downstream_urns,
        focus=focus,
        effective_level=effective_level,
        truncated=truncated_any,
    )


@router.get(
    "/nodes/top-level",
    response_model=TopLevelNodesResult,
    response_model_by_alias=True,
)
async def get_top_level_nodes(
    entityTypes: Optional[List[str]] = Query(
        None,
        description="Restrict to these entity type IDs. None = all types.",
    ),
    searchQuery: Optional[str] = Query(
        None,
        description="Case-insensitive substring match against displayName/urn.",
    ),
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(
        None,
        description="Keyset cursor (displayName of the last node on the previous page).",
    ),
    includeChildCount: bool = Query(True, description="Populate child_count on each node."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Return instances that have no incoming containment edge.

    "Top-level" is defined **structurally**: a node ``n`` is top-level iff
    there is no edge ``(n' -[:CONTAINMENT_EDGE]-> n)`` for any configured
    containment type. The result therefore mixes:
      - Instances of ontology root types (Domain, Platform, …)
      - Orphan instances of non-root types (e.g. a Table with no schema parent,
        perhaps from a broken or incremental import)

    The response's ``rootTypeCount`` and ``orphanCount`` fields let the UI
    distinguish the two classes (e.g. an "orphan" badge in the wizard tree).

    Containment edge types are resolved from the ontology bound to the active
    data source. If the ontology has no containment edges configured and no
    ``CONTAINMENT_EDGE_TYPES`` env override is present, the provider raises
    :class:`ProviderConfigurationError`, which is translated to HTTP 400 —
    the API must never silently fall back to hardcoded type names.

    **Route-ordering note.** This handler MUST be declared before
    ``/nodes/{urn}`` — FastAPI/Starlette matches routes in registration
    order, and the generic ``{urn}`` path would otherwise swallow
    ``/nodes/top-level`` and return 404 for a non-existent URN.
    """
    try:
        return await engine.get_top_level_or_orphan_nodes(
            entity_types=entityTypes,
            search_query=searchQuery,
            limit=limit,
            cursor=cursor,
            include_child_count=includeChildCount,
        )
    except ProviderConfigurationError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ontology configuration error: {exc}. Configure containment "
                "edge types on the active ontology (or set CONTAINMENT_EDGE_TYPES "
                "as a deployment-level override)."
            ),
        )


@router.get("/nodes/{urn}", response_model=GraphNode, response_model_by_alias=True)
async def get_node(
    urn: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    node = await engine.get_node(urn)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@router.get("/nodes/{urn}/parent", response_model=Optional[GraphNode], response_model_by_alias=True,
             deprecated=True)
async def get_node_parent(
    urn: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    """Get parent node (containment hierarchy).

    **Deprecated:** Use `GET /nodes/{urn}/ancestors?limit=1` instead.
    """
    logger.warning("Deprecated endpoint GET /nodes/%s/parent called — use GET /nodes/%s/ancestors?limit=1", urn, urn)
    return await engine.get_parent(urn)


@router.get("/nodes/{urn}/children", response_model=List[GraphNode], response_model_by_alias=True)
async def get_node_children(
    urn: str,
    edge_types: Optional[List[str]] = Query(None, alias="edgeTypes"),
    search_query: Optional[str] = Query(None, alias="searchQuery"),
    sort_property: Optional[str] = Query("displayName", alias="sortProperty", description="Node property to sort by. Pass null to skip sorting."),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = Query(None, description="Cursor for keyset pagination (displayName of last item). Takes precedence over offset."),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Lazy load children nodes."""
    return await engine.get_children(urn, edge_types=edge_types, search_query=search_query, limit=limit, offset=offset, sort_property=sort_property, cursor=cursor)


@router.get("/nodes/{urn}/children-with-edges", response_model=ChildrenWithEdgesResult, response_model_by_alias=True)
async def get_children_with_edges(
    urn: str,
    edge_types: Optional[List[str]] = Query(None, alias="edgeTypes"),
    lineage_edge_types: Optional[List[str]] = Query(None, alias="lineageEdgeTypes"),
    search_query: Optional[str] = Query(None, alias="searchQuery"),
    sort_property: Optional[str] = Query("displayName", alias="sortProperty", description="Node property to sort by. Pass null to skip sorting."),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = Query(None, description="Cursor for keyset pagination (displayName of last item). Takes precedence over offset."),
    include_lineage_edges: bool = Query(True, alias="includeLineageEdges"),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Get children with containment and lineage edges in a single round-trip."""
    await _enforce_fair_share(engine, ENDPOINT_CHILDREN)

    async def compute() -> ChildrenWithEdgesResult:
        return await engine.get_children_with_edges(
            urn, edge_types=edge_types, lineage_edge_types=lineage_edge_types,
            search_query=search_query, limit=limit, offset=offset,
            include_lineage_edges=include_lineage_edges,
            sort_property=sort_property, cursor=cursor,
        )

    scope = _cache_scope(engine)
    if scope is None:
        return await compute()

    return await get_graph_cache().get_or_compute(
        scope=scope,
        endpoint=ENDPOINT_CHILDREN,
        params={
            "urn": urn,
            "edgeTypes": sorted(edge_types) if edge_types else None,
            "lineageEdgeTypes": sorted(lineage_edge_types) if lineage_edge_types else None,
            "searchQuery": search_query,
            "sortProperty": sort_property,
            "limit": limit,
            "offset": offset,
            "cursor": cursor,
            "includeLineageEdges": include_lineage_edges,
        },
        compute=compute,
        model_cls=ChildrenWithEdgesResult,
    )


@router.post(
    "/nodes/{urn}/descendants/preview",
    response_model=DescendantPreviewResult,
    response_model_by_alias=True,
)
async def preview_node_descendants(
    urn: str,
    query: DescendantPreviewQuery,
    edge_types: Optional[List[str]] = Query(None, alias="edgeTypes"),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Server-side preview of descendants under `urn` matching the filter.

    Used by the ViewWizard to show a live "this scoped rule will match N
    entities" badge before the user authors a rule. Filters mirror the
    semantics of LayerAssignmentRuleConfig.conditions so the preview
    count matches what AssignmentEngine will actually resolve.
    """
    return await engine.get_descendants_preview(urn, query, edge_types=edge_types)


@router.post("/search", response_model=List[GraphNode], response_model_by_alias=True)
async def search_nodes(
    query: str = Body(..., embed=True),
    limit: int = Body(10, embed=True),
    offset: int = Body(0, embed=True),
    engine: ContextEngine = Depends(get_context_engine),
):
    return await engine.search_nodes(query, limit=limit, offset=offset)


@router.get("/edges", response_model=List[GraphEdge], response_model_by_alias=True,
             deprecated=True)
async def get_edges(
    edge_type: Optional[str] = Query(None, alias="edgeType"),
    source_urn: Optional[str] = Query(None, alias="sourceUrn"),
    target_urn: Optional[str] = Query(None, alias="targetUrn"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Generic edge query.

    **Deprecated:** Use `POST /edges/query` instead — supports bulk URN lists and complex filters.
    """
    logger.warning("Deprecated endpoint GET /edges called — use POST /edges/query")
    q = EdgeQuery(offset=offset, limit=limit)
    if edge_type:
        q.edge_types = [edge_type]
    if source_urn:
        q.source_urns = [source_urn]
    if target_urn:
        q.target_urns = [target_urn]
    return await engine.get_edges(q)


@router.get("/map/{urn}")
async def get_neighborhood_map(
    urn: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    """Get node and its immediate edges."""
    result = await engine.get_neighborhood(urn)
    if not result:
        raise HTTPException(status_code=404, detail="Node not found")
    return result


@router.get("/stats", deprecated=True)
async def get_graph_stats(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None, description="Target a specific data source within a workspace."),
    connectionId: Optional[str] = Query(None, description="Legacy connection ID."),
    session: AsyncSession = Depends(get_db_session),
):
    """**Deprecated:** Use `GET /introspection` instead — returns a superset of stats with full schema details.

    Cache-only read: serves the latest ``data_source_stats`` row populated
    by the stats service. The handler never calls the provider, so it
    cannot 504 regardless of graph size. Always returns HTTP 200 with
    the canonical ``{data, meta}`` envelope; cache state lives in
    ``meta.status``.
    """
    logger.warning("Deprecated endpoint GET /stats called — use GET /introspection")

    ds_id = await _resolve_data_source_id(session, ws_id, dataSourceId)
    if not ds_id:
        raise HTTPException(status_code=400, detail="dataSourceId is required")

    try:
        data, meta = await read_stats_cache(session, ds_id, ws_id, "node_stats")
        return JSONResponse(content=build_envelope(data, meta))
    except CacheMiss:
        pass
    except (OperationalError, SQLAlchemyError) as exc:
        logger.warning("get_graph_stats: database unavailable (ds_id=%s): %s", ds_id, exc)
        return JSONResponse(content=build_error_envelope(ds_id, reason="db_unavailable"))

    msg_id = await enqueue_stats_job_safe(ds_id, ws_id) if ws_id else None
    logger.info("stats_cache.served_computing endpoint=/graph/stats ds_id=%s msg_id=%s", ds_id, msg_id)
    return JSONResponse(content=build_computing_envelope(ds_id, ws_id, msg_id))


@router.get("/nodes", response_model=List[GraphNode], response_model_by_alias=True,
             deprecated=True)
async def get_nodes(
    entity_type: Optional[str] = Query(None, alias="entityType"),
    tag: Optional[str] = Query(None),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Generic node query.

    **Deprecated:** Use `POST /nodes/query` instead — supports complex filters and bulk operations.
    """
    logger.warning("Deprecated endpoint GET /nodes called — use POST /nodes/query")
    q = NodeQuery(
        entity_types=[entity_type] if entity_type else None,
        tags=[tag] if tag else None,
        limit=limit,
        offset=offset,
    )
    return await engine.get_nodes_query(q)


@router.get("/nodes/{urn}/ancestors", response_model=List[GraphNode], response_model_by_alias=True)
async def get_node_ancestors(
    urn: str,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    engine: ContextEngine = Depends(get_context_engine),
):
    return await engine.get_ancestors(urn, limit=limit, offset=offset)


@router.get("/nodes/{urn}/descendants", response_model=List[GraphNode], response_model_by_alias=True)
async def get_node_descendants(
    urn: str,
    depth: int = Query(5, ge=1),
    entity_type: Optional[str] = Query(None, alias="entityType"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    engine: ContextEngine = Depends(get_context_engine),
):
    entity_types = [entity_type] if entity_type else None
    return await engine.get_descendants(urn, depth=depth, entity_types=entity_types, limit=limit, offset=offset)


@router.get("/nodes/by-tag/{tag}", response_model=List[GraphNode], response_model_by_alias=True,
             deprecated=True)
async def get_nodes_by_tag_endpoint(
    tag: str,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    engine: ContextEngine = Depends(get_context_engine),
):
    """**Deprecated:** Use `POST /nodes/query` with `tags` filter instead."""
    logger.warning("Deprecated endpoint GET /nodes/by-tag/%s called — use POST /nodes/query with tags filter", tag)
    return await engine.get_nodes_by_tag(tag, limit=limit, offset=offset)


@router.get("/nodes/by-layer/{layer_id}", response_model=List[GraphNode], response_model_by_alias=True)
async def get_nodes_by_layer_endpoint(
    layer_id: str,
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    engine: ContextEngine = Depends(get_context_engine),
):
    return await engine.get_nodes_by_layer(layer_id, limit=limit, offset=offset)


class InternalEdgeQuery(BaseModel):
    """Fetch edges where BOTH source and target are in the provided URN set."""
    urns: List[str]
    edge_types: Optional[List[str]] = Field(None, alias="edgeTypes")
    limit: int = Field(
        default_factory=lambda: int(os.getenv("INTERNAL_EDGE_QUERY_LIMIT_DEFAULT", "50000")),
        le=200000,
    )
    class Config:
        populate_by_name = True


@router.post("/edges/between", response_model=List[GraphEdge], response_model_by_alias=True)
async def get_edges_between(
    query: InternalEdgeQuery = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Fetch edges where both source and target are in the URN set.

    Uses source_urns + target_urns AND-semantics in the Cypher query so only
    edges connecting nodes within the set are returned — no over-fetch or
    Python post-filter needed.
    """
    return await engine.get_edges(EdgeQuery(
        source_urns=query.urns,
        target_urns=query.urns,
        edge_types=query.edge_types,
        limit=query.limit,
    ))


@router.post("/edges/query", response_model=List[GraphEdge], response_model_by_alias=True)
async def query_edges(
    query: EdgeQuery = Body(..., embed=True),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Advanced edge query (bulk fetch)."""
    return await engine.get_edges(query)


@router.post("/nodes/query", response_model=List[GraphNode], response_model_by_alias=True)
async def query_nodes(
    query: NodeQuery = Body(..., embed=True),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Advanced node query (bulk fetch, complex filters)."""
    return await engine.get_nodes_query(query)


@router.get("/metadata/entity-types", response_model=List[str])
async def get_entity_types(
    engine: ContextEngine = Depends(get_context_engine),
):
    """Get distinct entity types in the graph."""
    values = await engine.get_distinct_values("entityType")
    return [str(v) for v in values]


@router.get("/metadata/tags", response_model=List[str])
async def get_tags(
    engine: ContextEngine = Depends(get_context_engine),
):
    """Get distinct tags in the graph."""
    values = await engine.get_distinct_values("tags")
    return [str(v) for v in values]


@router.get("/metadata/distinct/{property}")
async def get_distinct_values(
    property: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    """Generic endpoint to get distinct values for filters."""
    return await engine.get_distinct_values(property)


class SaveGraphRequest(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]


@router.post("/save")
async def save_graph(
    request: SaveGraphRequest,
    engine: ContextEngine = Depends(get_context_engine),
):
    """Save custom graph nodes and edges."""
    success = await engine.save_custom_graph(request.nodes, request.edges)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to save graph")
    await _invalidate_cache(engine)
    return {"status": "success", "message": "Graph saved successfully"}


# ``_freshness_headers`` previously lived here. It now lives in
# :mod:`backend.app.services.stats_cache` where it is emitted directly
# from :func:`read_stats_cache` alongside the cache tier classification
# and stats-service health signals — keeping header construction next
# to the read path prevents drift between what the cache says and what
# the response advertises.


@router.get("/introspection")
async def get_graph_introspection(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None, description="Target a specific data source within a workspace."),
    connectionId: Optional[str] = Query(None, description="Legacy connection ID."),
    session: AsyncSession = Depends(get_db_session),
):
    """Get detailed schema statistics for the graph.

    Cache-only read: serves the latest ``data_source_stats.schema_stats``
    row populated by the stats service. Always returns HTTP 200 with
    the canonical ``{data, meta}`` envelope; ``meta.status`` carries
    cache state (``fresh``/``stale``/``computing``). The handler never
    calls the provider; 504s are impossible by construction.
    """
    ds_id = await _resolve_data_source_id(session, ws_id, dataSourceId)
    if not ds_id:
        raise HTTPException(status_code=400, detail="dataSourceId is required")

    try:
        data, meta = await read_stats_cache(session, ds_id, ws_id, "schema_stats")
        return JSONResponse(content=build_envelope(data, meta))
    except CacheMiss:
        pass
    except (OperationalError, SQLAlchemyError) as exc:
        logger.warning("get_graph_introspection: database unavailable (ds_id=%s): %s", ds_id, exc)
        return JSONResponse(content=build_error_envelope(ds_id, reason="db_unavailable"))

    msg_id = await enqueue_stats_job_safe(ds_id, ws_id) if ws_id else None
    logger.info("stats_cache.served_computing endpoint=/introspection ds_id=%s msg_id=%s", ds_id, msg_id)
    return JSONResponse(content=build_computing_envelope(ds_id, ws_id, msg_id))


@router.get("/metadata/ontology", deprecated=True)
async def get_ontology_metadata(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None, description="Target a specific data source within a workspace."),
    connectionId: Optional[str] = Query(None, description="Legacy connection ID."),
    session: AsyncSession = Depends(get_db_session),
):
    """Get ontology metadata including containment edge types and entity hierarchies.

    **Deprecated:** Use `GET /metadata/schema` instead — returns a superset including ontology, entity types, and relationship definitions.

    Cache-only read with ``{data, meta}`` envelope, always HTTP 200.
    """
    logger.warning("Deprecated endpoint GET /metadata/ontology called — use GET /metadata/schema")

    ds_id = await _resolve_data_source_id(session, ws_id, dataSourceId)
    if not ds_id:
        raise HTTPException(status_code=400, detail="dataSourceId is required")

    try:
        data, meta = await read_stats_cache(session, ds_id, ws_id, "ontology_metadata")
        return JSONResponse(
            content=build_envelope(data, meta),
            headers={"Cache-Control": "private, max-age=300"},
        )
    except CacheMiss:
        pass
    except (OperationalError, SQLAlchemyError) as exc:
        logger.warning("get_ontology_metadata: database unavailable (ds_id=%s): %s", ds_id, exc)
        return JSONResponse(content=build_error_envelope(ds_id, reason="db_unavailable"))

    msg_id = await enqueue_stats_job_safe(ds_id, ws_id) if ws_id else None
    logger.info("stats_cache.served_computing endpoint=/metadata/ontology ds_id=%s msg_id=%s", ds_id, msg_id)
    return JSONResponse(content=build_computing_envelope(ds_id, ws_id, msg_id))


def _schema_etag(payload: dict, *, status: str, source: str) -> str:
    """Compute a weak ETag over the canonical schema payload + state markers.

    The ETag input includes ``meta.status`` and ``meta.source`` because
    those signal a semantic transition the client must observe — e.g. a
    synthetic schema (``status=partial``, ``source=ontology``) replaced
    by the real schema (``status=fresh``, ``source=postgres``) that
    happens to be byte-identical (empty graph case). Without status/
    source in the ETag, the client would 304 on the transition, never
    update its banner, and stay stuck on "partial" forever.

    ``updated_at`` is intentionally excluded — it changes every poll
    and would make 304s impossible. Cache freshness boundary crossings
    (fresh→stale) DO change ``status``, so they correctly invalidate.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    seed = f"{status}|{source}|{canonical}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f'W/"{digest}"'


def _schema_response(
    data: Optional[dict], request: Request, *, meta: dict,
) -> Response:
    """Build the envelope response for /metadata/schema with ETag handling.

    The envelope is ``{"data": data, "meta": meta}``. The ETag is
    computed over ``data`` plus ``meta.status`` and ``meta.source`` —
    so transitions that swap the *meaning* of an otherwise-identical
    payload (synthetic → real, fresh → stale) correctly invalidate the
    client's cached copy. ``meta.updated_at`` is excluded so steady-
    state polls within the same tier still benefit from 304s.
    """
    headers: dict[str, str] = {
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    if data is not None:
        etag = _schema_etag(data, status=meta.get("status", ""), source=meta.get("source", ""))
        headers["ETag"] = etag
        if_none_match = request.headers.get("if-none-match")
        if if_none_match and if_none_match == etag:
            return Response(status_code=304, headers=headers)
    return JSONResponse(content=build_envelope(data, meta), headers=headers)


@router.get("/metadata/schema")
async def get_graph_schema(
    request: Request,
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None, description="Target a specific data source within a workspace."),
    connectionId: Optional[str] = Query(None, description="Legacy connection ID."),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Get complete graph schema including entity types, relationship types,
    visual configurations, and hierarchy rules.
    This enables frontend to dynamically load schema from backend.

    Cache-first: reads the latest ``data_source_stats.graph_schema`` row
    populated by the stats service. On miss, serves a synthetic schema
    built from the data source's assigned ontology (zero entity counts,
    but correct types/relationships — canvas renders immediately while
    the real schema computes in the background; ``meta.status=partial``,
    ``meta.source=ontology``). If there is no ontology assigned either,
    returns ``meta.status=computing`` with a pollable jobId.

    Always HTTP 200. ``data`` carries the schema (or null when
    computing); ``meta`` carries cache state.

    A weak ETag is computed over ``data`` so clients that re-fetch with
    ``If-None-Match`` get a 304 — saves re-parsing unchanged schemas
    while ``meta`` still updates freshness on every read.
    """
    ds_id = await _resolve_data_source_id(session, ws_id, dataSourceId)
    if not ds_id:
        raise HTTPException(status_code=400, detail="dataSourceId is required")

    try:
        data, meta = await read_stats_cache(session, ds_id, ws_id, "graph_schema")
        return _schema_response(data, request, meta=meta)
    except CacheMiss:
        pass
    except (OperationalError, SQLAlchemyError) as exc:
        logger.warning("get_graph_schema: database unavailable (ds_id=%s): %s", ds_id, exc)
        return JSONResponse(content=build_error_envelope(ds_id, reason="db_unavailable"))

    # Cache miss: enqueue a real refresh in the background regardless of
    # whether synthetic schema rendered, so the frontend's poll hook
    # auto-upgrades from synthetic to real when the worker completes.
    msg_id = await enqueue_stats_job_safe(ds_id, ws_id) if ws_id else None
    job_id = msg_id or f"dedup:{ds_id}"
    poll_url = f"/api/v1/{ws_id}/graph/introspection/refresh/{job_id}" if ws_id else None

    synthetic = await build_synthetic_schema(session, ds_id)
    if synthetic:
        logger.info("stats_cache.served_synthetic endpoint=/metadata/schema ds_id=%s", ds_id)
        meta = build_meta(
            status="partial",
            source="ontology",
            data_source_id=ds_id,
            missing_fields=SYNTHETIC_SCHEMA_MISSING_FIELDS,
            refreshing=True,
            job_id=job_id,
            poll_url=poll_url,
        )
        return _schema_response(synthetic, request, meta=meta)

    logger.info("stats_cache.served_computing endpoint=/metadata/schema ds_id=%s msg_id=%s", ds_id, msg_id)
    return JSONResponse(content=build_computing_envelope(
        ds_id, ws_id, msg_id, missing_fields=SYNTHETIC_SCHEMA_MISSING_FIELDS,
    ))


# ── Async introspection refresh ──────────────────────────────────────
# On large graphs (1M+ nodes/edges), a live introspection can take
# minutes. The background stats service keeps the Postgres cache fresh
# on a 5-minute interval. This endpoint enqueues an on-demand refresh
# job onto the stats-service Redis stream (``stats.jobs``) so the actual
# work runs on the stats-worker thread pool with its own 600s timeout
# budget — not on a FastAPI request thread where it would race against
# ``HTTP_TIMEOUT_GRAPH_SECS``.
#
# Dedup: the stats service's Redis SET-NX claim prevents duplicate work
# when the scheduler and the user-triggered refresh collide on the same
# data source. Callers that lose the dedup race get ``status=
# already_computing`` with a deterministic jobId; they poll the same
# status endpoint regardless.


@router.post("/introspection/refresh")
async def refresh_introspection(
    ws_id: Optional[str] = None,
    dataSourceId: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """Trigger a non-blocking refresh of the schema/introspection cache.

    Pushes a job onto the stats-service Redis stream ``stats.jobs``.
    The stats worker — which owns the only code path allowed to
    introspect the provider — picks it up within seconds.

    Always HTTP 200 with the canonical ``{data, meta}`` envelope.
    ``meta.status="computing"`` regardless of whether we won the
    ``try_claim`` race; the caller polls the status endpoint identically
    either way and observes completion via ``data_source_stats.updated_at``.
    """
    ds_id = await _resolve_data_source_id(session, ws_id, dataSourceId)
    if not ds_id or not ws_id:
        raise HTTPException(status_code=400, detail="ws_id and dataSourceId required")

    msg_id = await enqueue_stats_job_safe(ds_id, ws_id)
    logger.info(
        "stats_cache.refresh_trigger ds_id=%s msg_id=%s outcome=%s",
        ds_id, msg_id, "enqueued" if msg_id else "dedup_or_redis_down",
    )
    return build_computing_envelope(ds_id, ws_id, msg_id)


@router.get("/introspection/refresh/{job_id}")
async def get_refresh_status(
    job_id: str,
    dataSourceId: Optional[str] = Query(None, description="Data source ID for completion inference."),
    since: Optional[str] = Query(None, description="ISO timestamp the caller considers the job 'started at'. A later cache updated_at proves completion."),
    session: AsyncSession = Depends(get_db_session),
):
    """Poll refresh status by comparing ``data_source_stats.updated_at``.

    The stats service does not track individual job lifecycles — it just
    upserts the cache row on completion. We infer completion: if the
    cache row's ``updated_at`` is newer than the ``since`` timestamp
    the caller sent, the job has completed.

    Returns the canonical ``{data, meta}`` envelope. ``meta.status`` is
    one of:

    * ``fresh`` — cache row exists and (when ``since`` provided)
                  ``updated_at > since`` proves the requested job finished
    * ``computing`` — no row yet, or ``updated_at`` not advanced past ``since``
    * ``error`` — DB unavailable
    """
    from backend.app.db.repositories.stats_repo import get_data_source_stats

    if not dataSourceId:
        return build_envelope(
            None,
            build_meta(
                status="error", source="error",
                data_source_id="",
                missing_fields=["dataSourceId_query_param_required"],
                job_id=job_id,
            ),
        )

    try:
        cache = await get_data_source_stats(session, dataSourceId)
    except (OperationalError, SQLAlchemyError) as exc:
        logger.warning("get_refresh_status: database unavailable (ds_id=%s): %s", dataSourceId, exc)
        return build_envelope(
            None,
            build_meta(
                status="error", source="error",
                data_source_id=dataSourceId,
                missing_fields=["db_unavailable"],
                job_id=job_id,
            ),
        )

    completed = False
    if since and cache and cache.updated_at:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            updated_dt = datetime.fromisoformat(cache.updated_at)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            completed = updated_dt > since_dt
        except (ValueError, TypeError):
            completed = False

    if completed:
        return build_envelope(
            None,
            build_meta(
                status="fresh", source="postgres",
                data_source_id=dataSourceId,
                age_seconds=0, ttl_seconds=None,
                refreshing=False, job_id=job_id,
                updated_at=cache.updated_at if cache else None,
            ),
        )

    return build_envelope(
        None,
        build_meta(
            status="computing", source="none",
            data_source_id=dataSourceId,
            refreshing=True, job_id=job_id,
            updated_at=cache.updated_at if cache else None,
        ),
    )


@router.post("/edges/aggregated", response_model=AggregatedEdgeResult, response_model_by_alias=True)
async def get_aggregated_edges(
    request: AggregatedEdgeRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Get aggregated edges between containers.
    Returns summarized edge information showing lineage connections
    at a higher granularity level (e.g., between datasets instead of columns).
    """
    await _enforce_fair_share(engine, ENDPOINT_AGGREGATED)

    async def compute() -> AggregatedEdgeResult:
        return await engine.get_aggregated_edges(request)

    scope = _cache_scope(engine)
    if scope is None:
        return await compute()

    # Sort URN lists so two semantically identical requests with differing
    # input order map to the same cache key — the frontend's chunked
    # fan-out frequently produces equivalent batches in different orders.
    return await get_graph_cache().get_or_compute(
        scope=scope,
        endpoint=ENDPOINT_AGGREGATED,
        params={
            "sourceUrns": sorted(request.source_urns or []),
            "targetUrns": sorted(request.target_urns or []) if request.target_urns else None,
            "granularity": request.granularity,
            "includeEdgeTypes": sorted(request.include_edge_types or []) if request.include_edge_types else None,
            "lineageEdgeTypes": sorted(request.lineage_edge_types or []) if request.lineage_edge_types else None,
            "containmentEdgeTypes": sorted(request.containment_edge_types or []) if request.containment_edge_types else None,
        },
        compute=compute,
        model_cls=AggregatedEdgeResult,
    )


@router.post("/edges/aggregated/materialize")
async def materialize_aggregated_edges(
    engine: ContextEngine = Depends(get_context_engine),
    batch_size: int = Body(1000, embed=True),
):
    """
    Trigger batch materialization of AGGREGATED edges.
    Scans all lineage edges and creates/updates [:AGGREGATED] relationships
    between ancestor pairs at equivalent hierarchy levels.

    This should be run after data ingestion or as a periodic maintenance task.
    """
    ontology = await engine.get_ontology_metadata()
    try:
        stats = await engine.materialize_aggregated_edges(
            batch_size=batch_size,
            containment_edge_types=list(ontology.containment_edge_types),
            lineage_edge_types=list(ontology.lineage_edge_types),
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse(content=stats)


@router.post("/nodes/create", response_model=CreateNodeResult, response_model_by_alias=True)
async def create_node(
    request: CreateNodeRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Create a new node with optional containment edge.
    If parentUrn is provided, automatically creates a CONTAINS edge
    based on ontology rules.
    """
    result = await engine.create_node(request)
    await _invalidate_cache(engine)
    return result


# ─── Edge CRUD ────────────────────────────────────────────────────────────────

@router.post("/edges", response_model=EdgeMutationResult, response_model_by_alias=True, status_code=201)
async def create_edge(
    request: CreateEdgeRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Create a directed edge between two existing nodes.

    Validates source/target entity types against the active ontology.
    If idempotencyKey is supplied and a matching edge already exists it is returned unchanged.
    """
    result = await engine.create_edge(request)
    await _invalidate_cache(engine)
    return result


@router.patch("/edges/{edge_id}", response_model=EdgeMutationResult, response_model_by_alias=True)
async def update_edge(
    edge_id: str,
    request: UpdateEdgeRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """Update mutable properties of an existing edge. Edge type is immutable."""
    result = await engine.update_edge(edge_id, request)
    await _invalidate_cache(engine)
    return result


@router.delete("/edges/{edge_id}", status_code=204)
async def delete_edge(
    edge_id: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    """Delete an edge by ID."""
    success = await engine.delete_edge(edge_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Edge '{edge_id}' not found")
    await _invalidate_cache(engine)


# ─── Preflight / guided-create APIs ─────────────────────────────────────────

class AllowedChildOption(BaseModel):
    entity_type: str = Field(alias="entityType")
    label: str
    description: Optional[str] = None
    allowed: bool
    reason: Optional[str] = None     # Non-null when allowed=False (explains why)

    class Config:
        populate_by_name = True


class AllowedEdgeOption(BaseModel):
    edge_type: str = Field(alias="edgeType")
    label: str
    description: Optional[str] = None
    allowed: bool
    reason: Optional[str] = None

    class Config:
        populate_by_name = True


@router.post("/commands/batch", response_model=BatchResponse, response_model_by_alias=True)
async def batch_commands(
    request: BatchCommandRequest = Body(...),
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Execute a batch of graph mutation commands.

    Each command is one of:
      create_node, update_node, delete_node,
      create_edge, update_edge, delete_edge

    Commands are executed in order. If fail_fast=true (default), execution
    stops on the first failure and returns partial results. If fail_fast=false,
    all commands are attempted and results are collected.

    All node/edge mutations are validated against the active ontology before
    any write is attempted.  Validation failures count as command failures.
    """
    from backend.common.models.graph import CreateNodeRequest as _CNR, CreateEdgeRequest as _CER
    from backend.common.models.graph import UpdateEdgeRequest as _UER

    results: List[BatchCommandResult] = []
    succeeded = 0
    failed = 0

    for cmd in request.commands:
        try:
            if cmd.op == "create_node":
                node_req = _CNR(**cmd.payload)
                res = await engine.create_node(node_req)
                if res.success:
                    succeeded += 1
                    results.append(BatchCommandResult(
                        ref=cmd.ref, op=cmd.op, success=True,
                        createdUrn=res.node.urn if res.node else None,
                    ))
                else:
                    failed += 1
                    results.append(BatchCommandResult(
                        ref=cmd.ref, op=cmd.op, success=False, error=res.error,
                    ))
            elif cmd.op == "create_edge":
                edge_req = _CER(**cmd.payload)
                res = await engine.create_edge(edge_req)
                if res.success:
                    succeeded += 1
                    results.append(BatchCommandResult(
                        ref=cmd.ref, op=cmd.op, success=True,
                        createdEdgeId=res.edge.id if res.edge else None,
                    ))
                else:
                    failed += 1
                    results.append(BatchCommandResult(
                        ref=cmd.ref, op=cmd.op, success=False, error=res.error,
                        warnings=res.warnings,
                    ))
            elif cmd.op == "delete_edge":
                edge_id = cmd.payload.get("edgeId") or cmd.payload.get("edge_id", "")
                ok = await engine.delete_edge(edge_id)
                if ok:
                    succeeded += 1
                    results.append(BatchCommandResult(ref=cmd.ref, op=cmd.op, success=True))
                else:
                    failed += 1
                    results.append(BatchCommandResult(
                        ref=cmd.ref, op=cmd.op, success=False,
                        error=f"Edge '{edge_id}' not found",
                    ))
            else:
                failed += 1
                results.append(BatchCommandResult(
                    ref=cmd.ref, op=cmd.op, success=False,
                    error=f"Unsupported op: {cmd.op}",
                ))
        except Exception as exc:
            failed += 1
            results.append(BatchCommandResult(
                ref=cmd.ref, op=cmd.op, success=False, error=str(exc),
            ))

        if request.fail_fast and failed > 0:
            # Fill remaining commands as skipped
            remaining = request.commands[len(results):]
            for skipped in remaining:
                results.append(BatchCommandResult(
                    ref=skipped.ref, op=skipped.op, success=False,
                    error="Skipped: batch aborted due to earlier failure (fail_fast=true)",
                ))
            break

    return BatchResponse(
        results=results,
        total=len(request.commands),
        succeeded=succeeded,
        failed=failed,
    )


@router.get("/nodes/{urn}/allowed-children", response_model=List[AllowedChildOption], response_model_by_alias=True)
async def get_allowed_children(
    urn: str,
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Return all entity types from the active ontology with an indication of
    whether each may be created as a child of this node.

    Used to populate and disable options in the guided create panel.
    """
    return await engine.get_allowed_children(urn)


@router.get("/nodes/{urn}/allowed-edges", response_model=List[AllowedEdgeOption], response_model_by_alias=True)
async def get_allowed_edges(
    urn: str,
    direction: str = Query("outgoing", description="outgoing | incoming | both"),
    engine: ContextEngine = Depends(get_context_engine),
):
    """
    Return all relationship types from the active ontology with an indication of
    whether each may be created from (or to) this node.

    Used to populate and disable options in the guided edge creator.
    """
    return await engine.get_allowed_edges(urn, direction=direction)
