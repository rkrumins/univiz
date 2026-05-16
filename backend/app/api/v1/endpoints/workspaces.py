"""
Admin Workspace endpoints — CRUD for workspaces and their data sources.
A workspace is an operational context containing one or more data sources,
each binding a Provider + Graph Name + Ontology.

RBAC Phase 2: each route declares its required permission via
``Depends(requires(...))``. List endpoints filter their results by the
caller's effective permissions so non-admins only see workspaces they
have a binding into. The legacy "everyone sees everything" behaviour
returns when ``RBAC_ENFORCE_WORKSPACES=false`` / ``RBAC_ENFORCE_DATASOURCES=false``.
"""
from typing import List
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import (
    get_current_user,
    get_permission_claims,
    rbac_flag,
    requires,
)
from backend.app.common.http_caching import make_etag, maybe_not_modified
from backend.app.db.engine import get_db_session
from backend.app.db.repositories import workspace_repo, provider_repo, ontology_definition_repo, data_source_repo
from backend.app.providers.manager import provider_manager as provider_registry  # alias during migration
from backend.app.services.permission_service import (
    PermissionClaims,
    has_permission,
)
from backend.app.services.stats_cache import (
    SYNTHETIC_SCHEMA_MISSING_FIELDS, CacheMiss,
    build_computing_envelope, build_envelope, build_meta,
    build_synthetic_schema, read_stats_cache,
)
from backend.auth_service.interface import User
from backend.common.models.management import (
    WorkspaceCreateRequest,
    WorkspaceUpdateRequest,
    WorkspaceResponse,
    DataSourceCreateRequest,
    DataSourceUpdateRequest,
    DataSourceResponse,
    WorkspaceDataSourceImpactResponse,
)
from backend.insights_service.enqueue import enqueue_stats_job_safe

router = APIRouter()


def _can_read_workspace(claims: PermissionClaims, ws_id: str) -> bool:
    """A user can read a workspace if they hold any binding into it
    (any non-empty permission entry under that ws_id) OR are global
    admin. Used for list filtering and the GET-by-id check."""
    if has_permission(claims, "system:admin"):
        return True
    return bool(claims.ws_perms.get(ws_id))


def _ensure_can_read_workspace(claims: PermissionClaims, ws_id: str) -> None:
    if rbac_flag("RBAC_ENFORCE_WORKSPACES") and not _can_read_workspace(claims, ws_id):
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{ws_id}' not found",
        )


# ================================================================== #
# Workspace CRUD                                                       #
# ================================================================== #

@router.get("", response_model=List[WorkspaceResponse])
async def list_workspaces(
    _user: User = Depends(get_current_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """List workspaces the caller has any binding into.

    System admins see every workspace; everyone else sees only the
    workspaces their role bindings (direct or via group) reach.
    Filtering happens after the repo fetch — fine at current scale,
    can move into a SQL JOIN if the workspace count becomes large.
    """
    workspaces = await workspace_repo.list_workspaces(session)
    if not rbac_flag("RBAC_ENFORCE_WORKSPACES"):
        return workspaces
    if has_permission(claims, "system:admin"):
        return workspaces
    return [w for w in workspaces if claims.ws_perms.get(w.id)]


@router.post("", response_model=WorkspaceResponse, status_code=201)
async def create_workspace(
    req: WorkspaceCreateRequest = Body(...),
    _user: User = Depends(requires("workspaces:create")),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new workspace with one or more data sources."""
    # Allow empty workspaces for "Skip for Now" onboarding
    if not req.data_sources:
        req.data_sources = []

    # Validate all referenced catalog items / providers and ontologies exist
    from backend.app.db.repositories import catalog_repo
    for ds in req.data_sources:
        if ds.catalog_item_id:
            if not await catalog_repo.get_catalog_item(session, ds.catalog_item_id):
                raise HTTPException(status_code=404, detail=f"Catalog Item '{ds.catalog_item_id}' not found")
        elif not ds.provider_id:
            raise HTTPException(status_code=422, detail="Each data source requires either catalogItemId or providerId")
        if ds.ontology_id and not await ontology_definition_repo.get_ontology(session, ds.ontology_id):
            raise HTTPException(status_code=404, detail=f"Ontology '{ds.ontology_id}' not found")

    try:
        return await workspace_repo.create_workspace(session, req)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str = Path(...),
    _user: User = Depends(get_current_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single workspace with its data sources.

    Returns 404 (not 403) when the caller has no binding — keeps
    workspace existence private from non-members.
    """
    _ensure_can_read_workspace(claims, workspace_id)
    ws = await workspace_repo.get_workspace(session, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    return ws


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: str = Path(...),
    req: WorkspaceUpdateRequest = Body(...),
    _user: User = Depends(requires("workspace:admin", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Update workspace metadata (name, description, is_active)."""
    ws = await workspace_repo.update_workspace(session, workspace_id, req)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    return ws


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str = Path(...),
    _user: User = Depends(requires("workspace:admin", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a workspace (cascades data sources, views, and rule-sets)."""
    ws = await workspace_repo.get_workspace_orm(session, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    await provider_registry.evict_workspace(workspace_id, session)
    deleted = await workspace_repo.delete_workspace(session, workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")


@router.post("/{workspace_id}/set-default", response_model=WorkspaceResponse)
async def set_default_workspace(
    workspace_id: str = Path(...),
    _user: User = Depends(requires("system:admin")),
    session: AsyncSession = Depends(get_db_session),
):
    """Promote a workspace to the default (used when no ws_id specified).

    Affects every user globally, so requires ``system:admin`` rather
    than the per-workspace admin permission.
    """
    success = await workspace_repo.set_default(session, workspace_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    # Clear cached default
    provider_registry._default_ws_id = None
    ws = await workspace_repo.get_workspace(session, workspace_id)
    return ws


# ================================================================== #
# Data Source Sub-Resource CRUD                                        #
# ================================================================== #

@router.get("/{workspace_id}/data-sources", response_model=List[DataSourceResponse])
async def list_data_sources(
    workspace_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:read", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """List all data sources for a workspace."""
    ws = await workspace_repo.get_workspace_orm(session, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    return await data_source_repo.list_data_sources(session, workspace_id)


@router.post("/{workspace_id}/data-sources", response_model=DataSourceResponse, status_code=201)
async def add_data_source(
    workspace_id: str = Path(...),
    req: DataSourceCreateRequest = Body(...),
    _user: User = Depends(requires("workspace:datasource:manage", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Add a data source to a workspace."""
    ws = await workspace_repo.get_workspace_orm(session, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found")
    # Validate references based on which path is used
    if req.catalog_item_id:
        from backend.app.db.repositories import catalog_repo
        if not await catalog_repo.get_catalog_item(session, req.catalog_item_id):
            raise HTTPException(status_code=404, detail=f"Catalog Item '{req.catalog_item_id}' not found")
    elif not req.provider_id:
        raise HTTPException(status_code=422, detail="Either catalogItemId or providerId is required")
    if req.ontology_id and not await ontology_definition_repo.get_ontology(session, req.ontology_id):
        raise HTTPException(status_code=404, detail=f"Ontology '{req.ontology_id}' not found")
    try:
        created = await data_source_repo.create_data_source(session, workspace_id, req)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        if "uq_ds_ws_prov_graph" in str(e) or "already allocated" in str(e):
            raise HTTPException(status_code=409, detail="This data source already exists on this workspace")
        raise

    # Commit before enqueueing the stats poll. The stats worker uses an
    # independent session pool (PoolRole.JOBS), so if it pulls the job
    # off the Redis stream before this transaction commits, ``_resolve_
    # graph_key`` will return None and the first poll will fail. Explicit
    # commit guarantees the row is visible to the worker by the time the
    # XADD lands. The dependency-cleanup commit at request end becomes a
    # no-op for this empty transaction.
    await session.commit()

    # Proactive seeding: enqueue an immediate stats poll so the cache is
    # populated by the time the user opens Explorer. Best-effort — Redis
    # being down silently falls through; the scheduler tick will pick up
    # the data source within ~30s once Redis recovers.
    await enqueue_stats_job_safe(created.id, workspace_id)

    return created


@router.put("/{workspace_id}/data-sources/{ds_id}", response_model=DataSourceResponse)
async def update_data_source(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    req: DataSourceUpdateRequest = Body(...),
    _user: User = Depends(requires("workspace:datasource:manage", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a data source. Evicts cached provider if provider/graph changed."""
    old_ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not old_ds or old_ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace")

    # Validate new ontology if changing
    if req.ontology_id and not await ontology_definition_repo.get_ontology(session, req.ontology_id):
        raise HTTPException(status_code=404, detail=f"Ontology '{req.ontology_id}' not found")

    # Track whether schema-invalidating fields changed so we know whether
    # to re-seed the stats cache below.
    schema_invalidating_change = (
        req.projection_mode is not None
        or req.dedicated_graph_name is not None
        or req.ontology_id is not None
    )

    # Evict old cache entry if provider/graph config changed
    if req.projection_mode is not None or req.dedicated_graph_name is not None:
        await provider_registry.evict_workspace(workspace_id, session)

    ds = await data_source_repo.update_data_source(session, ds_id, req)
    if not ds:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found")

    # Re-seed cache on schema-invalidating changes so the next read
    # doesn't serve stale schema/ontology. Commit first so the worker's
    # session sees the updated row when it picks up the job.
    if schema_invalidating_change:
        await session.commit()
        await enqueue_stats_job_safe(ds_id, workspace_id)

    return ds


@router.delete("/{workspace_id}/data-sources/{ds_id}", status_code=204)
async def remove_data_source(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:manage", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Remove a data source. Rejects if it's the last one in the workspace."""
    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace")

    count = await data_source_repo.count_data_sources(session, workspace_id)
    if count <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last data source in a workspace")

    await provider_registry.evict_workspace(workspace_id, session)
    await data_source_repo.delete_data_source(session, ds_id)

@router.get("/{workspace_id}/data-sources/{ds_id}/impact", response_model=WorkspaceDataSourceImpactResponse)
async def get_data_source_impact(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:read", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Return the blast radius of deleting a data source (e.g. affected semantic views)."""
    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace")
    
    return await data_source_repo.get_data_source_impact(session, ds_id)

@router.post("/{workspace_id}/data-sources/{ds_id}/set-primary", response_model=DataSourceResponse)
async def set_primary_data_source(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:manage", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Promote a data source to primary within its workspace."""
    success = await data_source_repo.set_primary(session, workspace_id, ds_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace")
    ds = await data_source_repo.get_data_source(session, ds_id)
    return ds


@router.patch("/{workspace_id}/data-sources/{ds_id}/projection-mode", response_model=DataSourceResponse)
async def set_projection_mode(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    mode: str = Body(..., embed=True),
    _user: User = Depends(requires("workspace:datasource:manage", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Set the aggregation edge projection mode for a data source.

    mode values:
    - "in_source"  — store AGGREGATED edges in the same graph as source data
    - "dedicated"  — store in a separate projection graph
    - ""           — clear override, inherit from provider default
    """
    if mode and mode not in ("in_source", "dedicated"):
        raise HTTPException(status_code=422, detail=f"Invalid projection mode: '{mode}'. Must be 'in_source', 'dedicated', or empty.")
    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace")

    # Guard: cannot change mode while an aggregation job is active
    from sqlalchemy import select
    from backend.app.services.aggregation.models import AggregationJobORM
    active_job = (
        await session.execute(
            select(AggregationJobORM)
            .where(AggregationJobORM.data_source_id == ds_id)
            .where(AggregationJobORM.status.in_(["pending", "running"]))
        )
    ).scalars().first()
    if active_job:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot change projection mode while aggregation job '{active_job.id}' is active. Cancel or wait for it to complete.",
        )

    ds.projection_mode = mode if mode else None
    from datetime import datetime, timezone
    ds.updated_at = datetime.now(timezone.utc).isoformat()
    await session.flush()
    await provider_registry.evict_workspace(workspace_id, session)
    return await data_source_repo.get_data_source(session, ds_id)


# ================================================================== #
# Cached Stats (DB-only — zero provider dependency)                    #
# ================================================================== #

@router.get("/{workspace_id}/datasources/{ds_id}/cached-stats")
async def get_cached_stats(
    request: Request,
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:read", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Return cached graph statistics for a data source.

    Cache-only read returning the canonical ``{data, meta}`` envelope
    with HTTP 200. ``data`` carries a composite of all cached fields
    (counts, schema_stats, ontology_metadata, graph_schema). On miss,
    enqueues a background refresh and returns ``meta.status=computing``.
    Never 404 when the data source exists.

    ETag/304 — the actual cached payload only changes when the stats
    job updates ``cache.updated_at``. We emit a strong ETag derived
    from (ds_id, updated_at) so clients that revalidate against an
    unchanged row get a 304 with no body. The 304 is only available
    on the cache-hit path (cold and expired-cache responses always
    carry a fresh "computing" envelope so polling kicks off correctly).
    """
    import json

    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace '{workspace_id}'")

    # ``read_stats_cache`` returns a single field at a time; /cached-stats
    # is the one endpoint that wants all four cached fields in a single
    # response, so it composes the envelope by hand using the same
    # freshness primitives every other handler relies on.
    from backend.app.db.repositories.stats_repo import get_data_source_stats
    from backend.app.services.stats_cache import (
        age_seconds, classify_stats_service_health, classify_tier,
        parse_iso, ttl_seconds,
    )

    cache = await get_data_source_stats(session, ds_id)
    if not cache:
        msg_id = await enqueue_stats_job_safe(ds_id, workspace_id)
        return JSONResponse(content=build_computing_envelope(ds_id, workspace_id, msg_id))

    age = age_seconds(parse_iso(cache.updated_at))
    tier = classify_tier(age)
    if tier == "expired":
        msg_id = await enqueue_stats_job_safe(ds_id, workspace_id)
        return JSONResponse(content=build_computing_envelope(ds_id, workspace_id, msg_id))

    # Cache-hit path: short-circuit with 304 if the client already has
    # this exact (ds_id, updated_at) tuple. The composite payload is a
    # pure function of those two, so an ETag match means "your cached
    # body is still byte-identical to what we'd send." Time-dependent
    # meta fields (age_seconds, ttl_seconds, refreshing) are recomputed
    # by the client from their cached updated_at — same accuracy.
    etag = make_etag("cached-stats", ds_id, cache.updated_at)
    not_modified = maybe_not_modified(request, etag)
    if not_modified is not None:
        return not_modified

    refreshing = False
    if tier == "stale":
        await enqueue_stats_job_safe(ds_id, workspace_id)
        refreshing = True

    def _maybe_load(raw):
        if not raw or raw == "{}":
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    composite = {
        "nodeCount": cache.node_count or 0,
        "edgeCount": cache.edge_count or 0,
        "entityTypeCounts": _maybe_load(cache.entity_type_counts) or {},
        "edgeTypeCounts": _maybe_load(cache.edge_type_counts) or {},
        "schemaStats": _maybe_load(cache.schema_stats),
        "ontologyMetadata": _maybe_load(cache.ontology_metadata),
        "graphSchema": _maybe_load(cache.graph_schema),
    }

    service_status, last_error = await classify_stats_service_health(session, ds_id)
    meta = build_meta(
        status="fresh" if tier == "fresh" else "stale",
        source="postgres",
        data_source_id=ds_id,
        age_seconds=age,
        ttl_seconds=ttl_seconds(age),
        stats_service_status=service_status,
        provider_health="unreachable" if last_error else "healthy",
        refreshing=refreshing,
        updated_at=cache.updated_at,
    )
    return JSONResponse(
        content=build_envelope(composite, meta),
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


# ================================================================== #
# Cached Schema (DB-only — zero provider dependency)                   #
# ================================================================== #

@router.get("/{workspace_id}/datasources/{ds_id}/cached-schema")
async def get_cached_schema(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:read", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Return cached graph schema for a data source.

    Cache-only read returning the canonical ``{data, meta}`` envelope
    with HTTP 200. On miss, serves a synthetic schema built from the
    assigned ontology (``meta.status=partial``, ``meta.source=ontology``)
    and enqueues a background refresh. If no ontology is assigned
    either, returns ``meta.status=computing``. Never 404 when the data
    source exists — "cache not populated yet" is a state, not an error.
    """
    # 404 here is a genuine "doesn't exist", not a cache-state error.
    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace '{workspace_id}'")

    try:
        data, meta = await read_stats_cache(session, ds_id, workspace_id, "graph_schema")
        return JSONResponse(content=build_envelope(data, meta))
    except CacheMiss:
        pass

    msg_id = await enqueue_stats_job_safe(ds_id, workspace_id)
    job_id = msg_id or f"dedup:{ds_id}"
    poll_url = f"/api/v1/{workspace_id}/graph/introspection/refresh/{job_id}"

    synthetic = await build_synthetic_schema(session, ds_id)
    if synthetic:
        meta = build_meta(
            status="partial", source="ontology",
            data_source_id=ds_id,
            missing_fields=SYNTHETIC_SCHEMA_MISSING_FIELDS,
            refreshing=True, job_id=job_id, poll_url=poll_url,
        )
        return JSONResponse(content=build_envelope(synthetic, meta))

    return JSONResponse(content=build_computing_envelope(
        ds_id, workspace_id, msg_id, missing_fields=SYNTHETIC_SCHEMA_MISSING_FIELDS,
    ))


# ================================================================== #
# Cached Ontology Metadata (DB-only — zero provider dependency)        #
# ================================================================== #

@router.get("/{workspace_id}/datasources/{ds_id}/cached-ontology")
async def get_cached_ontology(
    workspace_id: str = Path(...),
    ds_id: str = Path(...),
    _user: User = Depends(requires("workspace:datasource:read", workspace="workspace_id")),
    session: AsyncSession = Depends(get_db_session),
):
    """Return cached ontology metadata for a data source.

    Cache-only read returning the canonical ``{data, meta}`` envelope
    with HTTP 200. On miss, enqueues a background refresh and returns
    ``meta.status=computing``. Never 404 when the data source exists.
    """
    ds = await data_source_repo.get_data_source_orm(session, ds_id)
    if not ds or ds.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail=f"Data source '{ds_id}' not found in workspace '{workspace_id}'")

    try:
        data, meta = await read_stats_cache(session, ds_id, workspace_id, "ontology_metadata")
        return JSONResponse(content=build_envelope(data, meta))
    except CacheMiss:
        pass

    msg_id = await enqueue_stats_job_safe(ds_id, workspace_id)
    return JSONResponse(content=build_computing_envelope(ds_id, workspace_id, msg_id))
