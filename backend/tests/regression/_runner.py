"""Shared runner that exercises a GraphDataProvider through every
ABC method that matters for the reshape and asserts on snapshots.

Both the FalkorDB and Neo4j contract tests delegate here so the
behaviour pin is *exactly* identical across providers (modulo
provider-specific field shapes which the snapshot stabilises before
diff).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from backend.common.interfaces.provider import GraphDataProvider
from backend.common.models.graph import EdgeQuery, NodeQuery

from .fixtures import (
    ENTITY_LEVELS,
    containment_types,
    fixture_edges,
    fixture_nodes,
    lineage_types,
)
from .snapshot import assert_snapshot


async def seed(provider: GraphDataProvider) -> None:
    """Inject the fixture into a clean provider instance.

    Caller is responsible for handing us a graph that has been
    truncated; we don't try to delete-then-recreate here because the
    cleanup primitives differ across providers.
    """
    provider.set_containment_edge_types(containment_types(), from_ontology=True)
    if hasattr(provider, "set_entity_type_levels"):
        provider.set_entity_type_levels(ENTITY_LEVELS)
    if hasattr(provider, "set_resolved_edge_metadata"):
        provider.set_resolved_edge_metadata({}, lineage_types())
    if hasattr(provider, "ensure_indices"):
        try:
            await provider.ensure_indices(list({n.entity_type for n in fixture_nodes()}))
        except Exception:
            pass
    await provider.save_custom_graph(fixture_nodes(), fixture_edges())


async def run_all(provider: GraphDataProvider, *, snapshot_label: str) -> None:
    """Exercise every ABC method we want to pin and snapshot the output."""
    # --- Node ops -------------------------------------------------------
    n = await provider.get_node("urn:test:dataset:d1")
    assert_snapshot(provider=snapshot_label, name="get_node", actual=n)

    nodes = await provider.get_nodes(NodeQuery(entity_types=["dataset"], limit=100))
    assert_snapshot(provider=snapshot_label, name="get_nodes_dataset", actual=nodes)

    found = await provider.search_nodes("Dataset", limit=10)
    assert_snapshot(provider=snapshot_label, name="search_nodes_Dataset", actual=found)

    # --- Edge ops -------------------------------------------------------
    edges = await provider.get_edges(EdgeQuery(edge_types=["CONTAINS"], limit=100))
    assert_snapshot(provider=snapshot_label, name="get_edges_contains", actual=edges)

    # --- Containment ----------------------------------------------------
    children = await provider.get_children("urn:test:domain:root")
    assert_snapshot(provider=snapshot_label, name="get_children_root", actual=children)

    parent = await provider.get_parent("urn:test:dataset:d1")
    assert_snapshot(provider=snapshot_label, name="get_parent_d1", actual=parent)

    cwe = await provider.get_children_with_edges(
        "urn:test:domain:root",
        edge_types=containment_types(),
        lineage_edge_types=lineage_types(),
        limit=100,
    )
    assert_snapshot(provider=snapshot_label, name="get_children_with_edges_root", actual=cwe)

    # --- Lineage --------------------------------------------------------
    lineage_full = await provider.get_full_lineage(
        "urn:test:dataset:d1", upstream_depth=3, downstream_depth=3,
    )
    assert_snapshot(provider=snapshot_label, name="get_full_lineage_d1", actual=lineage_full)

    upstream = await provider.get_upstream("urn:test:dataset:d2", depth=3)
    assert_snapshot(provider=snapshot_label, name="get_upstream_d2", actual=upstream)

    downstream = await provider.get_downstream("urn:test:dataset:d1", depth=3)
    assert_snapshot(provider=snapshot_label, name="get_downstream_d1", actual=downstream)

    # --- Trace v2 -------------------------------------------------------
    # The lineage edges only exist between datasets (level=2), so a
    # level=2 trace should return them; a level=1 trace exercises the
    # inherited-lineage fallback (the schema has no AGGREGATED yet).
    try:
        trace = await provider.trace_at_level(
            "urn:test:dataset:d1", level=2,
            upstream_depth=2, downstream_depth=2,
            lineage_edge_types=lineage_types(),
            containment_edge_types=containment_types(),
            max_nodes=50, timeout_ms=5000,
        )
        assert_snapshot(provider=snapshot_label, name="trace_at_level2_d1", actual=trace)
    except NotImplementedError:
        # Provider may not implement Trace v2 yet; pin the behaviour.
        assert_snapshot(provider=snapshot_label, name="trace_at_level2_d1", actual="NotImplementedError")

    # --- Aggregated edges (read path) -----------------------------------
    agg_count = await provider.count_aggregated_edges()
    assert_snapshot(provider=snapshot_label, name="count_aggregated_initial", actual=agg_count)

    # --- Schema introspection -------------------------------------------
    schema = await provider.discover_schema()
    # Schema can include sample property keys / counts that drift between
    # runs; capture a stable subset.
    schema_subset = {
        "labels": sorted(schema.get("labels") or []),
        "edgeTypes": sorted(
            schema.get("edgeTypes")
            or schema.get("relationshipTypes")
            or []
        ),
    }
    assert_snapshot(provider=snapshot_label, name="discover_schema_subset", actual=schema_subset)

    # --- Stats ----------------------------------------------------------
    stats = await provider.get_stats()
    # Provider field varies; only pin the counts.
    pinned_stats = {
        "nodeCount": int(stats.get("nodeCount") or 0),
        "edgeCount": int(stats.get("edgeCount") or 0),
    }
    assert_snapshot(provider=snapshot_label, name="get_stats", actual=pinned_stats)
