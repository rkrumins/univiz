"""Unit tests for graph content addressing — the version dedup key.

Pure logic, no DB. These lock down the invariants the whole
content-addressed storage model depends on.
"""
import pytest

from backend.app.services.graph_versioning.content_address import (
    node_content_hash,
    edge_content_hash,
)


def _node(**kw):
    base = dict(
        entity_type="Table",
        display_name="orders",
        position={"x": 10, "y": 20},
        properties={"owner": "fin", "rows": 5},
        tags=["pii", "gold"],
    )
    base.update(kw)
    return node_content_hash(**base)


def test_node_hash_is_deterministic():
    assert _node() == _node()


def test_property_key_order_does_not_matter():
    a = _node(properties={"a": 1, "b": 2})
    b = _node(properties={"b": 2, "a": 1})
    assert a == b


def test_tags_are_order_insensitive_and_deduplicated():
    assert _node(tags=["gold", "pii"]) == _node(tags=["pii", "gold"])
    assert _node(tags=["pii", "pii", "gold"]) == _node(tags=["pii", "gold"])


def test_float_normalization_no_op_redrag():
    # A node re-dragged to the "same" place with float coords must NOT
    # produce a new version.
    assert _node(position={"x": 10, "y": 20}) == _node(
        position={"x": 10.0, "y": 20.0}
    )
    assert _node(position={"x": 0.0}) == _node(position={"x": -0.0})


def test_absent_equals_null():
    assert _node(properties={"owner": "fin", "rows": 5, "note": None}) == _node(
        properties={"owner": "fin", "rows": 5}
    )


def test_identity_is_not_content():
    # node_key / urn / surrogate id are NOT hashed: two distinct nodes
    # with identical content share one stored version row (the manifest
    # entry carries identity).
    assert _node() == _node()  # no key param exists by construction
    # Different content DOES change the hash.
    assert _node(display_name="orders") != _node(display_name="orders_v2")


def test_non_finite_float_rejected():
    with pytest.raises(ValueError):
        _node(position={"x": float("nan"), "y": 0})
    with pytest.raises(ValueError):
        _node(position={"x": float("inf"), "y": 0})


def test_edge_endpoint_change_is_a_new_version():
    a = edge_content_hash(
        source_node_key="urn:a", target_node_key="urn:b", edge_type="flows_to"
    )
    b = edge_content_hash(
        source_node_key="urn:a", target_node_key="urn:c", edge_type="flows_to"
    )
    assert a != b
    # Same endpoints + type ⇒ same content.
    assert a == edge_content_hash(
        source_node_key="urn:a", target_node_key="urn:b", edge_type="flows_to"
    )


def test_edge_properties_order_insensitive():
    a = edge_content_hash(
        source_node_key="s", target_node_key="t", edge_type="e",
        properties={"x": 1, "y": 2},
    )
    b = edge_content_hash(
        source_node_key="s", target_node_key="t", edge_type="e",
        properties={"y": 2, "x": 1},
    )
    assert a == b
