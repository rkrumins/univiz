"""Emit helper for the **Graph Store DB's own** transactional outbox.

Mirrors :func:`backend.app.db.repositories.outbox_event_repo.emit` but
writes :class:`GraphStoreOutboxEventORM` into the *graph-store* session.

Why a separate helper (not the management one): the row must land in
the Graph Store DB so that a graph write + its ``graph_change_event``
audit row + this outbox event all commit in a **single local
transaction**. Nothing ever spans both databases — that is the
structural fix for cross-DB atomicity.

The ``<domain>.<entity>.<verb>`` contract and the domain whitelist are
reused verbatim from the management outbox repo (imported, not copied)
so the two outboxes can never drift. Graph events are emitted under the
already-whitelisted ``visualization`` domain — no ``_VALID_DOMAINS``
change is required.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models_graph import GraphStoreOutboxEventORM

# Reuse the canonical event-type contract + domain whitelist. Importing
# (rather than re-declaring) guarantees the graph-store outbox honours
# exactly the same `<domain>.<entity>.<verb>` rules as the management
# outbox and cannot drift from DOMAIN_OWNERSHIP.md.
from backend.app.db.repositories.outbox_event_repo import (  # noqa: F401
    InvalidEventType,
    _validate_event_type,
)


async def emit(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_id: str,
    aggregate_type: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    event_version: int = 1,
) -> GraphStoreOutboxEventORM:
    """Append a domain event to the Graph Store outbox in the caller's
    open graph-store transaction.

    Args mirror the management ``emit``. ``session`` MUST be a Graph
    Store session (so the event commits atomically with the graph
    write + audit row). ``payload`` is stored as native JSONB (the
    Graph Store is Postgres-only).
    """
    domain = _validate_event_type(event_type)
    if aggregate_type is None:
        parts = event_type.split(".")
        aggregate_type = parts[1] if len(parts) >= 2 else domain

    event = GraphStoreOutboxEventORM(
        event_type=event_type,
        event_version=event_version,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
        processed=False,
    )
    session.add(event)
    return event


__all__ = ["emit", "InvalidEventType"]
