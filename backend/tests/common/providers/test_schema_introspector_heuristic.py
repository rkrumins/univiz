"""Unit tests for the suggest_mapping heuristic."""
from __future__ import annotations

from backend.common.providers.schema_introspection import (
    IntrospectionPayload,
    suggest_mapping,
)


def _payload(label_to_keys):
    return IntrospectionPayload(
        labels=list(label_to_keys.keys()),
        edge_types=[],
        label_property_keys=label_to_keys,
        raw={},
    )


def test_default_when_no_known_keys():
    mapping = suggest_mapping(_payload({"Dataset": []}))
    # Defaults preserved.
    assert mapping.identity_field == "urn"
    assert mapping.display_name_field == "displayName"
    assert mapping.entity_type_strategy == "label"


def test_picks_uuid_for_identity_when_present():
    mapping = suggest_mapping(_payload({
        "Dataset": ["uuid", "title", "summary"],
    }))
    assert mapping.identity_field == "uuid"
    assert mapping.display_name_field == "title"
    assert mapping.description_field == "summary"


def test_first_match_wins_in_priority_order():
    # Both "uuid" and "id" present — "urn" is highest priority but not present,
    # so "uuid" wins.
    mapping = suggest_mapping(_payload({
        "Dataset": ["uuid", "id", "name"],
    }))
    assert mapping.identity_field == "uuid"


def test_property_strategy_when_entityType_present():
    mapping = suggest_mapping(_payload({
        "Dataset": ["urn", "name", "entityType"],
    }))
    assert mapping.entity_type_strategy == "property"
    assert mapping.entity_type_field == "entityType"


def test_property_strategy_when_only_type_present():
    mapping = suggest_mapping(_payload({
        "Dataset": ["urn", "name", "type"],
    }))
    assert mapping.entity_type_strategy == "property"
    assert mapping.entity_type_field == "type"


def test_aggregates_keys_across_labels():
    # An identity-candidate appearing under any label is enough.
    mapping = suggest_mapping(_payload({
        "Dataset": ["name"],
        "Pipeline": ["uuid"],
    }))
    assert mapping.identity_field == "uuid"
