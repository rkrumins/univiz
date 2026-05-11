#!/usr/bin/env python3
"""
Import a Solidatus JSON export into FalkorDB.

Solidatus models use a Layer → Object → Attribute/Group hierarchy with
transitions (lineage) between attributes. This script converts that
structure into Synodic graph nodes and edges.

Entity type inference (two strategies, tried in order):

1. Prefix-based: IDs like LYR-xxx, OBJ-xxx, ATR-xxx, GRP-xxx
2. Hierarchy-based: when IDs are plain UUIDs, the position in the
   hierarchy determines the type:
     - roots/layers entries              →  "layer"
     - direct children of layers         →  "object"
     - children with their own children  →  "group"
     - leaf children (no children)       →  "attribute"

Edge type mapping:
  parent → child  →  "HAS"       (containment)
  transition      →  "FLOWS_TO"  (lineage)

These are custom types — the ontology resolver will pick them up via
introspection and generate default visuals automatically.

Usage:
  python backend/scripts/import_solidatus.py --file model.json --graph solidatus_demo
  python backend/scripts/import_solidatus.py --file model.json --graph solidatus_demo --source-system solidatus
  python backend/scripts/import_solidatus.py --file model.json --dry-run
  cat model.json | python backend/scripts/import_solidatus.py --graph solidatus_demo

Solidatus JSON format reference:
  https://docs.solidatus.com/api-documentation/api-actions/solidatus-json-format
"""



import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Dict, List, Optional, Any

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.common.models.graph import GraphNode, GraphEdge

logger = logging.getLogger("import_solidatus")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(message)s")

# ═══════════════════════════════════════════════════════════════════════════
# ENTITY TYPE DETECTION
# ═══════════════════════════════════════════════════════════════════════════

# Solidatus IDs *may* encode the entity type in a prefix (LYR-, OBJ-, ATR-, GRP-).
# When IDs are plain UUIDs, we fall back to hierarchy-based inference.
PREFIX_TO_TYPE = {
    "LYR": "layer",
    "OBJ": "object",
    "ATR": "attribute",
    "GRP": "group",
}


def detect_entity_type_from_prefix(solidatus_id: str) -> Optional[str]:
    """Try to derive entity type from a known Solidatus ID prefix.
    Returns None if the prefix is not recognized (e.g. plain UUID)."""
    prefix = solidatus_id.split("-", 1)[0].upper()
    return PREFIX_TO_TYPE.get(prefix)


def infer_entity_types(
    entities: Dict[str, Any],
    root_ids: List[str],
) -> Dict[str, str]:
    """Infer entity type for every entity based on prefix or hierarchy position.

    Strategy:
      1. If the ID has a known prefix (LYR-, OBJ-, ATR-, GRP-), use it.
      2. Otherwise infer from hierarchy position:
         - Listed in roots/layers  →  "layer"
         - Direct child of a layer →  "object"
         - Has children itself     →  "group"
         - Leaf (no children)      →  "attribute"
    """
    result: Dict[str, str] = {}
    root_set = set(root_ids)

    # Build a parent → children lookup
    children_of: Dict[str, List[str]] = {}
    parent_of: Dict[str, str] = {}
    for eid, edef in entities.items():
        kids = edef.get("children", [])
        children_of[eid] = kids
        for kid in kids:
            parent_of[kid] = eid

    for eid in entities:
        # Strategy 1: prefix-based
        from_prefix = detect_entity_type_from_prefix(eid)
        if from_prefix is not None:
            result[eid] = from_prefix
            continue

        # Strategy 2: hierarchy-based
        if eid in root_set:
            result[eid] = "layer"
        elif parent_of.get(eid) in root_set or result.get(parent_of.get(eid, "")) == "layer":
            # Direct child of a root/layer → object
            has_kids = bool(children_of.get(eid))
            result[eid] = "object" if has_kids else "attribute"
        elif children_of.get(eid):
            result[eid] = "group"
        else:
            result[eid] = "attribute"

    return result


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════════

class SolidatusGraphBuilder:
    """Converts a Solidatus JSON export into Synodic GraphNode/GraphEdge lists."""

    def __init__(self, source_system: str = "solidatus"):
        self.source_system = source_system
        self.nodes: List[GraphNode] = []
        self.edges: List[GraphEdge] = []
        self._urn_map: Dict[str, str] = {}  # solidatus_id → synodic URN

    def _make_urn(self, entity_type: str, solidatus_id: str) -> str:
        """Generate a deterministic Synodic URN from the Solidatus entity ID."""
        return f"urn:synodic:{self.source_system}:{entity_type}:{solidatus_id}"

    def _get_or_create_urn(self, solidatus_id: str, entity_type: str = "object") -> str:
        """Lazily resolve a Solidatus ID to a Synodic URN."""
        if solidatus_id not in self._urn_map:
            self._urn_map[solidatus_id] = self._make_urn(entity_type, solidatus_id)
        return self._urn_map[solidatus_id]

    def build(self, data: Dict[str, Any]) -> None:
        """Parse a Solidatus JSON export and populate nodes + edges."""
        entities = data.get("entities", {})
        # Solidatus uses "layers" or "roots" depending on export version
        root_ids: List[str] = data.get("layers", []) or data.get("roots", [])
        transitions = data.get("transitions", {})

        # ── Pre-pass: Infer entity types from prefixes + hierarchy ──
        type_map = infer_entity_types(entities, root_ids)

        # ── Pass 1: Create nodes for every entity ────────────────────
        for solidatus_id, entity_def in entities.items():
            entity_type = type_map.get(solidatus_id, "object")
            urn = self._get_or_create_urn(solidatus_id, entity_type)

            # Separate well-known fields from arbitrary properties
            props = dict(entity_def.get("properties", {}))
            props["solidatusId"] = solidatus_id

            # Mark layers
            if solidatus_id in root_ids:
                props["isLayer"] = True

            self.nodes.append(GraphNode(
                urn=urn,
                entityType=entity_type,
                displayName=entity_def.get("name", solidatus_id),
                qualifiedName=entity_def.get("name", solidatus_id),
                properties=props,
                tags=[],
            ))

        # ── Pass 2: Create containment edges (parent → child) ───────
        for solidatus_id, entity_def in entities.items():
            children = entity_def.get("children", [])
            parent_urn = self._get_or_create_urn(solidatus_id)

            for child_id in children:
                if child_id not in entities:
                    logger.warning(f"Child {child_id} referenced by {solidatus_id} not found in entities — skipping")
                    continue

                child_urn = self._get_or_create_urn(child_id)
                self.edges.append(GraphEdge(
                    id=f"has-{parent_urn}-{child_urn}",
                    sourceUrn=parent_urn,
                    targetUrn=child_urn,
                    edgeType="HAS",
                    properties={},
                ))

        # ── Pass 3: Create lineage edges (transitions) ──────────────
        for transition_id, transition_def in transitions.items():
            source_id = transition_def.get("source")
            target_id = transition_def.get("target")

            if not source_id or not target_id:
                logger.warning(f"Transition {transition_id} missing source/target — skipping")
                continue

            # Create placeholder nodes for references not in entities
            # (some Solidatus exports reference external entities)
            for ref_id in (source_id, target_id):
                if ref_id not in entities and ref_id not in self._urn_map:
                    ref_type = detect_entity_type_from_prefix(ref_id) or "attribute"
                    logger.info(f"Creating placeholder node for external reference {ref_id}")
                    urn = self._get_or_create_urn(ref_id, ref_type)
                    self.nodes.append(GraphNode(
                        urn=urn,
                        entityType=ref_type,
                        displayName=ref_id,
                        qualifiedName=ref_id,
                        properties={"solidatusId": ref_id, "placeholder": True},
                        tags=[],
                    ))

            source_urn = self._get_or_create_urn(source_id)
            target_urn = self._get_or_create_urn(target_id)

            props = dict(transition_def.get("properties", {}))
            props["solidatusTransitionId"] = transition_id

            self.edges.append(GraphEdge(
                id=f"flows-{source_urn}-{target_urn}",
                sourceUrn=source_urn,
                targetUrn=target_urn,
                edgeType="FLOWS_TO",
                properties=props,
            ))

    def print_stats(self) -> None:
        """Print summary statistics."""
        type_counts: Dict[str, int] = {}
        for n in self.nodes:
            type_counts[n.entity_type] = type_counts.get(n.entity_type, 0) + 1

        edge_counts: Dict[str, int] = {}
        for e in self.edges:
            edge_counts[e.edge_type] = edge_counts.get(e.edge_type, 0) + 1

        logger.info(f"Nodes: {len(self.nodes)}")
        for t, c in sorted(type_counts.items()):
            logger.info(f"  {t}: {c}")

        logger.info(f"Edges: {len(self.edges)}")
        for t, c in sorted(edge_counts.items()):
            logger.info(f"  {t}: {c}")


# ═══════════════════════════════════════════════════════════════════════════
# FALKORDB PUSH
# ═══════════════════════════════════════════════════════════════════════════

async def push_to_falkordb(builder: SolidatusGraphBuilder, graph_name: str):
    from backend.app.providers.falkordb_provider import FalkorDBProvider
    provider = FalkorDBProvider(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        graph_name=graph_name,
    )
    await provider._ensure_connected()

    CHUNK = 10_000
    logger.info(f"Pushing {len(builder.nodes)} nodes to graph '{graph_name}'...")
    for i in range(0, len(builder.nodes), CHUNK):
        await provider.save_custom_graph(builder.nodes[i:i + CHUNK], [])
        logger.info(f"  Nodes: {min(i + CHUNK, len(builder.nodes))}/{len(builder.nodes)}")

    logger.info(f"Pushing {len(builder.edges)} edges...")
    for i in range(0, len(builder.edges), CHUNK):
        await provider.save_custom_graph([], builder.edges[i:i + CHUNK])
        logger.info(f"  Edges: {min(i + CHUNK, len(builder.edges))}/{len(builder.edges)}")

    await provider.ensure_indices()
    logger.info("Push complete!")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import a Solidatus JSON export into FalkorDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backend/scripts/import_solidatus.py --file model.json --graph solidatus_demo
  python backend/scripts/import_solidatus.py --file model.json --graph solidatus_demo --source-system solidatus
  python backend/scripts/import_solidatus.py --file model.json --dry-run
  cat model.json | python backend/scripts/import_solidatus.py --graph solidatus_demo
        """,
    )
    parser.add_argument("--file", type=str, default=None,
                        help="Path to Solidatus JSON file (reads from stdin if omitted)")
    parser.add_argument("--graph", type=str, default=None,
                        help="FalkorDB graph name (required unless --dry-run)")
    parser.add_argument("--source-system", type=str, default="solidatus",
                        help="Source system identifier for URN generation (default: solidatus)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print stats without pushing to FalkorDB")

    args = parser.parse_args()

    # Validate arguments
    if not args.dry_run and not args.graph:
        parser.error("--graph is required unless --dry-run is specified")

    # Load JSON
    if args.file:
        logger.info(f"Reading {args.file}...")
        with open(args.file, "r") as f:
            data = json.load(f)
    elif not sys.stdin.isatty():
        logger.info("Reading from stdin...")
        data = json.load(sys.stdin)
    else:
        parser.error("Provide --file or pipe JSON to stdin")

    # Build graph
    builder = SolidatusGraphBuilder(source_system=args.source_system)
    builder.build(data)
    builder.print_stats()

    # Push or dry-run
    if args.dry_run:
        logger.info("Dry run — no data pushed.")
        # Print a few sample nodes for verification
        for node in builder.nodes[:5]:
            logger.info(f"  Sample: {node.urn}  type={node.entity_type}  name={node.display_name}")
    else:
        asyncio.run(push_to_falkordb(builder, args.graph))
