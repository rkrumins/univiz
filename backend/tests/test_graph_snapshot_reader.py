"""Unit tests for snapshot reconstruction + working-set application.
Pure logic, no DB (fetchers injected)."""
import gzip

import pytest

from backend.app.services.graph_versioning.commit import EdgeState, NodeState
from backend.app.services.graph_versioning.manifest import (
    ManifestEntry,
    build_snapshot,
    decode_partition_entries,
)
from backend.app.services.graph_versioning.snapshot_reader import (
    WorkingSetError,
    apply_changes,
    rebuild_snapshot,
)


def _entries(n):
    return [ManifestEntry(f"urn:{i}", "node", f"h{i}") for i in range(n)]


def _fetchers(snap):
    root = sorted((i, p.manifest_hash) for i, p in snap.partitions.items())
    by_hash = {p.manifest_hash: dict(p.entries) for p in snap.partitions.values()}
    return (lambda rh: root), (lambda mh: by_hash[mh])


def test_partition_gzip_roundtrips_through_decoder():
    snap = build_snapshot(_entries(50), 64)
    p = next(iter(snap.partitions.values()))
    restored = decode_partition_entries(gzip.decompress(p.gzip_bytes()))
    assert restored == dict(p.entries)


def test_rebuild_roundtrips_root_and_partitions():
    snap = build_snapshot(_entries(300), 128)
    fr, fp = _fetchers(snap)
    rebuilt = rebuild_snapshot(
        root_hash=snap.root_hash, partition_count=128,
        fetch_root=fr, fetch_partition=fp,
    )
    assert rebuilt.root_hash == snap.root_hash
    assert set(rebuilt.partitions) == set(snap.partitions)
    for idx, p in snap.partitions.items():
        assert rebuilt.partitions[idx].manifest_hash == p.manifest_hash
        assert dict(rebuilt.partitions[idx].entries) == dict(p.entries)


def test_none_or_unknown_root_is_empty_snapshot():
    empty = rebuild_snapshot(
        root_hash=None, partition_count=64,
        fetch_root=lambda h: None, fetch_partition=lambda h: {},
    )
    assert empty.partitions == {}
    unknown = rebuild_snapshot(
        root_hash="deadbeef", partition_count=64,
        fetch_root=lambda h: None, fetch_partition=lambda h: {},
    )
    assert unknown.partitions == {}


def test_corrupt_manifest_detected():
    snap = build_snapshot(_entries(20), 32)
    fr, _ = _fetchers(snap)
    # Partition fetcher returns tampered entries -> rebuilt root differs.
    with pytest.raises(ValueError, match="integrity"):
        rebuild_snapshot(
            root_hash=snap.root_hash, partition_count=32,
            fetch_root=fr,
            fetch_partition=lambda mh: {"urn:tampered": ("node", "x")},
        )


# ── apply_changes ──────────────────────────────────────────────────

def _node(k, name="n"):
    return NodeState(k, "T", name, {"x": 0, "y": 0}, {})


def test_apply_add_update_delete_nodes():
    base = {"urn:a": _node("urn:a", "a")}
    nodes, edges = apply_changes(
        base_nodes=base, base_edges={},
        changes=[
            {"change_type": "add_node", "payload": {
                "key": "urn:b", "entity_type": "T", "display_name": "b",
                "position": {"x": 1, "y": 1}, "properties": {}, "tags": []}},
            {"change_type": "update_node", "payload": {
                "key": "urn:a", "entity_type": "T", "display_name": "a2",
                "position": {"x": 0, "y": 0}, "properties": {}, "tags": []}},
        ],
    )
    assert set(nodes) == {"urn:a", "urn:b"}
    assert nodes["urn:a"].display_name == "a2"
    # base map not mutated
    assert base["urn:a"].display_name == "a"


def test_apply_edge_lifecycle_and_delete():
    base_n = {"urn:a": _node("urn:a"), "urn:b": _node("urn:b")}
    nodes, edges = apply_changes(
        base_nodes=base_n, base_edges={},
        changes=[
            {"change_type": "add_edge", "payload": {
                "key": "e1", "source_key": "urn:a", "target_key": "urn:b",
                "edge_type": "r", "properties": {}}},
            {"change_type": "delete_node", "payload": {"key": "urn:b"}},
        ],
    )
    assert "e1" in edges
    assert "urn:b" not in nodes  # dangling-edge now; validator catches at commit


@pytest.mark.parametrize("bad", [
    {"change_type": "update_node", "payload": {"key": "ghost"}},
    {"change_type": "delete_node", "payload": {"key": "ghost"}},
    {"change_type": "delete_edge", "payload": {"key": "ghost"}},
    {"change_type": "nonsense", "payload": {}},
])
def test_impossible_ops_raise(bad):
    with pytest.raises(WorkingSetError):
        apply_changes(base_nodes={}, base_edges={}, changes=[bad])


def test_duplicate_add_raises():
    with pytest.raises(WorkingSetError, match="already exists"):
        apply_changes(
            base_nodes={"urn:a": _node("urn:a")}, base_edges={},
            changes=[{"change_type": "add_node", "payload": {
                "key": "urn:a", "entity_type": "T", "display_name": "x",
                "position": None, "properties": {}, "tags": []}}],
        )
