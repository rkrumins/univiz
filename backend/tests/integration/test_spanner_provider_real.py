"""Integration tests for SpannerProvider against a real Spanner Enterprise instance.

The cloud-spanner-emulator does not implement Spanner Graph (GQL,
CREATE PROPERTY GRAPH). These tests cover the GQL surface and require
a real Enterprise-edition Spanner database.

Run with::

    SPANNER_TEST_INSTANCE=projects/<proj>/instances/<inst>/databases/<db> \
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json \
    pytest backend/tests/integration/test_spanner_provider_real.py -v

The test database is left in place after the run (no auto-drop) so a
human can inspect on failure.
"""
from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio

from backend.common.models.graph import GraphEdge, GraphNode


_INSTANCE_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/instances/(?P<instance>[^/]+)/databases/(?P<database>[^/]+)$"
)


def _real_instance_configured() -> bool:
    raw = os.getenv("SPANNER_TEST_INSTANCE", "")
    return bool(raw and _INSTANCE_RE.match(raw))


skip_if_no_real_spanner = pytest.mark.skipif(
    not _real_instance_configured(),
    reason="Set SPANNER_TEST_INSTANCE=projects/<p>/instances/<i>/databases/<d>",
)


@pytest_asyncio.fixture
async def spanner_provider():
    if not _real_instance_configured():
        pytest.skip("real Spanner not configured")

    from backend.graph.adapters.spanner_provider import SpannerProvider

    m = _INSTANCE_RE.match(os.environ["SPANNER_TEST_INSTANCE"])
    assert m is not None
    provider = SpannerProvider(
        project_id=m.group("project"),
        instance_id=m.group("instance"),
        database_id=m.group("database"),
        graph_name=os.getenv("SPANNER_TEST_GRAPH_NAME", "UniViz_Test"),
    )
    await provider._ensure_connected()
    yield provider
    await provider.close()


# ---------------------------------------------------------------------------
# Smoke tests covering the GQL surface
# ---------------------------------------------------------------------------

@skip_if_no_real_spanner
@pytest.mark.asyncio
async def test_get_node_via_gql(spanner_provider):
    nodes = [
        GraphNode(urn="urn:test:domain:a", entityType="domain", displayName="A"),
    ]
    await spanner_provider.save_custom_graph(nodes, [])
    n = await spanner_provider.get_node("urn:test:domain:a")
    assert n is not None
    assert n.entity_type == "domain"


@skip_if_no_real_spanner
@pytest.mark.asyncio
async def test_get_children_with_containment_types(spanner_provider):
    nodes = [
        GraphNode(urn="urn:test:domain:a", entityType="domain", displayName="A"),
        GraphNode(urn="urn:test:dataset:1", entityType="dataset", displayName="One"),
    ]
    edges = [
        GraphEdge(
            id="e1", sourceUrn="urn:test:domain:a",
            targetUrn="urn:test:dataset:1", edgeType="CONTAINS",
        ),
    ]
    spanner_provider.set_containment_edge_types(["CONTAINS"])
    await spanner_provider.save_custom_graph(nodes, edges)

    children = await spanner_provider.get_children("urn:test:domain:a")
    assert any(c.urn == "urn:test:dataset:1" for c in children)


@skip_if_no_real_spanner
@pytest.mark.asyncio
async def test_get_full_lineage_via_quantified_path(spanner_provider):
    nodes = [
        GraphNode(urn="urn:test:ds:1", entityType="dataset", displayName="1"),
        GraphNode(urn="urn:test:ds:2", entityType="dataset", displayName="2"),
        GraphNode(urn="urn:test:ds:3", entityType="dataset", displayName="3"),
    ]
    edges = [
        GraphEdge(id="e12", sourceUrn="urn:test:ds:1", targetUrn="urn:test:ds:2", edgeType="DERIVES_FROM"),
        GraphEdge(id="e23", sourceUrn="urn:test:ds:2", targetUrn="urn:test:ds:3", edgeType="DERIVES_FROM"),
    ]
    spanner_provider.set_resolved_edge_metadata({}, ["DERIVES_FROM"])
    await spanner_provider.save_custom_graph(nodes, edges)

    res = await spanner_provider.get_full_lineage(
        "urn:test:ds:1", upstream_depth=0, downstream_depth=3,
    )
    found = {n.urn for n in res.nodes}
    assert "urn:test:ds:2" in found
    assert "urn:test:ds:3" in found


@skip_if_no_real_spanner
@pytest.mark.asyncio
async def test_aggregated_materialize_then_count(spanner_provider):
    # Set up a hierarchy: schemaA contains ds1, schemaB contains ds2.
    nodes = [
        GraphNode(urn="urn:test:schema:A", entityType="schema", displayName="A"),
        GraphNode(urn="urn:test:schema:B", entityType="schema", displayName="B"),
        GraphNode(urn="urn:test:ds:1", entityType="dataset", displayName="1"),
        GraphNode(urn="urn:test:ds:2", entityType="dataset", displayName="2"),
    ]
    edges = [
        GraphEdge(id="cAd1", sourceUrn="urn:test:schema:A", targetUrn="urn:test:ds:1", edgeType="CONTAINS"),
        GraphEdge(id="cBd2", sourceUrn="urn:test:schema:B", targetUrn="urn:test:ds:2", edgeType="CONTAINS"),
    ]
    spanner_provider.set_containment_edge_types(["CONTAINS"])
    await spanner_provider.save_custom_graph(nodes, edges)

    before = await spanner_provider.count_aggregated_edges()
    await spanner_provider.on_lineage_edge_written(
        "urn:test:ds:1", "urn:test:ds:2", "lineage1", "DERIVES_FROM",
    )
    after = await spanner_provider.count_aggregated_edges()
    assert after >= before + 1
