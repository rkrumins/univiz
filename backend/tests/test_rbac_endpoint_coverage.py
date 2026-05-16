"""RBAC enforcement coverage for the routers hardened in the
backend security pass.

These routers were previously mounted with **no auth dependency at
all** (they depended only on ``get_db_session``). The fix wires
router-level ``requires(...)`` dependencies in
``backend/app/api/v1/api.py``:

  * Global-admin routers (providers, catalog, ontologies, features,
    context-model-templates, aggregation, insights) → ``system:admin``.
  * Workspace-scoped data routers (graph, assignments, assets,
    context-models) → ``workspace:datasource:read`` baseline, with the
    mutating routes additionally requiring
    ``workspace:datasource:manage``.

The tests assert the *gate is actually wired* by driving the real
``backend.app.main`` app through the conftest ``test_client`` and
overriding the auth dependencies per-test:

  * no authenticated user            → 401
  * authenticated but missing perm   → 403
  * sufficient permission            → NOT 401/403 (handler may still
    200/404/422 — we only care that the auth gate let it through)

We deliberately avoid asserting an exact success status for endpoints
whose handlers need extra services (aggregation, insights); for those
we only assert the deny paths, which is what proves enforcement.
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi import HTTPException, status
from httpx import AsyncClient

from backend.app.auth.dependencies import (
    get_current_user,
    get_permission_claims,
)
from backend.app.services.permission_service import PermissionClaims
from backend.auth_service.interface import User


_NON_ADMIN = User(
    id="usr_nonadmin",
    email="nonadmin@example.com",
    first_name="Non",
    last_name="Admin",
    role="user",
    status="active",
    auth_provider="local",
    created_at="2024-01-01T00:00:00Z",
    updated_at="2024-01-01T00:00:00Z",
)

_ADMIN_CLAIMS = PermissionClaims(sid="sess_admin", global_perms=("system:admin",))
_EMPTY_CLAIMS = PermissionClaims(sid="sess_none")


@contextlib.contextmanager
def _auth(*, user, claims):
    """Override the auth dependencies on the real app for one request.

    ``user=None`` simulates an unauthenticated caller (``get_current_user``
    raises 401, exactly as it does in production when the cookie is
    missing). The conftest ``test_client`` fixture clears all overrides
    after the test, so we only need to set them here.
    """
    from backend.app.main import app

    async def _ovr_user():
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return user

    def _ovr_claims():
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )
        return claims

    prev_user = app.dependency_overrides.get(get_current_user)
    prev_claims = app.dependency_overrides.get(get_permission_claims)
    app.dependency_overrides[get_current_user] = _ovr_user
    app.dependency_overrides[get_permission_claims] = _ovr_claims
    try:
        yield
    finally:
        app.dependency_overrides[get_current_user] = prev_user
        app.dependency_overrides[get_permission_claims] = prev_claims


# Representative GET endpoint per newly-gated global-admin router.
# These handlers only need the DB session, so with admin claims they
# resolve cleanly (empty list / config) — safe to assert the allow path.
_GLOBAL_ADMIN_GET = [
    "/api/v1/admin/ontologies",
    "/api/v1/admin/providers",
    "/api/v1/admin/catalog",
    "/api/v1/admin/features",
    "/api/v1/admin/context-model-templates",
]

# Routers whose handlers need extra services — assert deny paths only.
_GLOBAL_ADMIN_DENY_ONLY = [
    "/api/v1/admin/aggregation-jobs/summary",
    "/api/v1/admin/insights/providers/p1/assets",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _GLOBAL_ADMIN_GET + _GLOBAL_ADMIN_DENY_ONLY)
async def test_global_admin_router_rejects_unauthenticated(
    test_client: AsyncClient, path: str
):
    with _auth(user=None, claims=None):
        r = await test_client.get(path)
    assert r.status_code == 401, (path, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _GLOBAL_ADMIN_GET + _GLOBAL_ADMIN_DENY_ONLY)
async def test_global_admin_router_rejects_non_admin(
    test_client: AsyncClient, path: str
):
    with _auth(user=_NON_ADMIN, claims=_EMPTY_CLAIMS):
        r = await test_client.get(path)
    assert r.status_code == 403, (path, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _GLOBAL_ADMIN_GET)
async def test_global_admin_router_allows_system_admin(
    test_client: AsyncClient, path: str
):
    with _auth(user=_NON_ADMIN, claims=_ADMIN_CLAIMS):
        r = await test_client.get(path)
    assert r.status_code not in (401, 403), (path, r.status_code)


# ── workspace-scoped data routers ────────────────────────────────────

_WS_READ_GET = [
    "/api/v1/ws_a/context-models",
    "/api/v1/ws_a/assets/rule-sets",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _WS_READ_GET)
async def test_ws_router_rejects_unauthenticated(
    test_client: AsyncClient, path: str
):
    with _auth(user=None, claims=None):
        r = await test_client.get(path)
    assert r.status_code == 401, (path, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _WS_READ_GET)
async def test_ws_router_rejects_member_without_workspace_perm(
    test_client: AsyncClient, path: str
):
    with _auth(user=_NON_ADMIN, claims=_EMPTY_CLAIMS):
        r = await test_client.get(path)
    assert r.status_code == 403, (path, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _WS_READ_GET)
async def test_ws_router_allows_workspace_reader(
    test_client: AsyncClient, path: str
):
    claims = PermissionClaims(
        sid="sess_reader",
        ws_perms={"ws_a": ("workspace:datasource:read",)},
    )
    with _auth(user=_NON_ADMIN, claims=claims):
        r = await test_client.get(path)
    assert r.status_code not in (401, 403), (path, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _WS_READ_GET)
async def test_ws_router_enforces_workspace_isolation(
    test_client: AsyncClient, path: str
):
    """Read access to ws_b must NOT grant access to ws_a."""
    claims = PermissionClaims(
        sid="sess_other_ws",
        ws_perms={"ws_b": ("workspace:datasource:*",)},
    )
    with _auth(user=_NON_ADMIN, claims=claims):
        r = await test_client.get(path)
    assert r.status_code == 403, (path, r.status_code)


# ── workspace mutation gate (read baseline vs manage) ────────────────


@pytest.mark.asyncio
async def test_ws_mutation_forbidden_for_read_only_member(
    test_client: AsyncClient,
):
    """A workspace member with only ``workspace:datasource:read`` passes
    the router-level read gate but must be blocked from a mutating
    route by the per-endpoint ``workspace:datasource:manage`` gate."""
    claims = PermissionClaims(
        sid="sess_reader",
        ws_perms={"ws_a": ("workspace:datasource:read",)},
    )
    with _auth(user=_NON_ADMIN, claims=claims):
        r = await test_client.delete("/api/v1/ws_a/context-models/cm_does_not_exist")
    assert r.status_code == 403, r.status_code


@pytest.mark.asyncio
async def test_ws_mutation_allowed_for_manager(test_client: AsyncClient):
    """``workspace:datasource:*`` (the wildcard production collapses the
    user/admin role into) satisfies both the read baseline and the
    manage gate, so the auth layer lets the request through (the row
    doesn't exist → handler 404, which is fine — not 401/403)."""
    claims = PermissionClaims(
        sid="sess_mgr",
        ws_perms={"ws_a": ("workspace:datasource:*",)},
    )
    with _auth(user=_NON_ADMIN, claims=claims):
        r = await test_client.delete("/api/v1/ws_a/context-models/cm_does_not_exist")
    assert r.status_code not in (401, 403), r.status_code


@pytest.mark.asyncio
async def test_ws_mutation_forbidden_unauthenticated(test_client: AsyncClient):
    with _auth(user=None, claims=None):
        r = await test_client.delete("/api/v1/ws_a/context-models/cm_x")
    assert r.status_code == 401, r.status_code
