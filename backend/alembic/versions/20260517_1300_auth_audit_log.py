"""Create ``auth_audit_log`` (append-only audit trail).

Revision ID: 20260517_1300_auth_audit_log
Revises: 20260517_1200_user_sso_unique
Create Date: 2026-05-17 13:00

Phase 0 of the enterprise-IdP roadmap. The transactional outbox
(``outbox_events``) has had no consumer — events accumulate with
``processed = false`` forever. This lands the immutable sink the
outbox relay drains into: one append-only row per domain event,
keyed by the source outbox event id (UNIQUE) so a relay crash/retry
cannot double-record.

No backfill: the relay records events going forward. Idempotent —
table only created when absent.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260517_1300_auth_audit_log"
down_revision: Union[str, None] = "20260517_1200_user_sso_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = "auth_audit_log"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=True),
        sa.Column("aggregate_id", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "source_event_id", name="uq_auth_audit_source_event"
        ),
    )
    op.create_index(
        "idx_auth_audit_event_type", _TABLE, ["event_type"]
    )
    op.create_index(
        "idx_auth_audit_recorded_at", _TABLE, ["recorded_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("idx_auth_audit_recorded_at", table_name=_TABLE)
    op.drop_index("idx_auth_audit_event_type", table_name=_TABLE)
    op.drop_table(_TABLE)
