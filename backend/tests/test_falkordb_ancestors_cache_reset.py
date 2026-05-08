"""Tests for the containment-fingerprint-scoped ancestors cache key.

The Redis Hash that caches per-URN ancestor chains is namespaced by a
fingerprint of the resolved containment edge types
(``_ancestors_cache_key``). Different containment configurations
produce different cache namespaces, so a stale cached chain from an
earlier configuration cannot leak into a later one. These tests
exercise the helper without spinning up Redis or FalkorDB.
"""
from backend.app.providers.falkordb_provider import FalkorDBProvider


def _make_provider(graph_name: str = "demo_graph") -> FalkorDBProvider:
    """Build a provider shell sufficient to exercise ``set_containment_edge_types``
    + ``_ancestors_cache_key`` without provider config / connections."""
    p = FalkorDBProvider.__new__(FalkorDBProvider)
    p._graph_name = graph_name  # type: ignore[attr-defined]
    return p


def test_cache_key_is_stable_for_identical_types():
    p = _make_provider()
    p.set_containment_edge_types(["CONTAINS", "HAS_COLUMN"], from_ontology=True)
    k1 = p._ancestors_cache_key()
    p.set_containment_edge_types(["CONTAINS", "HAS_COLUMN"], from_ontology=True)
    k2 = p._ancestors_cache_key()
    assert k1 == k2


def test_cache_key_differs_when_types_change():
    p = _make_provider()
    p.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    k_one = p._ancestors_cache_key()
    p.set_containment_edge_types(["CONTAINS", "HAS_COLUMN"], from_ontology=True)
    k_two = p._ancestors_cache_key()
    assert k_one != k_two


def test_cache_key_is_case_insensitive():
    p1 = _make_provider()
    p2 = _make_provider()
    p1.set_containment_edge_types(["contains"], from_ontology=True)
    p2.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    assert p1._ancestors_cache_key() == p2._ancestors_cache_key()


def test_cache_key_is_order_independent():
    p1 = _make_provider()
    p2 = _make_provider()
    p1.set_containment_edge_types(["A", "B"], from_ontology=True)
    p2.set_containment_edge_types(["B", "A"], from_ontology=True)
    assert p1._ancestors_cache_key() == p2._ancestors_cache_key()


def test_empty_types_have_distinct_fingerprint():
    """Flat-graph aggregations (intentional empty containment) get
    their own stable cache namespace, distinct from any populated
    configuration. Keeps cross-job caching for repeat flat aggregations
    without ever clashing with a populated config."""
    p_empty = _make_provider()
    p_empty.set_containment_edge_types([], from_ontology=True)
    k_empty = p_empty._ancestors_cache_key()

    p_full = _make_provider()
    p_full.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    k_full = p_full._ancestors_cache_key()

    assert k_empty != k_full
    # Empty fingerprint must be deterministic (same provider, same key
    # across reads) — no random salt or timestamp involvement.
    assert k_empty == p_empty._ancestors_cache_key()


def test_cache_key_namespaces_by_graph_name():
    """Same containment config on different graphs gets different
    keys. Prevents one graph's chains from leaking to another."""
    p1 = _make_provider(graph_name="graph_one")
    p2 = _make_provider(graph_name="graph_two")
    p1.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    p2.set_containment_edge_types(["CONTAINS"], from_ontology=True)
    assert p1._ancestors_cache_key() != p2._ancestors_cache_key()
    # Both must follow the documented prefix convention.
    assert p1._ancestors_cache_key().startswith("graph_one:ancestors:")
    assert p2._ancestors_cache_key().startswith("graph_two:ancestors:")


def test_cache_key_handles_unset_types():
    """Before ``set_containment_edge_types`` is ever called the helper
    must still return a stable key (treated the same as empty set).
    Prevents AttributeError on cold-start callers."""
    p = _make_provider()
    k = p._ancestors_cache_key()
    assert k.startswith("demo_graph:ancestors:")
    # Match the empty-set key after explicit empty assignment.
    p.set_containment_edge_types([], from_ontology=True)
    assert p._ancestors_cache_key() == k
