"""
Repository for views table.
Views define how to visually render context models (or ad-hoc graphs).
Supports CRUD, filtering, favourites, and enterprise discovery.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy import select, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ViewORM,
    ViewFavouriteORM,
    WorkspaceORM,
    ContextModelORM,
    WorkspaceDataSourceORM,
    UserORM,
)
from backend.common.models.management import (
    ViewCreateRequest,
    ViewUpdateRequest,
    ViewResponse,
    ViewListResponse,
    ViewFacetValue,
    ViewFacetCreator,
    ViewFacetsResponse,
    ViewCatalogStats,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

async def _get_workspace_name(
    session: AsyncSession, workspace_id: Optional[str]
) -> Optional[str]:
    if not workspace_id:
        return None
    result = await session.execute(
        select(WorkspaceORM.name).where(WorkspaceORM.id == workspace_id)
    )
    return result.scalar_one_or_none()


async def _get_context_model_name(
    session: AsyncSession, context_model_id: Optional[str]
) -> Optional[str]:
    if not context_model_id:
        return None
    result = await session.execute(
        select(ContextModelORM.name).where(ContextModelORM.id == context_model_id)
    )
    return result.scalar_one_or_none()


async def _get_data_source_name(
    session: AsyncSession, data_source_id: Optional[str]
) -> Optional[str]:
    if not data_source_id:
        return None
    result = await session.execute(
        select(WorkspaceDataSourceORM.label).where(WorkspaceDataSourceORM.id == data_source_id)
    )
    return result.scalar_one_or_none()


async def _get_favourite_count(
    session: AsyncSession, view_id: str
) -> int:
    result = await session.execute(
        select(func.count()).where(ViewFavouriteORM.view_id == view_id)
    )
    return result.scalar() or 0


async def _is_favourited(
    session: AsyncSession, view_id: str, user_id: Optional[str]
) -> bool:
    if not user_id:
        return False
    result = await session.execute(
        select(ViewFavouriteORM.id).where(
            ViewFavouriteORM.view_id == view_id,
            ViewFavouriteORM.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


# ------------------------------------------------------------------ #
# ORM → Pydantic conversion                                           #
# ------------------------------------------------------------------ #

async def _get_creator_info(
    session: AsyncSession, user_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve ``(display_name, email)`` for a view's creator.

    Returns ``(None, None)`` when the user id is NULL, the legacy
    ``"anonymous"`` sentinel, or the user record no longer exists.
    Callers fall back to ``created_by`` (the raw id) in that case.
    """
    if not user_id or user_id == "anonymous":
        return None, None
    result = await session.execute(
        select(UserORM.first_name, UserORM.last_name, UserORM.email)
        .where(UserORM.id == user_id)
    )
    row = result.one_or_none()
    if not row:
        return None, None
    first, last, email = row
    display = f"{first or ''} {last or ''}".strip() or email
    return display, email


def _to_response(
    row: ViewORM,
    *,
    workspace_name: Optional[str] = None,
    data_source_name: Optional[str] = None,
    context_model_name: Optional[str] = None,
    created_by_name: Optional[str] = None,
    created_by_email: Optional[str] = None,
    favourite_count: int = 0,
    is_favourited: bool = False,
) -> ViewResponse:
    config_dict = json.loads(row.config or "{}")
    # Project layoutType from config so metadata-only consumers (e.g. the
    # ViewWizard scope resolver) don't have to parse the full config blob.
    layout_type = None
    if isinstance(config_dict, dict):
        raw_layout = config_dict.get("layoutType")
        layout_type = str(raw_layout) if raw_layout is not None else None
    return ViewResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        contextModelId=row.context_model_id,
        contextModelName=context_model_name,
        workspaceId=row.workspace_id,
        workspaceName=workspace_name,
        dataSourceId=row.data_source_id,
        dataSourceName=data_source_name,
        viewType=row.view_type or "graph",
        layoutType=layout_type,
        config=config_dict,
        visibility=row.visibility or "private",
        createdBy=row.created_by,
        createdByName=created_by_name,
        createdByEmail=created_by_email,
        tags=json.loads(row.tags) if row.tags else None,
        isPinned=bool(row.is_pinned) if row.is_pinned else False,
        favouriteCount=favourite_count,
        isFavourited=is_favourited,
        createdAt=row.created_at,
        updatedAt=row.updated_at,
        deletedAt=getattr(row, 'deleted_at', None),
        ontologyDigest=getattr(row, 'ontology_digest', None),
    )


async def _to_enriched_response(
    session: AsyncSession,
    row: ViewORM,
    user_id: Optional[str] = None,
) -> ViewResponse:
    """Build a ViewResponse enriched with workspace name, data source name, CM name, and favourite info.

    Single-row enrichment used by create/get/update paths where N+1 is not a concern.
    For list paths, call :func:`_batch_enrich_rows` instead — it issues 5 batched queries
    in parallel regardless of row count.
    """
    ws_name = await _get_workspace_name(session, row.workspace_id)
    ds_name = await _get_data_source_name(session, row.data_source_id)
    cm_name = await _get_context_model_name(session, row.context_model_id)
    creator_name, creator_email = await _get_creator_info(session, row.created_by)
    fav_count = await _get_favourite_count(session, row.id)
    fav = await _is_favourited(session, row.id, user_id)
    return _to_response(
        row,
        workspace_name=ws_name,
        data_source_name=ds_name,
        context_model_name=cm_name,
        created_by_name=creator_name,
        created_by_email=creator_email,
        favourite_count=fav_count,
        is_favourited=fav,
    )


# ------------------------------------------------------------------ #
# Batched enrichment for list paths (kills N+1)                       #
# ------------------------------------------------------------------ #
#
# Previously, list endpoints called _to_enriched_response per row, which
# fanned out to 6 sequential SELECTs per row (workspace name, datasource
# name, context-model name, creator info, favourite count, is-favourited).
# At limit=20 this was ~121 round-trips, and under any concurrency the
# WEB pool would saturate, ballooning per-query latency until the request
# took multiple seconds.
#
# _batch_enrich_rows issues at most 5 lookups regardless of row count,
# in parallel via asyncio.gather. _to_response remains the single
# row→Pydantic converter so existing single-row paths are unaffected.

def _batch_enrich_enabled() -> bool:
    """Kill-switch for the batched-enrichment path.

    Default ON. Set ``VIEWS_BATCH_ENRICH=false`` to fall back to the
    legacy per-row path while we observe the new query shapes in prod.
    Read on each call so test fixtures and runtime overrides take effect
    without re-import. Cheap (os.getenv hits a dict).
    """
    return os.getenv("VIEWS_BATCH_ENRICH", "true").lower() not in ("false", "0", "no", "")


async def _batch_enrich_rows(
    session: AsyncSession,
    rows: Sequence[ViewORM],
    user_id: Optional[str] = None,
    *,
    fav_count_overrides: Optional[Dict[str, int]] = None,
) -> List[ViewResponse]:
    """Enrich a batch of view rows with name lookups + favourite info using batched queries.

    Issues at most 5 SELECTs total (workspaces, datasources, context models,
    users, favourite counts, favourites-by-user) regardless of how many
    rows are passed — versus 6×N round-trips in the legacy per-row path.

    Queries are issued sequentially because ``AsyncSession`` is not safe
    for concurrent operations on a single session (it serialises via an
    internal lock); the win here is the *number* of round-trips, not
    parallelism.

    ``fav_count_overrides`` lets callers that already computed the
    favourite count (e.g. ``list_popular_views`` which JOINs an aggregated
    subquery) skip the GROUP BY query and reuse those counts.
    """
    if not rows:
        return []

    workspace_ids: Set[str] = {r.workspace_id for r in rows if r.workspace_id}
    ds_ids: Set[str] = {r.data_source_id for r in rows if r.data_source_id}
    cm_ids: Set[str] = {r.context_model_id for r in rows if r.context_model_id}
    creator_ids: Set[str] = {
        r.created_by for r in rows
        if r.created_by and r.created_by != "anonymous"
    }
    view_ids: List[str] = [r.id for r in rows]

    ws_map: Dict[str, str] = {}
    if workspace_ids:
        res = await session.execute(
            select(WorkspaceORM.id, WorkspaceORM.name)
            .where(WorkspaceORM.id.in_(workspace_ids))
        )
        ws_map = {wid: name for wid, name in res.all()}

    ds_map: Dict[str, str] = {}
    if ds_ids:
        res = await session.execute(
            select(WorkspaceDataSourceORM.id, WorkspaceDataSourceORM.label)
            .where(WorkspaceDataSourceORM.id.in_(ds_ids))
        )
        ds_map = {did: label for did, label in res.all()}

    cm_map: Dict[str, str] = {}
    if cm_ids:
        res = await session.execute(
            select(ContextModelORM.id, ContextModelORM.name)
            .where(ContextModelORM.id.in_(cm_ids))
        )
        cm_map = {cid: name for cid, name in res.all()}

    user_map: Dict[str, tuple[Optional[str], Optional[str]]] = {}
    if creator_ids:
        res = await session.execute(
            select(UserORM.id, UserORM.first_name, UserORM.last_name, UserORM.email)
            .where(UserORM.id.in_(creator_ids))
        )
        for uid, first, last, email in res.all():
            display = f"{first or ''} {last or ''}".strip() or email
            user_map[uid] = (display, email)

    # Favourite counts: skip the GROUP BY when the caller already has
    # them (popular views computes them from its JOINed subquery).
    if fav_count_overrides is not None:
        fav_counts: Dict[str, int] = dict(fav_count_overrides)
    elif view_ids:
        res = await session.execute(
            select(ViewFavouriteORM.view_id, func.count())
            .where(ViewFavouriteORM.view_id.in_(view_ids))
            .group_by(ViewFavouriteORM.view_id)
        )
        fav_counts = {vid: cnt for vid, cnt in res.all()}
    else:
        fav_counts = {}

    fav_set: Set[str] = set()
    if user_id and user_id != "anonymous" and view_ids:
        res = await session.execute(
            select(ViewFavouriteORM.view_id)
            .where(ViewFavouriteORM.view_id.in_(view_ids))
            .where(ViewFavouriteORM.user_id == user_id)
        )
        fav_set = {vid for (vid,) in res.all()}

    responses: List[ViewResponse] = []
    for row in rows:
        creator = user_map.get(row.created_by or "", (None, None))
        responses.append(_to_response(
            row,
            workspace_name=ws_map.get(row.workspace_id) if row.workspace_id else None,
            data_source_name=ds_map.get(row.data_source_id) if row.data_source_id else None,
            context_model_name=cm_map.get(row.context_model_id) if row.context_model_id else None,
            created_by_name=creator[0],
            created_by_email=creator[1],
            favourite_count=fav_counts.get(row.id, 0),
            is_favourited=row.id in fav_set,
        ))
    return responses


# ------------------------------------------------------------------ #
# CRUD                                                                 #
# ------------------------------------------------------------------ #

async def create_view(
    session: AsyncSession,
    req: ViewCreateRequest,
    *,
    ontology_digest: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ViewResponse:
    """Persist a new view.

    ``ontology_digest`` should be supplied by the endpoint layer, computed
    from the currently-resolved ontology for the view's workspace/data
    source. It is stored verbatim and used later by the wizard's drift
    detection. Pass None only when the caller has no way to resolve the
    ontology (e.g. ad-hoc tests, legacy seed scripts).

    ``user_id`` is stored in ``created_by`` so the Explorer can filter
    views by creator ("My Views"). Legacy rows created before this
    parameter existed have NULL ``created_by``.
    """
    logger.info(
        "create_view: name=%s workspace_id=%s data_source_id=%s digest=%s",
        req.name, req.workspace_id, req.data_source_id,
        ontology_digest[:12] + "…" if ontology_digest else None,
    )
    row = ViewORM(
        name=req.name,
        description=req.description,
        context_model_id=req.context_model_id,
        workspace_id=req.workspace_id,
        data_source_id=req.data_source_id,
        view_type=req.view_type or "graph",
        config=json.dumps(req.config) if req.config else "{}",
        visibility=req.visibility or "private",
        created_by=user_id,
        tags=json.dumps(req.tags) if req.tags else None,
        is_pinned=req.is_pinned,
        ontology_digest=ontology_digest,
    )
    session.add(row)
    await session.flush()
    return await _to_enriched_response(session, row, user_id)


async def get_view(
    session: AsyncSession, view_id: str
) -> Optional[ViewResponse]:
    result = await session.execute(
        select(ViewORM).where(ViewORM.id == view_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return await _to_enriched_response(session, row)


async def get_view_enriched(
    session: AsyncSession, view_id: str, user_id: Optional[str] = None
) -> Optional[ViewResponse]:
    result = await session.execute(
        select(ViewORM).where(ViewORM.id == view_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return await _to_enriched_response(session, row, user_id)


async def update_view(
    session: AsyncSession,
    view_id: str,
    req: ViewUpdateRequest,
    *,
    ontology_digest: Optional[str] = None,
) -> Optional[ViewResponse]:
    """Update an existing view.

    ``ontology_digest`` — when the endpoint layer can resolve the active
    ontology, it passes the fresh digest here so the saved view reflects
    the CURRENT ontology state (not the one it was originally created
    against). This resets the drift-detection baseline on every explicit
    save; passing None preserves the existing digest unchanged.
    """
    result = await session.execute(
        select(ViewORM).where(ViewORM.id == view_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None

    if req.name is not None:
        row.name = req.name
    if req.description is not None:
        row.description = req.description
    if req.context_model_id is not None:
        row.context_model_id = req.context_model_id
    if req.view_type is not None:
        row.view_type = req.view_type
    if req.config is not None:
        row.config = json.dumps(req.config)
    if req.visibility is not None:
        row.visibility = req.visibility
    if req.tags is not None:
        row.tags = json.dumps(req.tags)
    if req.is_pinned is not None:
        row.is_pinned = req.is_pinned
    if ontology_digest is not None:
        row.ontology_digest = ontology_digest

    row.updated_at = datetime.now(timezone.utc).isoformat()
    await session.flush()
    return await _to_enriched_response(session, row)


async def delete_view(
    session: AsyncSession, view_id: str
) -> bool:
    """Soft-delete a view by setting deleted_at timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    result = await session.execute(
        update(ViewORM)
        .where(ViewORM.id == view_id, ViewORM.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    return result.rowcount > 0


async def restore_view(
    session: AsyncSession, view_id: str
) -> bool:
    """Restore a soft-deleted view by clearing deleted_at."""
    result = await session.execute(
        update(ViewORM)
        .where(ViewORM.id == view_id, ViewORM.deleted_at.isnot(None))
        .values(deleted_at=None)
    )
    return result.rowcount > 0


async def permanently_delete_view(
    session: AsyncSession, view_id: str
) -> bool:
    """Hard-delete a view from the database (irreversible)."""
    result = await session.execute(
        delete(ViewORM).where(ViewORM.id == view_id)
    )
    return result.rowcount > 0


# ------------------------------------------------------------------ #
# Filtered listing & discovery                                         #
# ------------------------------------------------------------------ #

def _apply_view_filters(
    query,
    *,
    visibility: Optional[str] = None,
    visibility_in: Optional[List[str]] = None,
    workspace_id: Optional[str] = None,
    workspace_ids: Optional[List[str]] = None,
    context_model_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    view_type: Optional[str] = None,
    view_types: Optional[List[str]] = None,
    created_by: Optional[str] = None,
    created_by_in: Optional[List[str]] = None,
    created_after: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    favourited_only: bool = False,
    include_deleted: bool = False,
    deleted_only: bool = False,
    attention_only: bool = False,
):
    """Apply all filter predicates to a query on ``ViewORM``.

    Shared by the listing, counting, and discovery code paths so the same
    set of predicates produces the same result set whether the caller is
    fetching rows or the total count.
    """
    # Soft-delete filtering
    if deleted_only:
        query = query.where(ViewORM.deleted_at.isnot(None))
    elif not include_deleted:
        query = query.where(ViewORM.deleted_at.is_(None))

    # When favourited_only is True, inner-join on the favourites table so only
    # views the requesting user has bookmarked are returned.
    if favourited_only and user_id:
        query = query.join(
            ViewFavouriteORM,
            (ViewFavouriteORM.view_id == ViewORM.id) &
            (ViewFavouriteORM.user_id == user_id),
        )

    # Multi-workspace takes precedence over single-workspace filter.
    if workspace_ids:
        query = query.where(ViewORM.workspace_id.in_(workspace_ids))
    elif workspace_id:
        query = query.where(ViewORM.workspace_id == workspace_id)

    if context_model_id:
        query = query.where(ViewORM.context_model_id == context_model_id)
    if data_source_id:
        query = query.where(ViewORM.data_source_id == data_source_id)

    # View type: multi wins over single.
    if view_types:
        query = query.where(ViewORM.view_type.in_(view_types))
    elif view_type:
        query = query.where(ViewORM.view_type == view_type)

    # Creator: multi wins over single.
    if created_by_in:
        query = query.where(ViewORM.created_by.in_(created_by_in))
    elif created_by:
        query = query.where(ViewORM.created_by == created_by)

    if created_after:
        query = query.where(ViewORM.created_at >= created_after)

    # Visibility filters — visibility_in (set match) wins over single visibility.
    if visibility_in:
        query = query.where(ViewORM.visibility.in_(visibility_in))
    elif visibility:
        query = query.where(ViewORM.visibility == visibility)

    # Tag filter — OR semantics across the supplied tags. Tags are stored
    # as a JSON-encoded string (e.g. ``["finance","pii"]``), so we look
    # for the quoted token. This is a pragmatic match that's safe for
    # normal tag values (alphanumeric + dashes); more exotic characters
    # should be normalised upstream.
    if tags:
        query = query.where(
            or_(*[ViewORM.tags.ilike(f'%"{t}"%') for t in tags])
        )

    # Search / attention both need workspace + data source joins; do them once.
    needs_ws_join = bool(search) or attention_only
    needs_ds_join = bool(search) or attention_only
    if needs_ws_join:
        query = query.outerjoin(
            WorkspaceORM, ViewORM.workspace_id == WorkspaceORM.id,
        )
    if needs_ds_join:
        query = query.outerjoin(
            WorkspaceDataSourceORM,
            ViewORM.data_source_id == WorkspaceDataSourceORM.id,
        )

    if search:
        pattern = f"%{search}%"
        query = query.where(
            ViewORM.name.ilike(pattern)
            | ViewORM.description.ilike(pattern)
            | WorkspaceORM.name.ilike(pattern)
            | WorkspaceDataSourceORM.label.ilike(pattern)
            | ViewORM.created_by.ilike(pattern)
            | ViewORM.tags.ilike(pattern)
        )

    if attention_only:
        # Views "needing attention" are those that are stale (not updated in
        # 90 days), or reference an inactive/missing workspace or data source.
        # Mirrors the useViewHealth hook on the frontend so server-side
        # filtering returns the same set the client would compute locally.
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat()
        ds_id_set_but_missing = (
            ViewORM.data_source_id.isnot(None)
            & WorkspaceDataSourceORM.id.is_(None)
        )
        query = query.where(
            or_(
                ViewORM.updated_at < stale_cutoff,
                WorkspaceORM.id.is_(None),          # workspace missing (broken)
                WorkspaceORM.is_active.is_(False),  # workspace inactive (warning)
                WorkspaceDataSourceORM.is_active.is_(False),
                ds_id_set_but_missing,
            )
        )

    return query


async def list_views_filtered(
    session: AsyncSession,
    *,
    visibility: Optional[str] = None,
    visibility_in: Optional[List[str]] = None,
    workspace_id: Optional[str] = None,
    workspace_ids: Optional[List[str]] = None,
    context_model_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    view_type: Optional[str] = None,
    view_types: Optional[List[str]] = None,
    created_by: Optional[str] = None,
    created_by_in: Optional[List[str]] = None,
    created_after: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[List[str]] = None,
    limit: int = 50,
    offset: int = 0,
    user_id: Optional[str] = None,
    favourited_only: bool = False,
    include_deleted: bool = False,
    deleted_only: bool = False,
    attention_only: bool = False,
) -> ViewListResponse:
    """Return a paginated envelope of views matching the given filters.

    The envelope includes an authoritative ``total`` count so the Explorer
    can render "20 of 1,432" style stats without guessing from page size.
    ``has_more`` and ``next_offset`` are computed once on the server so
    callers never have to reason about "was this the last page?".
    """
    # --- shared filter application (select + count share this) -----------
    filter_kwargs = dict(
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
        user_id=user_id,
        favourited_only=favourited_only,
        include_deleted=include_deleted,
        deleted_only=deleted_only,
        attention_only=attention_only,
    )

    select_query = _apply_view_filters(select(ViewORM), **filter_kwargs)
    select_query = (
        select_query
        .order_by(ViewORM.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )

    # Count query uses the same filters but selects COUNT(DISTINCT id).
    # DISTINCT guards against the joins multiplying rows when a view has
    # many favourites (for favourited_only queries) or other cases.
    count_query = _apply_view_filters(
        select(func.count(func.distinct(ViewORM.id))),
        **filter_kwargs,
    )

    result = await session.execute(select_query)
    rows = result.scalars().unique().all()

    count_result = await session.execute(count_query)
    total = count_result.scalar_one() or 0

    if _batch_enrich_enabled():
        responses = await _batch_enrich_rows(session, rows, user_id)
    else:
        responses = [await _to_enriched_response(session, row, user_id) for row in rows]

    has_more = (offset + len(responses)) < total
    next_offset = offset + len(responses) if has_more else None
    return ViewListResponse(
        items=responses,
        total=total,
        has_more=has_more,
        next_offset=next_offset,
    )


async def get_view_facets(
    session: AsyncSession,
) -> ViewFacetsResponse:
    """Aggregate distinct tags, view types, and creators across non-deleted views.

    Used by the Explorer to populate the Tag / View Type / Creator filter
    dropdowns. Facets are intentionally GLOBAL (unscoped by other active
    filters) so users can always pick from the full set of values in the
    database rather than the intersection of their current filters —
    matches the behaviour users expect from Explorer-style UIs where the
    picker is a discovery tool, not a query refinement.
    """
    base_where = ViewORM.deleted_at.is_(None)

    # 1. Tags — parsed in Python since the column is a JSON string.
    tags_query = (
        select(ViewORM.tags)
        .where(base_where)
        .where(ViewORM.tags.isnot(None))
    )
    tags_result = await session.execute(tags_query)
    tag_counts: Dict[str, int] = {}
    for (tags_json,) in tags_result.all():
        try:
            parsed = json.loads(tags_json) if tags_json else []
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for t in parsed:
            if isinstance(t, str) and t:
                tag_counts[t] = tag_counts.get(t, 0) + 1
    tag_facets = [
        ViewFacetValue(value=v, count=c)
        for v, c in sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))
    ]

    # 2. View types — direct GROUP BY.
    vt_query = (
        select(ViewORM.view_type, func.count().label("cnt"))
        .where(base_where)
        .group_by(ViewORM.view_type)
        .order_by(func.count().desc())
    )
    vt_result = await session.execute(vt_query)
    view_type_facets = [
        ViewFacetValue(value=vt or "graph", count=cnt)
        for vt, cnt in vt_result.all()
    ]

    # 3. Creators — GROUP BY created_by + join users for display metadata.
    creators_query = (
        select(ViewORM.created_by, func.count(ViewORM.id).label("cnt"))
        .where(base_where)
        .where(ViewORM.created_by.isnot(None))
        .group_by(ViewORM.created_by)
        .order_by(func.count(ViewORM.id).desc())
    )
    creators_result = await session.execute(creators_query)
    creator_rows = creators_result.all()

    # Batch-resolve display names in a single query.
    creator_ids = [cid for cid, _ in creator_rows if cid]
    user_map: Dict[str, UserORM] = {}
    if creator_ids:
        users_result = await session.execute(
            select(UserORM).where(UserORM.id.in_(creator_ids))
        )
        for u in users_result.scalars().all():
            user_map[u.id] = u

    creator_facets: List[ViewFacetCreator] = []
    for creator_id, cnt in creator_rows:
        user = user_map.get(creator_id)
        if user:
            display_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email
            email = user.email
        else:
            # Orphaned row — creator id is stored but the user record is
            # gone or never existed (e.g. legacy "anonymous" sentinel).
            display_name = creator_id
            email = None
        creator_facets.append(ViewFacetCreator(
            user_id=creator_id,
            display_name=display_name,
            email=email,
            count=cnt,
        ))

    return ViewFacetsResponse(
        tags=tag_facets,
        view_types=view_type_facets,
        creators=creator_facets,
    )


async def get_view_stats(
    session: AsyncSession,
    *,
    visibility: Optional[str] = None,
    visibility_in: Optional[List[str]] = None,
    workspace_id: Optional[str] = None,
    workspace_ids: Optional[List[str]] = None,
    context_model_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    view_type: Optional[str] = None,
    view_types: Optional[List[str]] = None,
    created_by: Optional[str] = None,
    created_by_in: Optional[List[str]] = None,
    created_after: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    favourited_only: bool = False,
    include_deleted: bool = False,
    deleted_only: bool = False,
    attention_only: bool = False,
) -> ViewCatalogStats:
    """Compute the Explorer stats bar numbers for a given filter set.

    Accepts the same filter params as ``list_views_filtered`` and
    reuses ``_apply_view_filters`` so the stats always describe the
    exact same population the list endpoint would return. Four cheap
    aggregate queries — no row materialisation.
    """
    filter_kwargs = dict(
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
        user_id=user_id,
        favourited_only=favourited_only,
        include_deleted=include_deleted,
        deleted_only=deleted_only,
        # Respect an incoming ``attention_only`` on the base queries so
        # total/recent/last-activity all describe the same population.
        # The needs_attention query below always overlays True — when
        # both resolve to True the numbers line up as expected.
        attention_only=attention_only,
    )

    # Total — matches list ``total`` for identical filters.
    total_query = _apply_view_filters(
        select(func.count(func.distinct(ViewORM.id))),
        **filter_kwargs,
    )
    total_result = await session.execute(total_query)
    total = total_result.scalar_one() or 0

    # Recently added — same filters plus a created_after overlay.
    seven_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat()
    # Caller may already have a stricter created_after; use the later
    # of the two so we never broaden what they asked for.
    effective_created_after = (
        max(created_after, seven_days_ago)
        if created_after
        else seven_days_ago
    )
    recent_kwargs = {**filter_kwargs, "created_after": effective_created_after}
    recent_query = _apply_view_filters(
        select(func.count(func.distinct(ViewORM.id))),
        **recent_kwargs,
    )
    recent_result = await session.execute(recent_query)
    recently_added = recent_result.scalar_one() or 0

    # Needs attention — matches the ``attentionOnly`` list filter exactly.
    attention_kwargs = {**filter_kwargs, "attention_only": True}
    attention_query = _apply_view_filters(
        select(func.count(func.distinct(ViewORM.id))),
        **attention_kwargs,
    )
    attention_result = await session.execute(attention_query)
    needs_attention = attention_result.scalar_one() or 0

    # Last activity — MAX(updated_at) across the same filtered set.
    last_activity_query = _apply_view_filters(
        select(func.max(ViewORM.updated_at)),
        **filter_kwargs,
    )
    last_activity_result = await session.execute(last_activity_query)
    last_activity_at = last_activity_result.scalar_one()

    return ViewCatalogStats(
        total=total,
        recently_added=recently_added,
        needs_attention=needs_attention,
        last_activity_at=last_activity_at,
    )


async def list_popular_views(
    session: AsyncSession,
    *,
    limit: int = 20,
    user_id: Optional[str] = None,
) -> List[ViewResponse]:
    """List the most-favourited views visible to the caller.

    Visibility scoping:
    - ``enterprise`` and ``workspace`` visibility are visible to everyone
      (matches how ``list_views_filtered`` treats access today).
    - ``private`` views only surface for their creator, so user A never
      sees user B's private view bubble up into their Trending strip
      just because A has happened to favourite it.

    Zero-favourite views are excluded so Trending reflects actual
    popularity rather than padding with unloved views.
    """
    fav_count_sq = (
        select(
            ViewFavouriteORM.view_id,
            func.count().label("fav_count"),
        )
        .group_by(ViewFavouriteORM.view_id)
        .subquery()
    )

    # Privacy-safe visibility predicate: everyone sees non-private views;
    # private views only surface to their creator.
    visibility_predicate = ViewORM.visibility.in_(("enterprise", "workspace"))
    if user_id:
        visibility_predicate = or_(
            visibility_predicate,
            ViewORM.created_by == user_id,
        )

    query = (
        select(ViewORM, fav_count_sq.c.fav_count)
        .join(fav_count_sq, ViewORM.id == fav_count_sq.c.view_id)
        .where(ViewORM.deleted_at.is_(None))
        .where(fav_count_sq.c.fav_count > 0)
        .where(visibility_predicate)
        .order_by(
            fav_count_sq.c.fav_count.desc(),
            ViewORM.updated_at.desc(),
        )
        .limit(limit)
    )

    result = await session.execute(query)
    tuples = result.all()
    rows = [t[0] for t in tuples]
    # Favourite counts are already produced by the JOIN'd subquery; reuse
    # them so the batched enricher can skip its own GROUP BY.
    fav_count_overrides = {t[0].id: int(t[1] or 0) for t in tuples}

    if _batch_enrich_enabled():
        return await _batch_enrich_rows(
            session, rows, user_id,
            fav_count_overrides=fav_count_overrides,
        )

    # Legacy fallback (kill-switch path)
    responses = []
    for row_tuple in tuples:
        row = row_tuple[0]
        fav_count = row_tuple[1] or 0
        ws_name = await _get_workspace_name(session, row.workspace_id)
        ds_name = await _get_data_source_name(session, row.data_source_id)
        cm_name = await _get_context_model_name(session, row.context_model_id)
        creator_name, creator_email = await _get_creator_info(session, row.created_by)
        is_fav = await _is_favourited(session, row.id, user_id)
        responses.append(_to_response(
            row,
            workspace_name=ws_name,
            data_source_name=ds_name,
            context_model_name=cm_name,
            created_by_name=creator_name,
            created_by_email=creator_email,
            favourite_count=fav_count,
            is_favourited=is_fav,
        ))
    return responses


async def list_views_for_context_model(
    session: AsyncSession,
    context_model_id: str,
    user_id: Optional[str] = None,
) -> List[ViewResponse]:
    """Find all views referencing a given context model."""
    query = (
        select(ViewORM)
        .where(ViewORM.context_model_id == context_model_id)
        .where(ViewORM.deleted_at.is_(None))
        .order_by(ViewORM.updated_at.desc())
    )
    result = await session.execute(query)
    rows = result.scalars().all()
    if _batch_enrich_enabled():
        return await _batch_enrich_rows(session, rows, user_id)
    return [await _to_enriched_response(session, row, user_id) for row in rows]


async def update_visibility(
    session: AsyncSession, view_id: str, visibility: str,
    user_id: Optional[str] = None,
) -> Optional[ViewResponse]:
    result = await session.execute(
        select(ViewORM).where(ViewORM.id == view_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    row.visibility = visibility
    row.updated_at = datetime.now(timezone.utc).isoformat()
    await session.flush()
    return await _to_enriched_response(session, row, user_id)


# ------------------------------------------------------------------ #
# Favourites                                                           #
# ------------------------------------------------------------------ #

async def favourite_view(
    session: AsyncSession, view_id: str, user_id: str
) -> bool:
    existing = await session.execute(
        select(ViewFavouriteORM.id).where(
            ViewFavouriteORM.view_id == view_id,
            ViewFavouriteORM.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        return False
    fav = ViewFavouriteORM(view_id=view_id, user_id=user_id)
    session.add(fav)
    await session.flush()
    return True


async def unfavourite_view(
    session: AsyncSession, view_id: str, user_id: str
) -> bool:
    result = await session.execute(
        select(ViewFavouriteORM).where(
            ViewFavouriteORM.view_id == view_id,
            ViewFavouriteORM.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        await session.delete(row)
        return True
    return False
