"""Static tests for Spanner DDL templates and provider helpers.

No google-cloud-spanner / no Spanner instance required. Verifies the
shape of the generated DDL and the safety bounds on the
inline-LIMIT/OFFSET formatter (Spanner GQL cannot bind those).
"""
from __future__ import annotations

import pytest

from backend.common.interfaces.preflight import PreflightResult
from backend.graph.adapters.spanner_provider import (
    SpannerEditionError,
    SpannerProvider,
    _DDL_CREATE_CONTRIBUTION_INDEXES,
    _DDL_CREATE_GRAPH_EDGE,
    _DDL_CREATE_GRAPH_EDGE_CONTRIBUTION,
    _DDL_CREATE_GRAPH_NODE,
    _DDL_CREATE_INDEXES,
    _DDL_CREATE_PROPERTY_GRAPH,
    _agg_edge_id,
    _ancestor_pairs_for_leaf,
    _decode_json,
    _safe_int,
)


# ---------------------------------------------------------------------------
# DDL shape
# ---------------------------------------------------------------------------

def test_node_table_is_keyed_on_shard_urn_pair():
    """v2 schema: leading ``shard`` column to break URN-prefix
    hotspots (audit B8). The unique secondary index on ``urn`` (asserted
    in test_phase3_spanner_owned_schema.py) preserves single-key-seek
    semantics for WHERE urn = @u lookups."""
    assert "urn STRING(MAX) NOT NULL" in _DDL_CREATE_GRAPH_NODE
    assert "shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED" in _DDL_CREATE_GRAPH_NODE
    assert "PRIMARY KEY (shard, urn)" in _DDL_CREATE_GRAPH_NODE


def test_node_has_stored_generated_columns_for_hot_props():
    # level / qualified_name / layer_assignment are the canonical hot
    # filter properties; they live as STORED generated columns so we can
    # index them without paying JSON access cost on every read.
    for col in ("level", "qualified_name", "layer_assignment"):
        assert f"{col}" in _DDL_CREATE_GRAPH_NODE


def test_edge_table_interleaves_into_source_node():
    """v2 schema: child PK must start with parent PK ``(shard, urn)``
    for INTERLEAVE to be valid Spanner DDL."""
    assert "INTERLEAVE IN PARENT GraphNode" in _DDL_CREATE_GRAPH_EDGE
    assert "PRIMARY KEY (shard, urn, dest_urn, edge_id)" in _DDL_CREATE_GRAPH_EDGE


def test_indexes_include_reverse_traversal_index():
    # R_EDGE on (dest_urn, urn, edge_id) supports incoming-edge traversal
    # which is the dominant trace-orchestrator access pattern.
    assert any("R_EDGE" in ddl and "(dest_urn, urn, edge_id)" in ddl for ddl in _DDL_CREATE_INDEXES)


def test_property_graph_uses_dynamic_label_and_dynamic_properties():
    ddl = _DDL_CREATE_PROPERTY_GRAPH("UniViz")
    assert "CREATE PROPERTY GRAPH UniViz" in ddl
    assert "DYNAMIC LABEL (label)" in ddl
    assert "DYNAMIC PROPERTIES (properties)" in ddl
    # Static "Entity" / "EntityEdge" labels mean every node/edge has a
    # stable label for schema-agnostic GQL patterns regardless of dynamic type.
    assert "LABEL Entity" in ddl
    assert "LABEL EntityEdge" in ddl


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, 100),                # default
    ("", 100),                  # default
    (-1, 100),                  # negative -> default
    ("999999", 1000),           # clamped to max
    (50, 50),                   # passthrough
    ("abc", 100),               # garbage -> default
])
def test_safe_int_clamps_and_validates(value, expected):
    assert _safe_int(value, default=100, max_value=1000) == expected


def test_decode_json_handles_string_dict_bytes_none():
    assert _decode_json(None) is None
    assert _decode_json('{"a": 1}') == {"a": 1}
    assert _decode_json({"a": 1}) == {"a": 1}
    assert _decode_json(b'{"x": "y"}') == {"x": "y"}
    assert _decode_json("not-json") is None  # graceful fallback


# ---------------------------------------------------------------------------
# Construction validates required identifiers
# ---------------------------------------------------------------------------

def test_constructor_rejects_missing_identifiers():
    with pytest.raises(ValueError):
        SpannerProvider(
            project_id="", instance_id="i", database_id="d",
        )
    with pytest.raises(ValueError):
        SpannerProvider(
            project_id="p", instance_id="", database_id="d",
        )
    with pytest.raises(ValueError):
        SpannerProvider(
            project_id="p", instance_id="i", database_id="",
        )


def test_constructor_accepts_emulator_mode_without_credentials():
    p = SpannerProvider(
        project_id="test-project",
        instance_id="test-instance",
        database_id="test-db",
        use_emulator=True,
    )
    assert p.name == "spanner"


# ---------------------------------------------------------------------------
# Edition error type
# ---------------------------------------------------------------------------

def test_edition_error_is_runtime_error_subclass():
    # The wizard catches RuntimeError generically; SpannerEditionError must
    # subclass it so the existing error funnel renders the message.
    assert issubclass(SpannerEditionError, RuntimeError)


# ---------------------------------------------------------------------------
# Sidecar bookkeeping (Phase I.2)
# ---------------------------------------------------------------------------

def test_sidecar_table_uses_shard_prefixed_primary_key():
    """v2 schema: ``source_shard`` leads the PK so contribution rows
    distribute across splits even when a single source URN namespace
    dominates ingest. Audit B8."""
    ddl = _DDL_CREATE_GRAPH_EDGE_CONTRIBUTION
    assert "CREATE TABLE GraphEdgeContribution" in ddl
    assert "source_shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(source_urn), 256)) STORED" in ddl
    assert "PRIMARY KEY (source_shard, source_urn, target_urn, contributor_id)" in ddl
    # No interleave: contribution rows live independently of GraphNode
    # so a contributor edge being deleted does not cascade-delete the
    # AGGREGATED contribution row out from under the materialiser.
    assert "INTERLEAVE" not in ddl


def test_sidecar_indexes_cover_pair_lookup_and_contributor_lookup():
    by_pair = any("IDX_CONTRIB_BY_PAIR" in d for d in _DDL_CREATE_CONTRIBUTION_INDEXES)
    by_contributor = any(
        "IDX_CONTRIB_BY_CONTRIBUTOR" in d for d in _DDL_CREATE_CONTRIBUTION_INDEXES
    )
    assert by_pair and by_contributor


def test_agg_edge_id_is_deterministic():
    # Stable encoding so re-materialisation finds the existing row.
    assert _agg_edge_id("a", "b") == "agg:a|b"
    assert _agg_edge_id("urn:x", "urn:y") == "agg:urn:x|urn:y"


def test_ancestor_pairs_for_leaf_excludes_self_loops_and_dedupes():
    # Domain shared on both sides — the (domain, domain) pair must drop.
    pairs = _ancestor_pairs_for_leaf(
        ["leaf1", "schema1", "domain"],
        ["leaf2", "schema2", "domain"],
        "leaf1", "leaf2",
    )
    assert ("domain", "domain") not in pairs
    # Cross-product cardinality minus the one self-loop.
    assert len(pairs) == 3 * 3 - 1


def test_ancestor_pairs_for_leaf_includes_endpoints_when_chains_empty():
    # If the ancestor cache returns empty (e.g. fresh database), the
    # leaf endpoints themselves form the only AGGREGATED pair so the
    # materialiser still produces a usable AGGREGATED edge.
    pairs = _ancestor_pairs_for_leaf([], [], "leaf1", "leaf2")
    assert pairs == [("leaf1", "leaf2")]


# ---------------------------------------------------------------------------
# Preflight contract (Phase I.1)
# ---------------------------------------------------------------------------

def test_preflight_returns_canonical_result_type_annotation():
    import typing

    # ``from __future__ import annotations`` defers evaluation, so we
    # use get_type_hints to resolve the string to the actual class.
    hints = typing.get_type_hints(SpannerProvider.preflight)
    assert hints.get("return") is PreflightResult
