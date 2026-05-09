"""Deterministic fixture graph used across the provider-contract suites.

A small five-node hierarchy with two lineage edges so we exercise
containment + lineage + trace surfaces without snapshot bloat:

    domain:test
      contains schema:s1
        contains dataset:d1  ──derives_from──┐
      contains schema:s2                      │
        contains dataset:d2  ◄────────────────┘
        contains dataset:d3  ──derives_from──> dataset:d2

The fixture is intentionally small. Larger tests (pagination, fanout)
should live as their own targeted regressions, not in this baseline.
"""
from __future__ import annotations

from typing import List, Tuple

from backend.common.models.graph import GraphEdge, GraphNode


def fixture_nodes() -> List[GraphNode]:
    return [
        GraphNode(urn="urn:test:domain:root", entityType="domain", displayName="Root Domain"),
        GraphNode(urn="urn:test:schema:s1", entityType="schema", displayName="Schema 1"),
        GraphNode(urn="urn:test:schema:s2", entityType="schema", displayName="Schema 2"),
        GraphNode(urn="urn:test:dataset:d1", entityType="dataset", displayName="Dataset 1"),
        GraphNode(urn="urn:test:dataset:d2", entityType="dataset", displayName="Dataset 2"),
        GraphNode(urn="urn:test:dataset:d3", entityType="dataset", displayName="Dataset 3"),
    ]


def fixture_edges() -> List[GraphEdge]:
    return [
        # Containment
        GraphEdge(id="c1", sourceUrn="urn:test:domain:root", targetUrn="urn:test:schema:s1", edgeType="CONTAINS"),
        GraphEdge(id="c2", sourceUrn="urn:test:domain:root", targetUrn="urn:test:schema:s2", edgeType="CONTAINS"),
        GraphEdge(id="c3", sourceUrn="urn:test:schema:s1", targetUrn="urn:test:dataset:d1", edgeType="CONTAINS"),
        GraphEdge(id="c4", sourceUrn="urn:test:schema:s2", targetUrn="urn:test:dataset:d2", edgeType="CONTAINS"),
        GraphEdge(id="c5", sourceUrn="urn:test:schema:s2", targetUrn="urn:test:dataset:d3", edgeType="CONTAINS"),
        # Lineage
        GraphEdge(id="l1", sourceUrn="urn:test:dataset:d1", targetUrn="urn:test:dataset:d2", edgeType="DERIVES_FROM"),
        GraphEdge(id="l2", sourceUrn="urn:test:dataset:d3", targetUrn="urn:test:dataset:d2", edgeType="DERIVES_FROM"),
    ]


def containment_types() -> List[str]:
    return ["CONTAINS"]


def lineage_types() -> List[str]:
    return ["DERIVES_FROM"]


# Levels in the fixture hierarchy.
ENTITY_LEVELS = {
    "domain": 0,
    "schema": 1,
    "dataset": 2,
}
