"""Snapshot reader — reconstruct a :class:`Snapshot` from persisted
partition manifests, and apply a working set to a materialized graph
state.

Both are *pure* (DB access is injected as callables / passed in), so
the multi-commit, history, diff and blame paths are unit-testable
without a database — the same dependency-injection discipline used by
the outbox relay.

``rebuild_snapshot`` is the missing primitive that unblocks the 2nd
commit onward (the planner needs the base snapshot) and history/diff
reads. ``apply_changes`` is the engine's core working-set resolution:
base state + ordered ops -> resulting node/edge state.
"""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

from .commit import EdgeState, NodeState
from .manifest import PartitionManifest, Snapshot, _partition_hash, _root_hash

# fetch_root(root_hash)  -> list of (partition_index, partition_hash)
#                           or None if the root manifest is unknown.
FetchRoot = Callable[[str], "list[tuple[int, str]] | None"]
# fetch_partition(manifest_hash) -> {key: (kind, content_hash)}
FetchPartition = Callable[[str], Mapping[str, tuple[str, str]]]


def rebuild_snapshot(
    *,
    root_hash: str | None,
    partition_count: int,
    fetch_root: FetchRoot,
    fetch_partition: FetchPartition,
) -> Snapshot:
    """Reconstruct the Merkle snapshot identified by *root_hash*.

    ``root_hash=None`` (or an unknown root) yields the empty snapshot —
    i.e. the implicit pre-genesis state, so the first commit on a fresh
    branch diffs against "nothing". The reconstructed root hash is
    re-derived and asserted to equal *root_hash*, catching manifest
    corruption / partial writes before they propagate into a commit.
    """
    if not root_hash:
        return Snapshot(partition_count=partition_count, root_hash=_root_hash({}), partitions={})

    root = fetch_root(root_hash)
    if root is None:
        return Snapshot(partition_count=partition_count, root_hash=_root_hash({}), partitions={})

    partitions: dict[int, PartitionManifest] = {}
    partition_hashes: dict[int, str] = {}
    for idx, phash in root:
        entries = dict(fetch_partition(phash))
        # Content-addressed integrity: the partition's recomputed
        # content hash MUST equal the hash the root points at. Catches
        # a tampered/partial partition write, not just a bad root list.
        recomputed = _partition_hash(entries)
        if recomputed != phash:
            raise ValueError(
                f"manifest integrity error: partition {idx} content "
                f"hashes to {recomputed!r} but root references {phash!r}"
            )
        partitions[idx] = PartitionManifest(
            partition_index=idx, manifest_hash=phash, entries=entries
        )
        partition_hashes[idx] = phash

    rebuilt_root = _root_hash(partition_hashes)
    if rebuilt_root != root_hash:
        raise ValueError(
            f"manifest integrity error: rebuilt root {rebuilt_root!r} "
            f"!= expected {root_hash!r} (corrupt/partial manifest write)"
        )
    return Snapshot(
        partition_count=partition_count,
        root_hash=root_hash,
        partitions=partitions,
    )


# ── working-set application ────────────────────────────────────────

# A change op (mirrors graph_working_change.change_type). Payloads are
# the resolved final content (engine resolves staged_ temp ids first).
#   add_node    : {key, entity_type, display_name, position, properties, tags}
#   update_node : same shape (full replacement of that node's content)
#   delete_node : {key}
#   add_edge    : {key, source_key, target_key, edge_type, confidence, properties}
#   update_edge : same shape
#   delete_edge : {key}


class WorkingSetError(ValueError):
    """Working set is internally inconsistent (e.g. update/delete of a
    node that does not exist in base+prior-ops, or add of a duplicate).
    Surfaced before validation so the user gets a precise reason."""


def _node_from_payload(p: Mapping) -> NodeState:
    return NodeState(
        key=p["key"],
        entity_type=p.get("entity_type"),
        display_name=p.get("display_name"),
        position=p.get("position"),
        properties=p.get("properties") or {},
        tags=tuple(p.get("tags") or ()),
    )


def _edge_from_payload(p: Mapping) -> EdgeState:
    return EdgeState(
        key=p["key"],
        source_key=p["source_key"],
        target_key=p["target_key"],
        edge_type=p.get("edge_type"),
        confidence=p.get("confidence"),
        properties=p.get("properties") or {},
    )


def apply_changes(
    *,
    base_nodes: Mapping[str, NodeState],
    base_edges: Mapping[str, EdgeState],
    changes: Sequence[Mapping],
) -> tuple[dict[str, NodeState], dict[str, EdgeState]]:
    """Apply ordered working-set ops onto the base state, returning the
    resulting node/edge maps the commit planner consumes. Raises
    :class:`WorkingSetError` on an internally impossible op (the API
    turns this into a 4xx with the reason)."""
    nodes: dict[str, NodeState] = dict(base_nodes)
    edges: dict[str, EdgeState] = dict(base_edges)

    for ch in changes:
        op = ch["change_type"]
        if op == "add_node":
            n = _node_from_payload(ch["payload"])
            if n.key in nodes:
                raise WorkingSetError(f"add_node: {n.key!r} already exists")
            nodes[n.key] = n
        elif op == "update_node":
            n = _node_from_payload(ch["payload"])
            if n.key not in nodes:
                raise WorkingSetError(f"update_node: {n.key!r} does not exist")
            nodes[n.key] = n
        elif op == "delete_node":
            k = ch["payload"]["key"]
            if nodes.pop(k, None) is None:
                raise WorkingSetError(f"delete_node: {k!r} does not exist")
        elif op == "add_edge":
            e = _edge_from_payload(ch["payload"])
            if e.key in edges:
                raise WorkingSetError(f"add_edge: {e.key!r} already exists")
            edges[e.key] = e
        elif op == "update_edge":
            e = _edge_from_payload(ch["payload"])
            if e.key not in edges:
                raise WorkingSetError(f"update_edge: {e.key!r} does not exist")
            edges[e.key] = e
        elif op == "delete_edge":
            k = ch["payload"]["key"]
            if edges.pop(k, None) is None:
                raise WorkingSetError(f"delete_edge: {k!r} does not exist")
        else:
            raise WorkingSetError(f"unknown change_type: {op!r}")

    return nodes, edges


__all__ = [
    "FetchRoot",
    "FetchPartition",
    "rebuild_snapshot",
    "apply_changes",
    "WorkingSetError",
]
