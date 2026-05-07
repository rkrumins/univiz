"""
Ontology resolution gate — single source of truth for "can we aggregate?".

Pure, side-effect-free. No DB, no HTTP, no logging side effects. All callers
(API endpoint, AggregationService.trigger, worker re-validation) compute the
same ResolutionReport from the same inputs.

Hard criteria (block aggregation):
    1. Every introspected entity type has an entry in entity_type_definitions.
    2. Every introspected edge type has an entry in relationship_type_definitions.
    3. Every relationship type has both is_containment and is_lineage set.
    4. At least one relationship type has is_lineage=true.

Soft criteria (warn, do not block):
    5. Each entity type referenced in the introspected graph has a non-null level.
    6. Each entity type that participates in containment has matching can_contain
       / can_be_contained_by entries.
    7. Root entity types (no incoming containment) appear in root_entity_types.
"""
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .models import EntityTypeDefEntry, RelationshipTypeDefEntry
from .resolver import (
    parse_entity_definitions,
    parse_relationship_definitions,
)


@dataclass
class RelGap:
    """A relationship type that is missing classification flags."""
    id: str
    name: str
    is_containment: Optional[bool]
    is_lineage: Optional[bool]


@dataclass
class HierarchyGap:
    """An entity type with an incomplete hierarchy field. Advisory only."""
    entity_type: str
    missing_field: str  # "level" | "can_contain" | "can_be_contained_by" | "root_membership"


@dataclass
class ResolutionReport:
    resolved: bool
    ontology_id: Optional[str]
    ontology_version: Optional[int]
    ontology_is_published: bool
    missing_entity_types: List[str] = field(default_factory=list)
    missing_edge_types: List[str] = field(default_factory=list)
    unclassified_relationships: List[RelGap] = field(default_factory=list)
    has_lineage: bool = False
    has_containment: bool = False
    hierarchy_warnings: List[HierarchyGap] = field(default_factory=list)
    advisory_warnings: List[str] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    fingerprint: Optional[str] = None


def _was_explicitly_classified(raw: Dict[str, Any]) -> bool:
    """Check if both is_containment and is_lineage were explicitly set in the
    raw JSON dict (not just defaulted to false by the parser).

    Reads both snake_case and camelCase variants matching resolver._rel_def_from_dict.
    """
    has_cont = "is_containment" in raw or "isContainment" in raw
    has_lin = "is_lineage" in raw or "isLineage" in raw
    return has_cont and has_lin


def _compute_fingerprint(
    ontology_id: str,
    revision: int,
    entity_defs_raw: Dict[str, Any],
    rel_defs_raw: Dict[str, Any],
) -> str:
    """Stable fingerprint over the fields that actually affect aggregation.

    Excludes mutable metadata (name, description, updated_at) so harmless
    edits don't invalidate idempotency replays. Includes the rich
    definitions because that's where containment/lineage flags live.
    """
    canonical = json.dumps(
        {
            "id": ontology_id,
            "revision": revision,
            "entity_type_definitions": entity_defs_raw,
            "relationship_type_definitions": rel_defs_raw,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_resolution(
    *,
    ontology_id: str,
    ontology_version: int,
    ontology_is_published: bool,
    ontology_revision: int,
    entity_type_definitions_raw: Dict[str, Any],
    relationship_type_definitions_raw: Dict[str, Any],
    introspected_entity_ids: List[str],
    introspected_edge_ids: List[str],
) -> ResolutionReport:
    """Evaluate the resolution gate against an ontology + a graph schema snapshot.

    Inputs are raw JSON dicts (typically parsed from OntologyORM columns) and
    flat lists of entity / edge type IDs from a schema-stats snapshot. The
    function never reaches outside its arguments.

    When introspection lists are empty (stats cache miss / not yet
    populated) the per-graph criteria 1, 2, and 3 cannot be evaluated.
    Criterion 4 (at least one ``is_lineage`` relationship) still runs
    against the full ontology — that's a property of the ontology
    itself, not of any specific graph. This keeps re-triggers working
    even before the stats refresh has caught up.
    """
    entity_defs: Dict[str, EntityTypeDefEntry] = parse_entity_definitions(
        entity_type_definitions_raw or {}
    )
    rel_defs: Dict[str, RelationshipTypeDefEntry] = parse_relationship_definitions(
        relationship_type_definitions_raw or {}
    )

    blocking_reasons: List[str] = []

    # Criterion 1 — every introspected entity type defined.
    # Case-insensitive match: graph providers return labels like "Person"
    # while ontologies may key them differently. Same convention as resolver.
    defined_entity_keys = {k.upper() for k in entity_defs}
    missing_entity_types = sorted(
        {eid for eid in introspected_entity_ids if eid.upper() not in defined_entity_keys}
    )
    if missing_entity_types:
        blocking_reasons.append("missing_entity_types")

    # Criterion 2 — every introspected edge type defined.
    defined_rel_keys = {k.upper() for k in rel_defs}
    missing_edge_types = sorted(
        {eid for eid in introspected_edge_ids if eid.upper() not in defined_rel_keys}
    )
    if missing_edge_types:
        blocking_reasons.append("missing_edge_types")

    # Criterion 3 — every relationship explicitly classified.
    # Only relationships that exist in the introspected graph are required
    # to be classified. Extras in the ontology are fine and won't block
    # the gate. The raw dict is inspected (not the parser-defaulted
    # ``rel_def.is_*``) so we can distinguish "explicitly false"
    # from "not set".
    introspected_edge_keys_upper = {e.upper() for e in introspected_edge_ids}
    unclassified: List[RelGap] = []
    for rid, raw in (relationship_type_definitions_raw or {}).items():
        if rid.upper() not in introspected_edge_keys_upper:
            continue
        if not isinstance(raw, dict):
            continue
        if _was_explicitly_classified(raw):
            continue
        rel_def = rel_defs.get(rid)
        unclassified.append(
            RelGap(
                id=rid,
                name=rel_def.name if rel_def else rid,
                is_containment=raw.get("is_containment", raw.get("isContainment")),
                is_lineage=raw.get("is_lineage", raw.get("isLineage")),
            )
        )
    if unclassified:
        blocking_reasons.append("unclassified_relationships")

    # Criterion 4 — at least one ``is_lineage`` relationship in the
    # ontology. Checked against the FULL ontology (not just the
    # introspected subset) because the ontology's ability to express
    # lineage is independent of any specific graph's current contents.
    # Without this, a stats-cache miss (which produces empty
    # introspected_edge_ids) would spuriously block re-triggers.
    has_lineage = any(rel_def.is_lineage for rel_def in rel_defs.values())
    if not has_lineage:
        blocking_reasons.append("no_lineage")

    # Advisory — at least one ``is_containment`` relationship. Without
    # one, the aggregation worker has no way to walk the containment
    # tree upward, so AGGREGATED edges only ever connect direct lineage
    # endpoints (no ancestor-to-ancestor propagation). Aggregation will
    # technically run, but the user won't see the cross-tier roll-up
    # they expect.
    has_containment = any(rel_def.is_containment for rel_def in rel_defs.values())
    advisory_warnings: List[str] = []
    if not has_containment:
        advisory_warnings.append("no_containment_edges")

    # Soft criteria — hierarchy warnings (advisory, do not affect resolved flag).
    hierarchy_warnings: List[HierarchyGap] = []
    introspected_entity_keys_upper = {e.upper() for e in introspected_entity_ids}
    for eid, edef in entity_defs.items():
        if eid.upper() not in introspected_entity_keys_upper:
            continue
        raw = (entity_type_definitions_raw or {}).get(eid, {})
        raw_hier = raw.get("hierarchy", {}) if isinstance(raw, dict) else {}
        # Criterion 5 — level set
        if "level" not in raw_hier:
            hierarchy_warnings.append(HierarchyGap(entity_type=eid, missing_field="level"))
        # Criterion 6 — containment fields populated when applicable
        # We can't tell from the entity alone whether it should be a parent
        # or a child; surface both as warnings when they're empty AND the
        # entity participates in a containment relationship.
        appears_as_parent = any(
            rdef.is_containment and eid in rdef.source_types
            for rdef in rel_defs.values()
        )
        appears_as_child = any(
            rdef.is_containment and eid in rdef.target_types
            for rdef in rel_defs.values()
        )
        if appears_as_parent and not edef.hierarchy.can_contain:
            hierarchy_warnings.append(
                HierarchyGap(entity_type=eid, missing_field="can_contain")
            )
        if appears_as_child and not edef.hierarchy.can_be_contained_by:
            hierarchy_warnings.append(
                HierarchyGap(entity_type=eid, missing_field="can_be_contained_by")
            )

    fingerprint = _compute_fingerprint(
        ontology_id=ontology_id,
        revision=ontology_revision,
        entity_defs_raw=entity_type_definitions_raw or {},
        rel_defs_raw=relationship_type_definitions_raw or {},
    )

    resolved = not blocking_reasons

    return ResolutionReport(
        resolved=resolved,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
        ontology_is_published=ontology_is_published,
        missing_entity_types=missing_entity_types,
        missing_edge_types=missing_edge_types,
        unclassified_relationships=unclassified,
        has_lineage=has_lineage,
        has_containment=has_containment,
        hierarchy_warnings=hierarchy_warnings,
        advisory_warnings=advisory_warnings,
        blocking_reasons=blocking_reasons,
        fingerprint=fingerprint,
    )


def report_to_dict(report: ResolutionReport) -> Dict[str, Any]:
    """Serialize a ResolutionReport to a JSON-safe dict (snake_case keys)."""
    return {
        "resolved": report.resolved,
        "ontology_id": report.ontology_id,
        "ontology_version": report.ontology_version,
        "ontology_is_published": report.ontology_is_published,
        "missing_entity_types": report.missing_entity_types,
        "missing_edge_types": report.missing_edge_types,
        "unclassified_relationships": [asdict(r) for r in report.unclassified_relationships],
        "has_lineage": report.has_lineage,
        "has_containment": report.has_containment,
        "hierarchy_warnings": [asdict(h) for h in report.hierarchy_warnings],
        "advisory_warnings": report.advisory_warnings,
        "blocking_reasons": report.blocking_reasons,
        "fingerprint": report.fingerprint,
    }


def compute_fingerprint_from_ontology_orm(orm: Any) -> str:
    """Convenience: compute the fingerprint from a raw OntologyORM row.

    Used by ``ontologies.py`` invalidation hook and by service-level callers
    that already hold the ORM. Keeps the canonical_json shape in one place.
    """
    return _compute_fingerprint(
        ontology_id=orm.id,
        revision=getattr(orm, "revision", 0) or 0,
        entity_defs_raw=json.loads(orm.entity_type_definitions or "{}"),
        rel_defs_raw=json.loads(orm.relationship_type_definitions or "{}"),
    )
