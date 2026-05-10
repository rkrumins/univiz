"""Phase 6 verification tests — canonical-shape loader CLI.

Pins the contract that proves the loader's read+validate path before
any I/O hits a provider. The push path (against a stubbed provider) is
also exercised so the chunking + per-batch save_custom_graph contract
is testable without a real Spanner.

    P6.1   Loader resolves the small fixture and reports canonical
           counts (nodes/edges and per-type breakdowns).
    P6.2   Loader rejects a JSON file whose entries don't conform to
           GraphNode / GraphEdge — Pydantic validation runs BEFORE any
           provider connection is opened.
    P6.3   Loader's push path chunks nodes + edges via save_custom_graph
           in --batch-size groups; each batch is one provider call.
    P6.4   --dry-run skips the provider entirely.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.common.models.graph import GraphEdge, GraphNode
from backend.scripts.load_test_dataset import (
    _build_parser,
    _chunked,
    _load_json_path,
    _load_small_fixture,
    _push,
    _resolve_fixture,
)


# ──────────────────────────────────────────────────────────────────
# P6.1 — Fixture resolution
# ──────────────────────────────────────────────────────────────────


def test_small_fixture_loads_canonical_shape():
    nodes, edges = _load_small_fixture()
    # The shared regression fixture: 6 nodes, 7 edges, 3 entity types,
    # 2 edge types. If this drifts, the contract baseline drifts too.
    assert len(nodes) == 6
    assert len(edges) == 7
    assert {n.entity_type for n in nodes} == {"domain", "schema", "dataset"}
    assert {e.edge_type for e in edges} == {"CONTAINS", "DERIVES_FROM"}


def test_resolve_fixture_small():
    nodes, edges = _resolve_fixture("small")
    assert len(nodes) == 6 and len(edges) == 7


def test_resolve_fixture_unknown_raises():
    with pytest.raises(SystemExit) as exc:
        _resolve_fixture("unknown")
    assert "unknown" in str(exc.value)


# ──────────────────────────────────────────────────────────────────
# P6.2 — JSON validation (canonical shape)
# ──────────────────────────────────────────────────────────────────


def test_load_json_path_accepts_canonical_shape(tmp_path: Path):
    data = {
        "nodes": [
            {"urn": "urn:x:n1", "entityType": "test", "displayName": "N1"},
            {"urn": "urn:x:n2", "entityType": "test", "displayName": "N2"},
        ],
        "edges": [
            {
                "id": "e1",
                "sourceUrn": "urn:x:n1",
                "targetUrn": "urn:x:n2",
                "edgeType": "RELATES_TO",
            }
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(data))
    nodes, edges = _load_json_path(p)
    assert len(nodes) == 2 and len(edges) == 1
    assert nodes[0].urn == "urn:x:n1"
    assert edges[0].source_urn == "urn:x:n1"


def test_load_json_path_rejects_missing_top_level(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text(json.dumps({"only_nodes": []}))
    with pytest.raises(SystemExit) as exc:
        _load_json_path(p)
    assert "expected top-level object with 'nodes' and 'edges'" in str(exc.value)


def test_load_json_path_rejects_invalid_node_shape(tmp_path: Path):
    """Pydantic validation must run before any provider I/O so a
    malformed export doesn't waste a multi-minute push."""
    p = tmp_path / "bad_node.json"
    p.write_text(json.dumps({
        "nodes": [{"urn": "urn:x"}],  # missing required entityType + displayName
        "edges": [],
    }))
    with pytest.raises(SystemExit) as exc:
        _load_json_path(p)
    assert "canonical-shape validation" in str(exc.value)


def test_load_json_path_rejects_invalid_edge_shape(tmp_path: Path):
    p = tmp_path / "bad_edge.json"
    p.write_text(json.dumps({
        "nodes": [],
        "edges": [{"id": "e1"}],  # missing sourceUrn/targetUrn/edgeType
    }))
    with pytest.raises(SystemExit) as exc:
        _load_json_path(p)
    assert "canonical-shape validation" in str(exc.value)


def test_load_json_path_missing_file(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        _load_json_path(tmp_path / "nope.json")
    assert "fixture path not found" in str(exc.value)


# ──────────────────────────────────────────────────────────────────
# P6.3 — Push chunking
# ──────────────────────────────────────────────────────────────────


def test_chunked_basic():
    chunks = list(_chunked(list(range(2500)), 1000))
    assert len(chunks) == 3
    assert sum(len(c) for c in chunks) == 2500


class _StubProvider:
    """Provider stub that records every save_custom_graph call so the
    test can assert on chunking shape."""

    def __init__(self) -> None:
        self.calls: List[Tuple[List[GraphNode], List[GraphEdge]]] = []

    def set_containment_edge_types(self, types, *, from_ontology=True) -> None:
        # The loader probes for this method; accept and forget.
        self._types = list(types)

    async def save_custom_graph(
        self, nodes: List[GraphNode], edges: List[GraphEdge],
    ) -> bool:
        self.calls.append((list(nodes), list(edges)))
        return True

    async def close(self) -> None:
        pass


async def test_push_chunks_nodes_and_edges_separately():
    p = _StubProvider()
    nodes = [
        GraphNode(urn=f"urn:n:{i}", entityType="t", displayName=f"N{i}")
        for i in range(2500)
    ]
    edges = [
        GraphEdge(
            id=f"e{i}",
            sourceUrn=f"urn:n:{i}",
            targetUrn=f"urn:n:{i + 1}",
            edgeType="X",
        )
        for i in range(2499)
    ]

    result = await _push(p, nodes, edges, batch_size=1000)

    # 3 node chunks (1000, 1000, 500) + 3 edge chunks (1000, 1000, 499)
    # = 6 calls. Nodes go first (in 3 calls with edges=[]), then edges
    # (in 3 calls with nodes=[]).
    assert len(p.calls) == 6
    node_calls = p.calls[:3]
    edge_calls = p.calls[3:]
    assert [len(ns) for ns, _ in node_calls] == [1000, 1000, 500]
    assert all(es == [] for _, es in node_calls)
    assert [len(es) for _, es in edge_calls] == [1000, 1000, 499]
    assert all(ns == [] for ns, _ in edge_calls)
    assert result["nodes"] == 2500 and result["edges"] == 2499
    assert result["batch_size"] == 1000


async def test_push_handles_empty_edges():
    p = _StubProvider()
    nodes = [GraphNode(urn="urn:n:0", entityType="t", displayName="N0")]
    result = await _push(p, nodes, [], batch_size=10)
    # 1 node call; no edge calls (the for-loop over [] doesn't fire).
    assert len(p.calls) == 1
    assert p.calls[0] == (nodes, [])
    assert result["edges"] == 0


# ──────────────────────────────────────────────────────────────────
# P6.4 — --dry-run skips provider; CLI parses correctly
# ──────────────────────────────────────────────────────────────────


def test_parser_requires_fixture_or_path():
    """At least one source must be provided. argparse flags an error
    code 2 if neither is given."""
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--provider-id=p1"])
    assert exc.value.code == 2


def test_parser_fixture_and_path_mutually_exclusive():
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([
            "--fixture=small", "--fixture-path=/tmp/x.json", "--dry-run",
        ])
    assert exc.value.code == 2


def test_parser_default_batch_size_1000():
    parser = _build_parser()
    args = parser.parse_args(["--fixture=small", "--dry-run"])
    assert args.batch_size == 1000


def test_parser_dry_run_does_not_require_provider_id():
    parser = _build_parser()
    args = parser.parse_args(["--fixture=small", "--dry-run"])
    assert args.dry_run is True
    assert args.provider_id is None
