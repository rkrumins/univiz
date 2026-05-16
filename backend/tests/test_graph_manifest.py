"""Unit tests for the partitioned 2-level Merkle manifest.

These prove the load-bearing scaling properties: structural sharing
(an edit touches one partition) and Merkle-pruned diff (O(changed
partitions)). Pure logic, no DB.
"""
import pytest

from backend.app.services.graph_versioning.manifest import (
    ManifestEntry,
    build_snapshot,
    diff_snapshots,
    partition_for,
)


def _entries(n: int) -> list[ManifestEntry]:
    return [
        ManifestEntry(key=f"urn:n{i}", kind="node", content_hash=f"h{i}")
        for i in range(n)
    ]


def test_partition_for_is_deterministic_and_in_range():
    for k in ("urn:a", "urn:b", "edge:1", "x" * 200):
        p = partition_for(k, 4096)
        assert 0 <= p < 4096
        assert p == partition_for(k, 4096)


def test_partition_distribution_is_reasonable():
    counts = {}
    for i in range(20000):
        p = partition_for(f"urn:n{i}", 256)
        counts[p] = counts.get(p, 0) + 1
    # No catastrophic skew: all 256 buckets used, max bucket < 4x mean.
    assert len(counts) == 256
    mean = 20000 / 256
    assert max(counts.values()) < mean * 4


def test_build_snapshot_root_is_stable():
    e = _entries(50)
    a = build_snapshot(e, 64)
    b = build_snapshot(list(reversed(e)), 64)  # insertion order irrelevant
    assert a.root_hash == b.root_hash


def test_duplicate_key_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        build_snapshot(
            [
                ManifestEntry("urn:dup", "node", "h1"),
                ManifestEntry("urn:dup", "node", "h2"),
            ],
            64,
        )


def test_invalid_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        build_snapshot([ManifestEntry("urn:x", "widget", "h")], 64)


def test_structural_sharing_one_edit_touches_one_partition():
    base = _entries(5000)
    snap_a = build_snapshot(base, 4096)

    # Change exactly one node's content.
    changed = list(base)
    target = changed[1234]
    changed[1234] = ManifestEntry(target.key, "node", "CHANGED")
    snap_b = build_snapshot(changed, 4096)

    # Root differs (something changed)...
    assert snap_a.root_hash != snap_b.root_hash

    # ...but exactly ONE partition manifest hash differs; every other
    # partition is byte-identical by hash (reused, not rewritten).
    changed_idx = partition_for(target.key, 4096)
    differing = [
        idx
        for idx in set(snap_a.partitions) | set(snap_b.partitions)
        if snap_a.partitions.get(idx) and snap_b.partitions.get(idx)
        and snap_a.partitions[idx].manifest_hash
        != snap_b.partitions[idx].manifest_hash
    ]
    assert differing == [changed_idx]

    shared = sum(
        1
        for idx in set(snap_a.partitions) & set(snap_b.partitions)
        if snap_a.partitions[idx].manifest_hash
        == snap_b.partitions[idx].manifest_hash
    )
    # Vast majority of partitions are structurally shared.
    assert shared >= len(set(snap_a.partitions)) - 1


def test_diff_is_merkle_pruned():
    base = _entries(5000)
    snap_a = build_snapshot(base, 4096)
    changed = list(base)
    changed[42] = ManifestEntry(changed[42].key, "node", "NEW")
    snap_b = build_snapshot(changed, 4096)

    d = diff_snapshots(snap_a, snap_b)

    assert not d.is_empty
    assert [m[1].key for m in d.modified] == [base[42].key]
    assert d.added == [] and d.removed == []
    # The whole point: only the single divergent partition was opened,
    # not all ~thousands of populated partitions.
    assert d.scanned_partitions == 1
    assert d.total_partitions_considered > 100


def test_diff_detects_add_and_remove():
    a = build_snapshot(_entries(10), 64)
    b_entries = _entries(10)[:-1] + [ManifestEntry("urn:new", "node", "hx")]
    b = build_snapshot(b_entries, 64)
    d = diff_snapshots(a, b)
    assert {e.key for e in d.added} == {"urn:new"}
    assert {e.key for e in d.removed} == {"urn:n9"}
    assert d.modified == []


def test_identical_snapshots_have_empty_diff_and_zero_scan():
    a = build_snapshot(_entries(2000), 1024)
    b = build_snapshot(_entries(2000), 1024)
    d = diff_snapshots(a, b)
    assert d.is_empty
    assert d.scanned_partitions == 0  # every partition Merkle-pruned


def test_partition_count_mismatch_rejected():
    a = build_snapshot(_entries(5), 64)
    b = build_snapshot(_entries(5), 128)
    with pytest.raises(ValueError, match="partition_count"):
        diff_snapshots(a, b)


def test_partition_gzip_bytes_deterministic():
    snap = build_snapshot(_entries(100), 64)
    p = next(iter(snap.partitions.values()))
    assert p.gzip_bytes() == p.gzip_bytes()
