"""
Backfill script: writes ``n.level`` (hierarchy level from the ontology)
on every existing node that doesn't have it yet.

The trace v2 endpoints filter AGGREGATED edges by ``s.level`` and
``t.level`` at the database layer — this script populates that property
on nodes that were upserted before the provider started writing it.

Idempotent: ``WHERE n.level IS NULL`` ensures re-runs only touch nodes
that haven't been backfilled. Safe to run multiple times.

Usage:
    python -m backend.scripts.backfill_node_levels --workspace-id <id>
    python -m backend.scripts.backfill_node_levels --data-source-id <id>
    python -m backend.scripts.backfill_node_levels        # uses default workspace
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _sanitize_label(s: str) -> str:
    """Mirror provider label sanitization — keep alphanum + underscore."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(s))


async def _run_per_label(provider, label: str, level: int) -> int:
    """Run the level-set query for a single label. Returns affected node count.

    Uses MATCH with WHERE IS NULL (idempotent). Runs as one query — the
    label index makes the scan cheap even on multi-million-node graphs;
    the WHERE filter prunes already-backfilled nodes.
    """
    safe = _sanitize_label(label)
    # FalkorDB: _query (write); Neo4j: _run_write
    write = getattr(provider, "_query", None) or getattr(provider, "_run_write", None)
    if write is None:
        raise RuntimeError(f"Provider {type(provider).__name__} has no recognized write method")

    cypher = (
        f"MATCH (n:`{safe}`) WHERE n.level IS NULL "
        f"SET n.level = $level "
        f"RETURN count(n) AS updated"
    )
    result = await write(cypher, params={"level": level})

    # FalkorDB returns a Result object with .result_set; Neo4j _run_write returns a list of records.
    try:
        rs = getattr(result, "result_set", None)
        if rs is not None:
            return int(rs[0][0]) if rs and rs[0] else 0
        if isinstance(result, list) and result:
            row = result[0]
            return int(row.get("updated", 0)) if isinstance(row, dict) else int(row[0])
    except Exception:
        pass
    return 0


async def backfill(workspace_id: Optional[str] = None, data_source_id: Optional[str] = None) -> None:
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

        # Resolving the ontology pushes set_entity_type_levels() to the provider
        # as a side effect. We also build the level map locally so the backfill
        # can target labels we know about, even if the provider hasn't cached it.
        ontology = await engine.get_ontology_metadata()
        levels: Dict[str, int] = {}
        for et_id, et_def in (getattr(ontology, "entity_type_definitions", {}) or {}).items():
            hierarchy = getattr(et_def, "hierarchy", None)
            level = getattr(hierarchy, "level", None) if hierarchy else None
            if isinstance(level, int):
                levels[et_id] = level

        if not levels:
            logger.warning("No entity types with hierarchy.level found in ontology — nothing to backfill")
            return

        logger.info("Backfilling n.level for %d entity types: %s",
                    len(levels), sorted(levels.items(), key=lambda kv: kv[1]))

        total_updated = 0
        for et_id, level in sorted(levels.items(), key=lambda kv: kv[1]):
            try:
                updated = await _run_per_label(engine.provider, et_id, level)
                logger.info("  %s (level=%d): updated %d nodes", et_id, level, updated)
                total_updated += updated
            except Exception as exc:
                logger.warning("  %s (level=%d): failed — %s", et_id, level, exc)

        logger.info("Backfill complete: %d nodes updated total", total_updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill n.level on existing nodes from ontology hierarchy.level")
    parser.add_argument("--workspace-id", help="Target workspace ID")
    parser.add_argument("--data-source-id", help="Target data source ID (optional)")
    args = parser.parse_args()

    asyncio.run(backfill(workspace_id=args.workspace_id, data_source_id=args.data_source_id))
