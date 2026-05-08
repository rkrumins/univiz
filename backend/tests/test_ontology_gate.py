"""Unit tests for ``backend.app.ontology.gate.check_resolution``.

The gate is pure — no DB, no HTTP — so we can drive every branch
with raw dicts and assert ResolutionReport fields directly.
"""
from backend.app.ontology.gate import (
    check_resolution,
    compute_fingerprint_from_ontology_orm,
)


def _make_relationship(*, is_containment=None, is_lineage=None, name="Rel"):
    """Build a raw relationship dict. Omitting a flag leaves the key
    out of the dict so the gate can detect "explicitly classified"
    vs "defaulted by parser"."""
    out = {"name": name}
    if is_containment is not None:
        out["is_containment"] = is_containment
    if is_lineage is not None:
        out["is_lineage"] = is_lineage
    return out


def _make_entity(*, level=None, name="Entity"):
    out = {"name": name}
    if level is not None:
        out["hierarchy"] = {"level": level}
    return out


def _kwargs(**overrides):
    base = dict(
        ontology_id="ont1",
        ontology_version=1,
        ontology_is_published=False,
        ontology_revision=0,
        entity_type_definitions_raw={},
        relationship_type_definitions_raw={},
        introspected_entity_ids=[],
        introspected_edge_ids=[],
    )
    base.update(overrides)
    return base


# ── Hard criteria ─────────────────────────────────────────────────────


def test_full_pass():
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={
            "Table": _make_entity(level=2, name="Table"),
            "Column": _make_entity(level=3, name="Column"),
        },
        relationship_type_definitions_raw={
            "HAS_COL": _make_relationship(is_containment=True, is_lineage=False),
            "FLOWS_TO": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table", "Column"],
        introspected_edge_ids=["HAS_COL", "FLOWS_TO"],
    ))
    assert report.resolved is True
    assert report.blocking_reasons == []
    assert report.has_lineage is True
    assert report.fingerprint and len(report.fingerprint) == 64


def test_missing_entity_type_blocks():
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table", "Sensor"],
        introspected_edge_ids=["FLOWS"],
    ))
    assert report.resolved is False
    assert "missing_entity_types" in report.blocking_reasons
    assert "Sensor" in report.missing_entity_types
    assert "Table" not in report.missing_entity_types


def test_missing_edge_type_blocks():
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS", "EMITS"],
    ))
    assert report.resolved is False
    assert "missing_edge_types" in report.blocking_reasons
    assert "EMITS" in report.missing_edge_types


def test_unclassified_relationship_blocks():
    # "DEPENDS_ON" exists in the ontology but neither flag is set —
    # the user never declared whether it's containment or lineage.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "DEPENDS_ON": {"name": "Depends On"},  # no flags
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["DEPENDS_ON", "FLOWS"],
    ))
    assert report.resolved is False
    assert "unclassified_relationships" in report.blocking_reasons
    ids = [g.id for g in report.unclassified_relationships]
    assert ids == ["DEPENDS_ON"]


def test_explicit_false_flags_count_as_classified():
    # is_containment=False AND is_lineage=False is a valid "neither"
    # classification — the user explicitly declared the relationship
    # is neither containment nor lineage. Should not block.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "ANNOTATES": _make_relationship(is_containment=False, is_lineage=False),
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["ANNOTATES", "FLOWS"],
    ))
    assert report.unclassified_relationships == []
    assert "unclassified_relationships" not in report.blocking_reasons


def test_no_lineage_blocks():
    # Every relationship is classified, but none has is_lineage=True.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "HAS_COL": _make_relationship(is_containment=True, is_lineage=False),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["HAS_COL"],
    ))
    assert report.resolved is False
    assert "no_lineage" in report.blocking_reasons
    assert report.has_lineage is False


def test_has_lineage_uses_full_ontology_not_just_introspected():
    # Stats cache miss → introspected lists are empty. The ontology has
    # a lineage relationship, so the gate should NOT block on
    # ``no_lineage`` — that's a property of the ontology, not the
    # current graph contents.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS_TO": _make_relationship(is_containment=False, is_lineage=True),
            "HAS_COL": _make_relationship(is_containment=True, is_lineage=False),
        },
        introspected_entity_ids=[],
        introspected_edge_ids=[],
    ))
    assert report.resolved is True
    assert report.has_lineage is True
    assert "no_lineage" not in report.blocking_reasons


def test_unclassified_only_for_introspected_edges():
    # An ontology may carry definitions for relationships that don't
    # exist in this graph. Those don't need to be classified — only
    # introspected edges trigger the gate.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "UNUSED": {"name": "Unused"},  # no flags but not in graph
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    ))
    assert report.unclassified_relationships == []
    assert report.resolved is True


# ── Hierarchy warnings (advisory, do not flip resolved) ───────────────


def test_hierarchy_missing_level_warns_does_not_block():
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={
            "Table": {"name": "Table"},  # no hierarchy.level
        },
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    ))
    fields = {(w.entity_type, w.missing_field) for w in report.hierarchy_warnings}
    assert ("Table", "level") in fields
    assert report.resolved is True
    assert "missing_entity_types" not in report.blocking_reasons


def test_hierarchy_warns_only_for_introspected_entities():
    # Entity defined but NOT in the introspected graph — no warnings.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={
            "Table": _make_entity(level=1),
            "Unused": {"name": "Unused"},  # no level, but not in graph
        },
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    ))
    assert all(w.entity_type != "Unused" for w in report.hierarchy_warnings)


# ── Case-insensitive matching ─────────────────────────────────────────


def test_case_insensitive_entity_match():
    # Ontology defines 'table' (lowercase) but the graph reports 'Table'.
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"table": _make_entity(level=1, name="table")},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    ))
    assert "Table" not in report.missing_entity_types


def test_case_insensitive_edge_match():
    report = check_resolution(**_kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "flows": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    ))
    assert "FLOWS" not in report.missing_edge_types


# ── Fingerprint stability ─────────────────────────────────────────────


def test_fingerprint_stable_for_identical_inputs():
    kwargs = _kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    )
    fp1 = check_resolution(**kwargs).fingerprint
    fp2 = check_resolution(**kwargs).fingerprint
    assert fp1 == fp2


def test_fingerprint_changes_when_classification_flips():
    base = _kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    )
    edited = dict(base)
    edited["relationship_type_definitions_raw"] = {
        "FLOWS": _make_relationship(is_containment=True, is_lineage=False),
    }
    fp1 = check_resolution(**base).fingerprint
    fp2 = check_resolution(**edited).fingerprint
    assert fp1 != fp2


def test_fingerprint_stable_when_revision_unchanged_but_introspection_differs():
    # Introspection is NOT part of the fingerprint — the fingerprint
    # describes the ontology, not the graph it's evaluated against.
    base = _kwargs(
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    )
    other = dict(base)
    other["introspected_entity_ids"] = ["Table", "Sensor"]
    fp1 = check_resolution(**base).fingerprint
    fp2 = check_resolution(**other).fingerprint
    assert fp1 == fp2


def test_fingerprint_changes_on_revision_bump():
    base = _kwargs(
        ontology_revision=1,
        entity_type_definitions_raw={"Table": _make_entity(level=1)},
        relationship_type_definitions_raw={
            "FLOWS": _make_relationship(is_containment=False, is_lineage=True),
        },
        introspected_entity_ids=["Table"],
        introspected_edge_ids=["FLOWS"],
    )
    other = dict(base)
    other["ontology_revision"] = 2
    fp1 = check_resolution(**base).fingerprint
    fp2 = check_resolution(**other).fingerprint
    assert fp1 != fp2


# ── ORM helper ─────────────────────────────────────────────────────────


class _FakeOrm:
    def __init__(self, *, id, revision, entity_type_definitions, relationship_type_definitions):
        self.id = id
        self.revision = revision
        self.entity_type_definitions = entity_type_definitions
        self.relationship_type_definitions = relationship_type_definitions


def test_compute_fingerprint_from_ontology_orm_matches_check_resolution():
    import json
    entity_raw = {"Table": _make_entity(level=1)}
    rel_raw = {"FLOWS": _make_relationship(is_containment=False, is_lineage=True)}
    orm = _FakeOrm(
        id="ont42",
        revision=7,
        entity_type_definitions=json.dumps(entity_raw),
        relationship_type_definitions=json.dumps(rel_raw),
    )
    via_helper = compute_fingerprint_from_ontology_orm(orm)
    via_check = check_resolution(
        ontology_id="ont42",
        ontology_version=1,
        ontology_is_published=False,
        ontology_revision=7,
        entity_type_definitions_raw=entity_raw,
        relationship_type_definitions_raw=rel_raw,
        introspected_entity_ids=[],
        introspected_edge_ids=[],
    ).fingerprint
    assert via_helper == via_check
