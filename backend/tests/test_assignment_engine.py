"""
Phase 3 — Unit tests for backend.app.services.assignment_engine.AssignmentEngine

Tests target the pure-logic internal helpers (_build_parent_cache, _get_ancestors,
_build_rule_index) which do not depend on the module-level context_engine singleton.
"""
import pytest

from backend.app.services.assignment_engine import AssignmentEngine
from backend.common.models.assignment import (
    EntityAssignmentConfig,
    LayerAssignmentRuleConfig,
    RuleCondition,
    RuleOperator,
    ViewLayerConfig,
)
from backend.common.models.graph import GraphEdge, GraphNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _edge(src: str, tgt: str, etype: str = "CONTAINS") -> GraphEdge:
    return GraphEdge(id=f"{src}->{tgt}", sourceUrn=src, targetUrn=tgt, edgeType=etype)


# ---------------------------------------------------------------------------
# _build_parent_cache
# ---------------------------------------------------------------------------


class TestBuildParentCache:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def test_contains_edges_map_target_to_source(self):
        """CONTAINS: source=parent, target=child -> child maps to parent."""
        edges = [_edge("urn:parent", "urn:child", "CONTAINS")]
        result = self.engine._build_parent_cache(edges, containment_edge_types={"CONTAINS"})
        assert result["parent_map"]["urn:child"] == "urn:parent"

    def test_belongs_to_edges_map_source_to_target(self):
        """BELONGS_TO: source=child, target=parent -> child maps to parent."""
        edges = [_edge("urn:child", "urn:parent", "BELONGS_TO")]
        result = self.engine._build_parent_cache(edges, containment_edge_types={"BELONGS_TO"})
        assert result["parent_map"]["urn:child"] == "urn:parent"

    def test_non_containment_edges_are_ignored(self):
        """Only edges with types in containment_edge_types are processed."""
        edges = [
            _edge("urn:a", "urn:b", "CONTAINS"),
            _edge("urn:c", "urn:d", "TRANSFORMS"),
        ]
        result = self.engine._build_parent_cache(edges, containment_edge_types={"CONTAINS"})
        assert "urn:b" in result["parent_map"]
        assert "urn:c" not in result["parent_map"]
        assert "urn:d" not in result["parent_map"]

    def test_mixed_containment_types(self):
        """Both CONTAINS and BELONGS_TO in one call."""
        edges = [
            _edge("urn:parent1", "urn:child1", "CONTAINS"),
            _edge("urn:child2", "urn:parent2", "BELONGS_TO"),
        ]
        result = self.engine._build_parent_cache(
            edges, containment_edge_types={"CONTAINS", "BELONGS_TO"},
        )
        assert result["parent_map"]["urn:child1"] == "urn:parent1"
        assert result["parent_map"]["urn:child2"] == "urn:parent2"

    def test_empty_containment_types_means_flat(self):
        """Empty set = ontology says no containment -> parent_map is empty."""
        edges = [_edge("urn:a", "urn:b", "CONTAINS")]
        result = self.engine._build_parent_cache(edges, containment_edge_types=set())
        assert result["parent_map"] == {}

    def test_none_containment_types_uses_fallback(self):
        """None = legacy path -> falls back to hardcoded {CONTAINS, BELONGS_TO}."""
        edges = [_edge("urn:parent", "urn:child", "CONTAINS")]
        result = self.engine._build_parent_cache(edges, containment_edge_types=None)
        assert result["parent_map"]["urn:child"] == "urn:parent"


# ---------------------------------------------------------------------------
# _get_ancestors
# ---------------------------------------------------------------------------


class TestGetAncestors:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def test_linear_chain(self):
        """a -> b -> c produces ancestors [b, c] for a, [c] for b, [] for c."""
        cache = {"parent_map": {"urn:a": "urn:b", "urn:b": "urn:c"}}
        ancestors = self.engine._get_ancestors("urn:a", cache)
        assert ancestors == ["urn:b", "urn:c"]

    def test_no_parent_returns_empty(self):
        cache = {"parent_map": {}}
        ancestors = self.engine._get_ancestors("urn:orphan", cache)
        assert ancestors == []

    def test_cycle_protection(self):
        """Cycles do not cause infinite loop; partial chain is returned."""
        cache = {"parent_map": {"urn:a": "urn:b", "urn:b": "urn:a"}}
        ancestors = self.engine._get_ancestors("urn:a", cache)
        # Should get at least one ancestor before cycle stops
        assert "urn:b" in ancestors
        # Must terminate (cycle-safe)
        assert len(ancestors) <= 2


# ---------------------------------------------------------------------------
# _build_rule_index
# ---------------------------------------------------------------------------


class TestBuildRuleIndex:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def test_type_rules_indexed(self):
        rule = LayerAssignmentRuleConfig(id="r1", priority=10, entityTypes=["dataset"])
        layer = ViewLayerConfig(id="L1", name="Layer 1", color="#fff", order=0, rules=[rule])
        index = self.engine._build_rule_index([layer])
        assert "dataset" in index["by_type"]
        assert index["by_type"]["dataset"][0][0] == "L1"

    def test_tag_rules_indexed(self):
        rule = LayerAssignmentRuleConfig(id="r2", priority=5, tags=["pii"])
        layer = ViewLayerConfig(id="L2", name="Layer 2", color="#000", order=1, rules=[rule])
        index = self.engine._build_rule_index([layer])
        assert "pii" in index["by_tag"]
        assert index["by_tag"]["pii"][0][0] == "L2"

    def test_pattern_rules_compiled(self):
        rule = LayerAssignmentRuleConfig(id="r3", priority=3, urnPattern="urn:li:dataset:*")
        layer = ViewLayerConfig(id="L3", name="Layer 3", color="#aaa", order=2, rules=[rule])
        index = self.engine._build_rule_index([layer])
        assert len(index["patterns"]) == 1
        _, _, regex = index["patterns"][0]
        assert regex.match("urn:li:dataset:foo")
        assert not regex.match("urn:li:chart:foo")

    def test_instance_assignments_indexed(self):
        assignment = EntityAssignmentConfig(
            entityId="urn:x",
            layerId="L4",
            priority=100,
            assignedBy="test",
            assignedAt="2026-01-01",
        )
        layer = ViewLayerConfig(
            id="L4", name="Layer 4", color="#bbb", order=3,
            entityAssignments=[assignment],
        )
        index = self.engine._build_rule_index([layer])
        assert "urn:x" in index["instances"]
        assert index["instances"]["urn:x"][0] == "L4"

    def test_entity_types_on_layer_create_synthetic_rules(self):
        layer = ViewLayerConfig(
            id="L5", name="Layer 5", color="#ccc", order=4,
            entityTypes=["chart"],
        )
        index = self.engine._build_rule_index([layer])
        assert "chart" in index["by_type"]

    def test_empty_layers_returns_empty_index(self):
        index = self.engine._build_rule_index([])
        assert index["by_type"] == {}
        assert index["by_tag"] == {}
        assert index["patterns"] == []
        assert index["instances"] == {}
        assert index["scoped"] == []

    def test_scope_only_rule_lands_in_scoped_bucket(self):
        rule = LayerAssignmentRuleConfig(
            id="r-scope", priority=10, scopeRootUrn="urn:p"
        )
        layer = ViewLayerConfig(
            id="L1", name="L1", color="#fff", order=0, rules=[rule]
        )
        index = self.engine._build_rule_index([layer])
        assert ("L1", rule) in index["scoped"]
        assert index["by_type"] == {} and index["by_tag"] == {} and index["patterns"] == []


# ---------------------------------------------------------------------------
# _match_condition / _rule_predicates_pass
# ---------------------------------------------------------------------------


def _node(urn: str, *, entity_type: str = "dataset", display_name: str = "n",
          properties=None, tags=None) -> GraphNode:
    return GraphNode(
        urn=urn,
        entityType=entity_type,
        displayName=display_name,
        properties=properties or {},
        tags=tags or [],
    )


class TestMatchCondition:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def test_equals_on_property(self):
        node = _node("urn:a", properties={"owner": "team-a"})
        cond = RuleCondition(field="owner", operator=RuleOperator.EQUALS, value="team-a")
        assert self.engine._match_condition(node, cond) is True

    def test_not_equals_on_property(self):
        node = _node("urn:a", properties={"owner": "team-a"})
        cond = RuleCondition(field="owner", operator=RuleOperator.NOT_EQUALS, value="team-b")
        assert self.engine._match_condition(node, cond) is True

    def test_contains_case_insensitive(self):
        node = _node("urn:a", properties={"owner": "Team-Alpha"})
        cond = RuleCondition(field="owner", operator=RuleOperator.CONTAINS, value="alpha")
        assert self.engine._match_condition(node, cond) is True

    def test_starts_with(self):
        node = _node("urn:a", properties={"owner": "team-a"})
        cond = RuleCondition(field="owner", operator=RuleOperator.STARTS_WITH, value="team")
        assert self.engine._match_condition(node, cond) is True

    def test_ends_with(self):
        node = _node("urn:a", properties={"owner": "team-a"})
        cond = RuleCondition(field="owner", operator=RuleOperator.ENDS_WITH, value="-a")
        assert self.engine._match_condition(node, cond) is True

    def test_exists_true_when_property_present(self):
        node = _node("urn:a", properties={"owner": "team-a"})
        cond = RuleCondition(field="owner", operator=RuleOperator.EXISTS)
        assert self.engine._match_condition(node, cond) is True

    def test_exists_false_when_missing(self):
        node = _node("urn:a")
        cond = RuleCondition(field="owner", operator=RuleOperator.EXISTS)
        assert self.engine._match_condition(node, cond) is False

    def test_missing_property_is_no_match_for_non_exists(self):
        node = _node("urn:a")
        cond = RuleCondition(field="owner", operator=RuleOperator.EQUALS, value="team-a")
        assert self.engine._match_condition(node, cond) is False

    def test_field_falls_back_to_displayName(self):
        node = _node("urn:a", display_name="Foo")
        cond = RuleCondition(field="displayName", operator=RuleOperator.EQUALS, value="Foo")
        assert self.engine._match_condition(node, cond) is True


class TestRulePredicatesPass:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def test_scope_requires_ancestor_match(self):
        rule = LayerAssignmentRuleConfig(id="r", priority=1, scopeRootUrn="urn:p")
        node = _node("urn:c")
        assert self.engine._rule_predicates_pass(rule, node, ancestors=set()) is False
        assert self.engine._rule_predicates_pass(rule, node, ancestors={"urn:p"}) is True

    def test_conditions_all_must_pass(self):
        rule = LayerAssignmentRuleConfig(
            id="r", priority=1,
            conditions=[
                RuleCondition(field="owner", operator=RuleOperator.EQUALS, value="team-a"),
                RuleCondition(field="env", operator=RuleOperator.EQUALS, value="prod"),
            ],
        )
        n_match = _node("urn:c", properties={"owner": "team-a", "env": "prod"})
        n_partial = _node("urn:c", properties={"owner": "team-a", "env": "dev"})
        assert self.engine._rule_predicates_pass(rule, n_match, ancestors=set()) is True
        assert self.engine._rule_predicates_pass(rule, n_partial, ancestors=set()) is False

    def test_scope_and_conditions_combined(self):
        rule = LayerAssignmentRuleConfig(
            id="r", priority=1, scopeRootUrn="urn:p",
            conditions=[RuleCondition(field="owner", operator=RuleOperator.EQUALS, value="team-a")],
        )
        node = _node("urn:c", properties={"owner": "team-a"})
        assert self.engine._rule_predicates_pass(rule, node, ancestors={"urn:p"}) is True
        assert self.engine._rule_predicates_pass(rule, node, ancestors=set()) is False


# ---------------------------------------------------------------------------
# _resolve_assignment — scoped/conditions interaction with inheritance
# ---------------------------------------------------------------------------


class TestResolveAssignmentScoped:
    def setup_method(self):
        self.engine = AssignmentEngine()

    def _index(self, layers):
        return self.engine._build_rule_index(layers)

    def test_scoped_rule_assigns_descendant(self):
        rule = LayerAssignmentRuleConfig(
            id="r1", priority=100, entityTypes=["dataset"], scopeRootUrn="urn:p",
        )
        layer = ViewLayerConfig(id="L1", name="L1", color="#fff", order=0, rules=[rule])
        index = self._index([layer])
        parent_cache = {"parent_map": {"urn:c": "urn:p"}}
        node = _node("urn:c", entity_type="dataset")
        result = self.engine._resolve_assignment(
            node, parent_id="urn:p", parent_assignment=None,
            index=index, layers=[layer], layer_sequence_map={"L1": 0},
            parent_cache=parent_cache,
        )
        assert result is not None and result.layer_id == "L1" and result.rule_id == "r1"

    def test_scoped_rule_skips_non_descendant(self):
        rule = LayerAssignmentRuleConfig(
            id="r1", priority=100, entityTypes=["dataset"], scopeRootUrn="urn:p",
        )
        layer = ViewLayerConfig(id="L1", name="L1", color="#fff", order=0, rules=[rule])
        index = self._index([layer])
        parent_cache = {"parent_map": {}}
        node = _node("urn:other", entity_type="dataset")
        result = self.engine._resolve_assignment(
            node, parent_id=None, parent_assignment=None,
            index=index, layers=[layer], layer_sequence_map={"L1": 0},
            parent_cache=parent_cache,
        )
        # Falls back to default (first layer at confidence 0.5), not the scoped rule.
        assert result is not None and result.rule_id is None and result.confidence == 0.5

    def test_conditions_filter_rule_match(self):
        rule = LayerAssignmentRuleConfig(
            id="r1", priority=100, entityTypes=["dataset"], scopeRootUrn="urn:p",
            conditions=[RuleCondition(field="owner", operator=RuleOperator.EQUALS, value="team-a")],
        )
        layer = ViewLayerConfig(id="L1", name="L1", color="#fff", order=0, rules=[rule])
        index = self._index([layer])
        parent_cache = {"parent_map": {"urn:c": "urn:p"}}
        n_match = _node("urn:c", entity_type="dataset", properties={"owner": "team-a"})
        n_skip = _node("urn:c2", entity_type="dataset", properties={"owner": "team-b"})
        r1 = self.engine._resolve_assignment(
            n_match, parent_id="urn:p", parent_assignment=None,
            index=index, layers=[layer], layer_sequence_map={"L1": 0},
            parent_cache={"parent_map": {"urn:c": "urn:p"}},
        )
        r2 = self.engine._resolve_assignment(
            n_skip, parent_id="urn:p", parent_assignment=None,
            index=index, layers=[layer], layer_sequence_map={"L1": 0},
            parent_cache={"parent_map": {"urn:c2": "urn:p"}},
        )
        assert r1.rule_id == "r1"
        assert r2.rule_id is None  # filtered out, falls to default

    def test_inheritance_pre_empts_scoped_rule(self):
        # If the parent is assigned, the child inherits before rules are consulted —
        # documented intended interaction.
        from backend.common.models.assignment import EntityAssignment
        rule = LayerAssignmentRuleConfig(
            id="r1", priority=100, entityTypes=["dataset"], scopeRootUrn="urn:p",
        )
        layer_a = ViewLayerConfig(id="LA", name="LA", color="#fff", order=0)
        layer_b = ViewLayerConfig(id="LB", name="LB", color="#000", order=1, rules=[rule])
        index = self._index([layer_a, layer_b])
        parent_cache = {"parent_map": {"urn:c": "urn:p"}}
        parent_assignment = EntityAssignment(
            entityId="urn:p", layerId="LA", isInherited=False, confidence=1.0
        )
        node = _node("urn:c", entity_type="dataset")
        result = self.engine._resolve_assignment(
            node, parent_id="urn:p", parent_assignment=parent_assignment,
            index=index, layers=[layer_a, layer_b], layer_sequence_map={"LA": 0, "LB": 1},
            parent_cache=parent_cache,
        )
        # Inherits from parent (LA), not the scoped rule's LB
        assert result is not None and result.layer_id == "LA" and result.is_inherited is True
