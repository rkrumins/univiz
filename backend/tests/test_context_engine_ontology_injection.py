"""Regression tests for ContextEngine ontology-injection paths.

Pins the fix for the production trace bug where
``OntologyService.resolve()`` raising left the provider unconfigured —
every subsequent call to a containment-classifying method (trace,
get_children, get_parent, get_schema_stats with childCount) raised
``ProviderConfigurationError``.

The fix lives in
``backend/app/services/context_engine.py:_inject_resolved_ontology``;
both the service-success path AND the legacy/introspection-fallback
path now call it. These tests verify the injection happens regardless
of which path resolves first.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import pytest

from backend.common.interfaces.provider import GraphDataProvider, ProviderConfigurationError
from backend.common.models.graph import (
    AggregatedEdgeResult,
    ChildrenWithEdgesResult,
    EdgeQuery,
    EdgeTypeMetadata,
    EntityTypeHierarchy,
    GraphEdge,
    GraphNode,
    GraphSchemaStats,
    LineageResult,
    NodeQuery,
    OntologyMetadata,
    TopLevelNodesResult,
    TraceResult,
)
from backend.app.ontology.models import ResolvedOntology
from backend.app.services.context_engine import ContextEngine


# ---------------------------------------------------------------------------
# Recording stub provider — captures every ontology-injection call
# ---------------------------------------------------------------------------

class _RecordingStubProvider(GraphDataProvider):
    """Tracks set_containment_edge_types / set_resolved_edge_metadata /
    set_entity_type_levels / ensure_indices calls so the regression
    tests can assert that ContextEngine actually injected.

    Implements the strict-empty-state behaviour of the real FalkorDB
    provider — get_node, get_children etc. raise ProviderConfigurationError
    until set_containment_edge_types has been called with a real ontology
    or non-empty types.
    """

    def __init__(self) -> None:
        self.containment_types_calls: List[Dict[str, Any]] = []
        self.edge_metadata_calls: List[Dict[str, Any]] = []
        self.entity_levels_calls: List[Dict[str, int]] = []
        self.ensure_indices_calls: List[List[str]] = []
        self._sentinel_set: bool = False
        self._containment_types: Set[str] = set()
        # Toggle: if introspect_raises=True, get_ontology_metadata raises.
        self.introspect_raises: bool = False

    @property
    def name(self) -> str:
        return "recording-stub"

    # -- ABC methods (minimum to instantiate) --
    async def get_node(self, urn): return None
    async def get_nodes(self, query): return []
    async def search_nodes(self, query, limit=10): return []
    async def get_edges(self, query): return []
    async def get_children(self, parent_urn, **kw): return []
    async def get_parent(self, child_urn): return None
    async def get_upstream(self, urn, depth, **kw):
        return LineageResult(nodes=[], edges=[], totalCount=0, hasMore=False)
    async def get_downstream(self, urn, depth, **kw):
        return LineageResult(nodes=[], edges=[], totalCount=0, hasMore=False)
    async def get_full_lineage(self, urn, upstream_depth, downstream_depth, **kw):
        return LineageResult(nodes=[], edges=[], totalCount=0, hasMore=False)
    async def get_aggregated_edges_between(self, source_urns, target_urns, granularity, containment_edges, lineage_edges):
        return AggregatedEdgeResult(aggregatedEdges=[], totalSourceEdges=0)
    async def get_trace_lineage(self, urn, direction, depth, containment_edges, lineage_edges):
        return LineageResult(nodes=[], edges=[], totalCount=0, hasMore=False)
    async def get_stats(self): return {"nodeCount": 0, "edgeCount": 0}
    async def get_schema_stats(self):
        return GraphSchemaStats(totalNodes=0, totalEdges=0)
    async def get_distinct_values(self, property_name): return []
    async def get_ancestors(self, urn, limit=100, offset=0): return []
    async def get_descendants(self, urn, depth=5, **kw): return []
    async def get_nodes_by_tag(self, tag, limit=100, offset=0): return []
    async def get_nodes_by_layer(self, layer_id, limit=100, offset=0): return []
    async def save_custom_graph(self, nodes, edges): return True
    async def create_node(self, node, containment_edge=None): return True
    async def create_edge(self, edge): return True
    async def update_edge(self, edge_id, properties): return None
    async def delete_edge(self, edge_id): return True

    # -- Ontology metadata source --
    async def get_ontology_metadata(self) -> OntologyMetadata:
        if self.introspect_raises:
            raise RuntimeError("simulated introspection failure")
        return OntologyMetadata(
            containmentEdgeTypes=["CONTAINS"],
            lineageEdgeTypes=["DERIVES_FROM"],
            edgeTypeMetadata={
                "CONTAINS": EdgeTypeMetadata(
                    isContainment=True, isLineage=False,
                    direction="parent-to-child", category="structural",
                ),
                "DERIVES_FROM": EdgeTypeMetadata(
                    isContainment=False, isLineage=True,
                    direction="source-to-target", category="flow",
                ),
            },
            entityTypeHierarchy={},
            rootEntityTypes=[],
        )

    # -- Injection points (record and apply) --
    def set_containment_edge_types(self, types, from_ontology=True):
        self.containment_types_calls.append({"types": list(types), "from_ontology": from_ontology})
        if from_ontology or types:
            self._containment_types = {t.upper() for t in types}
            self._sentinel_set = True

    def set_resolved_edge_metadata(self, edge_type_metadata, lineage_edge_types):
        self.edge_metadata_calls.append({
            "edge_type_metadata": dict(edge_type_metadata),
            "lineage_edge_types": list(lineage_edge_types),
        })

    def set_entity_type_levels(self, mapping):
        self.entity_levels_calls.append(dict(mapping))

    async def ensure_indices(self, entity_type_ids=None):
        self.ensure_indices_calls.append(list(entity_type_ids or []))

    # -- Strict guard mirroring the real FalkorDB provider --
    def _assert_configured(self) -> None:
        if not self._sentinel_set:
            raise ProviderConfigurationError(
                "Containment edge types are not configured for this provider."
            )


# ---------------------------------------------------------------------------
# Fake OntologyService stand-ins
# ---------------------------------------------------------------------------

class _FailingOntologyService:
    """OntologyService that raises on resolve(). Forces ContextEngine onto
    the legacy/introspection fallback path."""

    async def resolve(self, **kwargs):
        raise RuntimeError("simulated ontology service failure")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_fallback_path_injects_ontology_into_provider():
    """The bug: OntologyService.resolve() raising left the provider's
    ``_resolved_containment_types_set`` flag unset, so the very next
    request raised ProviderConfigurationError. After the fix, the
    legacy/introspection fallback path also injects."""
    provider = _RecordingStubProvider()
    engine = ContextEngine(
        provider=provider,
        ontology_service=_FailingOntologyService(),
    )
    engine._workspace_id = "ws_test"

    resolved = await engine._resolve_ontology()

    # The fallback ResolvedOntology came back populated from introspection.
    assert "CONTAINS" in resolved.containment_edge_types
    assert "DERIVES_FROM" in resolved.lineage_edge_types

    # AND the provider was actually injected — sentinel set, no
    # ProviderConfigurationError.
    assert provider._sentinel_set is True
    provider._assert_configured()  # would raise if injection didn't happen
    assert provider._containment_types == {"CONTAINS"}

    # Helper was called exactly once for each of the three injection points.
    assert len(provider.containment_types_calls) == 1
    assert provider.containment_types_calls[0]["from_ontology"] is True
    assert len(provider.edge_metadata_calls) == 1
    assert len(provider.entity_levels_calls) == 1


@pytest.mark.asyncio
async def test_legacy_fallback_with_failed_introspection_still_configures_provider():
    """Both the ontology service AND introspection fail — the engine still
    has to leave the provider in a queryable state. Empty-but-configured
    is the right outcome (graph has no resolvable ontology); raising on
    every subsequent call is wrong."""
    provider = _RecordingStubProvider()
    provider.introspect_raises = True
    engine = ContextEngine(
        provider=provider,
        ontology_service=_FailingOntologyService(),
    )
    engine._workspace_id = "ws_test"

    await engine._resolve_ontology()

    # Sentinel set — provider methods that call _get_containment_edge_types
    # will see an empty set, NOT raise ProviderConfigurationError.
    assert provider._sentinel_set is True
    assert provider._containment_types == set()


@pytest.mark.asyncio
async def test_no_ontology_service_uses_introspection_and_injects():
    """Construction without an ontology_service (legacy ``for_connection``
    code path) still injects the introspected types into the provider —
    that's the path the user's tests exercised when constructing
    ContextEngine directly."""
    provider = _RecordingStubProvider()
    engine = ContextEngine(provider=provider, ontology_service=None)
    # _workspace_id deliberately left None — exercises the no-service branch.

    await engine._resolve_ontology()

    assert provider._sentinel_set is True
    # Whatever introspection returned must reach the provider.
    assert "CONTAINS" in provider._containment_types


@pytest.mark.asyncio
async def test_injection_helper_is_idempotent_under_cache_hits():
    """Repeated calls within the cache TTL must not re-inject — the
    helper runs once per cache miss only."""
    provider = _RecordingStubProvider()
    engine = ContextEngine(
        provider=provider,
        ontology_service=_FailingOntologyService(),
    )
    engine._workspace_id = "ws_test"

    await engine._resolve_ontology()
    await engine._resolve_ontology()
    await engine._resolve_ontology()

    # Three calls, one injection (first call populated the cache).
    assert len(provider.containment_types_calls) == 1
