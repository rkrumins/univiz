"""Phase 0 — outbox relay drains ``outbox_events`` into the
append-only ``auth_audit_log``.

Uses the per-test ``db_session`` (SQLite, all tables created from the
ORM) so the relay logic is exercised without a live Postgres.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.db.models import AuthAuditLogORM, OutboxEventORM
from backend.app.db.repositories import user_repo
from backend.app.services.outbox_relay import drain_once


@pytest.mark.asyncio
async def test_drain_records_and_marks_processed(db_session):
    await user_repo.create_outbox_event(
        db_session,
        event_type="user.logged_in",
        payload={"user_id": "usr_1", "email": "a@b.com"},
    )

    recorded = await drain_once(db_session)
    assert recorded == 1

    audit = (
        await db_session.execute(select(AuthAuditLogORM))
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].event_type == "user.logged_in"
    assert json.loads(audit[0].payload)["user_id"] == "usr_1"
    assert audit[0].occurred_at  # carried from the source event

    ev = (await db_session.execute(select(OutboxEventORM))).scalars().one()
    assert ev.processed is True

    # Re-draining is a no-op — the processed flag filters it out.
    assert await drain_once(db_session) == 0


@pytest.mark.asyncio
async def test_drain_is_idempotent_on_source_event_id(db_session):
    """A crash between the audit insert and the processed commit leaves
    the event unprocessed; the next drain must not double-record."""
    ev = await user_repo.create_outbox_event(
        db_session, event_type="user.logged_out", payload={"user_id": "usr_2"},
    )
    assert await drain_once(db_session) == 1

    # Simulate the crash: processed never committed.
    ev.processed = False
    await db_session.flush()

    assert await drain_once(db_session) == 0  # already audited
    audit = (
        await db_session.execute(
            select(AuthAuditLogORM).where(
                AuthAuditLogORM.source_event_id == ev.id
            )
        )
    ).scalars().all()
    assert len(audit) == 1
    ev_after = (
        await db_session.execute(select(OutboxEventORM))
    ).scalars().one()
    assert ev_after.processed is True


@pytest.mark.asyncio
async def test_drain_empty_outbox_returns_zero(db_session):
    assert await drain_once(db_session) == 0
