"""Unit tests for authored-graph mutation validation. Pure, no DB."""
import pytest

from backend.app.services.graph_versioning.validation import (
    EdgeSpec,
    GraphValidationError,
    NodeSpec,
    OntologySpec,
    validate_graph_state,
)


def _ok_state():
    return dict(
        nodes=[NodeSpec("urn:a", "Table"), NodeSpec("urn:b", "Table")],
        edges=[EdgeSpec("e1", "urn:a", "urn:b", "flows_to")],
    )


def test_schemaless_valid_passes():
    validate_graph_state(schema_mode="schemaless", **_ok_state())


def test_schemaless_allows_freeform_types():
    # No ontology, arbitrary/None types are fine structurally.
    validate_graph_state(
        schema_mode="schemaless",
        nodes=[NodeSpec("urn:a", None), NodeSpec("urn:b", "Whatever")],
        edges=[EdgeSpec("e1", "urn:a", "urn:b", None)],
    )


def test_dangling_edge_is_rejected():
    with pytest.raises(GraphValidationError) as ei:
        validate_graph_state(
            schema_mode="schemaless",
            nodes=[NodeSpec("urn:a", "T")],
            edges=[EdgeSpec("e1", "urn:a", "urn:missing", "rel")],
        )
    codes = {v.code for v in ei.value.violations}
    assert "edge_dangling_target" in codes


def test_duplicate_node_key_rejected():
    with pytest.raises(GraphValidationError) as ei:
        validate_graph_state(
            schema_mode="schemaless",
            nodes=[NodeSpec("urn:a", "T"), NodeSpec("urn:a", "T")],
            edges=[],
        )
    assert any(v.code == "node_key_duplicate" for v in ei.value.violations)


def test_all_violations_reported_at_once():
    with pytest.raises(GraphValidationError) as ei:
        validate_graph_state(
            schema_mode="schemaless",
            nodes=[NodeSpec("", None), NodeSpec("urn:a", "T")],
            edges=[
                EdgeSpec("e1", "urn:a", "ghost1", "r"),
                EdgeSpec("e2", "ghost2", "urn:a", "r"),
            ],
        )
    codes = sorted(v.code for v in ei.value.violations)
    assert "node_key_empty" in codes
    assert codes.count("edge_dangling_source") == 1
    assert codes.count("edge_dangling_target") == 1
    assert len(ei.value.violations) >= 3


def test_strict_requires_ontology():
    with pytest.raises(ValueError, match="requires an OntologySpec"):
        validate_graph_state(schema_mode="strict", **_ok_state())


def test_strict_enforces_types():
    onto = OntologySpec(
        entity_types=frozenset({"Table"}),
        relationship_types=frozenset({"flows_to"}),
    )
    # Valid against ontology.
    validate_graph_state(schema_mode="strict", ontology=onto, **_ok_state())

    # Unknown entity + relationship type.
    with pytest.raises(GraphValidationError) as ei:
        validate_graph_state(
            schema_mode="strict",
            ontology=onto,
            nodes=[NodeSpec("urn:a", "Ghost"), NodeSpec("urn:b", "Table")],
            edges=[EdgeSpec("e1", "urn:a", "urn:b", "teleports_to")],
        )
    codes = {v.code for v in ei.value.violations}
    assert "node_type_unknown" in codes
    assert "edge_type_unknown" in codes


def test_unknown_schema_mode_rejected():
    with pytest.raises(ValueError, match="unknown schema_mode"):
        validate_graph_state(schema_mode="loose", **_ok_state())


def test_duplicate_edge_key_rejected():
    with pytest.raises(GraphValidationError) as ei:
        validate_graph_state(
            schema_mode="schemaless",
            nodes=[NodeSpec("urn:a", "T"), NodeSpec("urn:b", "T")],
            edges=[
                EdgeSpec("e1", "urn:a", "urn:b", "r"),
                EdgeSpec("e1", "urn:b", "urn:a", "r"),
            ],
        )
    assert any(v.code == "edge_key_duplicate" for v in ei.value.violations)
