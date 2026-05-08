"""DB-bound helpers that adapt the pure ``gate.check_resolution`` to a
specific data source.

This module is the single place that loads the assigned ontology and
the cached graph schema stats for a data source, and feeds them into
the gate. Both the trigger endpoint (proxy-mode preflight) and the
``AggregationService`` (direct-mode authoritative gate) call into
``build_resolution_report`` so the two paths cannot drift.

Typed exceptions distinguish the four "no report" cases. Callers map
each to the appropriate HTTP status / domain error.
"""
from __future__ import annotations

import json
from typing import List, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import gate as ontology_gate
from .gate import ResolutionReport


class DataSourceMissing(LookupError):
    """The ds_id has no row in workspace_data_sources."""


class OntologyNotAssigned(ValueError):
    """The data source exists but has no ontology_id set."""


class OntologyMissing(LookupError):
    """ds.ontology_id references an ontologies row that no longer exists
    (dangling FK after a hard delete or race during ondelete="SET NULL")."""


async def load_introspected_type_ids(
    session: AsyncSession, ds_id: str,
) -> Tuple[List[str], List[str]]:
    """Read entity- and edge-type IDs from the cached graph schema stats.

    Cache miss returns ``([], [])`` — the gate's criterion 4
    (``has_lineage``) operates on the full ontology so a missing stats
    cache no longer spuriously fails the gate.
    """
    from backend.app.db.repositories.stats_repo import get_data_source_stats

    cache = await get_data_source_stats(session, ds_id)
    if not cache or not cache.schema_stats:
        return [], []
    try:
        payload = json.loads(cache.schema_stats)
    except (TypeError, ValueError):
        return [], []
    entity_stats = payload.get("entityTypeStats") or payload.get("entity_type_stats") or []
    edge_stats = payload.get("edgeTypeStats") or payload.get("edge_type_stats") or []
    entity_ids = [
        e.get("id") for e in entity_stats if isinstance(e, dict) and e.get("id")
    ]
    edge_ids = [
        e.get("id") for e in edge_stats if isinstance(e, dict) and e.get("id")
    ]
    return entity_ids, edge_ids


async def build_resolution_report(
    session: AsyncSession, ds_id: str,
) -> ResolutionReport:
    """Run the ontology-resolution gate for ``ds_id``.

    Raises:
        DataSourceMissing: no workspace_data_sources row for ds_id.
        OntologyNotAssigned: DS row exists but ontology_id is null.
        OntologyMissing: ontology_id refers to a row that no longer exists.

    Returns the ``ResolutionReport`` (which may itself be unresolved).
    Callers decide how to surface failures (HTTP code, domain exception).
    """
    from backend.app.db.models import OntologyORM, WorkspaceDataSourceORM

    ds = await session.get(WorkspaceDataSourceORM, ds_id)
    if ds is None:
        raise DataSourceMissing(ds_id)
    if not ds.ontology_id:
        raise OntologyNotAssigned(ds_id)
    orm = await session.get(OntologyORM, ds.ontology_id)
    if orm is None:
        raise OntologyMissing(ds.ontology_id)

    entity_ids, edge_ids = await load_introspected_type_ids(session, ds_id)
    return ontology_gate.check_resolution(
        ontology_id=orm.id,
        ontology_version=orm.version,
        ontology_is_published=bool(orm.is_published),
        ontology_revision=getattr(orm, "revision", 0) or 0,
        entity_type_definitions_raw=json.loads(orm.entity_type_definitions or "{}"),
        relationship_type_definitions_raw=json.loads(orm.relationship_type_definitions or "{}"),
        introspected_entity_ids=entity_ids,
        introspected_edge_ids=edge_ids,
    )
