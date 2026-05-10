"""Spanner contract test — pins every ABC method's response shape.

Mirrors ``test_falkordb_provider_contract.py`` and
``test_neo4j_provider_contract.py``. Runs the shared ``_runner.run_all``
harness against a live Spanner Enterprise database and snapshots the
output to ``backend/tests/regression/snapshots/spanner_owned/``.

This test only covers **Mode A — owned schema** (canonical
``GraphNode`` / ``GraphEdge`` shape, platform-bootstrapped tables).
**Mode B — customer-supplied schema with ``SchemaMapping``** is added
when the provider's Phase 5 wiring lands (separate fixture, separate
snapshot directory).

Run::

    # Capture baseline (one-time, against a real Enterprise instance):
    UPDATE_PROVIDER_SNAPSHOTS=1 \\
        SPANNER_TEST_INSTANCE=projects/<p>/instances/<i>/databases/<d> \\
        SPANNER_TEST_GRAPH=UniViz \\
        SPANNER_TEST_CREDENTIALS_JSON=path/to/sa.json \\
        pytest backend/tests/regression/test_spanner_provider_contract.py -v

    # Subsequent runs (no UPDATE_PROVIDER_SNAPSHOTS):
    SPANNER_TEST_INSTANCE=... \\
        pytest backend/tests/regression/test_spanner_provider_contract.py -v

A diff means an externally observable behaviour change. Fix the code,
not the snapshot — unless the change is intentional, in which case
re-capture the baseline and code-review the snapshot diff.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

from . import _runner


# ───────────────────────────────────────────────────────────────────
# Real-Spanner gate
# ───────────────────────────────────────────────────────────────────
#
# Spanner Enterprise is required because the contract test exercises
# GQL methods (get_node, get_children, trace_at_level, ...) that the
# cloud-spanner-emulator does not implement. The gate is a single env
# var; without it, the test is silently skipped so a developer's
# pytest run doesn't fail just because they don't have a Spanner
# instance handy.

_DB_PATH_RE = re.compile(
    r"^projects/[a-z][-a-z0-9]{4,28}[a-z0-9]/instances/[A-Za-z0-9_-]+"
    r"/databases/[A-Za-z0-9_-]+$"
)


def _real_spanner_target() -> Optional[str]:
    raw = os.getenv("SPANNER_TEST_INSTANCE", "").strip()
    if not raw:
        return None
    if not _DB_PATH_RE.match(raw):
        # Fail fast rather than silently skip when the env is set but
        # malformed — a typo here in CI would otherwise look like green.
        raise pytest.UsageError(
            f"SPANNER_TEST_INSTANCE={raw!r} doesn't match "
            "projects/<p>/instances/<i>/databases/<d>"
        )
    return raw


skip_if_no_real_spanner = pytest.mark.skipif(
    _real_spanner_target() is None,
    reason=(
        "Real Spanner not configured. Set SPANNER_TEST_INSTANCE to "
        "projects/<p>/instances/<i>/databases/<d> (Enterprise edition "
        "required — emulator does not support GQL)."
    ),
)


# ───────────────────────────────────────────────────────────────────
# Fixture — fresh provider against a real Spanner database
# ───────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def provider():
    from backend.graph.adapters.spanner_provider import SpannerProvider

    target = _real_spanner_target()
    assert target, "skip marker should have prevented this"
    # Parse the projects/.../databases/... path back into the three IDs.
    parts = target.split("/")
    project_id, instance_id, database_id = parts[1], parts[3], parts[5]

    graph_name = os.getenv("SPANNER_TEST_GRAPH", "UniViz")
    creds_path = os.getenv("SPANNER_TEST_CREDENTIALS_JSON")
    creds_json = None
    if creds_path:
        p = Path(creds_path)
        if not p.is_file():
            raise pytest.UsageError(
                f"SPANNER_TEST_CREDENTIALS_JSON={creds_path!r} not found"
            )
        creds_json = p.read_text()

    p = SpannerProvider(
        project_id=project_id,
        instance_id=instance_id,
        database_id=database_id,
        graph_name=graph_name,
        credentials_json=creds_json,
        use_emulator=False,
        extra_config={},
    )
    # Ensure schema is bootstrapped (idempotent on Enterprise; recreates
    # the v2 schema first time, no-op afterwards). Phase 1's substrate
    # bounds this with a deadline.
    await p._ensure_connected()

    # Best-effort clean slate. ``purge_aggregated_edges`` covers the
    # AGGREGATED sidecar; the contract test seeds fresh data anyway so
    # collisions are rare. We deliberately do NOT drop+recreate the
    # schema (real Spanner DDL is slow) — instead we tolerate stale
    # rows and let save_custom_graph idempotently INSERT OR UPDATE.
    try:
        await p.purge_aggregated_edges(batch_size=1000)
    except Exception:
        pass

    await _runner.seed(p)
    yield p

    # Cleanup: purge the AGGREGATED sidecar so back-to-back runs don't
    # accumulate test contributions. Schema-level cleanup is left to
    # the operator (the test database is meant to be ephemeral anyway).
    try:
        await p.purge_aggregated_edges(batch_size=1000)
    except Exception:
        pass
    await p.close()


# ───────────────────────────────────────────────────────────────────
# The test itself
# ───────────────────────────────────────────────────────────────────


@skip_if_no_real_spanner
@pytest.mark.asyncio
async def test_spanner_provider_contract_mode_a(provider):
    """Mode A — platform-owned canonical schema. Mirrors the FalkorDB
    and Neo4j contract tests; same harness, same fixture, same
    assertions. The snapshot label scopes the baseline to its own
    directory so Mode B (when added) can capture a parallel set."""
    await _runner.run_all(provider, snapshot_label="spanner_owned")
