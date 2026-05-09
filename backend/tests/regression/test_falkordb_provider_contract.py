"""FalkorDB contract test — pins every ABC method's response shape.

Run before Phase B (FalkorDB reshape onto the shared base):

    # Capture baseline (against the existing FalkorDB provider).
    UPDATE_PROVIDER_SNAPSHOTS=1 \\
        FALKORDB_HOST=localhost FALKORDB_PORT=6379 \\
        pytest backend/tests/regression/test_falkordb_provider_contract.py -v

    # During the reshape, run without UPDATE_PROVIDER_SNAPSHOTS:
    pytest backend/tests/regression/test_falkordb_provider_contract.py -v

A diff means the reshape has changed externally observable behaviour —
fix the reshape, not the snapshot.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from . import _runner


def _falkordb_available() -> bool:
    try:
        import falkordb  # noqa: F401
        import redis
        r = redis.Redis(
            host=os.getenv("FALKORDB_HOST", "localhost"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
            socket_connect_timeout=0.5,
        )
        r.ping()
        r.close()
        return True
    except Exception:
        return False


skip_if_no_falkordb = pytest.mark.skipif(
    not _falkordb_available(),
    reason=(
        "FalkorDB not reachable. Start: docker run -p 6379:6379 falkordb/falkordb"
    ),
)


@pytest_asyncio.fixture
async def provider():
    from backend.app.providers.falkordb_provider import FalkorDBProvider

    graph_name = f"test_regression_{os.getpid()}"
    p = FalkorDBProvider(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        graph_name=graph_name,
    )
    await p._ensure_connected()
    # Best-effort clean slate: drop the test graph if a previous run left it.
    try:
        await p._graph.delete()
    except Exception:
        pass
    await _runner.seed(p)
    yield p
    try:
        await p._graph.delete()
    except Exception:
        pass
    await p.close()


@skip_if_no_falkordb
@pytest.mark.asyncio
async def test_falkordb_provider_contract(provider):
    await _runner.run_all(provider, snapshot_label="falkordb")
