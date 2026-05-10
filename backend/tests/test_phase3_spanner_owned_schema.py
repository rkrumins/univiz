"""
Phase 3 verification tests — owned-schema rework.

Pins the contract that closes audit BLOCKERs B8 (PK hotspot) + B9 (JSON
cell-size guard) and MAJORs M10 (PROPERTIES ALL COLUMNS bug) + M11
(_AGG_LABEL collision risk).

    P3.1  DDL templates carry the hash-shard PK and a UNIQUE secondary
          index on urn (lookups by urn still hit a single key seek).
    P3.2  Property-graph DDL excludes the JSON ``properties`` column AND
          the synthetic ``shard`` column from the ALL COLUMNS clause —
          stops the JSON bag from being exposed twice and keeps the
          distribution column off the user-visible surface.
    P3.3  ``_AGG_LABEL`` is the lowercase namespaced sentinel; not the
          old ``"AGGREGATED"`` literal that could collide with customer
          ontologies.
    P3.4  ``_safe_json_dumps`` raises ``ProviderInputError`` when the
          encoded payload exceeds the byte cap — surfaces as HTTP 400
          rather than as an atomic-batch failure inside Spanner.
    P3.5  Ontology validator rejects any edge type that case-insensitively
          collides with the AGGREGATED sentinel.
"""
from __future__ import annotations

import json

import pytest

from backend.common.interfaces.provider import (
    ProviderConfigurationError,
    ProviderInputError,
)
from backend.graph.adapters.spanner_provider import (
    SpannerProvider,
    _AGG_LABEL,
    _DDL_CREATE_GRAPH_NODE,
    _DDL_CREATE_GRAPH_EDGE,
    _DDL_CREATE_GRAPH_EDGE_CONTRIBUTION,
    _DDL_CREATE_INDEXES,
    _DDL_CREATE_PROPERTY_GRAPH,
    _SPANNER_JSON_MAX_BYTES,
    _safe_json_dumps,
)


# ──────────────────────────────────────────────────────────────────
# P3.1 — Hash-shard PK + unique secondary index
# ──────────────────────────────────────────────────────────────────


def test_graphnode_ddl_carries_shard_column_and_composite_pk():
    ddl = _DDL_CREATE_GRAPH_NODE
    assert "shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED" in ddl
    assert "PRIMARY KEY (shard, urn)" in ddl


def test_graphedge_ddl_carries_shard_column_and_composite_pk():
    ddl = _DDL_CREATE_GRAPH_EDGE
    assert "shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(urn), 256)) STORED" in ddl
    assert "PRIMARY KEY (shard, urn, dest_urn, edge_id)" in ddl
    # Interleaving must still work: child PK starts with parent PK
    # (shard, urn) — Spanner enforces this prefix relationship.
    assert "INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE" in ddl


def test_graphedgecontribution_ddl_carries_source_shard():
    ddl = _DDL_CREATE_GRAPH_EDGE_CONTRIBUTION
    assert "source_shard INT64 NOT NULL AS (MOD(FARM_FINGERPRINT(source_urn), 256)) STORED" in ddl
    assert "PRIMARY KEY (source_shard, source_urn, target_urn, contributor_id)" in ddl


def test_unique_index_on_urn_supports_property_graph_key():
    """KEY (urn) in CREATE PROPERTY GRAPH requires a uniqueness
    guarantee on urn alone; the secondary unique index is what provides
    it now that the PK is composite (shard, urn). Audit B8."""
    idx_ddl = next((s for s in _DDL_CREATE_INDEXES if "IDX_GraphNode_URN" in s), None)
    assert idx_ddl is not None, "missing unique secondary index on GraphNode.urn"
    assert "UNIQUE INDEX" in idx_ddl
    assert "ON GraphNode (urn)" in idx_ddl


# ──────────────────────────────────────────────────────────────────
# P3.2 — Property-graph DDL EXCEPT clause
# ──────────────────────────────────────────────────────────────────


def test_property_graph_ddl_excludes_properties_and_shard_from_all_columns():
    """LABEL Entity PROPERTIES ALL COLUMNS would expose the JSON
    ``properties`` column twice (typed + dynamic) and surface the
    synthetic ``shard`` column to GQL queries. EXCEPT closes both
    issues. Audit M10."""
    ddl = _DDL_CREATE_PROPERTY_GRAPH("UniViz")
    # NODE side — applied to GraphNode AS Entity
    assert "LABEL Entity PROPERTIES ALL COLUMNS EXCEPT (properties, shard)" in ddl
    # EDGE side — applied to GraphEdge
    assert "LABEL EntityEdge PROPERTIES ALL COLUMNS EXCEPT (properties, shard)" in ddl
    # And we still expose the dynamic JSON bag — that's where the
    # caller-defined keys live.
    assert "DYNAMIC PROPERTIES (properties)" in ddl


def test_property_graph_ddl_uses_validated_graph_name():
    """The DDL emitter doesn't validate by itself — that's __init__'s
    job (Phase 2). This sanity-checks the template just inserts the
    name verbatim, so the validation gate isn't bypassable."""
    ddl = _DDL_CREATE_PROPERTY_GRAPH("MyGraph")
    assert "CREATE PROPERTY GRAPH MyGraph" in ddl


# ──────────────────────────────────────────────────────────────────
# P3.3 — _AGG_LABEL rename
# ──────────────────────────────────────────────────────────────────


def test_agg_label_is_namespaced_sentinel():
    assert _AGG_LABEL == "_synodic_aggregated"
    # Lowercase per Spanner Graph guidance.
    assert _AGG_LABEL == _AGG_LABEL.lower()
    # Underscore-prefixed so any reasonable customer namespace can't
    # legitimately collide.
    assert _AGG_LABEL.startswith("_")


# ──────────────────────────────────────────────────────────────────
# P3.4 — _safe_json_dumps cell-size guard
# ──────────────────────────────────────────────────────────────────


def test_safe_json_dumps_passes_small_payload():
    out = _safe_json_dumps({"a": 1, "b": "x" * 100}, field="t.props", owner_id="urn:1")
    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": "x" * 100}


def test_safe_json_dumps_rejects_oversized_payload():
    huge = {"big": "x" * (_SPANNER_JSON_MAX_BYTES + 1)}
    with pytest.raises(ProviderInputError) as exc:
        _safe_json_dumps(huge, field="GraphNode.properties", owner_id="urn:huge")
    msg = str(exc.value)
    assert "GraphNode.properties" in msg
    assert "urn:huge" in msg
    assert str(_SPANNER_JSON_MAX_BYTES) in msg


def test_safe_json_dumps_uses_compact_separators():
    out = _safe_json_dumps({"a": 1, "b": 2}, field="t", owner_id="x")
    # No spaces between separators — what Spanner stores must be what we measured.
    assert ", " not in out
    assert ": " not in out


def test_safe_json_dumps_threshold_is_8_mib():
    """Sanity-check the default — important because a wrong default
    would either reject reasonable payloads or skip the guard."""
    assert _SPANNER_JSON_MAX_BYTES == 8 * 1024 * 1024


# ──────────────────────────────────────────────────────────────────
# P3.5 — Ontology validator (collision rejection)
# ──────────────────────────────────────────────────────────────────


def _make_provider() -> SpannerProvider:
    return SpannerProvider(
        project_id="p", instance_id="i", database_id="d",
        graph_name="TestGraph",
    )


@pytest.mark.parametrize("collision", [
    "_synodic_aggregated",
    "_SYNODIC_AGGREGATED",
    "_Synodic_Aggregated",
])
def test_ontology_collision_via_containment_rejected(collision):
    p = _make_provider()
    with pytest.raises(ProviderConfigurationError) as exc:
        p.set_containment_edge_types(["CONTAINS", collision])
    msg = str(exc.value)
    assert "AGGREGATED sentinel" in msg
    assert collision in msg


def test_ontology_collision_via_lineage_metadata_rejected():
    p = _make_provider()
    with pytest.raises(ProviderConfigurationError):
        p.set_resolved_edge_metadata(
            edge_type_metadata={"_synodic_aggregated": {"is_lineage": True}},
            lineage_edge_types=["DERIVES_FROM"],
        )


def test_ontology_collision_via_lineage_list_rejected():
    p = _make_provider()
    with pytest.raises(ProviderConfigurationError):
        p.set_resolved_edge_metadata(
            edge_type_metadata={},
            lineage_edge_types=["DERIVES_FROM", "_Synodic_AGGREGATED"],
        )


def test_clean_ontology_accepted():
    p = _make_provider()
    # No collision — all customer-supplied edge types differ from sentinel.
    p.set_containment_edge_types(["CONTAINS", "PARENT_OF"])
    p.set_resolved_edge_metadata(
        edge_type_metadata={"DERIVES_FROM": {"is_lineage": True}},
        lineage_edge_types=["DERIVES_FROM", "READS_FROM"],
    )
    assert p._resolved_containment_types == {"CONTAINS", "PARENT_OF"}
    assert "DERIVES_FROM" in p._resolved_lineage_types


# ──────────────────────────────────────────────────────────────────
# Integration smoke — ontology validator does NOT block the standard
# default ontology shipped in the codebase.
# ──────────────────────────────────────────────────────────────────


def test_default_ontology_does_not_collide():
    """Ensures the rename is safe for existing deployments: nothing in
    the codebase's default ontology types matches the new sentinel.
    Sanity check, not exhaustive."""
    p = _make_provider()
    # Common entity-type-edge values used in fixtures and runbooks.
    common_edge_types = [
        "CONTAINS", "PARENT_OF", "DERIVES_FROM", "READS_FROM",
        "WRITES_TO", "DEPENDS_ON", "RELATED_TO",
    ]
    p.set_containment_edge_types(common_edge_types)
    # If no exception, we're good.
