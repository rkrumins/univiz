"""Provider-agnostic loader for canonical-shape graph datasets.

Loads a JSON file whose ``nodes`` and ``edges`` arrays already conform
to the canonical ``GraphNode`` / ``GraphEdge`` Pydantic models in
``backend/common/models/graph.py``, validates them, and pushes them
through ``provider.save_custom_graph`` in chunks.

Usage::

    # Dry-run (validate the JSON without touching any provider)
    python -m backend.scripts.load_test_dataset \\
        --fixture small --dry-run

    # Push the small 6-node fixture into a registered Spanner provider
    python -m backend.scripts.load_test_dataset \\
        --provider-id prov_abc123 --fixture small

    # Push the bundled 228k-node demo dataset into a Neo4j provider
    python -m backend.scripts.load_test_dataset \\
        --provider-id prov_neo4j --fixture demo --batch-size 5000

    # Push an arbitrary JSON file
    python -m backend.scripts.load_test_dataset \\
        --provider-id prov_abc123 --fixture-path path/to/graph.json

The loader is provider-agnostic: it resolves the registered provider
via ``provider_manager`` and writes through whichever backend
(FalkorDB / Neo4j / Spanner) the row points at. As long as the JSON
matches the canonical Pydantic shape, every provider's
``save_custom_graph`` accepts it identically.

Mode B (customer-supplied schema with ``SchemaMapping``) is *not*
covered by this loader yet — that lands when the Spanner provider's
Phase 5 wiring goes in. For now the canonical-shape ingest path
proves the end-to-end onboarding works.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from backend.common.interfaces.provider import GraphDataProvider
from backend.common.models.graph import GraphEdge, GraphNode

logger = logging.getLogger("load_test_dataset")

# Path of this repo's root, used to resolve --fixture=demo to the
# pre-bundled 228k-node JSON file.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────────────────────────────────────────────────
# Fixture resolution
# ───────────────────────────────────────────────────────────────────


def _load_small_fixture() -> Tuple[List[GraphNode], List[GraphEdge]]:
    """The 6-node hierarchical fixture used by the regression contract
    tests. Reusing the exact same nodes/edges keeps the loader's smoke
    test and the contract test on the same baseline."""
    from backend.tests.regression.fixtures import fixture_edges, fixture_nodes
    return fixture_nodes(), fixture_edges()


def _load_json_path(path: Path) -> Tuple[List[GraphNode], List[GraphEdge]]:
    """Parse a canonical-shape JSON file. Pydantic validates the shape
    on construction; bad data fails before any I/O happens."""
    if not path.is_file():
        raise SystemExit(f"fixture path not found: {path}")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or "nodes" not in raw or "edges" not in raw:
        raise SystemExit(
            f"{path}: expected top-level object with 'nodes' and 'edges' arrays"
        )
    try:
        nodes = [GraphNode.model_validate(n) for n in raw["nodes"]]
    except Exception as exc:
        raise SystemExit(
            f"{path}: nodes failed canonical-shape validation: {exc}"
        ) from exc
    try:
        edges = [GraphEdge.model_validate(e) for e in raw["edges"]]
    except Exception as exc:
        raise SystemExit(
            f"{path}: edges failed canonical-shape validation: {exc}"
        ) from exc
    return nodes, edges


def _resolve_fixture(name: str) -> Tuple[List[GraphNode], List[GraphEdge]]:
    if name == "small":
        return _load_small_fixture()
    if name == "demo":
        path = _REPO_ROOT / "demo_graph_with_lineage.json"
        if not path.is_file():
            raise SystemExit(
                f"demo fixture missing at {path}. Either provide a path "
                "via --fixture-path or check out the demo dataset."
            )
        return _load_json_path(path)
    raise SystemExit(f"unknown --fixture={name!r} (use 'small' or 'demo')")


# ───────────────────────────────────────────────────────────────────
# Provider resolution
# ───────────────────────────────────────────────────────────────────


async def _resolve_provider(provider_id: str) -> GraphDataProvider:
    """Resolve a registered provider through the same factory the API
    uses, so onboarding goes through the exact runtime path the
    platform takes when serving traffic."""
    from backend.app.db.engine import get_db_session
    from backend.app.db.repositories import provider_repo
    from backend.app.providers.manager import provider_manager

    async for session in get_db_session():
        orm = await provider_repo.get_provider_orm(session, provider_id)
        if orm is None:
            raise SystemExit(f"provider {provider_id!r} not found")
        # _create_provider_instance unwraps host/port/credentials/extra_config
        # the same way the production dispatch does.
        from backend.app.db.repositories.connection_repo import _decrypt
        creds_blob = orm.credentials or ""
        creds = _decrypt(creds_blob) if creds_blob else {}
        if isinstance(creds, str):
            try:
                creds = json.loads(creds) or {}
            except json.JSONDecodeError:
                creds = {}
        extra_config = (
            json.loads(orm.extra_config) if orm.extra_config else None
        )
        return provider_manager._create_provider_instance(
            orm.provider_type,
            orm.host,
            orm.port,
            None,  # graph_name resolved from extra_config per provider
            bool(orm.tls_enabled),
            creds,
            extra_config=extra_config,
        )
    raise SystemExit("could not open management DB session")


# ───────────────────────────────────────────────────────────────────
# Push loop
# ───────────────────────────────────────────────────────────────────


def _chunked(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _push(
    provider: GraphDataProvider,
    nodes: List[GraphNode],
    edges: List[GraphEdge],
    *,
    batch_size: int,
) -> Dict[str, Any]:
    """Push canonical-shape data through ``save_custom_graph`` in
    batches. Provider-agnostic: every ABC implementation accepts the
    same Pydantic types."""
    # Some providers are aggregation-aware; configure the minimum so
    # `save_custom_graph` doesn't refuse to write because containment
    # types are unresolved. The ontology-set methods are no-ops when
    # the data already carries the correct edge_type strings.
    if hasattr(provider, "set_containment_edge_types"):
        try:
            from backend.tests.regression.fixtures import containment_types
            provider.set_containment_edge_types(containment_types(), from_ontology=True)
        except Exception as exc:
            logger.warning("containment-type setup skipped: %s", exc)

    t0 = time.monotonic()
    n_total = len(nodes)
    e_total = len(edges)

    logger.info("pushing %d nodes in batches of %d", n_total, batch_size)
    for i, chunk in enumerate(_chunked(nodes, batch_size), start=1):
        await provider.save_custom_graph(chunk, [])
        done = min(i * batch_size, n_total)
        logger.info("  nodes: %d / %d", done, n_total)

    logger.info("pushing %d edges in batches of %d", e_total, batch_size)
    for i, chunk in enumerate(_chunked(edges, batch_size), start=1):
        await provider.save_custom_graph([], chunk)
        done = min(i * batch_size, e_total)
        logger.info("  edges: %d / %d", done, e_total)

    elapsed = time.monotonic() - t0
    return {
        "nodes": n_total,
        "edges": e_total,
        "batch_size": batch_size,
        "elapsed_s": round(elapsed, 2),
    }


# ───────────────────────────────────────────────────────────────────
# Stats
# ───────────────────────────────────────────────────────────────────


def _stats(nodes: List[GraphNode], edges: List[GraphEdge]) -> str:
    type_counts: Dict[str, int] = {}
    for n in nodes:
        type_counts[n.entity_type] = type_counts.get(n.entity_type, 0) + 1
    edge_counts: Dict[str, int] = {}
    for e in edges:
        edge_counts[e.edge_type] = edge_counts.get(e.edge_type, 0) + 1

    lines = [f"  nodes: {len(nodes)}"]
    for t, c in sorted(type_counts.items()):
        lines.append(f"    {t}: {c}")
    lines.append(f"  edges: {len(edges)}")
    for t, c in sorted(edge_counts.items()):
        lines.append(f"    {t}: {c}")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="load_test_dataset",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--fixture",
        choices=["small", "demo"],
        help="Use a built-in fixture: 'small' = 6-node hierarchy from "
             "backend/tests/regression/fixtures.py; 'demo' = the bundled "
             "228k-node demo_graph_with_lineage.json at the repo root.",
    )
    src.add_argument(
        "--fixture-path",
        type=Path,
        help="Path to a JSON file with top-level {nodes:[...], edges:[...]} "
             "arrays whose elements conform to GraphNode / GraphEdge.",
    )
    p.add_argument(
        "--provider-id",
        default=None,
        help="Registered provider row id in the management DB. Required "
             "unless --dry-run is set.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Records per save_custom_graph call (default: 1000).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the fixture against GraphNode/GraphEdge but do not "
             "open a provider connection. Useful to confirm a JSON file "
             "matches the canonical shape before any I/O.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-batch progress logs.",
    )
    return p


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Source resolution.
    if args.fixture_path:
        nodes, edges = _load_json_path(args.fixture_path)
    else:
        nodes, edges = _resolve_fixture(args.fixture)

    logger.info("dataset shape:\n%s", _stats(nodes, edges))

    if args.dry_run:
        logger.info("--dry-run: skipping provider push")
        return 0

    if not args.provider_id:
        logger.error("--provider-id is required unless --dry-run is set")
        return 2

    provider = await _resolve_provider(args.provider_id)
    try:
        result = await _push(
            provider, nodes, edges, batch_size=args.batch_size,
        )
    finally:
        if hasattr(provider, "close"):
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("provider close raised: %s", exc)

    logger.info("done: %s", result)
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
