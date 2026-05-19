from fastapi import APIRouter
from .endpoints import (
    graph, assignments, providers, ontologies, workspaces,
    assets, context_models, catalog, views, features,
    auth, users, announcements, aggregation, stats_admin,
    insights, me,
    groups, workspace_members, view_grants, role_bindings,
    permissions_admin, access_requests, rbac_search,
    graphs as authored_graphs,
)
from backend.auth_service.api.router import router as auth_session_router

api_router = APIRouter()

# ── Auth & user routers ───────────────────────────────────────────────
# Two routers under /auth:
#   * auth_session_router (auth_service): /login, /logout, /refresh, /me
#     — cookie-based session lifecycle, owned by the extractable auth service.
#   * auth.router (legacy): /signup, /forgot-password, /reset-password,
#     /verify-invite — flows that don't issue session cookies. Will follow
#     into the auth service in a later move.
api_router.include_router(
    auth_session_router, prefix="/auth", tags=["auth"],
)
api_router.include_router(
    auth.router, prefix="/auth", tags=["auth"],
)
api_router.include_router(
    users.router, prefix="/users", tags=["users"],
)
api_router.include_router(
    users.admin_router, prefix="/admin/users", tags=["admin:users"],
)
# RBAC Phase 1: /me/permissions for FE permission hydration.
api_router.include_router(
    me.router, prefix="/me", tags=["me"],
)

# ── Admin routers (workspace-centric) ───────────────────────────────
api_router.include_router(
    providers.router, prefix="/admin/providers", tags=["admin:providers"],
)
api_router.include_router(
    catalog.router, prefix="/admin/catalog", tags=["admin:catalog"],
)
api_router.include_router(
    ontologies.router, prefix="/admin/ontologies", tags=["admin:ontologies"],
)
api_router.include_router(
    workspaces.router, prefix="/admin/workspaces", tags=["admin:workspaces"],
)
api_router.include_router(
    context_models.template_router, prefix="/admin/context-model-templates",
    tags=["admin:context-model-templates"],
)
api_router.include_router(
    features.router, prefix="/admin/features", tags=["admin:features"],
)
api_router.include_router(
    announcements.admin_router, prefix="/admin/announcements", tags=["admin:announcements"],
)

# ── RBAC Phase 2 admin surface ───────────────────────────────────────
# Group CRUD + membership; per-workspace member bindings; per-view
# explicit grants; and the role-binding audit endpoint. All require
# the appropriate RBAC permission via ``requires(...)``.
api_router.include_router(
    groups.router, prefix="/admin/groups", tags=["admin:rbac:groups"],
)
api_router.include_router(
    workspace_members.router,
    prefix="/admin/workspaces/{ws_id}/members",
    tags=["admin:rbac:workspace-members"],
)
api_router.include_router(
    role_bindings.router,
    prefix="/admin/role-bindings",
    tags=["admin:rbac:audit"],
)
# Permissions catalogue + role definitions + per-user access map.
# Backs the Permissions admin page (Role matrix, By-user lens).
api_router.include_router(
    permissions_admin.router,
    prefix="/admin",
    tags=["admin:rbac:permissions"],
)
api_router.include_router(
    view_grants.router,
    prefix="/views/{view_id}/grants",
    tags=["views:grants"],
)

# RBAC Phase 4.3 — self-service access requests.
# Mounted on three different prefixes so the auth gate is naturally
# scoped: any-user submit, any-user "my requests", and admin inbox.
api_router.include_router(
    access_requests.public_router,
    prefix="/access-requests",
    tags=["access-requests"],
)
api_router.include_router(
    access_requests.me_router,
    prefix="/me",
    tags=["me:access-requests"],
)
api_router.include_router(
    access_requests.admin_ws_router,
    prefix="/admin/workspaces/{ws_id}/access-requests",
    tags=["admin:access-requests:inbox"],
)
api_router.include_router(
    access_requests.admin_router,
    prefix="/admin/access-requests",
    tags=["admin:access-requests"],
)

# RBAC Phase 4.5 — unified RBAC search across users, groups,
# workspaces, roles, and permissions. Backs the search bar at the
# top of the Permissions admin surface.
api_router.include_router(
    rbac_search.router,
    prefix="/admin/rbac/search",
    tags=["admin:rbac:search"],
)

# ── Public announcements (no auth — all users see banners) ────────────
api_router.include_router(
    announcements.router, prefix="/announcements", tags=["announcements"],
)
# Aggregation service: /api/v1/admin/...
api_router.include_router(
    aggregation.router, prefix="/admin", tags=["admin:aggregation"],
)
# Stats service: /api/v1/admin/stats-polling
api_router.include_router(
    stats_admin.router, prefix="/admin", tags=["admin:stats"],
)
# Insights service: /api/v1/admin/insights/providers/{id}/assets[/...]
# Cache-only reads for pre-registration discovery.
api_router.include_router(
    insights.router, prefix="/admin/insights", tags=["admin:insights"],
)

# ── Top-level views (first-class, cross-workspace) ─────────────────
api_router.include_router(
    views.router, prefix="/views", tags=["views"],
)

# ── Workspace-scoped data routers ───────────────────────────────────
# Graph endpoints: /api/v1/{ws_id}/graph/trace, /api/v1/{ws_id}/graph/nodes, etc.
# (api_router is already mounted at /api/v1, so prefix is just /{ws_id}/graph)
api_router.include_router(
    graph.router, prefix="/{ws_id}/graph", tags=["graph:workspace"],
)
# User-authored versioned graphs: /api/v1/{ws_id}/graphs/...
# (routes carry the full /{ws_id}/graphs path, so no prefix here).
api_router.include_router(
    authored_graphs.router, tags=["graphs:authored"],
)
# Assignment compute (workspace-scoped)
api_router.include_router(
    assignments.router, prefix="/{ws_id}/graph/assignments", tags=["assignments:workspace"],
)
# Asset endpoints: /api/v1/{ws_id}/assets/rule-sets
api_router.include_router(
    assets.router, prefix="/{ws_id}/assets", tags=["assets:workspace"],
)
# Context models: /api/v1/{ws_id}/context-models
api_router.include_router(
    context_models.router, prefix="/{ws_id}/context-models", tags=["context-models"],
)
