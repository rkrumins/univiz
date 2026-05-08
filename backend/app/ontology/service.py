"""
LocalOntologyService — the concrete OntologyServiceProtocol implementation.

Runs in-process alongside the main backend app.
When the ontology service is later extracted as a standalone microservice,
create RemoteOntologyService that implements the same protocol via HTTP.
"""
import json
import logging
import uuid
from typing import Dict, List, Optional

from backend.common.models.graph import GraphSchemaStats, OntologyMetadata
from backend.common.models.management import OntologyCreateRequest

from .defaults import (
    SYSTEM_DEFAULT_ONTOLOGY_NAME,
    SYSTEM_DEFAULT_ONTOLOGY_VERSION,
    SYSTEM_ENTITY_TYPES,
    SYSTEM_RELATIONSHIP_TYPES,
)
from .models import (
    CoverageReport,
    EntityTypeDefEntry,
    OntologyData,
    RelationshipTypeDefEntry,
    ResolvedOntology,
    ValidationIssue,
)
from .protocols import OntologyRepositoryProtocol
from .resolver import (
    check_coverage,
    derive_flat_lists,
    parse_entity_definitions,
    parse_relationship_definitions,
    resolve_ontology,
    suggest_entity_defs_from_stats,
    suggest_relationship_defs_from_stats,
    validate_ontology,
)

logger = logging.getLogger(__name__)


class LocalOntologyService:
    """
    In-process implementation of OntologyServiceProtocol.

    DI contract: caller injects repository.
    """

    def __init__(
        self,
        repository: OntologyRepositoryProtocol,
    ) -> None:
        self._repo = repository

    # ------------------------------------------------------------------ #
    # Core: resolve                                                         #
    # ------------------------------------------------------------------ #

    async def resolve(
        self,
        workspace_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
        introspected_entity_ids: Optional[List[str]] = None,
        introspected_rel_ids: Optional[List[str]] = None,
    ) -> ResolvedOntology:
        """
        Two-layer merge:
        1. Assigned ontology (per data source or workspace)
        2. Introspected types (gap-fill — types in graph but not in ontology)

        The system-default merge layer was removed. Aggregation requires
        the assigned ontology to fully cover and classify the graph
        (enforced by ``backend.app.ontology.gate``); falling back to a
        system default would mask the gaps that gate is meant to surface.
        Read paths get the same behaviour for consistency — Layer 2
        still synthesizes fallback definitions for un-onboarded data
        sources so the explorer keeps rendering unmapped types.
        """
        assigned: Optional[OntologyData] = None
        if workspace_id:
            try:
                assigned = await self._repo.get_for_data_source(workspace_id, data_source_id)
            except Exception:
                logger.exception("Failed to load assigned ontology for workspace=%s ds=%s", workspace_id, data_source_id)

        return resolve_ontology(
            system_default=None,
            assigned=assigned,
            introspected_entity_ids=introspected_entity_ids,
            introspected_rel_ids=introspected_rel_ids,
        )

    # ------------------------------------------------------------------ #
    # Suggest                                                               #
    # ------------------------------------------------------------------ #

    async def suggest_from_introspection(
        self,
        introspected_stats: GraphSchemaStats,
        introspected_ontology: OntologyMetadata,
        base_ontology_id: Optional[str] = None,
    ) -> OntologyCreateRequest:
        base_entity_defs: Dict[str, EntityTypeDefEntry] = {}
        base_rel_defs: Dict[str, RelationshipTypeDefEntry] = {}

        if base_ontology_id:
            base_data = await self._repo.get_by_id(base_ontology_id)
            if base_data:
                base_entity_defs = parse_entity_definitions(base_data.entity_type_definitions)
                base_rel_defs = parse_relationship_definitions(base_data.relationship_type_definitions)

        suggested_entities = suggest_entity_defs_from_stats(
            introspected_stats.entity_type_stats,
            existing_defs=base_entity_defs,
        )
        suggested_rels = suggest_relationship_defs_from_stats(
            introspected_stats.edge_type_stats,
            existing_defs=base_rel_defs,
        )

        flat = derive_flat_lists(suggested_entities, suggested_rels)

        entity_type_defs_raw = {
            k: _entity_def_to_dict(v) for k, v in suggested_entities.items()
        }
        rel_type_defs_raw = {
            k: _rel_def_to_dict(v) for k, v in suggested_rels.items()
        }

        return OntologyCreateRequest(
            name="Suggested Ontology (from graph introspection)",
            version=1,
            containmentEdgeTypes=flat.containment_edge_types,
            lineageEdgeTypes=flat.lineage_edge_types,
            edgeTypeMetadata=flat.edge_type_metadata,
            entityTypeHierarchy=flat.entity_type_hierarchy,
            rootEntityTypes=flat.root_entity_types,
            entityTypeDefinitions=entity_type_defs_raw,
            relationshipTypeDefinitions=rel_type_defs_raw,
            scope="workspace",
        )

    # ------------------------------------------------------------------ #
    # Coverage                                                              #
    # ------------------------------------------------------------------ #

    async def check_coverage(
        self,
        ontology_id: str,
        introspected_stats: GraphSchemaStats,
    ) -> CoverageReport:
        data = await self._repo.get_by_id(ontology_id)
        if not data:
            return CoverageReport(coverage_percent=0.0)

        entity_defs = parse_entity_definitions(data.entity_type_definitions)
        rel_defs = parse_relationship_definitions(data.relationship_type_definitions)

        graph_entity_ids = [s.id for s in introspected_stats.entity_type_stats]
        graph_rel_ids = [s.id for s in introspected_stats.edge_type_stats]

        return check_coverage(entity_defs, rel_defs, graph_entity_ids, graph_rel_ids)

    # ------------------------------------------------------------------ #
    # Validation                                                            #
    # ------------------------------------------------------------------ #

    def validate_ontology(
        self,
        entity_defs: Dict[str, EntityTypeDefEntry],
        relationship_defs: Dict[str, RelationshipTypeDefEntry],
    ) -> List[ValidationIssue]:
        return validate_ontology(entity_defs, relationship_defs)

    # ------------------------------------------------------------------ #
    # Seed system defaults                                                  #
    # ------------------------------------------------------------------ #

    async def seed_system_defaults(self) -> None:
        """
        Ensure the system default ontology exists.
        Strategy: merge-not-overwrite.
          - If not present: create with all defaults.
          - If present: add new type keys that are missing; never delete existing.
        """
        existing = await self._repo.get_system_default()
        flat = derive_flat_lists(
            parse_entity_definitions(SYSTEM_ENTITY_TYPES),
            parse_relationship_definitions(SYSTEM_RELATIONSHIP_TYPES),
        )

        if existing is None:
            new_id = f"bp_{uuid.uuid4().hex[:12]}"
            data = OntologyData(
                id=new_id,
                name=SYSTEM_DEFAULT_ONTOLOGY_NAME,
                version=SYSTEM_DEFAULT_ONTOLOGY_VERSION,
                entity_type_definitions=SYSTEM_ENTITY_TYPES,
                relationship_type_definitions=SYSTEM_RELATIONSHIP_TYPES,
                containment_edge_types=flat.containment_edge_types,
                lineage_edge_types=flat.lineage_edge_types,
                edge_type_metadata=flat.edge_type_metadata,
                entity_type_hierarchy=flat.entity_type_hierarchy,
                root_entity_types=flat.root_entity_types,
                is_system=True,
                scope="universal",
            )
            await self._repo.save(data)
            logger.info("Seeded system default ontology id=%s", new_id)
            return

        # Merge: add missing keys, keep existing
        entity_defs = dict(existing.entity_type_definitions)
        rel_defs = dict(existing.relationship_type_definitions)
        added = False
        for k, v in SYSTEM_ENTITY_TYPES.items():
            if k not in entity_defs:
                entity_defs[k] = v
                added = True
        for k, v in SYSTEM_RELATIONSHIP_TYPES.items():
            if k not in rel_defs:
                rel_defs[k] = v
                added = True

        if added:
            new_flat = derive_flat_lists(
                parse_entity_definitions(entity_defs),
                parse_relationship_definitions(rel_defs),
            )
            updated = OntologyData(
                id=existing.id,
                name=existing.name,
                version=existing.version,
                entity_type_definitions=entity_defs,
                relationship_type_definitions=rel_defs,
                containment_edge_types=new_flat.containment_edge_types,
                lineage_edge_types=new_flat.lineage_edge_types,
                edge_type_metadata=new_flat.edge_type_metadata,
                entity_type_hierarchy=new_flat.entity_type_hierarchy,
                root_entity_types=new_flat.root_entity_types,
                is_system=existing.is_system,
                scope=existing.scope,
            )
            await self._repo.save(updated)
            logger.info("Updated system default ontology with new type definitions id=%s", existing.id)
        else:
            logger.debug("System default ontology already up to date id=%s", existing.id)


# ---------------------------------------------------------------------------
# Serialization helpers (domain model -> plain dict for OntologyCreateRequest)
# ---------------------------------------------------------------------------


def _entity_def_to_dict(e: EntityTypeDefEntry) -> dict:
    return {
        "name": e.name,
        "plural_name": e.plural_name,
        "description": e.description,
        "visual": {
            "icon": e.visual.icon,
            "color": e.visual.color,
            "color_secondary": e.visual.color_secondary,
            "shape": e.visual.shape,
            "size": e.visual.size,
            "border_style": e.visual.border_style,
            "show_in_minimap": e.visual.show_in_minimap,
        },
        "hierarchy": {
            "level": e.hierarchy.level,
            "can_contain": e.hierarchy.can_contain,
            "can_be_contained_by": e.hierarchy.can_be_contained_by,
            "default_expanded": e.hierarchy.default_expanded,
            "roll_up_fields": e.hierarchy.roll_up_fields,
        },
        "behavior": {
            "selectable": e.behavior.selectable,
            "draggable": e.behavior.draggable,
            "expandable": e.behavior.expandable,
            "traceable": e.behavior.traceable,
            "click_action": e.behavior.click_action,
            "double_click_action": e.behavior.double_click_action,
            "expansion_mode": e.behavior.expansion_mode,
        },
        "fields": [
            {
                "id": f.id,
                "name": f.name,
                "type": f.type,
                "required": f.required,
                "show_in_node": f.show_in_node,
                "show_in_panel": f.show_in_panel,
                "show_in_tooltip": f.show_in_tooltip,
                "display_order": f.display_order,
                "format": f.format,
            }
            for f in e.fields
        ],
    }


def _rel_def_to_dict(r: RelationshipTypeDefEntry) -> dict:
    return {
        "name": r.name,
        "description": r.description,
        "category": r.category,
        "is_containment": r.is_containment,
        "is_lineage": r.is_lineage,
        "direction": r.direction,
        "visual": {
            "stroke_color": r.visual.stroke_color,
            "stroke_width": r.visual.stroke_width,
            "stroke_style": r.visual.stroke_style,
            "animated": r.visual.animated,
            "animation_speed": r.visual.animation_speed,
            "arrow_type": r.visual.arrow_type,
            "curve_type": r.visual.curve_type,
        },
        "source_types": r.source_types,
        "target_types": r.target_types,
        "bidirectional": r.bidirectional,
        "show_label": r.show_label,
        "label_field": r.label_field,
    }
