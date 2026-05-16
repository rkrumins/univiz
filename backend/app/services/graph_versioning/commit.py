"""Commit planner — the pure algorithm behind ``commit``.

Given the base snapshot and a working set, computes *exactly what a
commit must persist* without touching a database: the new Merkle
snapshot, which partition manifests changed, which content blobs are
new (dedup), the change events, and the commit metadata. The DB layer
then becomes a thin, low-risk "persist this plan" adapter — all the
correctness lives here and is unit-testable.

Scope (Phase-1): change events are object-level (created/updated/
deleted per node/edge). Per-attribute audit rows are a documented
refinement layered on top later (the strategy doc's graph_change_event
already carries attribute_path); object-level is correct and
sufficient for the MVP and keeps this planner simple.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .content_address import edge_content_hash, node_content_hash
from .manifest import ManifestEntry, Snapshot, build_snapshot, diff_snapshots
from .validation import (
    EdgeSpec,
    NodeSpec,
    OntologySpec,
    validate_graph_state,
)


@dataclass(frozen=True)
class NodeState:
    """Resolved final state of a node (temp ids already resolved by the
    engine). ``key`` is the stable urn."""

    key: str
    entity_type: str | None
    display_name: str | None
    position: Mapping[str, object] | None
    properties: Mapping[str, object]
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeState:
    key: str
    source_key: str
    target_key: str
    edge_type: str | None
    confidence: object | None = None
    properties: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VersionRow:
    """A content-addressed blob the commit must ensure exists
    (INSERT ... ON CONFLICT (graph_id, content_hash) DO NOTHING)."""

    kind: str            # 'node' | 'edge'
    key: str
    content_hash: str


@dataclass(frozen=True)
class ChangeEvent:
    object_kind: str     # 'node' | 'edge'
    object_id: str
    action: str          # 'created' | 'updated' | 'deleted'
    prev_content_hash: str | None
    new_content_hash: str | None


@dataclass(frozen=True)
class CommitPlan:
    root_hash: str
    new_snapshot: Snapshot
    # Partition manifests whose hash changed vs base (upsert these;
    # content-addressed so unchanged ones are reused by hash).
    changed_partitions: tuple[int, ...]
    # New content blobs to dedup-insert (absent ones only).
    new_versions: tuple[VersionRow, ...]
    change_events: tuple[ChangeEvent, ...]
    delta_summary: Mapping[str, int]


class EmptyCommitError(ValueError):
    """Raised when a working set produces no change vs the base
    (nothing to commit — refused, like git)."""


def _node_entries(nodes: Mapping[str, NodeState]) -> dict[str, str]:
    return {
        k: node_content_hash(
            entity_type=n.entity_type,
            display_name=n.display_name,
            position=n.position,
            properties=n.properties,
            tags=n.tags,
        )
        for k, n in nodes.items()
    }


def _edge_entries(edges: Mapping[str, EdgeState]) -> dict[str, str]:
    return {
        k: edge_content_hash(
            source_node_key=e.source_key,
            target_node_key=e.target_key,
            edge_type=e.edge_type,
            confidence=e.confidence,
            properties=e.properties,
        )
        for k, e in edges.items()
    }


def plan_commit(
    *,
    base_snapshot: Snapshot | None,
    nodes: Mapping[str, NodeState],
    edges: Mapping[str, EdgeState],
    partition_count: int,
    schema_mode: str,
    ontology: OntologySpec | None = None,
) -> CommitPlan:
    """Plan a commit of the resulting graph state (``nodes``/``edges``
    are the FINAL state after the engine applied the working set onto
    the base and resolved temp ids).

    Steps: validate → content-hash everything → build the new Merkle
    snapshot → Merkle-diff vs base → derive new blobs, changed
    partitions, change events, delta summary. Raises
    :class:`EmptyCommitError` if nothing changed.
    """
    # 1. Integrity gate (raises GraphValidationError with all issues).
    validate_graph_state(
        nodes=[NodeSpec(n.key, n.entity_type) for n in nodes.values()],
        edges=[
            EdgeSpec(e.key, e.source_key, e.target_key, e.edge_type)
            for e in edges.values()
        ],
        schema_mode=schema_mode,
        ontology=ontology,
    )

    # 2. Content-address every object.
    node_hashes = _node_entries(nodes)
    edge_hashes = _edge_entries(edges)

    entries = [
        ManifestEntry(k, "node", h) for k, h in node_hashes.items()
    ] + [
        ManifestEntry(k, "edge", h) for k, h in edge_hashes.items()
    ]

    # 3. New snapshot + Merkle diff vs base.
    new_snapshot = build_snapshot(entries, partition_count)

    if base_snapshot is None:
        # Genesis commit: empty graph genesis is still refused.
        if not entries:
            raise EmptyCommitError("nothing to commit (empty genesis)")
        added = entries
        removed: list[ManifestEntry] = []
        modified: list[tuple[ManifestEntry, ManifestEntry]] = []
        changed_parts = tuple(sorted(new_snapshot.partitions))
    else:
        if base_snapshot.root_hash == new_snapshot.root_hash:
            raise EmptyCommitError("nothing to commit (no change vs base)")
        d = diff_snapshots(base_snapshot, new_snapshot)
        added, removed, modified = d.added, d.removed, d.modified
        changed_parts = _changed_partitions(base_snapshot, new_snapshot)

    # 4. New content blobs (added + the new side of modified). Dedup is
    #    enforced at the DB by UNIQUE(graph_id, content_hash); we still
    #    de-duplicate within the plan so identical content edited on
    #    two objects yields one row.
    seen: set[tuple[str, str]] = set()
    new_versions: list[VersionRow] = []
    for e in added:
        if (e.kind, e.content_hash) not in seen:
            seen.add((e.kind, e.content_hash))
            new_versions.append(VersionRow(e.kind, e.key, e.content_hash))
    for _old, new in modified:
        if (new.kind, new.content_hash) not in seen:
            seen.add((new.kind, new.content_hash))
            new_versions.append(VersionRow(new.kind, new.key, new.content_hash))

    # 5. Change events (object-level).
    events: list[ChangeEvent] = []
    for e in added:
        events.append(ChangeEvent(e.kind, e.key, "created", None, e.content_hash))
    for old, new in modified:
        events.append(
            ChangeEvent(new.kind, new.key, "updated", old.content_hash, new.content_hash)
        )
    for e in removed:
        events.append(ChangeEvent(e.kind, e.key, "deleted", e.content_hash, None))

    delta_summary = {
        "nodes_added": sum(1 for e in added if e.kind == "node"),
        "nodes_modified": sum(1 for _o, n in modified if n.kind == "node"),
        "nodes_removed": sum(1 for e in removed if e.kind == "node"),
        "edges_added": sum(1 for e in added if e.kind == "edge"),
        "edges_modified": sum(1 for _o, n in modified if n.kind == "edge"),
        "edges_removed": sum(1 for e in removed if e.kind == "edge"),
    }

    return CommitPlan(
        root_hash=new_snapshot.root_hash,
        new_snapshot=new_snapshot,
        changed_partitions=changed_parts,
        new_versions=tuple(new_versions),
        change_events=tuple(events),
        delta_summary=delta_summary,
    )


def _changed_partitions(old: Snapshot, new: Snapshot) -> tuple[int, ...]:
    """Partition indices whose manifest hash differs (or appeared /
    disappeared) — exactly the manifests the commit must write."""
    out: list[int] = []
    for idx in sorted(set(old.partitions) | set(new.partitions)):
        oh = old.partitions[idx].manifest_hash if idx in old.partitions else None
        nh = new.partitions[idx].manifest_hash if idx in new.partitions else None
        if oh != nh:
            out.append(idx)
    return tuple(out)


__all__ = [
    "NodeState",
    "EdgeState",
    "VersionRow",
    "ChangeEvent",
    "CommitPlan",
    "EmptyCommitError",
    "plan_commit",
]
