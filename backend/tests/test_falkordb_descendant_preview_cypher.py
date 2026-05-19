"""Unit tests for `_build_descendant_preview_cypher` — pure query/param builder.

Tests run without FalkorDB. They guard the Cypher we emit and the parameter
binding behavior across every FilterOperator, so the preview semantics stay
in lockstep with the documented contract.
"""
import pytest

from backend.app.providers.falkordb_provider import _build_descendant_preview_cypher
from backend.common.models.graph import FilterOperator, PropertyFilter


def _build(**kwargs):
    defaults = dict(
        rel_list=["CONTAINS"],
        name_substring=None,
        entity_types=None,
        property_filter=None,
        sample_limit=50,
        hard_cap=5000,
    )
    defaults.update(kwargs)
    return _build_descendant_preview_cypher(**defaults)


class TestStructure:
    def test_single_rel_type_uses_variable_length_path(self):
        cypher, params = _build()
        assert "MATCH (p)-[:CONTAINS*1..]->(d)" in cypher
        assert "WITH DISTINCT d" in cypher
        assert "count(d) AS total" in cypher
        assert "collect(d)[..$sampleLimit] AS sample" in cypher
        assert params["sampleLimit"] == 50 and params["hardCap"] == 5000

    def test_multi_rel_types_use_alternation(self):
        cypher, _ = _build(rel_list=["CONTAINS", "HAS_FIELD"])
        assert "MATCH (p)-[:CONTAINS|HAS_FIELD*1..]->(d)" in cypher

    def test_rel_type_sanitization_for_unsafe_chars(self):
        cypher, _ = _build(rel_list=["A;DROP"])
        assert "[:A_DROP*1..]" in cypher
        assert "DROP" not in cypher.replace("A_DROP", "")  # no raw injection survived


class TestNameSubstring:
    def test_name_substring_emits_case_insensitive_clause(self):
        cypher, params = _build(name_substring="foo")
        assert "toLower(d.displayName) CONTAINS toLower($nameSub)" in cypher
        assert "toLower(d.urn) CONTAINS toLower($nameSub)" in cypher
        assert params["nameSub"] == "foo"

    def test_no_name_clause_when_absent(self):
        cypher, params = _build(name_substring=None)
        assert "$nameSub" not in cypher
        assert "nameSub" not in params


class TestEntityTypesClause:
    def test_entity_types_filter(self):
        cypher, params = _build(entity_types=["dataset", "table"])
        assert "d.entityType IN $entityTypes" in cypher
        assert params["entityTypes"] == ["dataset", "table"]

    def test_no_entity_types_clause_when_absent(self):
        cypher, params = _build(entity_types=None)
        assert "$entityTypes" not in cypher
        assert "entityTypes" not in params


class TestPropertyFilterOperators:
    @pytest.mark.parametrize(
        "op,expected_fragment,value",
        [
            (FilterOperator.EQUALS, "d.owner = $pfv", "team-a"),
            (FilterOperator.CONTAINS, "toLower(toString(d.owner)) CONTAINS toLower(toString($pfv))", "alpha"),
            (FilterOperator.STARTS_WITH, "toLower(toString(d.owner)) STARTS WITH toLower(toString($pfv))", "team"),
            (FilterOperator.ENDS_WITH, "toLower(toString(d.owner)) ENDS WITH toLower(toString($pfv))", "-a"),
            (FilterOperator.GT, "d.owner > $pfv", 10),
            (FilterOperator.LT, "d.owner < $pfv", 100),
            (FilterOperator.IN, "d.owner IN $pfv", ["a", "b"]),
            (FilterOperator.NOT_IN, "NOT d.owner IN $pfv", ["a"]),
        ],
    )
    def test_value_operators_param_bind(self, op, expected_fragment, value):
        cypher, params = _build(
            property_filter=PropertyFilter(field="owner", operator=op, value=value)
        )
        assert expected_fragment in cypher
        assert params["pfv"] == value

    def test_exists_drops_pfv_param(self):
        cypher, params = _build(
            property_filter=PropertyFilter(field="owner", operator=FilterOperator.EXISTS, value=None)
        )
        assert "d.owner IS NOT NULL" in cypher
        assert "pfv" not in params

    def test_not_exists_drops_pfv_param(self):
        cypher, params = _build(
            property_filter=PropertyFilter(field="owner", operator=FilterOperator.NOT_EXISTS, value=None)
        )
        assert "d.owner IS NULL" in cypher
        assert "pfv" not in params

    def test_field_sanitization(self):
        cypher, _ = _build(
            property_filter=PropertyFilter(field="a.b;DROP", operator=FilterOperator.EQUALS, value="x")
        )
        # Dots / semicolons replaced; original field never appears verbatim
        assert "d.a_b_DROP" in cypher
        assert "d.a.b;DROP" not in cypher


class TestCombined:
    def test_all_filters_combined(self):
        cypher, params = _build(
            rel_list=["CONTAINS", "HAS_FIELD"],
            name_substring="foo",
            entity_types=["dataset"],
            property_filter=PropertyFilter(field="owner", operator=FilterOperator.STARTS_WITH, value="team"),
            sample_limit=10,
            hard_cap=200,
        )
        # WHERE has parent first, then name, then entityTypes, then property
        assert cypher.index("p.urn = $parent") < cypher.index("toLower(d.displayName)")
        assert cypher.index("toLower(d.displayName)") < cypher.index("d.entityType IN")
        assert cypher.index("d.entityType IN") < cypher.index("STARTS WITH")
        assert params == {
            "parent": "",
            "sampleLimit": 10,
            "hardCap": 200,
            "nameSub": "foo",
            "entityTypes": ["dataset"],
            "pfv": "team",
        }
