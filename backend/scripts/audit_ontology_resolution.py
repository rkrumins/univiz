"""Audit which existing data sources fail the ontology-resolution gate.

Reads every ``WorkspaceDataSourceORM`` with a non-null ``ontology_id``,
loads the assigned ``OntologyORM`` and the cached graph schema stats,
runs ``backend.app.ontology.gate.check_resolution``, and prints a CSV
of failing data sources. Read-only; never mutates ontologies or jobs.

Usage::

    python -m backend.scripts.audit_ontology_resolution \\
        > /tmp/ontology_resolution_audit.csv

Useful as a one-shot before deploying the resolution gate so ops can
flag data sources whose previously-tolerant aggregation will start
returning HTTP 422 on next retrigger.
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
from typing import List, Tuple

from sqlalchemy import select

from backend.app.db.engine import get_admin_session
from backend.app.db.models import OntologyORM, WorkspaceDataSourceORM
from backend.app.db.repositories.stats_repo import get_data_source_stats
from backend.app.ontology import gate as ontology_gate


async def _audit_one(session, ds: WorkspaceDataSourceORM) -> Tuple[str, str, str, str, List[str]]:
    """Return (ds_id, ontology_id, resolved, message, blocking_reasons)
    for the audit CSV. Best-effort: any exception becomes a row with
    ``message=error: ...`` so ops sees it instead of the script bailing.
    """
    if not ds.ontology_id:
        return ds.id, "", "n/a", "no ontology assigned", []
    orm = await session.get(OntologyORM, ds.ontology_id)
    if orm is None:
        return ds.id, ds.ontology_id, "no", "ontology row missing", ["ontology_missing"]
    cache = await get_data_source_stats(session, ds.id)
    entity_ids: List[str] = []
    edge_ids: List[str] = []
    if cache and cache.schema_stats:
        try:
            payload = json.loads(cache.schema_stats)
        except (TypeError, ValueError):
            payload = {}
        for s in payload.get("entityTypeStats") or payload.get("entity_type_stats") or []:
            if isinstance(s, dict) and s.get("id"):
                entity_ids.append(s["id"])
        for s in payload.get("edgeTypeStats") or payload.get("edge_type_stats") or []:
            if isinstance(s, dict) and s.get("id"):
                edge_ids.append(s["id"])
    report = ontology_gate.check_resolution(
        ontology_id=orm.id,
        ontology_version=orm.version,
        ontology_is_published=bool(orm.is_published),
        ontology_revision=getattr(orm, "revision", 0) or 0,
        entity_type_definitions_raw=json.loads(orm.entity_type_definitions or "{}"),
        relationship_type_definitions_raw=json.loads(orm.relationship_type_definitions or "{}"),
        introspected_entity_ids=entity_ids,
        introspected_edge_ids=edge_ids,
    )
    return (
        ds.id,
        ds.ontology_id,
        "yes" if report.resolved else "no",
        "; ".join(report.blocking_reasons) or "",
        report.blocking_reasons,
    )


async def main() -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow([
        "data_source_id",
        "ontology_id",
        "resolved",
        "blocking_reasons",
        "missing_entity_types",
        "missing_edge_types",
        "unclassified_relationships",
    ])
    async with get_admin_session() as session:
        rows = (await session.execute(select(WorkspaceDataSourceORM))).scalars().all()
        for ds in rows:
            try:
                ds_id, ontology_id, resolved, _, _ = await _audit_one(session, ds)
                # Re-run for the structured fields — auditors usually
                # want them broken out, not jammed into one column.
                if ontology_id:
                    orm = await session.get(OntologyORM, ds.ontology_id)
                    if orm:
                        cache = await get_data_source_stats(session, ds.id)
                        ent_ids: List[str] = []
                        ed_ids: List[str] = []
                        if cache and cache.schema_stats:
                            payload = json.loads(cache.schema_stats or "{}")
                            for s in payload.get("entityTypeStats") or []:
                                if isinstance(s, dict) and s.get("id"):
                                    ent_ids.append(s["id"])
                            for s in payload.get("edgeTypeStats") or []:
                                if isinstance(s, dict) and s.get("id"):
                                    ed_ids.append(s["id"])
                        report = ontology_gate.check_resolution(
                            ontology_id=orm.id,
                            ontology_version=orm.version,
                            ontology_is_published=bool(orm.is_published),
                            ontology_revision=getattr(orm, "revision", 0) or 0,
                            entity_type_definitions_raw=json.loads(orm.entity_type_definitions or "{}"),
                            relationship_type_definitions_raw=json.loads(orm.relationship_type_definitions or "{}"),
                            introspected_entity_ids=ent_ids,
                            introspected_edge_ids=ed_ids,
                        )
                        writer.writerow([
                            ds.id,
                            orm.id,
                            "yes" if report.resolved else "no",
                            "|".join(report.blocking_reasons),
                            "|".join(report.missing_entity_types),
                            "|".join(report.missing_edge_types),
                            "|".join(g.id for g in report.unclassified_relationships),
                        ])
                        continue
                writer.writerow([ds.id, ontology_id or "", resolved, "", "", "", ""])
            except Exception as exc:  # noqa: BLE001 — best-effort audit
                writer.writerow([ds.id, ds.ontology_id or "", "error", str(exc), "", "", ""])


if __name__ == "__main__":
    asyncio.run(main())
