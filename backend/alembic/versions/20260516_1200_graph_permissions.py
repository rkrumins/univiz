"""Seed workspace:graph:* permissions + role bundles.

Revision ID: 20260516_1200_graph_permissions
Revises: 20260512_1100_jobs_current_phase
Create Date: 2026-05-16

Adds the RBAC permission catalogue entries for user-authored versioned
graphs and bundles them into the built-in roles. Idempotent via
ON CONFLICT (mirrors 20260430_1200_rbac_schema._seed_permissions), so
re-running is safe.

The matching wildcard prefix + seed leaves live in
backend/app/services/permission_service.py and ship in the same change
(the _SEED_LEAVES docstring there requires code+migration co-delivery).

This is a *management*-DB migration: RBAC lives in the management DB.
The graph CONTENT lives in the decoupled Graph Store DB, but
authorization for it is evaluated by the existing
``requires("workspace:graph:...", workspace=...)`` dependency, so the
permission rows belong here next to the other workspace:* permissions.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260516_1200_graph_permissions"
down_revision: Union[str, None] = "20260512_1100_jobs_current_phase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (id, description, category) — category "workspace" matches the
# existing workspace-scoped permissions.
_PERMISSIONS: list[tuple[str, str, str]] = [
    ("workspace:graph:create", "Create user-authored graphs in a workspace.",      "workspace"),
    ("workspace:graph:read",   "List and open authored graphs / their history.",   "workspace"),
    ("workspace:graph:edit",   "Edit nodes/edges in an authored graph working set.", "workspace"),
    ("workspace:graph:delete", "Soft-delete an authored graph.",                    "workspace"),
    ("workspace:graph:commit", "Commit working-set changes to a branch.",           "workspace"),
    ("workspace:graph:branch", "Create / delete branches and tags.",                "workspace"),
    ("workspace:graph:merge",  "Merge branches (incl. conflict resolution).",       "workspace"),
]

_ALL = [p[0] for p in _PERMISSIONS]
# user = full authoring; viewer = read-only. admin gets everything.
_USER_PERMS = [
    "workspace:graph:create",
    "workspace:graph:read",
    "workspace:graph:edit",
    "workspace:graph:delete",
    "workspace:graph:commit",
    "workspace:graph:branch",
    "workspace:graph:merge",
]
_VIEWER_PERMS = ["workspace:graph:read"]

_ROLE_PERMISSIONS: list[tuple[str, str]] = (
    [("admin", p) for p in _ALL]
    + [("user", p) for p in _USER_PERMS]
    + [("viewer", p) for p in _VIEWER_PERMS]
)


def _has_table(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()
    # RBAC tables are created by 20260430_1200_rbac_schema. Guard so
    # this migration is a no-op on a DB where RBAC was never applied
    # (defensive — the revision chain guarantees ordering).
    if not (_has_table(bind, "permissions") and _has_table(bind, "role_permissions")):
        return

    for pid, pdesc, pcat in _PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, description, category) "
                "VALUES (:id, :description, :category) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": pid, "description": pdesc, "category": pcat},
        )
    for role_name, permission_id in _ROLE_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_name, permission_id) "
                "VALUES (:role_name, :permission_id) "
                "ON CONFLICT (role_name, permission_id) DO NOTHING"
            ),
            {"role_name": role_name, "permission_id": permission_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not (_has_table(bind, "permissions") and _has_table(bind, "role_permissions")):
        return
    ids = tuple(_ALL)
    bind.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": list(ids)},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": list(ids)},
    )
