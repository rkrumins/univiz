"""PermissionService — resolve a user's effective permissions.

The resolver runs once per login and produces a compact claim payload
that is embedded in the access JWT. Every subsequent request
authorizes against those claims instead of going back to the DB.

The shape returned matches the JWT claim schema agreed in the design:

    {
        "sid": "<session id>",
        "global": ["workspaces:create", "users:manage", ...],
        "ws": {
            "ws_finance":   ["workspace:admin", "workspace:view:*", ...],
            "ws_marketing": ["workspace:view:read", ...]
        }
    }

Wildcards (e.g. ``workspace:view:*``) are expanded by the resolver
when every action under a domain is granted, keeping the token small
for users in many workspaces.

This module deliberately exposes a single function — ``resolve`` — so
the call site (``LocalIdentityService.login``) is one line. Internal
helpers stay private.

Phase 1: this module is imported by the auth service to populate the
JWT claim. The ``requires(...)`` dependency reads the claim back. The
actual three-layer view evaluator lives in
``view_access.py`` and is wired into endpoints in Phase 2.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.repositories import (
    binding_repo,
    permission_repo,
    user_repo,
)


# Permission ids that the wildcard collapser knows about. Any permission
# whose id starts with one of these prefixes is a candidate for ``*``
# expansion when every leaf under the prefix is granted.
_WILDCARD_PREFIXES = (
    "workspace:view",
    "workspace:datasource",
    "workspace:graph",
)


@dataclass(frozen=True)
class PermissionClaims:
    """The permission claim shape embedded in the access JWT.

    Frozen so the resolver caller cannot accidentally mutate the
    structure between resolution and serialization.
    """
    sid: str                                 # session id (random, used for revocation)
    global_perms: tuple[str, ...] = ()
    ws_perms: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def to_jwt_dict(self) -> dict:
        """Serialize to the JWT claim layout. Field names must stay
        stable — they are a wire contract with the FastAPI dependency."""
        return {
            "sid": self.sid,
            "global": list(self.global_perms),
            "ws": {ws: list(perms) for ws, perms in self.ws_perms.items()},
        }

    @classmethod
    def from_jwt_dict(cls, payload: dict) -> "PermissionClaims":
        sid = payload.get("sid", "")
        global_perms = tuple(payload.get("global", ()) or ())
        raw_ws = payload.get("ws", {}) or {}
        ws_perms = {ws: tuple(perms) for ws, perms in raw_ws.items()}
        return cls(sid=sid, global_perms=global_perms, ws_perms=ws_perms)


def new_session_id() -> str:
    """Random per-login session id used as the revocation key."""
    return f"sess_{secrets.token_urlsafe(16)}"


# ── Resolution ────────────────────────────────────────────────────────

async def resolve(
    session: AsyncSession,
    user_id: str,
    *,
    sid: str | None = None,
) -> PermissionClaims:
    """Compute the user's effective permissions across all scopes.

    One call gathers:
      1. the user's group ids
      2. every direct + group binding affecting them
      3. the permission set for every distinct role they are bound to

    Then folds those into a (global, ws_id → permissions) map, with
    wildcard collapsing for compactness.
    """
    sid = sid or new_session_id()

    group_ids = await user_repo.get_groups_for_user(session, user_id)
    bindings = await binding_repo.list_for_user_with_groups(
        session, user_id=user_id, group_ids=group_ids
    )

    # Collect distinct role names so we can fetch role_permissions in
    # one query. This keeps resolve() O(1) DB calls regardless of how
    # many bindings the user has.
    role_names = sorted({b.role_name for b in bindings})
    role_perms = await permission_repo.get_role_permissions_for_roles(
        session, role_names
    )

    # Aggregate into per-scope permission sets.
    global_set: set[str] = set()
    ws_sets: dict[str, set[str]] = {}

    for b in bindings:
        perms_for_role = role_perms.get(b.role_name, [])
        if b.scope_type == "global":
            global_set.update(perms_for_role)
        else:
            ws_id = b.scope_id or ""
            if not ws_id:
                continue
            ws_sets.setdefault(ws_id, set()).update(perms_for_role)

    # Admin shortcut: a global admin binding implies every workspace
    # permission in every workspace, but the workspace bindings already
    # carry that for the membership-backfilled rows. We DO NOT
    # synthesize implicit ws scopes here — the JWT must list workspaces
    # explicitly so the FE knows which workspaces to display. The
    # admin's actual access in a given workspace falls back to the
    # global "system:admin" implicit-allow check inside ``requires()``.

    return PermissionClaims(
        sid=sid,
        global_perms=tuple(sorted(global_set)),
        ws_perms={
            ws: _collapse_wildcards(perms) for ws, perms in ws_sets.items()
        },
    )


# ── Wildcard collapsing ───────────────────────────────────────────────

def _collapse_wildcards(perms: set[str]) -> tuple[str, ...]:
    """If every permission under a known prefix is present, collapse
    them into ``prefix:*``. Reduces token size for users in many
    workspaces. The ``requires()`` dependency expands the wildcard back.
    """
    out: set[str] = set(perms)
    for prefix in _WILDCARD_PREFIXES:
        leaves = {p for p in out if p.startswith(prefix + ":")}
        # We only collapse if there are >= 2 leaves under the prefix
        # AND all of them are present in our seed catalogue. Otherwise
        # the wildcard is misleading.
        all_known_leaves = _known_leaves_for_prefix(prefix)
        if all_known_leaves and leaves >= all_known_leaves:
            out -= leaves
            out.add(prefix + ":*")
    return tuple(sorted(out))


# Built once at import time from the seed catalogue. Hard-coded here
# rather than fetched from the DB because the catalogue is part of the
# code: it's defined in the migration and the Phase 1 plan, and any
# change to it ships in the same commit as a code update.
_SEED_LEAVES: dict[str, frozenset[str]] = {
    "workspace:view": frozenset({
        "workspace:view:create",
        "workspace:view:edit",
        "workspace:view:delete",
        "workspace:view:read",
    }),
    "workspace:datasource": frozenset({
        "workspace:datasource:manage",
        "workspace:datasource:read",
    }),
    "workspace:graph": frozenset({
        "workspace:graph:create",
        "workspace:graph:read",
        "workspace:graph:edit",
        "workspace:graph:delete",
        "workspace:graph:commit",
        "workspace:graph:branch",
        "workspace:graph:merge",
    }),
}


def _known_leaves_for_prefix(prefix: str) -> frozenset[str]:
    return _SEED_LEAVES.get(prefix, frozenset())


# ── Claim-side helpers (used by ``requires(...)``) ────────────────────

def has_permission(
    claims: PermissionClaims,
    permission: str,
    *,
    workspace_id: str | None = None,
) -> bool:
    """Check whether the resolved claims grant ``permission`` in the
    given scope. ``workspace_id`` is required for workspace-scoped
    permissions; pass ``None`` for global ones.

    Wildcard expansion: a claim of ``workspace:view:*`` matches any
    ``workspace:view:<leaf>`` lookup.

    Global-admin shortcut: a global ``system:admin`` claim implies
    every other permission, in every workspace, full stop.
    """
    # System admin shortcut: implies all.
    if "system:admin" in claims.global_perms:
        return True

    if workspace_id is None:
        return permission in claims.global_perms

    bucket = claims.ws_perms.get(workspace_id, ())
    if permission in bucket:
        return True
    # Wildcard match: e.g. permission='workspace:view:edit' against
    # claim 'workspace:view:*'.
    for granted in bucket:
        if granted.endswith(":*"):
            prefix = granted[:-2]
            if permission.startswith(prefix + ":"):
                return True
    return False


async def simulate_for_user(
    session: AsyncSession,
    user_id: str,
    *,
    role_perm_override: dict[str, list[str]] | None = None,
    excluded_binding_id: str | None = None,
    excluded_role_name: str | None = None,
) -> tuple[set[str], dict[str, set[str]]]:
    """Compute hypothetical effective permissions for a user.

    Used by the Phase 4.4 impact-preview endpoints to answer
    questions like "if I drop ``workspace:view:edit`` from the User
    role, what does Alice lose?" without writing to the DB.

    Hooks:

    * ``role_perm_override`` — temporarily replace the permission set
      for one or more roles. Useful for ``preview-update``.
    * ``excluded_binding_id`` — pretend the named binding doesn't
      exist. Used by ``preview-revoke``.
    * ``excluded_role_name`` — pretend every binding to this role
      doesn't exist. Used by ``preview-delete``.

    Returns the same ``(global_perms, ws_perms)`` shape as
    ``resolve`` but as raw sets (no wildcard collapse) so callers can
    diff cleanly.
    """
    group_ids = await user_repo.get_groups_for_user(session, user_id)
    bindings = await binding_repo.list_for_user_with_groups(
        session, user_id=user_id, group_ids=group_ids
    )

    if excluded_binding_id is not None:
        bindings = [b for b in bindings if b.id != excluded_binding_id]
    if excluded_role_name is not None:
        bindings = [b for b in bindings if b.role_name != excluded_role_name]

    role_names = sorted({b.role_name for b in bindings})
    role_perms = await permission_repo.get_role_permissions_for_roles(
        session, role_names
    )
    if role_perm_override:
        for name, perms in role_perm_override.items():
            role_perms[name] = list(perms)

    global_set: set[str] = set()
    ws_sets: dict[str, set[str]] = {}
    for b in bindings:
        perms_for_role = role_perms.get(b.role_name, [])
        if b.scope_type == "global":
            global_set.update(perms_for_role)
        else:
            ws_id = b.scope_id or ""
            if not ws_id:
                continue
            ws_sets.setdefault(ws_id, set()).update(perms_for_role)
    return global_set, ws_sets


__all__ = [
    "PermissionClaims",
    "resolve",
    "new_session_id",
    "has_permission",
    "simulate_for_user",
]
