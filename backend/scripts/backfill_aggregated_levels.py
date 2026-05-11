"""
Backfill script: writes ``r.sourceLevel`` and ``r.targetLevel`` on every
existing ``:AGGREGATED`` edge that doesn't have them yet.

These two relationship properties power the trace fast-path's level-pair
filter. Pre-this-script, AGGREGATED edges materialised before the
provider began stamping levels lack the metadata; the level-pair index
can't help them. After this runs, the entire AGGREGATED edge set is
queryable via:

    MATCH (s)-[r:AGGREGATED]->(t)
    WHERE r.sourceLevel = $focusLevel AND r.targetLevel = $targetLevel

instead of the legacy:

    MATCH (s)-[r:AGGREGATED]->(t)
    WHERE labels(s)[0] IN $sTypes AND labels(t)[0] IN $tTypes  -- per-row scan

Idempotent: ``WHERE r.sourceLevel IS NULL`` ensures re-runs only touch
edges that haven't been backfilled. Cursor-resumable through the LIMIT
chunks. Safe to run multiple times.

Companion to ``backfill_node_levels.py`` — that script writes
``n.level`` on nodes; this one writes ``r.sourceLevel`` /
``r.targetLevel`` on AGGREGATED edges.

Usage:
    python -m backend.scripts.backfill_aggregated_levels --workspace-id <id>
    python -m backend.scripts.backfill_aggregated_levels --data-source-id <id>
    python -m backend.scripts.backfill_aggregated_levels        # default workspace
    python -m backend.scripts.backfill_aggregated_levels --batch-size 10000

Run order after a fresh ingest / ontology change:
    1. backfill_node_levels.py           (writes n.level)
    2. backfill_aggregated_levels.py     (writes r.sourceLevel / r.targetLevel)
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Dict, Optional, Set

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.app.db.engine import get_async_session
from backend.app.db.repositories import workspace_repo
from backend.app.providers.manager import provider_manager
from backend.app.services.context_engine import ContextEngine


def _derive_levels_from_ontology(ontology) -> Dict[str, int]:
    """Derive entity-type levels from the ontology's ``can_contain`` /
    ``can_be_contained_by`` declarations.

    Used when ``hierarchy.level`` isn't explicitly set on any entity type
    in the ontology (e.g. Solidatus, BAS). The ontology still declares
    parent/child relationships between types — we just need to project
    those onto an integer level dimension. The ontology is the source of
    truth; we never query the graph for this.

    Algorithm:
      1. Collect every entity type and its declared parents (from
         ``can_be_contained_by``) and children (from ``can_contain``).
         Both are merged into a single parent map for robustness — some
         ontologies only populate one direction.
      2. Types with no incoming containment in the ontology = level 0
         (roots).
      3. Each remaining type's level = max(parent_levels) + 1, iterated
         to a fixed point.

    Returns ``{entity_type_id: level}`` or an empty dict if the ontology
    has no containment declarations.
    """
    defs = getattr(ontology, "entity_type_definitions", None) or {}
    if not defs:
        return {}

    all_types: Set[str] = set(defs.keys())
    parents: Dict[str, Set[str]] = {}

    # Pass 1: harvest declared parent/child relationships from both
    # directions so we don't lose data when only one is populated.
    for et_id, et_def in defs.items():
        hierarchy = getattr(et_def, "hierarchy", None)
        if hierarchy is None:
            continue
        can_contain = list(getattr(hierarchy, "can_contain", None) or [])
        can_be_contained_by = list(getattr(hierarchy, "can_be_contained_by", None) or [])

        # This type's parents are explicitly declared in can_be_contained_by.
        for parent in can_be_contained_by:
            if parent and parent != et_id and parent in all_types:
                parents.setdefault(et_id, set()).add(parent)

        # This type's children list also implies the reverse — every child
        # listed here has THIS type as a potential parent.
        for child in can_contain:
            if child and child != et_id and child in all_types:
                parents.setdefault(child, set()).add(et_id)

    if not parents:
        return {}

    # Roots: types with no declared parents are level 0
    levels: Dict[str, int] = {t: 0 for t in all_types if t not in parents}

    # Iterate to fixed point. Cycle-safe via iteration cap = len(all_types)+1.
    # A non-cyclic containment DAG converges in at most len(longest chain) steps.
    max_iterations = len(all_types) + 1
    for _ in range(max_iterations):
        changed = False
        for t in all_types:
            ps = parents.get(t)
            if not ps:
                continue
            parent_levels = [levels[p] for p in ps if p in levels]
            if not parent_levels:
                continue
            new_level = max(parent_levels) + 1
            if levels.get(t) != new_level:
                levels[t] = new_level
                changed = True
        if not changed:
            break

    return levels

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def _backfill_chunk(provider, level_map: Dict[str, int], chunk_size: int) -> int:
    """Backfill one chunk of AGGREGATED edges. Returns affected count.

    The query operates against the projection graph because that's where
    AGGREGATED lives in both projection modes. Falls back to the source
    graph when the provider lacks the projection write helper (older
    providers / dedicated mode misconfig). ``$levelMap[labels(...)]``
    resolves the entity-type-label to a hierarchy level in a single
    pass.
    """
    write = getattr(provider, "_proj_query", None) or getattr(provider, "_query", None)
    if write is None:
        raise RuntimeError(
            f"Provider {type(provider).__name__} has no recognised write method "
            "(_proj_query or _query)"
        )

    cypher = (
        "MATCH (s)-[r:AGGREGATED]->(t) "
        "WHERE r.sourceLevel IS NULL OR r.targetLevel IS NULL "
        "WITH s, t, r LIMIT $limit "
        "SET r.sourceLevel = $levelMap[labels(s)[0]], "
        "    r.targetLevel = $levelMap[labels(t)[0]] "
        "RETURN count(r) AS updated"
    )
    result = await write(cypher, params={"levelMap": level_map, "limit": int(chunk_size)})

    rs = getattr(result, "result_set", None)
    if rs is not None:
        return int(rs[0][0]) if rs and rs[0] else 0
    if isinstance(result, list) and result:
        row = result[0]
        if isinstance(row, dict):
            return int(row.get("updated", 0))
        return int(row[0])
    return 0


async def backfill(
    workspace_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    batch_size: int = 5000,
) -> None:
    async with get_async_session() as session:
        if not workspace_id and not data_source_id:
            ws = await workspace_repo.get_default_workspace(session)
            if not ws:
                logger.error("No workspace specified and no default workspace found")
                return
            workspace_id = ws.id
            logger.info("Using default workspace: %s (%s)", ws.name, ws.id)

        engine = await ContextEngine.for_workspace(
            workspace_id, provider_manager, session, data_source_id=data_source_id
        )

        # Resolve ontology and build the entity_type → level map. We need the
        # RESOLVED ontology (carries entity_type_definitions with hierarchy
        # data), not the flat OntologyMetadata projection. Resolving has the
        # side effect of injecting the level map onto the provider when the
        # ontology declares hierarchy.level, so the trace fast path picks
        # it up immediately.
        resolve = getattr(engine, "_resolve_ontology", None)
        if callable(resolve):
            ontology = await resolve()
        else:
            ontology = await engine.get_ontology_metadata()

        level_map: Dict[str, int] = {}
        for et_id, et_def in (getattr(ontology, "entity_type_definitions", {}) or {}).items():
            hierarchy = getattr(et_def, "hierarchy", None)
            level = getattr(hierarchy, "level", None) if hierarchy else None
            if isinstance(level, int):
                level_map[et_id] = level

        derived = False
        if not level_map:
            # Ontology declares no hierarchy.level — derive from the same
            # ontology's can_contain / can_be_contained_by declarations. This
            # is still ontology-driven (no graph queries); it just projects
            # the declared parent/child structure onto integer levels.
            logger.info(
                "No hierarchy.level declared in ontology — deriving levels "
                "from ontology can_contain / can_be_contained_by relations"
            )
            level_map = _derive_levels_from_ontology(ontology)
            derived = bool(level_map)
            if not level_map:
                logger.warning(
                    "Ontology has no containment relations declared either — "
                    "cannot derive level map. Backfill cannot proceed; either "
                    "add hierarchy.level on entity types or declare can_contain / "
                    "can_be_contained_by in the ontology."
                )
                return

        # Inject the level map onto the provider so traces running in this
        # process (or future processes that re-resolve the ontology with
        # this script's side effects) benefit immediately. For derived maps
        # the ontology resolver wouldn't have set this, so we do it here.
        if derived and hasattr(engine.provider, "set_entity_type_levels"):
            engine.provider.set_entity_type_levels(level_map)
            logger.info(
                "Injected derived level map onto provider (%d types)",
                len(level_map),
            )

        logger.info(
            "Backfilling AGGREGATED.sourceLevel/targetLevel using %s level map "
            "of %d types: %s",
            "DERIVED" if derived else "declared",
            len(level_map),
            sorted(level_map.items(), key=lambda kv: kv[1]),
        )

        total_updated = 0
        chunk_index = 0
        while True:
            chunk_index += 1
            try:
                updated = await _backfill_chunk(engine.provider, level_map, batch_size)
            except Exception as exc:
                logger.error("Chunk %d failed: %s", chunk_index, exc)
                raise

            total_updated += updated
            logger.info("  chunk %d: %d edges updated (running total: %d)",
                        chunk_index, updated, total_updated)

            # Drain condition: a chunk that updated fewer edges than the
            # batch size has reached the end of the unprocessed set. Note
            # this is robust to concurrent materialisation — new edges are
            # written with the level metadata already in place by the
            # materialiser (see _materialize_edges_batched + on_lineage_
            # edge_written), so re-runs converge to a stable empty set.
            if updated < batch_size:
                break

        logger.info("Backfill complete: %d AGGREGATED edges updated total", total_updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill r.sourceLevel / r.targetLevel on existing :AGGREGATED edges."
    )
    parser.add_argument("--workspace-id", help="Target workspace ID")
    parser.add_argument("--data-source-id", help="Target data source ID (optional)")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Edges per chunk (default: 5000). Tune up for faster runs on idle clusters; "
             "tune down if competing writes are creating MERGE lock contention.",
    )
    args = parser.parse_args()

    asyncio.run(
        backfill(
            workspace_id=args.workspace_id,
            data_source_id=args.data_source_id,
            batch_size=args.batch_size,
        )
    )
