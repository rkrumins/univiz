"""Neo4j contract test — pins every ABC method's response shape.

Run before Phase C (Neo4j reshape onto the shared base):

    UPDATE_PROVIDER_SNAPSHOTS=1 \\
        NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=test \\
        pytest backend/tests/regression/test_neo4j_provider_contract.py -v
"""
from __future__ import annotations

import os
import socket

import pytest
import pytest_asyncio

from . import _runner


def _neo4j_reachable() -> bool:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    # Strip scheme; bolt[+s] both share host:port semantics.
    if "://" in uri:
        uri = uri.split("://", 1)[1]
    host, _, port = uri.partition(":")
    if not port:
        port = "7687"
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


skip_if_no_neo4j = pytest.mark.skipif(
    not _neo4j_reachable(),
    reason="Neo4j not reachable on $NEO4J_URI (default bolt://localhost:7687)",
)


@pytest_asyncio.fixture
async def provider():
    from backend.graph.adapters.neo4j_provider import Neo4jProvider

    p = Neo4jProvider(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        username=os.getenv("NEO4J_USERNAME", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "test"),
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )
    # Clean slate for the regression namespace.
    try:
        await p._run_write(
            "MATCH (n) WHERE n.urn STARTS WITH 'urn:test:' DETACH DELETE n",
            {},
        )
    except Exception:
        pass
    await _runner.seed(p)
    yield p
    try:
        await p._run_write(
            "MATCH (n) WHERE n.urn STARTS WITH 'urn:test:' DETACH DELETE n",
            {},
        )
    except Exception:
        pass
    await p.close()


@skip_if_no_neo4j
@pytest.mark.asyncio
async def test_neo4j_provider_contract(provider):
    await _runner.run_all(provider, snapshot_label="neo4j")
