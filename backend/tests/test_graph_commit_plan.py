"""Unit tests for the commit planner. Pure, no DB — this is the core
commit algorithm (validate -> content-address -> Merkle snapshot ->
diff -> blobs/partitions/events/delta)."""
import pytest

from backend.app.services.graph_versioning.commit import (
    EdgeState,
    EmptyCommitError,
    NodeState,
    plan_commit,
)
from backend.app.services.graph_versioning.validation import (
    GraphValidationError,
    OntologySpec,
)

PC = 256  # partition_count for tests


def _n(key, name="n", **kw):
    return NodeState(
        key=key,
        entity_type=kw.get("entity_type", "Table"),
        display_name=name,
        position=kw.get("position", {"x": 0, "y": 0}),
        properties=kw.get("properties", {}),
        tags=tuple(kw.get("tags", ())),
    )


def _genesis(n=10):
    nodes = {f"urn:{i}": _n(f"urn:{i}", f"n{i}") for i in range(n)}
    return plan_commit(
        base_snapshot=None,
        nodes=nodes,
        edges={},
        partition_count=PC,
        schema_mode="schemaless",
    )


def test_genesis_commit_creates_everything():
    plan = _genesis(10)
    assert len(plan.new_versions) == 10
    assert len(plan.change_events) == 10
    assert all(e.action == "created" for e in plan.change_events)
    assert plan.delta_summary["nodes_added"] == 10
    assert plan.root_hash == plan.new_snapshot.root_hash


def test_empty_genesis_refused():
    with pytest.raises(EmptyCommitError):
        plan_commit(
            base_snapshot=None, nodes={}, edges={},
            partition_count=PC, schema_mode="schemaless",
        )


def test_noop_commit_refused():
    base = _genesis(5).new_snapshot
    same_nodes = {f"urn:{i}": _n(f"urn:{i}", f"n{i}") for i in range(5)}
    with pytest.raises(EmptyCommitError, match="no change"):
        plan_commit(
            base_snapshot=base, nodes=same_nodes, edges={},
            partition_count=PC, schema_mode="schemaless",
        )


def test_incremental_commit_only_writes_changed_partition_and_new_blob():
    base_plan = _genesis(500)
    base = base_plan.new_snapshot
    nodes = {f"urn:{i}": _n(f"urn:{i}", f"n{i}") for i in range(500)}
    # Modify exactly one node's content.
    nodes["urn:123"] = _n("urn:123", "RENAMED")

    plan = plan_commit(
        base_snapshot=base, nodes=nodes, edges={},
        partition_count=PC, schema_mode="schemaless",
    )

    # Exactly one object changed -> one change event, one new blob.
    assert len(plan.change_events) == 1
    ev = plan.change_events[0]
    assert ev.object_id == "urn:123" and ev.action == "updated"
    assert ev.prev_content_hash and ev.new_content_hash
    assert ev.prev_content_hash != ev.new_content_hash
    assert len(plan.new_versions) == 1
    assert plan.new_versions[0].key == "urn:123"
    # Only the partition holding urn:123 is rewritten (structural
    # sharing): far fewer than the populated-partition count.
    assert 1 <= len(plan.changed_partitions) <= 2
    assert plan.delta_summary["nodes_modified"] == 1


def test_add_and_delete_tracked():
    base = _genesis(5).new_snapshot
    nodes = {f"urn:{i}": _n(f"urn:{i}", f"n{i}") for i in range(5)}
    del nodes["urn:4"]                       # delete one
    nodes["urn:new"] = _n("urn:new", "fresh")  # add one

    plan = plan_commit(
        base_snapshot=base, nodes=nodes, edges={},
        partition_count=PC, schema_mode="schemaless",
    )
    by = {(e.object_id, e.action) for e in plan.change_events}
    assert ("urn:new", "created") in by
    assert ("urn:4", "deleted") in by
    assert plan.delta_summary["nodes_added"] == 1
    assert plan.delta_summary["nodes_removed"] == 1
    # Deleted object contributes no new blob; added one does.
    assert {v.key for v in plan.new_versions} == {"urn:new"}


def test_identical_content_dedups_to_one_blob():
    # Two new nodes with byte-identical content -> one version row.
    base = _genesis(3).new_snapshot
    nodes = {f"urn:{i}": _n(f"urn:{i}", f"n{i}") for i in range(3)}
    nodes["urn:x"] = _n("urn:x", "SAME", properties={"k": 1})
    nodes["urn:y"] = _n("urn:y", "SAME", properties={"k": 1})

    plan = plan_commit(
        base_snapshot=base, nodes=nodes, edges={},
        partition_count=PC, schema_mode="schemaless",
    )
    # urn:x and urn:y share content_hash -> deduped in the plan.
    assert len(plan.new_versions) == 1
    assert len(plan.change_events) == 2  # both still get a 'created' event


def test_edges_supported_and_validated():
    nodes = {"urn:a": _n("urn:a"), "urn:b": _n("urn:b")}
    edges = {"e1": EdgeState("e1", "urn:a", "urn:b", "flows_to")}
    plan = plan_commit(
        base_snapshot=None, nodes=nodes, edges=edges,
        partition_count=PC, schema_mode="schemaless",
    )
    assert plan.delta_summary["edges_added"] == 1
    assert any(v.kind == "edge" for v in plan.new_versions)


def test_dangling_edge_blocks_commit():
    with pytest.raises(GraphValidationError):
        plan_commit(
            base_snapshot=None,
            nodes={"urn:a": _n("urn:a")},
            edges={"e1": EdgeState("e1", "urn:a", "urn:ghost", "r")},
            partition_count=PC, schema_mode="schemaless",
        )


def test_strict_mode_blocks_unknown_type():
    onto = OntologySpec(
        entity_types=frozenset({"Table"}),
        relationship_types=frozenset(),
    )
    with pytest.raises(GraphValidationError):
        plan_commit(
            base_snapshot=None,
            nodes={"urn:a": _n("urn:a", entity_type="Ghost")},
            edges={},
            partition_count=PC, schema_mode="strict", ontology=onto,
        )
