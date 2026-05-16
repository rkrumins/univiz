"""Repository: role_bindings — the central binding table.

A binding ties a Subject (user|group) to a Role within a Scope
(global|workspace). Bindings are the single source of truth for RBAC;
JWT claims are derived from them at login time by the
``PermissionResolver``.

Subject and scope identifiers are polymorphic strings — referential
integrity to users / groups / workspaces is enforced by the on-delete
handlers in those tables, plus the consistency check constraint on
(scope_type, scope_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, delete, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import RoleBindingORM


def _is_expired(expires_at: Optional[str], *, now: datetime) -> bool:
    """True if a time-bound binding has lapsed.

    ``expires_at`` is a nullable ISO-8601 string (NULL = never expires).
    Parsed defensively: a value we can't parse is treated as
    non-expiring so a malformed timestamp can never silently revoke a
    legitimate grant — the alternative (fail-closed on parse error)
    would lock users out on a bad write, which is worse than the
    status quo where expiry wasn't enforced at all.
    """
    if not expires_at:
        return False
    raw = expires_at.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now


VALID_SUBJECT_TYPES = {"user", "group"}
VALID_SCOPE_TYPES = {"global", "workspace"}
# Phase-1 hardcoded role names. Kept for backward-compat with tests
# that still reference the constant; the actual enum is the canonical
# ``roles`` table (Phase 3 migration). Endpoint-layer validation should
# go through ``role_repo.get_role`` / ``role_is_bindable_in_scope``.
VALID_ROLE_NAMES_PHASE_1 = {"admin", "user", "viewer"}


def _validate(
    subject_type: str, scope_type: str, scope_id: Optional[str], role_name: str
) -> None:
    """Surface argument-shape errors before the DB rejects them.

    Phase 3 dropped the role-name CHECK constraint — the canonical
    ``roles`` table is the source of truth and endpoints validate via
    ``role_repo``. We retain the subject + scope shape checks because
    those still have DB-level CHECK constraints and ``IntegrityError``
    is a worse caller experience than ``ValueError``.
    """
    if subject_type not in VALID_SUBJECT_TYPES:
        raise ValueError(f"subject_type must be one of {VALID_SUBJECT_TYPES}, got {subject_type!r}")
    if scope_type not in VALID_SCOPE_TYPES:
        raise ValueError(f"scope_type must be one of {VALID_SCOPE_TYPES}, got {scope_type!r}")
    if scope_type == "global" and scope_id is not None:
        raise ValueError("scope_id must be NULL when scope_type='global'")
    if scope_type == "workspace" and scope_id is None:
        raise ValueError("scope_id is required when scope_type='workspace'")
    if not role_name or not isinstance(role_name, str):
        raise ValueError("role_name must be a non-empty string")


async def create_binding(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
    role_name: str,
    scope_type: str,
    scope_id: Optional[str] = None,
    granted_by: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> RoleBindingORM:
    _validate(subject_type, scope_type, scope_id, role_name)
    binding = RoleBindingORM(
        subject_type=subject_type,
        subject_id=subject_id,
        role_name=role_name,
        scope_type=scope_type,
        scope_id=scope_id,
        granted_by=granted_by,
        expires_at=expires_at,
    )
    session.add(binding)
    await session.flush()
    return binding


async def delete_binding(session: AsyncSession, binding_id: str) -> bool:
    result = await session.execute(
        delete(RoleBindingORM).where(RoleBindingORM.id == binding_id)
    )
    return (result.rowcount or 0) > 0


async def get_binding(session: AsyncSession, binding_id: str) -> Optional[RoleBindingORM]:
    result = await session.execute(
        select(RoleBindingORM).where(RoleBindingORM.id == binding_id)
    )
    return result.scalar_one_or_none()


async def list_for_subject(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
) -> list[RoleBindingORM]:
    """Bindings directly attached to one subject. Does NOT expand groups."""
    result = await session.execute(
        select(RoleBindingORM).where(
            RoleBindingORM.subject_type == subject_type,
            RoleBindingORM.subject_id == subject_id,
        )
    )
    return list(result.scalars().all())


async def list_for_user_with_groups(
    session: AsyncSession,
    *,
    user_id: str,
    group_ids: list[str],
) -> list[RoleBindingORM]:
    """Hot-path resolver query.

    Returns every binding that grants permissions to ``user_id`` —
    direct (subject_type='user', subject_id=user_id) plus indirect via
    group membership. Run inside ``PermissionResolver.resolve``.
    """
    direct = and_(
        RoleBindingORM.subject_type == "user",
        RoleBindingORM.subject_id == user_id,
    )
    if group_ids:
        indirect = and_(
            RoleBindingORM.subject_type == "group",
            RoleBindingORM.subject_id.in_(group_ids),
        )
        where = or_(direct, indirect)
    else:
        where = direct
    result = await session.execute(select(RoleBindingORM).where(where))
    now = datetime.now(timezone.utc)
    return [
        b for b in result.scalars().all()
        if not _is_expired(b.expires_at, now=now)
    ]


async def list_for_scope(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: Optional[str] = None,
) -> list[RoleBindingORM]:
    """Reverse lookup: who has access to this workspace?"""
    if scope_type == "global":
        stmt = select(RoleBindingORM).where(
            RoleBindingORM.scope_type == "global",
            RoleBindingORM.scope_id.is_(None),
        )
    else:
        stmt = select(RoleBindingORM).where(
            RoleBindingORM.scope_type == scope_type,
            RoleBindingORM.scope_id == scope_id,
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_binding(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
    role_name: str,
    scope_type: str,
    scope_id: Optional[str] = None,
) -> Optional[RoleBindingORM]:
    """Lookup by the unique key — useful for idempotent grants."""
    _validate(subject_type, scope_type, scope_id, role_name)
    stmt = select(RoleBindingORM).where(
        RoleBindingORM.subject_type == subject_type,
        RoleBindingORM.subject_id == subject_id,
        RoleBindingORM.role_name == role_name,
        RoleBindingORM.scope_type == scope_type,
    )
    if scope_id is None:
        stmt = stmt.where(RoleBindingORM.scope_id.is_(None))
    else:
        stmt = stmt.where(RoleBindingORM.scope_id == scope_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def delete_subject_bindings(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
) -> int:
    """Cascade-style cleanup. Used when a user or group is deleted."""
    result = await session.execute(
        delete(RoleBindingORM).where(
            RoleBindingORM.subject_type == subject_type,
            RoleBindingORM.subject_id == subject_id,
        )
    )
    return result.rowcount or 0


async def delete_scope_bindings(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
) -> int:
    """Used when a workspace is deleted — drops every binding to it."""
    result = await session.execute(
        delete(RoleBindingORM).where(
            RoleBindingORM.scope_type == scope_type,
            RoleBindingORM.scope_id == scope_id,
        )
    )
    return result.rowcount or 0
