"""Unit tests for ``backend.app.services.permission_service``.

Covers the resolver (DB-backed), wildcard collapsing (pure), and
``has_permission`` (pure) — the three pieces that together gate every
RBAC enforcement in Phase 2.
"""
from __future__ import annotations

import pytest

from backend.app.db.repositories import binding_repo, group_repo
from backend.app.db.models import (
    GroupMemberORM,
    PermissionORM,
    RolePermissionORM,
    UserORM,
)
from backend.app.services.permission_service import (
    PermissionClaims,
    has_permission,
    new_session_id,
    resolve,
    _collapse_wildcards,
)


# ── helpers ──────────────────────────────────────────────────────────

async def _seed_full_catalogue(db_session) -> None:
    """Insert the same permission catalogue the migration would produce."""
    catalogue = [
        ("system:admin", "system"),
        ("users:manage", "system"),
        ("groups:manage", "system"),
        ("workspaces:create", "system"),
        ("workspace:admin", "workspace"),
        ("workspace:datasource:manage", "workspace"),
        ("workspace:datasource:read", "workspace"),
        ("workspace:view:create", "workspace"),
        ("workspace:view:edit", "workspace"),
        ("workspace:view:delete", "workspace"),
        ("workspace:view:read", "workspace"),
    ]
    for pid, cat in catalogue:
        db_session.add(PermissionORM(id=pid, description=pid, category=cat))

    admin_perms = [c[0] for c in catalogue]
    user_perms = [
        "workspace:datasource:manage", "workspace:datasource:read",
        "workspace:view:create", "workspace:view:edit",
        "workspace:view:delete", "workspace:view:read",
    ]
    viewer_perms = ["workspace:datasource:read", "workspace:view:read"]

    for r, perms in (("admin", admin_perms), ("user", user_perms), ("viewer", viewer_perms)):
        for p in perms:
            db_session.add(RolePermissionORM(role_name=r, permission_id=p))
    await db_session.flush()


async def _seed_user(db_session, user_id="usr_alice") -> str:
    db_session.add(UserORM(
        id=user_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        first_name="A",
        last_name="L",
        status="active",
        auth_provider="local",
    ))
    await db_session.flush()
    return user_id


# ── pure helpers ─────────────────────────────────────────────────────

def test_collapse_wildcards_full_view_set():
    full = {
        "workspace:view:create",
        "workspace:view:edit",
        "workspace:view:delete",
        "workspace:view:read",
    }
    assert _collapse_wildcards(full) == ("workspace:view:*",)


def test_collapse_wildcards_partial_set_left_intact():
    partial = {"workspace:view:read"}
    assert _collapse_wildcards(partial) == ("workspace:view:read",)


def test_collapse_wildcards_mixed_domain():
    perms = {
        "workspace:view:create", "workspace:view:edit",
        "workspace:view:delete", "workspace:view:read",
        "workspace:datasource:read",  # only one of two — not collapsed
    }
    out = _collapse_wildcards(perms)
    assert "workspace:view:*" in out
    assert "workspace:datasource:read" in out
    assert "workspace:datasource:*" not in out


def test_has_permission_global_and_workspace():
    claims = PermissionClaims(
        sid="sess_x",
        global_perms=("workspaces:create",),
        ws_perms={"ws_a": ("workspace:view:read",)},
    )
    assert has_permission(claims, "workspaces:create")
    assert has_permission(claims, "workspace:view:read", workspace_id="ws_a")
    assert not has_permission(claims, "workspace:view:read", workspace_id="ws_b")
    assert not has_permission(claims, "users:manage")


def test_has_permission_wildcard_expansion():
    claims = PermissionClaims(
        sid="sess_x",
        ws_perms={"ws_a": ("workspace:view:*",)},
    )
    assert has_permission(claims, "workspace:view:edit", workspace_id="ws_a")
    assert has_permission(claims, "workspace:view:read", workspace_id="ws_a")
    # outside the wildcard prefix
    assert not has_permission(claims, "workspace:admin", workspace_id="ws_a")


def test_has_permission_global_admin_is_implicit_allow():
    claims = PermissionClaims(sid="sess_a", global_perms=("system:admin",))
    assert has_permission(claims, "workspace:view:delete", workspace_id="ws_anywhere")
    assert has_permission(claims, "users:manage")


def test_jwt_round_trip_preserves_shape():
    claims = PermissionClaims(
        sid="sess_z",
        global_perms=("workspaces:create",),
        ws_perms={"ws_x": ("workspace:view:read",)},
    )
    restored = PermissionClaims.from_jwt_dict(claims.to_jwt_dict())
    assert restored == claims


def test_new_session_id_prefixed_and_unique():
    a = new_session_id()
    b = new_session_id()
    assert a.startswith("sess_")
    assert a != b


# ── DB-backed resolver ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_user_with_no_bindings_returns_empty_claims(db_session):
    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)

    claims = await resolve(db_session, user_id, sid="sess_test")
    assert claims.sid == "sess_test"
    assert claims.global_perms == ()
    assert claims.ws_perms == {}


@pytest.mark.asyncio
async def test_resolve_user_with_global_admin(db_session):
    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="admin", scope_type="global",
    )
    claims = await resolve(db_session, user_id)
    assert "system:admin" in claims.global_perms
    assert "users:manage" in claims.global_perms
    # No workspace bindings → no ws claims (admin is implied at check time)
    assert claims.ws_perms == {}


@pytest.mark.asyncio
async def test_resolve_user_with_workspace_user_role(db_session):
    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="user", scope_type="workspace", scope_id="ws_a",
    )

    claims = await resolve(db_session, user_id)
    assert "ws_a" in claims.ws_perms
    perms = set(claims.ws_perms["ws_a"])
    # 'user' role gets the full view + datasource sets → wildcards collapse
    assert "workspace:view:*" in perms
    assert "workspace:datasource:*" in perms


@pytest.mark.asyncio
async def test_resolve_unions_direct_and_group_bindings(db_session):
    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    g = await group_repo.create_group(db_session, name="finance")
    db_session.add(GroupMemberORM(group_id=g.id, user_id=user_id))
    await db_session.flush()

    # Direct: viewer in ws_a.
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="viewer", scope_type="workspace", scope_id="ws_a",
    )
    # Indirect via group: user in ws_b.
    await binding_repo.create_binding(
        db_session,
        subject_type="group", subject_id=g.id,
        role_name="user", scope_type="workspace", scope_id="ws_b",
    )

    claims = await resolve(db_session, user_id)
    assert "ws_a" in claims.ws_perms
    assert "ws_b" in claims.ws_perms
    assert "workspace:view:read" in claims.ws_perms["ws_a"]
    # ws_b 'user' bundle collapses to wildcard
    assert "workspace:view:*" in claims.ws_perms["ws_b"]


@pytest.mark.asyncio
async def test_resolve_overlapping_roles_in_same_workspace_take_union(db_session):
    """A user bound directly AND via a group in the same workspace should
    end up with the union of both role permission sets."""
    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    g = await group_repo.create_group(db_session, name="ops")
    db_session.add(GroupMemberORM(group_id=g.id, user_id=user_id))
    await db_session.flush()

    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="viewer", scope_type="workspace", scope_id="ws_x",
    )
    await binding_repo.create_binding(
        db_session,
        subject_type="group", subject_id=g.id,
        role_name="user", scope_type="workspace", scope_id="ws_x",
    )

    claims = await resolve(db_session, user_id)
    perms = set(claims.ws_perms["ws_x"])
    # Wildcards from the 'user' role should be present (union folded in)
    assert "workspace:view:*" in perms or {
        "workspace:view:create", "workspace:view:edit",
        "workspace:view:delete", "workspace:view:read",
    } <= perms


# ── time-bound bindings (expires_at enforcement) ─────────────────────


def _iso(dt) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_resolve_excludes_expired_binding(db_session):
    """A binding whose ``expires_at`` is in the past must not grant
    any permissions through ``resolve``."""
    from datetime import datetime, timedelta, timezone

    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    past = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="user", scope_type="workspace", scope_id="ws_a",
        expires_at=past,
    )

    claims = await resolve(db_session, user_id)
    assert claims.ws_perms == {}
    assert not has_permission(
        claims, "workspace:view:read", workspace_id="ws_a"
    )


@pytest.mark.asyncio
async def test_resolve_includes_unexpired_and_null_expiry_bindings(db_session):
    """A future ``expires_at`` and a NULL ``expires_at`` both still
    grant permissions."""
    from datetime import datetime, timedelta, timezone

    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    future = _iso(datetime.now(timezone.utc) + timedelta(days=1))

    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="viewer", scope_type="workspace", scope_id="ws_future",
        expires_at=future,
    )
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="viewer", scope_type="workspace", scope_id="ws_forever",
        expires_at=None,
    )

    claims = await resolve(db_session, user_id)
    assert has_permission(
        claims, "workspace:view:read", workspace_id="ws_future"
    )
    assert has_permission(
        claims, "workspace:view:read", workspace_id="ws_forever"
    )


@pytest.mark.asyncio
async def test_resolve_expired_global_admin_loses_implicit_allow(db_session):
    """Regression: an expired global-admin binding must not keep
    granting the system:admin implicit-allow."""
    from datetime import datetime, timedelta, timezone

    await _seed_full_catalogue(db_session)
    user_id = await _seed_user(db_session)
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=1))
    await binding_repo.create_binding(
        db_session,
        subject_type="user", subject_id=user_id,
        role_name="admin", scope_type="global",
        expires_at=past,
    )

    claims = await resolve(db_session, user_id)
    assert "system:admin" not in claims.global_perms
    assert not has_permission(claims, "users:manage")
