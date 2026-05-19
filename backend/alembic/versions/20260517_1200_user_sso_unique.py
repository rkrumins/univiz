"""Add UNIQUE(auth_provider, external_id) to ``users``.

Revision ID: 20260517_1200_user_sso_unique
Revises: 20260512_1100_jobs_current_phase
Create Date: 2026-05-17 12:00

Phase 0 of the enterprise-IdP roadmap. SSO/JIT provisioning must
find-or-provision a user by ``(auth_provider, external_id)`` — never by
email (email is mutable and reassignable). A composite UNIQUE makes the
provider subject the durable identity key and stops duplicate SSO rows
under a race.

NULLs are distinct under a UNIQUE constraint in both Postgres and
SQLite, so existing local accounts (``auth_provider='local'``,
``external_id IS NULL``) are unaffected — no backfill, no collision.

Idempotent: the constraint is only added when absent so partial reruns
and fresh deploys (where 0001_baseline / create_all already added it)
are safe.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260517_1200_user_sso_unique"
down_revision: Union[str, None] = "20260512_1100_jobs_current_phase"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "users"
_CONSTRAINT = "uq_users_provider_external_id"


def _has_constraint(inspector) -> bool:
    names = {uc["name"] for uc in inspector.get_unique_constraints(_TABLE)}
    return _CONSTRAINT in names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    if _has_constraint(inspector):
        return
    op.create_unique_constraint(
        _CONSTRAINT, _TABLE, ["auth_provider", "external_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    if not _has_constraint(inspector):
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
