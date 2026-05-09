"""Shared ``discover_schema`` heuristic.

Each provider supplies database-specific introspection (label list,
edge-type list, sample property keys per label). This module owns the
**common** part: turning that introspected metadata into a
``SchemaMapping`` suggestion that translates a foreign property schema
to Synodic's canonical fields.

The heuristic is conservative: when a foreign property name appears
that we recognise (e.g. ``urn``, ``uuid``, ``id`` for identity), suggest
it as the corresponding canonical field. When nothing matches, leave
the canonical default in place and let the user map it manually.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from backend.graph.adapters.schema_mapping import SchemaMapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic table
# ---------------------------------------------------------------------------

# Ordered candidates per canonical Synodic field; first match wins.
_IDENTITY_CANDIDATES = ("urn", "uuid", "id", "guid", "_id")
_DISPLAY_NAME_CANDIDATES = ("displayName", "name", "title", "label")
_QUALIFIED_NAME_CANDIDATES = ("qualifiedName", "fullyQualifiedName", "fqn", "path")
_DESCRIPTION_CANDIDATES = ("description", "desc", "summary")
_TAGS_CANDIDATES = ("tags", "labels", "categories")


@dataclass
class IntrospectionPayload:
    """The provider-specific introspection result.

    ``label_property_keys`` maps each label/edge-type to a list of
    observed top-level property keys (best-effort sampled).
    """
    labels: List[str]
    edge_types: List[str]
    label_property_keys: Dict[str, List[str]]
    raw: Dict[str, Any]  # provider-specific extras (information_schema, db.labels(), ...)


# ---------------------------------------------------------------------------
# Introspector base
# ---------------------------------------------------------------------------

class SchemaIntrospector:
    """Provider-side introspection adapter.

    Subclasses implement the three async methods. ``discover`` builds
    an ``IntrospectionPayload`` and runs ``suggest_mapping`` against it.
    """

    async def labels(self) -> List[str]:
        raise NotImplementedError

    async def edge_types(self) -> List[str]:
        raise NotImplementedError

    async def label_property_keys(self, label: str) -> List[str]:
        """Top-level property keys observed for nodes with this label."""
        raise NotImplementedError

    async def raw_metadata(self) -> Dict[str, Any]:
        """Provider-specific extras (e.g. INFORMATION_SCHEMA rows). Optional."""
        return {}

    async def discover(self) -> Dict[str, Any]:
        """Returns the ``discover_schema`` payload consumed by the wizard.

        The key for edge types is ``relationshipTypes`` to match the
        frontend ``SchemaDiscoveryResult`` contract in
        ``frontend/src/services/providerService.ts`` and the existing
        Neo4j payload shape in ``backend/graph/adapters/neo4j_provider.py``.
        """
        labels = await self.labels()
        edge_types = await self.edge_types()
        label_keys: Dict[str, List[str]] = {}
        for label in labels:
            try:
                label_keys[label] = await self.label_property_keys(label)
            except Exception as exc:
                logger.warning("introspector: property-keys for label=%s failed: %s", label, exc)
                label_keys[label] = []
        raw = await self.raw_metadata()
        payload = IntrospectionPayload(
            labels=labels, edge_types=edge_types,
            label_property_keys=label_keys, raw=raw,
        )
        suggested = suggest_mapping(payload)
        return {
            "labels": labels,
            "relationshipTypes": edge_types,
            "labelDetails": {
                label: {"propertyKeys": keys}
                for label, keys in label_keys.items()
            },
            "suggestedMapping": suggested.model_dump(),
            "raw": raw,
        }


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------

def suggest_mapping(payload: IntrospectionPayload) -> SchemaMapping:
    """Heuristically pick foreign field names for canonical Synodic fields.

    Aggregates property keys across all labels (a property name that
    appears under any label is a candidate). Preserves SchemaMapping
    defaults when no candidate matches.
    """
    all_keys: set[str] = set()
    for keys in payload.label_property_keys.values():
        all_keys.update(keys)

    overrides: Dict[str, Any] = {}

    identity = _first_match(_IDENTITY_CANDIDATES, all_keys)
    if identity and identity != SchemaMapping().identity_field:
        overrides["identity_field"] = identity

    display = _first_match(_DISPLAY_NAME_CANDIDATES, all_keys)
    if display and display != SchemaMapping().display_name_field:
        overrides["display_name_field"] = display

    qualified = _first_match(_QUALIFIED_NAME_CANDIDATES, all_keys)
    if qualified and qualified != SchemaMapping().qualified_name_field:
        overrides["qualified_name_field"] = qualified

    description = _first_match(_DESCRIPTION_CANDIDATES, all_keys)
    if description and description != SchemaMapping().description_field:
        overrides["description_field"] = description

    tags = _first_match(_TAGS_CANDIDATES, all_keys)
    if tags and tags != SchemaMapping().tags_field:
        overrides["tags_field"] = tags

    # Entity type strategy: if the foreign data has an explicit
    # ``entityType`` property, prefer property-based dispatch; otherwise
    # rely on labels (the default for label-bearing stores).
    if "entityType" in all_keys or "type" in all_keys:
        overrides["entity_type_strategy"] = "property"
        if "entityType" in all_keys:
            overrides["entity_type_field"] = "entityType"
        elif "type" in all_keys:
            overrides["entity_type_field"] = "type"

    if overrides:
        return SchemaMapping(**overrides)
    return SchemaMapping()


def _first_match(candidates: tuple, present: set) -> Optional[str]:
    for c in candidates:
        if c in present:
            return c
    return None
