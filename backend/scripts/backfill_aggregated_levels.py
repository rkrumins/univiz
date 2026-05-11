"""
Backfill script: stamps ``r.sourceLevel``, ``r.targetLevel``, and
``r.levelDigest`` on every ``:AGGREGATED`` edge whose stamp is missing or
stale relative to the current ontology.

These properties power the trace fast-path's level-pair filter:

    MATCH (s)-[r:AGGREGATED]->(t)
    WHERE r.sourceLevel = $focusLevel AND r.targetLevel = $targetLevel

instead of the legacy per-row label scan:

    MATCH (s)-[r:AGGREGATED]->(t)
    WHERE labels(s)[0] IN $sTypes AND labels(t)[0] IN $tTypes

**Convergence:** every chunk's WHERE filters on ``r.levelDigest IS NULL
OR r.levelDigest <> $digest``. The SET assigns the current digest, so a
processed edge no longer matches the WHERE on the next chunk. The drain
condition is ``updated == 0``. This works for any ontology and any graph
state (including edges whose endpoint labels have no declared level —
those get sentinel ``-1`` and are not re-processed).

**Drift handling:** running this after an ontology edit re-stamps every
edge whose digest no longer matches. Same script, no flags needed.

**Unstampable edges:** if either endpoint's label is not in the current
level map, both ``sourceLevel`` and ``targetLevel`` are written as ``-1``
(``UNKNOWN_LEVEL`` in ``ontology_levels``). The trace fast path treats
``-1`` as "unknown level" and falls back to the label-scan path for
those edges only — the rest of the graph keeps its fast path.

Companion to ``backfill_node_levels.py`` — that script writes ``n.level``
on nodes; this one writes the edge stamps.

Usage:
    python -m backend.scripts.backfill_aggregated_levels --workspace-id <id>
    python -m backend.scripts.backfill_aggregated_levels --data-source-id <id>
    python -m backend.scripts.backfill_aggregated_levels        # default workspace
    python -m backend.scripts.backfill_aggregated_levels --batch-size 10000

Run order after a fresh ingest / ontology change:
    1. backfill_node_levels.py           (writes n.level)
    2. backfill_aggregated_levels.py     (writes r.sourceLevel / r.targetLevel / r.levelDigest)
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Dict, Optional

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.app.db.engine import get_async_session
from backend.app.db.repositories import workspace_repo
from backend.app.providers.manager import provider_manager
from backend.app.services.context_engine import ContextEngine
from backend.app.services.ontology_levels import (
    UNKNOWN_LEVEL,
    compute_level_digest,
    derive_level_map,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def _backfill_chunk(
    provider,
    level_map: Dict[str, int],
    digest: str,
    chunk_size: int,
) -> int:
    """Stamp one chunk of AGGREGATED edges. Returns the number of edges
    stamped in this chunk.

    Selection: edges whose ``r.levelDigest`` is NULL (never stamped) or
    differs from the current ``$digest`` (ontology drifted since last
    stamping). After SET, every selected edge carries the current digest
    and no longer matches the WHERE, so re-runs converge.

    Stamping: ``$levelMap[labels(...)]`` lookup returns ``NULL`` when an
    endpoint's label is not in the current map. ``COALESCE(... , -1)``
    converts that NULL to the ``UNKNOWN_LEVEL`` sentinel, which (a)
    distinguishes "label has no declared level" from "edge never
    stamped" and (b) lets the WHERE filter NOT re-pick the edge on the
    next chunk (it now has a non-null digest).

    The query operates against the projection graph because that's where
    AGGREGATED lives in both projection modes. Falls back to the source
    graph when the provider lacks the projection write helper.
    """
    write = getattr(provider, "_proj_query", None) or getattr(provider, "_query", None)
    if write is None:
        raise RuntimeError(
            f"Provider {type(provider).__name__} has no recognised write method "
            "(_proj_query or _query)"
        )

    cypher = (
        "MATCH (s)-[r:AGGREGATED]->(t) "
        "WHERE r.levelDigest IS NULL OR r.levelDigest <> $digest "
        "WITH s, t, r LIMIT $limit "
        "SET r.sourceLevel = COALESCE($levelMap[labels(s)[0]], $unknown), "
        "    r.targetLevel = COALESCE($levelMap[labels(t)[0]], $unknown), "
        "    r.levelDigest = $digest "
        "RETURN count(r) AS updated"
    )
    result = await write(
        cypher,
        params={
            "levelMap": level_map,
            "digest": digest,
            "unknown": UNKNOWN_LEVEL,
            "limit": int(chunk_size),
        },
    )

    rs = getattr(result, "result_set", None)
    if rs is not None:
        return int(rs[0][0]) if rs and rs[0] else 0
    if isinstance(result, list) and result:
        row = result[0]
        if isinstance(row, dict):
            return int(row.get("updated", 0))
        return int(row[0])
    return 0


async def _count_unstampable_labels(provider, level_map: Dict[str, int]) -> Dict[str, int]:
    """Report which endpoint labels appear on AGGREGATED edges but are NOT
    in the current level map. Pure observability — drives a one-time log
    line so operators know the ontology is incomplete instead of guessing
    why some edges got stamped with -1.
    """
    write = getattr(provider, "_proj_query", None) or getattr(provider, "_query", None)
    if write is None:
        return {}

    cypher = (
        "MATCH (s)-[r:AGGREGATED]->(t) "
        "WITH labels(s)[0] AS sLbl, labels(t)[0] AS tLbl "
        "WITH collect(sLbl) + collect(tLbl) AS labels_list "
        "UNWIND labels_list AS lbl "
        "WITH lbl WHERE NOT lbl IN $known "
        "RETURN lbl, count(*) AS count"
    )
    try:
        result = await write(cypher, params={"known": list(level_map.keys())})
    except Exception:
        return {}

    rs = getattr(result, "result_set", None) or []
    return {row[0]: int(row[1]) for row in rs if row}


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

        # Resolve the ontology and build the level map. We use the same
        # shared function as the runtime (`derive_level_map`), so the digest
        # the script computes matches the digest the provider stamps on
        # newly-materialized edges. Resolving has the side effect of
        # injecting the level map onto the provider via ContextEngine.
        resolve = getattr(engine, "_resolve_ontology", None)
        if callable(resolve):
            ontology = await resolve()
        else:
            ontology = await engine.get_ontology_metadata()

        level_map = derive_level_map(ontology)
        if not level_map:
            logger.warning(
                "Ontology declares no hierarchy.level and has no "
                "can_contain / can_be_contained_by relations either — "
                "cannot derive a level map. Every edge will be stamped "
                "with %d (unknown). Either add hierarchy.level on entity "
                "types or declare containment relations.", UNKNOWN_LEVEL,
            )

        digest = compute_level_digest(level_map)

        # Make sure the provider has the same map+digest as we do, so the
        # cold-start probe checks against the digest we're about to stamp
        # and the on-ingest hook stamps the same digest going forward.
        if hasattr(engine.provider, "set_entity_type_levels"):
            engine.provider.set_entity_type_levels(level_map)

        logger.info(
            "Backfilling AGGREGATED stamps. levelDigest=%s, level_map "
            "(%d types): %s",
            digest[:12],
            len(level_map),
            sorted(level_map.items(), key=lambda kv: kv[1]),
        )

        # Observability: report which entity-type labels appear on edges
        # but aren't in the level map. Those edges will be stamped with
        # UNKNOWN_LEVEL and take the label-scan path at trace time.
        unstampable = await _count_unstampable_labels(engine.provider, level_map)
        if unstampable:
            logger.warning(
                "Endpoint labels not in level map (will be stamped %d): "
                "%s. Add these to the ontology to enable the level-pair "
                "fast path for the affected edges.",
                UNKNOWN_LEVEL,
                sorted(unstampable.items(), key=lambda kv: -kv[1]),
            )

        total_updated = 0
        chunk_index = 0
        while True:
            chunk_index += 1
            try:
                updated = await _backfill_chunk(
                    engine.provider, level_map, digest, batch_size,
                )
            except Exception as exc:
                logger.error("Chunk %d failed: %s", chunk_index, exc)
                raise

            total_updated += updated
            logger.info("  chunk %d: %d edges stamped (running total: %d)",
                        chunk_index, updated, total_updated)

            # Drain: every selected edge gets the current digest, so it
            # falls out of the WHERE on the next iteration. An empty
            # chunk means we're done. This converges for any ontology
            # and any graph state — including the unstampable-label
            # case (those edges get -1 + current digest and exit the
            # selection too).
            if updated == 0:
                break

        logger.info(
            "Backfill complete: %d AGGREGATED edges stamped with "
            "levelDigest=%s", total_updated, digest[:12],
        )

        # Re-probe so the in-process provider's `_levels_backfilled`
        # flag flips to True without waiting for the next traffic.
        probe = getattr(engine.provider, "_check_levels_backfilled", None)
        if callable(probe):
            await probe()


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
