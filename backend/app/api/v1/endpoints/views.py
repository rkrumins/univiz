"""
View endpoints (top-level, cross-workspace).

Views are visual renderings of context models (or ad-hoc graphs).
Mounted at /api/v1/views

RBAC Phase 2C: each route enforces the three-layer view evaluator
(``backend.app.services.view_access``). Reads pass when ANY of:
workspace binding, visibility tier, or explicit ``resource_grants``.
Mutations (create / edit / delete / change-visibility / restore /
hard-delete) check the corresponding action predicate.

The list endpoint filters its items post-fetch when enforcement is on.
That means ``total``/``hasMore`` can overestimate for non-admins
(callers see fewer rows than the count claims). A Phase 3 SQL refactor
will push the filter into the query for accurate paging; for now the
trade-off is acceptable because the kill-switch
``RBAC_ENFORCE_VIEWS=false`` reverts to the legacy behaviour.
"""
import logging
import os
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import (
    get_optional_user,
    get_permission_claims,
    rbac_flag,
    requires,
)
from backend.app.common.single_flight import normalised_principal, read_views_sf
from backend.app.db.engine import get_db_session
from backend.app.db.models import ViewORM
from backend.app.db.repositories import view_repo
from backend.app.providers.manager import provider_manager as provider_registry  # alias during migration
from backend.app.services.context_engine import ContextEngine
from backend.app.services.permission_service import PermissionClaims
from backend.app.services import view_access
from backend.auth_service.interface import User
from backend.common.models.management import (
    ViewCreateRequest,
    ViewUpdateRequest,
    ViewResponse,
    ViewListResponse,
    ViewFacetsResponse,
    ViewCatalogStats,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _viewer_context(
    session: AsyncSession,
    user: Optional[User],
    claims: PermissionClaims,
) -> view_access.ViewerContext:
    """Build the per-request ViewerContext used by every guarded route."""
    return await view_access.ViewerContext.build(session, user=user, claims=claims)


async def _load_view_orm(session: AsyncSession, view_id: str) -> ViewORM:
    """Fetch the raw ORM row (the access predicates need it).

    The endpoint then calls ``view_repo.get_view_enriched`` to return
    the response shape — kept separate so the access check happens
    against the authoritative row and not a lossy DTO projection.
    """
    from sqlalchemy import select
    row = await session.execute(
        select(ViewORM).where(ViewORM.id == view_id)
    )
    view = row.scalar_one_or_none()
    if view is None:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")
    return view


class _ViewProxy:
    """Adapter that exposes the access-predicate fields off a
    ``ViewResponse``-shaped object.

    The list endpoint receives DTOs from the repo, not ORM rows. The
    predicates only need ``id``, ``workspace_id``, ``visibility``, and
    ``created_by`` — all present on the response shape. We wrap the
    DTO in this lightweight proxy so the predicate functions stay
    typed against ``ViewORM`` without forcing a re-fetch.
    """

    __slots__ = ("id", "workspace_id", "visibility", "created_by")

    def __init__(self, *, id, workspace_id, visibility, created_by):
        self.id = id
        self.workspace_id = workspace_id
        self.visibility = visibility
        self.created_by = created_by

    @classmethod
    def from_response(cls, item) -> "_ViewProxy":
        # Pydantic models expose snake-case attrs; legacy ORM responses
        # via ``from_attributes`` do too. Both paths funnel here.
        return cls(
            id=getattr(item, "id"),
            workspace_id=getattr(item, "workspace_id", None),
            visibility=getattr(item, "visibility", "private"),
            created_by=getattr(item, "created_by", None),
        )


# Suppress the "imported but unused" hint while os is referenced via
# rbac_flag. The flag wrapper itself reads os.environ.
_ = os

# Fallback user_id when no auth token is present (backward compatibility).
_ANONYMOUS_USER = "anonymous"


def _user_id(user) -> str:
    """Extract user_id from the optional user dependency, or fall back to anonymous."""
    return user.id if user else _ANONYMOUS_USER


async def _compute_ontology_digest(
    session: AsyncSession,
    workspace_id: Optional[str],
    data_source_id: Optional[str],
) -> Optional[str]:
    """Resolve the active ontology for a view's scope and return its digest.

    Best-effort: if the engine can't be built (no workspace, provider down,
    unresolvable ontology), returns None so the caller stores NULL — the
    wizard treats NULL as "drift check unavailable" and just skips the
    banner. Drift detection is a UX feature, never a save blocker.
    """
    if not workspace_id:
        return None
    try:
        engine = await ContextEngine.for_workspace(
            workspace_id, provider_registry, session, data_source_id=data_source_id,
        )
        return await engine.get_ontology_digest()
    except Exception as exc:
        logger.warning(
            "Ontology digest computation failed for ws=%s ds=%s: %s",
            workspace_id, data_source_id, exc,
        )
        return None


@router.get("/popular", response_model=List[ViewResponse])
async def list_popular_views(
    limit: int = Query(20, le=100),
    user=Depends(get_optional_user),
    session: AsyncSession = Depends(get_db_session),
):
    """List the most-favourited enterprise-visible views.

    Single-flight wrapped: when N concurrent callers hit this with the
    same (principal, limit) pair the leader runs the query and the
    others receive the same result. Most-favourited views are a
    homepage-style render that frequently sees burst traffic from
    every Explorer tab opening at once; this kills the thundering
    herd against the views + favourites tables.
    """
    principal = normalised_principal(_user_id(user))
    key = ("popular", principal, limit)
    return await read_views_sf.run(
        key,
        lambda: view_repo.list_popular_views(
            session, limit=limit, user_id=_user_id(user),
        ),
    )


@router.get("/facets", response_model=ViewFacetsResponse)
async def get_view_facets(
    session: AsyncSession = Depends(get_db_session),
) -> ViewFacetsResponse:
    """Return distinct tags, view types, and creators across non-deleted views.

    Used to populate the Explorer's Tag / View Type / Creator filter
    dropdowns from the authoritative DB-wide set of values rather than
    deriving them from the currently-loaded page (which would miss
    tags/creators beyond the first page at scale).

    Single-flight wrapped: facets is a global aggregation read with a
    fixed key. Under any concurrency, exactly one worker runs the
    aggregate and the rest piggy-back on its result.
    """
    return await read_views_sf.run(
        ("facets",),
        lambda: view_repo.get_view_facets(session),
    )


@router.get("/stats", response_model=ViewCatalogStats)
async def get_view_stats(
    visibility: Optional[str] = Query(None),
    visibility_in: Optional[List[str]] = Query(None, alias="visibilityIn"),
    workspace_id: Optional[str] = Query(None, alias="workspaceId"),
    workspace_ids: Optional[List[str]] = Query(None, alias="workspaceIds"),
    context_model_id: Optional[str] = Query(None, alias="contextModelId"),
    data_source_id: Optional[str] = Query(None, alias="dataSourceId"),
    view_type: Optional[str] = Query(None, alias="viewType"),
    view_types: Optional[List[str]] = Query(None, alias="viewTypes"),
    created_by: Optional[str] = Query(None, alias="createdBy"),
    created_by_in: Optional[List[str]] = Query(None, alias="createdByIn"),
    created_after: Optional[str] = Query(None, alias="createdAfter"),
    search: Optional[str] = Query(None),
    tags: Optional[List[str]] = Query(None),
    favourited_only: bool = Query(False, alias="favouritedOnly"),
    include_deleted: bool = Query(False, alias="includeDeleted"),
    deleted_only: bool = Query(False, alias="deletedOnly"),
    attention_only: bool = Query(False, alias="attentionOnly"),
    user=Depends(get_optional_user),
    session: AsyncSession = Depends(get_db_session),
) -> ViewCatalogStats:
    """Aggregate counts for the Explorer stats bar, scoped to the same
    filters the list endpoint accepts. All four numbers describe the
    currently-filtered population so the stats bar stays in sync as
    users narrow their query.
    """
    return await view_repo.get_view_stats(
        session,
        visibility=visibility,
        visibility_in=visibility_in,
        workspace_id=workspace_id,
        workspace_ids=workspace_ids,
        context_model_id=context_model_id,
        data_source_id=data_source_id,
        view_type=view_type,
        view_types=view_types,
        created_by=created_by,
        created_by_in=created_by_in,
        created_after=created_after,
        search=search,
        tags=tags,
        user_id=_user_id(user),
        favourited_only=favourited_only,
        include_deleted=include_deleted,
        deleted_only=deleted_only,
        attention_only=attention_only,
    )


@router.get("/", response_model=ViewListResponse)
async def list_views(
    visibility: Optional[str] = Query(None),
    visibility_in: Optional[List[str]] = Query(None, alias="visibilityIn"),
    workspace_id: Optional[str] = Query(None, alias="workspaceId"),
    workspace_ids: Optional[List[str]] = Query(None, alias="workspaceIds"),
    context_model_id: Optional[str] = Query(None, alias="contextModelId"),
    data_source_id: Optional[str] = Query(None, alias="dataSourceId"),
    view_type: Optional[str] = Query(None, alias="viewType"),
    view_types: Optional[List[str]] = Query(None, alias="viewTypes"),
    created_by: Optional[str] = Query(None, alias="createdBy"),
    created_by_in: Optional[List[str]] = Query(None, alias="createdByIn"),
    created_after: Optional[str] = Query(None, alias="createdAfter"),
    search: Optional[str] = Query(None),
    tags: Optional[List[str]] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    favourited_only: bool = Query(False, alias="favouritedOnly"),
    include_deleted: bool = Query(False, alias="includeDeleted"),
    deleted_only: bool = Query(False, alias="deletedOnly"),
    attention_only: bool = Query(False, alias="attentionOnly"),
    include: List[str] = Query(
        default_factory=list,
        description=(
            "Optional embedded resources. ``include=popular`` folds the "
            "Explorer's trending strip into this response (under "
            "``popular``) so the page only makes one request instead of "
            "two."
        ),
    ),
    popular_limit: int = Query(
        10,
        le=100,
        alias="popularLimit",
        description="Cap on the embedded popular list when include=popular.",
    ),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
) -> ViewListResponse:
    """List accessible views as a paginated envelope.

    Returns ``{ items, total, hasMore, nextOffset }``. ``total`` is the
    authoritative count of matches so callers never have to infer "is
    there another page?" from array length.

    Filter params (single/multi pairs — the multi-value param wins when both are sent):
    - ``workspaceId`` / ``workspaceIds``
    - ``visibility`` / ``visibilityIn``
    - ``viewType`` / ``viewTypes``
    - ``createdBy`` / ``createdByIn``

    Additional filters:
    - ``createdAfter`` — ISO timestamp; returns views created on or after.
    - ``tags`` — OR semantics across the supplied tags.
    - ``attentionOnly`` — stale (>90d), inactive workspace/source, or
      broken data source reference. Mirrors the frontend health model
      so pagination stays accurate on large catalogs.
    """
    response = await view_repo.list_views_filtered(
        session,
        visibility=visibility,
        visibility_in=visibility_in,
        workspace_id=workspace_id,
        workspace_ids=workspace_ids,
        context_model_id=context_model_id,
        data_source_id=data_source_id,
        view_type=view_type,
        view_types=view_types,
        created_by=created_by,
        created_by_in=created_by_in,
        created_after=created_after,
        search=search,
        tags=tags,
        limit=limit,
        offset=offset,
        user_id=_user_id(user),
        favourited_only=favourited_only,
        include_deleted=include_deleted,
        deleted_only=deleted_only,
        attention_only=attention_only,
    )

    # Optional ?include=popular: fold the trending strip into the same
    # response so the Explorer only makes one round-trip instead of two.
    # ``list_popular_views`` enforces its own visibility scoping (private
    # views only surface to their creator), so it does not need to pass
    # through the RBAC post-filter below — popular IS the visible set.
    if "popular" in include:
        response.popular = await view_repo.list_popular_views(
            session, limit=popular_limit, user_id=_user_id(user),
        )

    if not rbac_flag("RBAC_ENFORCE_VIEWS"):
        return response

    # Three-layer post-filter. ``response.items`` is a list of
    # ViewResponse-shaped objects from the repo, NOT ORM rows — but
    # they carry the fields our predicates need (id, workspace_id,
    # visibility, created_by). We adapt them here rather than re-
    # querying the DB.
    ctx = await _viewer_context(session, user, claims)
    keep = []
    for item in response.items:
        proxy = _ViewProxy.from_response(item)
        if await view_access.can_read_view(session, ctx, proxy):
            keep.append(item)
    response.items = keep
    return response


@router.post("/", response_model=ViewResponse, status_code=201)
async def create_view(
    req: ViewCreateRequest = Body(...),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new view. workspaceId is required.

    Captures the current ontology digest on the new row so later edits
    can detect ontology drift. Records created_by as the authenticated
    user's ID so views can be filtered by creator in the Explorer.

    Authorization: requires ``workspace:view:create`` in the target
    workspace. Phase 2C enforces; Phase 1 left this open.
    """
    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        from backend.app.services.permission_service import has_permission
        if not has_permission(
            claims, "workspace:view:create", workspace_id=req.workspace_id,
        ):
            raise HTTPException(
                status_code=403,
                detail="Missing permission: workspace:view:create",
            )

    digest = await _compute_ontology_digest(
        session, req.workspace_id, req.data_source_id,
    )
    return await view_repo.create_view(
        session, req, ontology_digest=digest, user_id=_user_id(user),
    )


@router.get("/{view_id}", response_model=ViewResponse)
async def get_view(
    view_id: str = Path(...),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a single view by ID, enriched with workspace context and favourite data."""
    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        view_orm = await _load_view_orm(session, view_id)
        ctx = await _viewer_context(session, user, claims)
        if not await view_access.can_read_view(session, ctx, view_orm):
            # 404 (not 403) so view existence stays private from
            # users with no access path.
            raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")

    view = await view_repo.get_view_enriched(
        session, view_id, user_id=_user_id(user),
    )
    if not view:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")
    return view


@router.put("/{view_id}", response_model=ViewResponse)
async def update_view(
    view_id: str = Path(...),
    req: ViewUpdateRequest = Body(...),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Update an existing view.

    Refreshes the stored ontology digest to the CURRENT ontology state so
    subsequent edits will flag drift only for changes that happen after
    this save — every explicit save resets the drift baseline.
    """
    existing = await view_repo.get_view(session, view_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")

    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        view_orm = await _load_view_orm(session, view_id)
        ctx = await _viewer_context(session, user, claims)
        if not await view_access.can_edit_view(session, ctx, view_orm):
            raise HTTPException(
                status_code=403,
                detail="Missing permission: workspace:view:edit",
            )

    digest = await _compute_ontology_digest(
        session, existing.workspace_id, existing.data_source_id,
    )
    view = await view_repo.update_view(session, view_id, req, ontology_digest=digest)
    if not view:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")
    return view


@router.delete("/{view_id}", status_code=204)
async def delete_view(
    view_id: str = Path(...),
    permanent: bool = Query(False),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a view. Soft-deletes by default; pass ?permanent=true to remove from DB.

    Soft-delete: creator OR ``workspace:view:delete``.
    Hard-delete: ``workspace:admin`` only (per the action matrix).
    """
    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        view_orm = await _load_view_orm(session, view_id)
        ctx = await _viewer_context(session, user, claims)
        if permanent:
            allowed = view_access.can_hard_delete_view(ctx, view_orm)
            need = "workspace:admin"
        else:
            allowed = view_access.can_delete_view(ctx, view_orm)
            need = "workspace:view:delete"
        if not allowed:
            raise HTTPException(status_code=403, detail=f"Missing permission: {need}")

    if permanent:
        deleted = await view_repo.permanently_delete_view(session, view_id)
    else:
        deleted = await view_repo.delete_view(session, view_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")


@router.post("/{view_id}/restore", response_model=ViewResponse)
async def restore_view(
    view_id: str = Path(...),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Restore a soft-deleted view (workspace admin only)."""
    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        view_orm = await _load_view_orm(session, view_id)
        ctx = await _viewer_context(session, user, claims)
        if not view_access.can_restore_view(ctx, view_orm):
            raise HTTPException(
                status_code=403,
                detail="Missing permission: workspace:admin",
            )

    restored = await view_repo.restore_view(session, view_id)
    if not restored:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found or not deleted")
    view = await view_repo.get_view_enriched(session, view_id, user_id=_user_id(user))
    return view


@router.put("/{view_id}/visibility", response_model=ViewResponse)
async def update_view_visibility(
    view_id: str = Path(...),
    visibility: str = Body(..., embed=True),
    user=Depends(get_optional_user),
    claims: PermissionClaims = Depends(get_permission_claims),
    session: AsyncSession = Depends(get_db_session),
):
    """Change the visibility of a view (private | workspace | enterprise).

    Creator or workspace admin only — see the action matrix.
    """
    if visibility not in ("private", "workspace", "enterprise"):
        raise HTTPException(status_code=422, detail="visibility must be one of: private, workspace, enterprise")

    if rbac_flag("RBAC_ENFORCE_VIEWS"):
        view_orm = await _load_view_orm(session, view_id)
        ctx = await _viewer_context(session, user, claims)
        if not view_access.can_change_visibility(ctx, view_orm):
            raise HTTPException(
                status_code=403,
                detail="Only the creator or a workspace admin can change visibility",
            )

    view = await view_repo.update_visibility(
        session, view_id, visibility, user_id=_user_id(user),
    )
    if not view:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")
    return view


@router.post("/{view_id}/favourite", status_code=201)
async def favourite_view(
    view_id: str = Path(...),
    user=Depends(get_optional_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Favourite a view for the current user."""
    view = await view_repo.get_view(session, view_id)
    if not view:
        raise HTTPException(status_code=404, detail=f"View '{view_id}' not found")
    created = await view_repo.favourite_view(session, view_id, _user_id(user))
    return {"favourited": True, "created": created}


@router.delete("/{view_id}/favourite", status_code=204)
async def unfavourite_view(
    view_id: str = Path(...),
    user=Depends(get_optional_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Remove favourite for the current user."""
    removed = await view_repo.unfavourite_view(session, view_id, _user_id(user))
    if not removed:
        raise HTTPException(status_code=404, detail="Favourite not found")
