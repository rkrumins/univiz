"""Integration tests for SpannerProvider against the cloud-spanner-emulator.

The emulator does NOT support Spanner Graph (CREATE PROPERTY GRAPH /
GQL) as of Dec 2025. These tests exclusively exercise the SQL substrate:
schema bootstrap (tables + indexes), mutation API ingestion, single-node
SQL reads, INFORMATION_SCHEMA discovery, and AGGREGATED edge counting
via SQL.

GQL paths (get_node, get_children, trace_at_level, expand_aggregated)
are covered in ``test_spanner_provider_real.py``, gated on the
``SPANNER_TEST_INSTANCE`` env var.

Run prerequisites
-----------------
    docker compose -f docker-compose.test.yml up -d spanner-emulator
    SPANNER_EMULATOR_HOST=localhost:9010 \
        gcloud spanner instances create test-instance \
            --config=emulator-config --nodes=1 --description=test

Or bootstrap via the helper at the bottom of this file.
"""
from __future__ import annotations

import os
import socket

import pytest
import pytest_asyncio

from backend.common.models.graph import GraphEdge, GraphNode


# ---------------------------------------------------------------------------
# Reachability gates
# ---------------------------------------------------------------------------

def _emulator_reachable() -> bool:
    """Return True iff the cloud-spanner-emulator is listening on localhost:9010."""
    host = os.getenv("SPANNER_EMULATOR_HOST", "localhost:9010")
    try:
        h, p = host.split(":")
        with socket.create_connection((h, int(p)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _spanner_client_installed() -> bool:
    try:
        import google.cloud.spanner  # noqa: F401
        return True
    except ImportError:
        return False


skip_if_no_emulator = pytest.mark.skipif(
    not _spanner_client_installed() or not _emulator_reachable(),
    reason=(
        "Spanner emulator not running or google-cloud-spanner not installed. "
        "Start: docker compose -f docker-compose.test.yml up -d spanner-emulator "
        "&& pip install google-cloud-spanner"
    ),
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def spanner_provider():
    if not _emulator_reachable() or not _spanner_client_installed():
        pytest.skip("emulator not running")

    # Late imports so the test module is collectable in environments
    # without google-cloud-spanner.
    from google.cloud import spanner_admin_instance_v1, spanner_admin_database_v1
    from backend.graph.adapters.spanner_provider import SpannerProvider

    os.environ["SPANNER_EMULATOR_HOST"] = os.getenv("SPANNER_EMULATOR_HOST", "localhost:9010")

    project_id = "test-project"
    instance_id = "test-instance"
    database_id = f"test_db_{os.getpid()}"

    # Provision instance + database via admin clients.
    inst_client = spanner_admin_instance_v1.InstanceAdminClient()
    db_client = spanner_admin_database_v1.DatabaseAdminClient()

    parent = f"projects/{project_id}"
    try:
        inst_client.get_instance(name=f"{parent}/instances/{instance_id}")
    except Exception:
        op = inst_client.create_instance(
            parent=parent, instance_id=instance_id,
            instance={
                "config": f"projects/{project_id}/instanceConfigs/emulator-config",
                "display_name": "test", "node_count": 1,
            },
        )
        op.result(timeout=30)

    db_full = f"{parent}/instances/{instance_id}/databases/{database_id}"
    try:
        db_client.get_database(name=db_full)
    except Exception:
        op = db_client.create_database(
            parent=f"{parent}/instances/{instance_id}",
            create_statement=f"CREATE DATABASE `{database_id}`",
        )
        op.result(timeout=30)

    provider = SpannerProvider(
        project_id=project_id, instance_id=instance_id,
        database_id=database_id, graph_name="UniViz",
        use_emulator=True,
    )
    # Bypass property-graph DDL on the emulator (it's unsupported).
    # ``_ensure_connected`` runs ``_bootstrap_property_graph`` which will raise;
    # we tolerate that exception and proceed with SQL-only tests.
    try:
        await provider._ensure_connected()
    except Exception as exc:
        # Expected on the emulator: CREATE PROPERTY GRAPH is unsupported.
        # Tables and indexes were created by ``_bootstrap_tables`` first;
        # mark connected so subsequent SQL paths work.
        if "property" in str(exc).lower() or "graph" in str(exc).lower():
            provider._connected = True
            provider._schema_bootstrapped = True
        else:
            raise

    yield provider

    await provider.close()
    # Drop the database so the next test gets a clean slate.
    try:
        db_client.drop_database(database=db_full)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests (SQL-only — emulator-safe)
# ---------------------------------------------------------------------------

@skip_if_no_emulator
@pytest.mark.asyncio
async def test_schema_bootstrap_creates_tables_and_indexes(spanner_provider):
    rows = await spanner_provider._execute_sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = '' AND table_name IN ('GraphNode', 'GraphEdge')"
    )
    names = {r["table_name"] for r in rows}
    assert {"GraphNode", "GraphEdge"} <= names

    idx_rows = await spanner_provider._execute_sql(
        "SELECT index_name FROM information_schema.indexes "
        "WHERE table_schema = '' AND table_name IN ('GraphNode', 'GraphEdge')"
    )
    idx_names = {r["index_name"] for r in idx_rows}
    assert "R_EDGE" in idx_names
    assert "IDX_NODE_LABEL" in idx_names


@skip_if_no_emulator
@pytest.mark.asyncio
async def test_save_custom_graph_then_get_nodes_batch(spanner_provider):
    nodes = [
        GraphNode(urn="urn:test:domain:a", entityType="domain", displayName="A"),
        GraphNode(urn="urn:test:dataset:1", entityType="dataset", displayName="One"),
    ]
    edges = [
        GraphEdge(id="e1", sourceUrn="urn:test:domain:a", targetUrn="urn:test:dataset:1", edgeType="CONTAINS"),
    ]
    await spanner_provider.save_custom_graph(nodes, edges)

    fetched = await spanner_provider.get_nodes_batch(["urn:test:domain:a", "urn:test:dataset:1"])
    by_urn = {n.urn: n for n in fetched}
    assert by_urn["urn:test:domain:a"].entity_type == "domain"
    assert by_urn["urn:test:dataset:1"].display_name == "One"


@skip_if_no_emulator
@pytest.mark.asyncio
async def test_count_aggregated_edges_returns_zero_initially(spanner_provider):
    n = await spanner_provider.count_aggregated_edges()
    assert n == 0


@skip_if_no_emulator
@pytest.mark.asyncio
async def test_get_stats_reports_node_and_edge_counts(spanner_provider):
    nodes = [GraphNode(urn=f"urn:test:n:{i}", entityType="t", displayName=f"N{i}") for i in range(3)]
    await spanner_provider.save_custom_graph(nodes, [])
    stats = await spanner_provider.get_stats()
    assert stats["nodeCount"] >= 3
    assert stats["provider"] == "spanner"


@skip_if_no_emulator
@pytest.mark.asyncio
async def test_introspector_returns_distinct_labels(spanner_provider):
    nodes = [
        GraphNode(urn="urn:test:n1", entityType="dataset", displayName="x"),
        GraphNode(urn="urn:test:n2", entityType="pipeline", displayName="y"),
    ]
    await spanner_provider.save_custom_graph(nodes, [])
    labels = await spanner_provider._introspector.labels()
    assert "dataset" in labels
    assert "pipeline" in labels
