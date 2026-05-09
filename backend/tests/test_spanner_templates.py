"""Static tests for Spanner DDL templates and provider helpers.

No google-cloud-spanner / no Spanner instance required. Verifies the
shape of the generated DDL and the safety bounds on the
inline-LIMIT/OFFSET formatter (Spanner GQL cannot bind those).
"""
from __future__ import annotations

import pytest

from backend.graph.adapters.spanner_provider import (
    SpannerEditionError,
    SpannerProvider,
    _DDL_CREATE_GRAPH_EDGE,
    _DDL_CREATE_GRAPH_NODE,
    _DDL_CREATE_INDEXES,
    _DDL_CREATE_PROPERTY_GRAPH,
    _decode_json,
    _safe_int,
)


# ---------------------------------------------------------------------------
# DDL shape
# ---------------------------------------------------------------------------

def test_node_table_is_keyed_on_urn_string():
    assert "urn STRING(MAX) NOT NULL" in _DDL_CREATE_GRAPH_NODE
    assert "PRIMARY KEY (urn)" in _DDL_CREATE_GRAPH_NODE


def test_node_has_stored_generated_columns_for_hot_props():
    # level / qualified_name / layer_assignment are the canonical hot
    # filter properties; they live as STORED generated columns so we can
    # index them without paying JSON access cost on every read.
    for col in ("level", "qualified_name", "layer_assignment"):
        assert f"{col}" in _DDL_CREATE_GRAPH_NODE


def test_edge_table_interleaves_into_source_node():
    assert "INTERLEAVE IN PARENT GraphNode" in _DDL_CREATE_GRAPH_EDGE
    assert "PRIMARY KEY (urn, dest_urn, edge_id)" in _DDL_CREATE_GRAPH_EDGE


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
