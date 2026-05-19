import re
import time
import logging
from typing import TYPE_CHECKING, List, Dict, Optional, Set, Any, Tuple
from backend.app.models.graph import GraphNode, GraphEdge
from backend.app.models.assignment import (
    LayerAssignmentRequest, LayerAssignmentResult, EntityAssignment,
    ViewLayerConfig, LayerAssignmentRuleConfig, LayerAssignmentStats,
    EntityAssignmentConfig, RuleCondition, RuleOperator,
)

if TYPE_CHECKING:
    from backend.app.services.context_engine import ContextEngine

logger = logging.getLogger(__name__)

class AssignmentEngine:
    def __init__(self):
        pass

    async def compute_assignments(
        self,
        request: LayerAssignmentRequest,
        engine: Optional["ContextEngine"] = None,
    ) -> LayerAssignmentResult:
        """Compute layer assignments using the provided workspace-scoped engine.

        ``engine`` must be the ContextEngine created by ``get_context_engine``
        in the FastAPI endpoint. Passing it here ensures ``_resolve_ontology()``
        runs before any provider call that needs containment edge types, which
        eliminates the intermittent ProviderConfigurationError that occurred
        when the ontology cache was cold.

        """
        if engine is None:
            raise ValueError("compute_assignments requires an explicit ContextEngine")

        start_time = time.time()

        # ── Resolve ontology FIRST ────────────────────────────────────────
        # This calls _resolve_ontology() which pushes the ontology-authoritative
        # containment edge types into the provider via set_containment_edge_types().
        # Any subsequent provider call (including get_nodes, which internally uses
        # _get_containment_edge_types() for child-count queries) is then guaranteed
        # to find the types already injected.
        from backend.app.models.graph import NodeQuery, EdgeQuery
        ontology = await engine.get_ontology_metadata()
        containment_edge_types: Set[str] = (
            set(ontology.containment_edge_types) if ontology.containment_edge_types else set()
        )

        # ── Fetch graph data via the scoped engine ────────────────────────
        all_nodes = await engine.get_nodes_query(NodeQuery())
        all_edges = await engine.get_edges(EdgeQuery())

        logging.info(f"Computing assignments for {len(all_nodes)} nodes and {len(all_edges)} edges")

        # 2. Build Indices
        rule_index = self._build_rule_index(request.layers)
        # Pass the resolved set directly — an empty set is valid (flat graph, no hierarchy).
        # Do NOT convert empty set to None, as that triggers hardcoded fallbacks.
        parent_cache = self._build_parent_cache(all_edges, containment_edge_types=containment_edge_types)
        layer_sequence_map = {l.id: i for i, l in enumerate(request.layers)}

        # 3. Compute Assignments
        assignments: Dict[str, EntityAssignment] = {}
        unassigned_ids: List[str] = []

        # Sort nodes by depth (parents first) for inheritance
        nodes_by_depth = sorted(all_nodes, key=lambda n: len(self._get_ancestors(n.urn, parent_cache)))

        for node in nodes_by_depth:
            parent_id = parent_cache["parent_map"].get(node.urn)
            parent_assignment = assignments.get(parent_id) if parent_id else None

            result = self._resolve_assignment(
                node, parent_id, parent_assignment, rule_index, request.layers, layer_sequence_map,
                parent_cache=parent_cache,
            )

            if result:
                assignments[node.urn] = result
            else:
                unassigned_ids.append(node.urn)

        # 4. Prepare Result
        compute_time_ms = (time.time() - start_time) * 1000
        
        return LayerAssignmentResult(
            assignments=assignments,
            parentMap=parent_cache["parent_map"],
            edges=request.include_edges and all_edges or [], # Return edges if requested
            unassignedEntityIds=unassigned_ids,
            stats=LayerAssignmentStats(
                totalNodes=len(all_nodes),
                assignedNodes=len(assignments),
                computeTimeMs=compute_time_ms
            )
        )

    # ==========================================
    # Index Builders
    # ==========================================

    def _build_rule_index(self, layers: List[ViewLayerConfig]) -> Dict[str, Any]:
        by_type: Dict[str, List[Tuple[str, LayerAssignmentRuleConfig]]] = {}
        by_tag: Dict[str, List[Tuple[str, LayerAssignmentRuleConfig]]] = {}
        patterns: List[Tuple[str, LayerAssignmentRuleConfig, Any]] = [] # (layerId, rule, regex)
        # Scoped rules that match purely via scope_root_urn / conditions
        # (no type, tag, or urn_pattern selector). These are checked for
        # every candidate node — there is no faster index.
        scoped: List[Tuple[str, LayerAssignmentRuleConfig]] = []
        instances: Dict[str, Tuple[str, EntityAssignmentConfig]] = {}

        for layer in layers:
            # Index instance assignments
            if layer.entity_assignments:
                for config in layer.entity_assignments:
                    instances[config.entity_id] = (layer.id, config)

            # Index rules
            if layer.rules:
                for rule in layer.rules:
                    indexed = False
                    # Index by Entity Type
                    if rule.entity_types:
                        for type_ in rule.entity_types:
                            if type_ not in by_type: by_type[type_] = []
                            by_type[type_].append((layer.id, rule))
                        indexed = True

                    # Index by Tag
                    if rule.tags:
                        for tag in rule.tags:
                            if tag not in by_tag: by_tag[tag] = []
                            by_tag[tag].append((layer.id, rule))
                        indexed = True

                    # Compile Patterns
                    if rule.urn_pattern:
                        try:
                            # Convert glob-like to regex
                            pattern = rule.urn_pattern.replace('.', r'\.').replace('*', '.*').replace('?', '.')
                            regex = re.compile(f"^{pattern}$", re.IGNORECASE)
                            patterns.append((layer.id, rule, regex))
                            indexed = True
                        except Exception as e:
                            logger.warning(f"Invalid regex pattern {rule.urn_pattern}: {e}")

                    # Scoped-only rule: scope_root_urn and/or conditions, no selector
                    if not indexed and (rule.scope_root_urn or rule.conditions):
                        scoped.append((layer.id, rule))

            # Synthetic rules for layer.entityTypes (legacy support / basic config)
            if layer.entity_types:
                for type_ in layer.entity_types:
                    if type_ not in by_type: by_type[type_] = []
                    # Create synthetic rule
                    synth_rule = LayerAssignmentRuleConfig(
                        id=f"_type_{layer.id}_{type_}",
                        priority=0,
                        entityTypes=[type_]
                    )
                    by_type[type_].append((layer.id, synth_rule))

        return {
            "by_type": by_type,
            "by_tag": by_tag,
            "patterns": patterns,
            "scoped": scoped,
            "instances": instances
        }

    def _build_parent_cache(
        self,
        edges: List[GraphEdge],
        containment_edge_types: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        parent_map = {}
        # Use ontology-provided containment types.
        # None = caller didn't resolve ontology yet, use hardcoded fallback.
        # Empty set = ontology explicitly defines no containment (flat graph).
        if containment_edge_types is not None:
            ct = {t.upper() for t in containment_edge_types}
        else:
            logger.warning(
                "containment_edge_types is None in _build_parent_cache — "
                "falling back to hardcoded {CONTAINS, BELONGS_TO}. "
                "This should only happen during legacy/un-resolved paths."
            )
            ct = {"CONTAINS", "BELONGS_TO"}

        # Determine parent->child direction from edge semantics:
        # Convention: if source has *more* children than target for this edge type,
        # source is the parent. As a simple heuristic compatible with the existing
        # CONTAINS (parent->child) and BELONGS_TO (child->parent) semantics,
        # we check if the edge type matches the BELONGS_TO pattern (child is source).
        # For any other containment type we assume source=parent, target=child.
        belongs_to_upper = "BELONGS_TO"

        for edge in edges:
            et = edge.edge_type.upper() if isinstance(edge.edge_type, str) else str(edge.edge_type).upper()
            if et not in ct:
                continue
            if et == belongs_to_upper:
                # BELONGS_TO: source is child, target is parent
                parent_map[edge.source_urn] = edge.target_urn
            else:
                # Default containment direction: source is parent, target is child
                parent_map[edge.target_urn] = edge.source_urn

        return {"parent_map": parent_map}

    def _get_ancestors(self, entity_id: str, cache: Dict[str, Any]) -> List[str]:
        # Simple non-cached recursion for sorting phase
        # Could be optimized with full chain cache if needed
        parent_map = cache["parent_map"]
        ancestors = []
        current = entity_id
        visited = set()
        
        while current in parent_map and current not in visited:
            visited.add(current)
            parent = parent_map[current]
            ancestors.append(parent)
            current = parent
            
        return ancestors

    # ==========================================
    # Logic
    # ==========================================

    def _ancestor_set(self, entity_id: str, parent_cache: Dict[str, Any]) -> Set[str]:
        parent_map = parent_cache["parent_map"]
        ancestors: Set[str] = set()
        current = entity_id
        visited: Set[str] = set()
        while current in parent_map and current not in visited:
            visited.add(current)
            parent = parent_map[current]
            ancestors.add(parent)
            current = parent
        return ancestors

    def _match_condition(self, node: GraphNode, cond: RuleCondition) -> bool:
        # Pull the value from node.properties first; fall back to a few
        # common top-level fields for ergonomics so authors can target
        # displayName / entityType / urn without prefixing "properties.".
        field = cond.field
        if node.properties and field in node.properties:
            value = node.properties[field]
        elif field == "displayName":
            value = node.display_name
        elif field == "entityType":
            value = node.entity_type
        elif field == "urn":
            value = node.urn
        else:
            value = None

        op = cond.operator
        if op == RuleOperator.EXISTS:
            return value is not None
        if value is None:
            # Every operator except EXISTS treats a missing field as no match.
            return False

        target = cond.value
        if op == RuleOperator.EQUALS:
            return value == target
        if op == RuleOperator.NOT_EQUALS:
            return value != target
        sv = str(value).lower()
        st = str(target).lower() if target is not None else ""
        if op == RuleOperator.CONTAINS:
            return st in sv
        if op == RuleOperator.STARTS_WITH:
            return sv.startswith(st)
        if op == RuleOperator.ENDS_WITH:
            return sv.endswith(st)
        return False

    def _rule_predicates_pass(
        self,
        rule: LayerAssignmentRuleConfig,
        node: GraphNode,
        ancestors: Set[str],
    ) -> bool:
        if rule.scope_root_urn and rule.scope_root_urn not in ancestors:
            return False
        if rule.conditions:
            for c in rule.conditions:
                if not self._match_condition(node, c):
                    return False
        return True

    def _resolve_assignment(
        self,
        node: GraphNode,
        parent_id: Optional[str],
        parent_assignment: Optional[EntityAssignment],
        index: Dict[str, Any],
        layers: List[ViewLayerConfig],
        layer_sequence_map: Dict[str, int],
        parent_cache: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntityAssignment]:

        entity_id = node.urn
        entity_type = node.entity_type
        entity_tags = node.tags or []

        # 1. Instance Assignment (Highest Priority)
        instance_match = index["instances"].get(entity_id)
        if instance_match:
            layer_id, config = instance_match
            return EntityAssignment(
                entityId=entity_id,
                layerId=layer_id,
                logicalNodeId=config.logical_node_id,
                confidence=1.0,
                isInherited=False
            )

        # 2. Inheritance
        if parent_assignment and parent_assignment.layer_id != "unassigned": # Check validity
            # Check if parent assignment allows inheritance (check origin rule?)
            # For simplicity, if parent is assigned, we try to inherit.
            # We assume "inheritsChildren" is true by default or checked when parent was assigned

            # Note: We need to know if the parent matched via a rule that implies inheritance.
            # The simplified logic: If parent is assigned, child belongs to same layer unless overridden.
            return EntityAssignment(
                entityId=entity_id,
                layerId=parent_assignment.layer_id,
                logicalNodeId=parent_assignment.logical_node_id, # Inherit logical node too? Maybe.
                isInherited=True,
                inheritedFromId=parent_id, # or parent_assignment.entity_id
                confidence=parent_assignment.confidence
            )

        # 3. Rule Matching — scope_root_urn and conditions gate every candidate
        ancestors = self._ancestor_set(entity_id, parent_cache) if parent_cache else set()
        candidates = []

        def _try_add(layer_id: str, rule: LayerAssignmentRuleConfig) -> None:
            if self._rule_predicates_pass(rule, node, ancestors):
                candidates.append((layer_id, rule, rule.priority))

        # 3a. Type Rules
        if entity_type in index["by_type"]:
            for layer_id, rule in index["by_type"][entity_type]:
                _try_add(layer_id, rule)

        # 3b. Tag Rules
        for tag in entity_tags:
            if tag in index["by_tag"]:
                for layer_id, rule in index["by_tag"][tag]:
                    _try_add(layer_id, rule)

        # 3c. Pattern Rules
        for layer_id, rule, regex in index["patterns"]:
            if regex.match(entity_id):
                _try_add(layer_id, rule)

        # 3d. Scoped-only rules (scope_root_urn / conditions, no other selector)
        for layer_id, rule in index.get("scoped", []):
            _try_add(layer_id, rule)

        # Pick Winner
        if candidates:
            # Sort by priority desc
            candidates.sort(key=lambda x: x[2], reverse=True)
            winner_layer_id, winner_rule, _ = candidates[0]
            
            return EntityAssignment(
                entityId=entity_id,
                layerId=winner_layer_id,
                ruleId=winner_rule.id,
                confidence=1.0
            )

        # 4. Default
        if layers:
            return EntityAssignment(
                entityId=entity_id,
                layerId=layers[0].id,
                confidence=0.5 # Default fallback
            )

        return None

assignment_engine = AssignmentEngine()
