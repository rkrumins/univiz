"""
Announcement endpoints.

Public:
    GET /api/v1/announcements          — active announcements (no auth)
    GET /api/v1/announcements/config   — global banner config (no auth)

Admin:
    GET    /api/v1/admin/announcements          — all announcements
    POST   /api/v1/admin/announcements          — create
    PATCH  /api/v1/admin/announcements/{id}     — update
    DELETE /api/v1/admin/announcements/{id}     — delete
    GET    /api/v1/admin/announcements/config   — read config
    PUT    /api/v1/admin/announcements/config   — update config
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import require_admin
from backend.app.common.http_caching import make_etag, maybe_not_modified
from backend.app.db.engine import get_db_session
from backend.app.db.repositories import announcement_repo, feature_flags_repo
from backend.common.models.management import (
    AnnouncementCreateRequest,
    AnnouncementUpdateRequest,
    AnnouncementResponse,
    AnnouncementConfigUpdateRequest,
    AnnouncementConfigResponse,
)

_VALID_BANNER_TYPES = ("info", "warning", "success")

# ── Public router — no auth required ──────────────────────────────────

router = APIRouter()


@router.get("", response_model=list[AnnouncementResponse])
async def get_active_announcements(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    """Return all active announcements for banner display.

    Returns empty list if 'announcementsEnabled' feature flag is off.

    ETag/304 — this endpoint is polled every 15s × every user, so the
    response body is the same on the vast majority of requests. We
    emit a strong ETag derived from the active-set's count + max
    ``updated_at``: any add/edit/delete flips the tag; anything else
    matches and we return 304 with no body. At 1000 users that turns
    ~67 req/s of JSON serialisation into ~67 req/s of header-only
    revalidation. The DB roundtrip stays — it's a single-table indexed
    scan; the savings are body serialisation + bandwidth.
    """
    values, _, _ = await feature_flags_repo.get_feature_flags(session)
    enabled = values.get("announcementsEnabled", True)
    items = await announcement_repo.get_active_announcements(session) if enabled else []

    # Composite tag: count guards against deletes (max updated_at alone
    # would miss "one row removed but surviving rows are older"); max
    # updated_at guards against in-place edits. The "enabled" bit means
    # toggling the flag also flips the tag so clients revalidate.
    max_updated = max((a.updated_at for a in items), default="")
    etag = make_etag("announcements", enabled, len(items), max_updated)

    not_modified = maybe_not_modified(request, etag)
    if not_modified is not None:
        return not_modified

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    return items


@router.get("/config", response_model=AnnouncementConfigResponse)
async def get_announcement_config_public(
    session: AsyncSession = Depends(get_db_session),
):
    """Return global banner config (polling interval, default snooze) — no auth."""
    return await announcement_repo.get_announcement_config(session)


# ── Admin router — requires admin role ────────────────────────────────

admin_router = APIRouter()


@admin_router.get("", response_model=list[AnnouncementResponse])
async def list_all_announcements(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Return all announcements (active and inactive) for admin management."""
    return await announcement_repo.list_announcements(session)


@admin_router.post("", response_model=AnnouncementResponse, status_code=201)
async def create_announcement(
    req: AnnouncementCreateRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if req.banner_type not in _VALID_BANNER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"bannerType must be one of: {', '.join(_VALID_BANNER_TYPES)}",
        )
    if req.snooze_duration_minutes < 0:
        raise HTTPException(status_code=400, detail="snoozeDurationMinutes must be >= 0")
    return await announcement_repo.create_announcement(session, req, created_by=user.id)


@admin_router.patch("/{ann_id}", response_model=AnnouncementResponse)
async def update_announcement(
    ann_id: str,
    req: AnnouncementUpdateRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    if req.banner_type is not None and req.banner_type not in _VALID_BANNER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"bannerType must be one of: {', '.join(_VALID_BANNER_TYPES)}",
        )
    if req.snooze_duration_minutes is not None and req.snooze_duration_minutes < 0:
        raise HTTPException(status_code=400, detail="snoozeDurationMinutes must be >= 0")
    result = await announcement_repo.update_announcement(session, ann_id, req, updated_by=user.id)
    if result is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    return result


@admin_router.delete("/{ann_id}", status_code=204)
async def delete_announcement(
    ann_id: str,
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    ok = await announcement_repo.delete_announcement(session, ann_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Announcement not found")


# ── Config endpoints (admin) ───────────────────────────────────────────

@admin_router.get("/config", response_model=AnnouncementConfigResponse)
async def get_announcement_config(
    _user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Return global announcement config."""
    return await announcement_repo.get_announcement_config(session)


@admin_router.put("/config", response_model=AnnouncementConfigResponse)
async def update_announcement_config(
    req: AnnouncementConfigUpdateRequest,
    user=Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """Update global announcement config (polling interval, default snooze)."""
    if req.poll_interval_seconds is not None and req.poll_interval_seconds < 5:
        raise HTTPException(status_code=400, detail="pollIntervalSeconds must be >= 5")
    if req.default_snooze_minutes is not None and req.default_snooze_minutes < 0:
        raise HTTPException(status_code=400, detail="defaultSnoozeMinutes must be >= 0")
    return await announcement_repo.update_announcement_config(session, req, updated_by=user.id)
