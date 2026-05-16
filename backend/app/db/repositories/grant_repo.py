"""Repository: resource_grants — per-View explicit shares.

Layer 3 of the View access model. A grant adds a subject (user or
group) to a single view at a narrow role (editor or viewer). It is
**additive** — independent of workspace membership.

The grant role enum is intentionally narrower than the global RBAC
role enum. ``editor`` here is *resource-scoped* (can edit this one
view, cannot delete it, gains no other workspace permissions) — see
the design plan for the full action matrix.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, delete, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import ResourceGrantORM


VALID_RESOURCE_TYPES = {"view", "graph"}
VALID_SUBJECT_TYPES = {"user", "group"}
VALID_GRANT_ROLES = {"editor", "viewer"}


def _validate(resource_type: str, subject_type: str, role_name: str) -> None:
    if resource_type not in VALID_RESOURCE_TYPES:
        raise ValueError(f"resource_type must be one of {VALID_RESOURCE_TYPES}")
    if subject_type not in VALID_SUBJECT_TYPES:
        raise ValueError(f"subject_type must be one of {VALID_SUBJECT_TYPES}")
    if role_name not in VALID_GRANT_ROLES:
        raise ValueError(f"role_name must be one of {VALID_GRANT_ROLES}")


async def create_grant(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    subject_type: str,
    subject_id: str,
    role_name: str,
    granted_by: Optional[str] = None,
) -> ResourceGrantORM:
    _validate(resource_type, subject_type, role_name)
    grant = ResourceGrantORM(
        resource_type=resource_type,
        resource_id=resource_id,
        subject_type=subject_type,
        subject_id=subject_id,
        role_name=role_name,
        granted_by=granted_by,
    )
    session.add(grant)
    await session.flush()
    return grant


async def delete_grant(session: AsyncSession, grant_id: str) -> bool:
    result = await session.execute(
        delete(ResourceGrantORM).where(ResourceGrantORM.id == grant_id)
    )
    return (result.rowcount or 0) > 0


async def list_grants_for_resource(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
) -> list[ResourceGrantORM]:
    result = await session.execute(
        select(ResourceGrantORM).where(
            ResourceGrantORM.resource_type == resource_type,
            ResourceGrantORM.resource_id == resource_id,
        )
    )
    return list(result.scalars().all())


async def list_grants_for_user_with_groups(
    session: AsyncSession,
    *,
    user_id: str,
    group_ids: list[str],
    resource_type: Optional[str] = None,
) -> list[ResourceGrantORM]:
    """Every grant the user inherits — direct or via group membership."""
    direct = and_(
        ResourceGrantORM.subject_type == "user",
        ResourceGrantORM.subject_id == user_id,
    )
    if group_ids:
        indirect = and_(
            ResourceGrantORM.subject_type == "group",
            ResourceGrantORM.subject_id.in_(group_ids),
        )
        where = or_(direct, indirect)
    else:
        where = direct
    stmt = select(ResourceGrantORM).where(where)
    if resource_type is not None:
        stmt = stmt.where(ResourceGrantORM.resource_type == resource_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_grant(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
    subject_type: str,
    subject_id: str,
) -> Optional[ResourceGrantORM]:
    """Lookup by the unique (resource × subject) key — for idempotent shares."""
    result = await session.execute(
        select(ResourceGrantORM).where(
            ResourceGrantORM.resource_type == resource_type,
            ResourceGrantORM.resource_id == resource_id,
            ResourceGrantORM.subject_type == subject_type,
            ResourceGrantORM.subject_id == subject_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_resource_grants(
    session: AsyncSession,
    *,
    resource_type: str,
    resource_id: str,
) -> int:
    """Cleanup when a resource is deleted."""
    result = await session.execute(
        delete(ResourceGrantORM).where(
            ResourceGrantORM.resource_type == resource_type,
            ResourceGrantORM.resource_id == resource_id,
        )
    )
    return result.rowcount or 0


async def delete_subject_grants(
    session: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
) -> int:
    """Cleanup when a user or group is deleted."""
    result = await session.execute(
        delete(ResourceGrantORM).where(
            ResourceGrantORM.subject_type == subject_type,
            ResourceGrantORM.subject_id == subject_id,
        )
    )
    return result.rowcount or 0
