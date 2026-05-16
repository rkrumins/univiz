"""Partitioned 2-level Merkle manifest.

A graph snapshot is *not* one flat tree (rewriting a million-entry root
on every commit is O(graph)). Instead every node/edge key is hashed
into one of ``partition_count`` fixed buckets:

    root manifest  ──>  N partition manifests  ──>  objects
    (N partition hashes)   ((key,kind,hash)[])     (content blobs)

Properties this buys (the whole point):

* **O(changed objects) commit** — a commit that touches K objects
  rewrites only the ≤K partition manifests whose entries changed plus
  the root; every other partition manifest is reused *by hash*
  (structural sharing across commits and branches — identical content
  ⇒ identical ``manifest_hash`` ⇒ one stored row).
* **O(changed partitions) diff** — comparing two snapshots skips every
  partition whose hash is equal on both sides (the vast majority);
  only divergent partitions are opened. Independent of total graph
  size or history length.

This module is pure (stdlib only) and fully unit-testable. Hashes are
content-only (never gzip/serialization artifacts) so structural
sharing is stable across processes. The gzip bytes are produced
separately for the ``graph_partition_manifest.entries`` BYTEA column
(``mtime=0`` for deterministic output).
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

# partition_index sentinel for the root manifest row (matches
# models_graph: GraphPartitionManifestORM.partition_index == -1).
ROOT_PARTITION_INDEX = -1

# Stable hash of an empty partition (a partition with no entries). A
# partition absent on one side of a diff is treated as this.
_EMPTY_PARTITION_HASH = hashlib.sha256(b"[]").hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One object's place in the manifest: stable identity + content
    pointer. ``kind`` ∈ {'node','edge'}; ``key`` is the urn/edge id;
    ``content_hash`` is the dedup key from :mod:`content_address`."""

    key: str
    kind: str
    content_hash: str


@dataclass(frozen=True)
class PartitionManifest:
    partition_index: int
    manifest_hash: str
    # key -> (kind, content_hash)
    entries: Mapping[str, tuple[str, str]]

    def gzip_bytes(self) -> bytes:
        """Deterministic gzip of the canonical entry list — exactly
        what is stored in graph_partition_manifest.entries."""
        return gzip.compress(_canonical_entries_bytes(self.entries), mtime=0)


@dataclass(frozen=True)
class Snapshot:
    partition_count: int
    root_hash: str
    partitions: Mapping[int, PartitionManifest]


@dataclass(frozen=True)
class SnapshotDiff:
    added: list[ManifestEntry] = field(default_factory=list)
    removed: list[ManifestEntry] = field(default_factory=list)
    # (old_entry, new_entry) where content_hash changed
    modified: list[tuple[ManifestEntry, ManifestEntry]] = field(default_factory=list)
    # How many partitions were actually opened (proves Merkle pruning).
    scanned_partitions: int = 0
    total_partitions_considered: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def partition_for(key: str, partition_count: int) -> int:
    """Deterministic, uniform partition assignment for a key.

    blake2b (stdlib, fast, good distribution) of the UTF-8 key, first
    8 bytes as an int, mod ``partition_count``. Stable across
    processes and Python versions — the partition layout of a graph is
    frozen at create time, so this must never change for a given key.
    """
    if partition_count <= 0:
        raise ValueError("partition_count must be positive")
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % partition_count


def _canonical_entries_bytes(entries: Mapping[str, tuple[str, str]]) -> bytes:
    """Canonical, order-stable serialization of a partition's entries.
    Sorted by key so the same logical content always hashes the same."""
    rows = [
        [k, entries[k][0], entries[k][1]]
        for k in sorted(entries)
    ]
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _partition_hash(entries: Mapping[str, tuple[str, str]]) -> str:
    if not entries:
        return _EMPTY_PARTITION_HASH
    return hashlib.sha256(_canonical_entries_bytes(entries)).hexdigest()


def _root_hash(partition_hashes: Mapping[int, str]) -> str:
    """Hash over the (partition_index, partition_hash) pairs, sorted.
    Empty/absent partitions are not included so they don't perturb the
    root (a graph that never used partition 7 hashes the same as one
    whose partition 7 went empty)."""
    rows = [
        [idx, h]
        for idx, h in sorted(partition_hashes.items())
        if h != _EMPTY_PARTITION_HASH
    ]
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_snapshot(
    entries: Iterable[ManifestEntry],
    partition_count: int,
) -> Snapshot:
    """Build a full snapshot (root + partition manifests) from the live
    object set. Raises on a duplicate key (the caller must resolve
    node/edge identity before snapshotting)."""
    buckets: dict[int, dict[str, tuple[str, str]]] = {}
    for e in entries:
        if e.kind not in ("node", "edge"):
            raise ValueError(f"invalid manifest entry kind: {e.kind!r}")
        idx = partition_for(e.key, partition_count)
        bucket = buckets.setdefault(idx, {})
        if e.key in bucket:
            raise ValueError(f"duplicate manifest key in snapshot: {e.key!r}")
        bucket[e.key] = (e.kind, e.content_hash)

    partitions: dict[int, PartitionManifest] = {}
    partition_hashes: dict[int, str] = {}
    for idx, bucket in buckets.items():
        h = _partition_hash(bucket)
        partitions[idx] = PartitionManifest(
            partition_index=idx, manifest_hash=h, entries=dict(bucket)
        )
        partition_hashes[idx] = h

    return Snapshot(
        partition_count=partition_count,
        root_hash=_root_hash(partition_hashes),
        partitions=partitions,
    )


def diff_snapshots(old: Snapshot, new: Snapshot) -> SnapshotDiff:
    """Merkle-pruned diff. Only partitions whose hash differs between
    the two snapshots are opened; the rest are skipped wholesale. Cost
    is O(changed partitions + changed objects)."""
    if old.partition_count != new.partition_count:
        # Different partition layout ⇒ no structural sharing possible.
        # (partition_count is frozen at graph create, so in practice
        # this only happens diffing across graphs — reject loudly.)
        raise ValueError("cannot diff snapshots with different partition_count")

    all_idx = set(old.partitions) | set(new.partitions)
    added: list[ManifestEntry] = []
    removed: list[ManifestEntry] = []
    modified: list[tuple[ManifestEntry, ManifestEntry]] = []
    scanned = 0

    for idx in sorted(all_idx):
        op = old.partitions.get(idx)
        np = new.partitions.get(idx)
        old_hash = op.manifest_hash if op else _EMPTY_PARTITION_HASH
        new_hash = np.manifest_hash if np else _EMPTY_PARTITION_HASH
        if old_hash == new_hash:
            continue  # Merkle prune — entire partition unchanged.
        scanned += 1
        old_entries = op.entries if op else {}
        new_entries = np.entries if np else {}
        for key in old_entries.keys() | new_entries.keys():
            o = old_entries.get(key)
            n = new_entries.get(key)
            if o is None and n is not None:
                added.append(ManifestEntry(key, n[0], n[1]))
            elif n is None and o is not None:
                removed.append(ManifestEntry(key, o[0], o[1]))
            elif o is not None and n is not None and o[1] != n[1]:
                modified.append(
                    (
                        ManifestEntry(key, o[0], o[1]),
                        ManifestEntry(key, n[0], n[1]),
                    )
                )

    return SnapshotDiff(
        added=sorted(added, key=lambda e: e.key),
        removed=sorted(removed, key=lambda e: e.key),
        modified=sorted(modified, key=lambda m: m[1].key),
        scanned_partitions=scanned,
        total_partitions_considered=len(all_idx),
    )


__all__ = [
    "ROOT_PARTITION_INDEX",
    "ManifestEntry",
    "PartitionManifest",
    "Snapshot",
    "SnapshotDiff",
    "partition_for",
    "build_snapshot",
    "diff_snapshots",
]
