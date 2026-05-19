"""Three-way merge engine — the keystone for branch merges (Phase 2)
and cross-graph fork/PR merges (Phase 2.5).

Pure, stdlib-only, fully unit-testable. Operates on Merkle
:class:`Snapshot`s (the `key -> (kind, content_hash)` entry maps), so
the *same* algorithm merges two branches of one graph **or** a fork
graph into its base — content hashes are global semantic identities,
independent of which `graph_id` stores the blob.

Pipeline (matches the strategy doc + Key-risk #3):

1. ``three_way_merge(base, ours, theirs)`` — Merkle-pruned per-object
   three-way classification → cleanly auto-merged entries + a list of
   conflicts (``add_add | modify_modify | edit_delete``). Skips every
   partition whose hash is equal across all three snapshots.
2. ``apply_resolutions`` — fold human conflict choices into the final
   entry map (refuses if any conflict is unresolved).
3. ``check_referential_integrity`` — **mandatory** post-merge pass over
   the *merged result*: no dangling edges, no new containment cycles.
   Re-run after resolution (a resolution can itself reintroduce a
   dangling edge — never trusted blindly). ``dangling_edge`` /
   ``structural`` are never auto-resolved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .manifest import Snapshot

# key -> (kind, content_hash)
EntryMap = dict[str, tuple[str, str]]

# content_hash -> (source_key, target_key, edge_type)  [edges only]
EdgeEndpointResolver = Callable[[str], "tuple[str, str, str | None]"]


@dataclass(frozen=True)
class MergeConflict:
    key: str
    kind: str                       # 'node' | 'edge'
    conflict_class: str             # add_add | modify_modify | edit_delete
    base_hash: str | None
    ours_hash: str | None
    theirs_hash: str | None


@dataclass(frozen=True)
class MergeOutcome:
    auto: EntryMap = field(default_factory=dict)          # cleanly merged
    conflicts: list[MergeConflict] = field(default_factory=list)
    scanned_partitions: int = 0
    total_partitions: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True)
class IntegrityViolation:
    code: str                       # 'dangling_edge' | 'containment_cycle'
    detail: str
    key: str


class UnresolvedConflictsError(ValueError):
    pass


class MergeIntegrityError(ValueError):
    def __init__(self, violations: list[IntegrityViolation]) -> None:
        self.violations = violations
        super().__init__(
            f"{len(violations)} post-merge integrity violation(s): "
            + "; ".join(f"[{v.code}] {v.detail}" for v in violations[:5])
        )


def _all_entries(s: Snapshot) -> EntryMap:
    out: EntryMap = {}
    for pm in s.partitions.values():
        for k, v in pm.entries.items():
            out[k] = (v[0], v[1])
    return out


def _phash(s: Snapshot, idx: int) -> str | None:
    p = s.partitions.get(idx)
    return p.manifest_hash if p else None


def three_way_merge(
    base: Snapshot, ours: Snapshot, theirs: Snapshot
) -> MergeOutcome:
    """Classic per-object three-way merge, Merkle-pruned. ``base`` is
    the common ancestor (LCA for branch merges; the fork point for
    fork/PR merges). Partition layouts must match (frozen
    ``partition_count``)."""
    if not (base.partition_count == ours.partition_count == theirs.partition_count):
        raise ValueError("cannot merge snapshots with different partition_count")

    all_idx = (
        set(base.partitions) | set(ours.partitions) | set(theirs.partitions)
    )
    auto: EntryMap = {}
    conflicts: list[MergeConflict] = []
    scanned = 0

    # Pre-extract full maps once (cheap; entry maps, not blobs).
    B, O, T = _all_entries(base), _all_entries(ours), _all_entries(theirs)

    for idx in sorted(all_idx):
        bh, oh, th = _phash(base, idx), _phash(ours, idx), _phash(theirs, idx)
        if bh == oh == th:
            continue  # Merkle prune: identical on all three sides.
        scanned += 1

    # Object-level classification over the union of keys (only keys in a
    # scanned/divergent partition can differ; iterating all keys is
    # still O(graph) worst case but bounded by the union of changed
    # sides — acceptable, and the partition prune above short-circuits
    # the common "mostly unchanged" case for the hash compare).
    keys = set(B) | set(O) | set(T)
    for k in keys:
        b = B.get(k)
        o = O.get(k)
        t = T.get(k)
        bH = b[1] if b else None
        oH = o[1] if o else None
        tH = t[1] if t else None
        kind = (o or t or b)[0]  # type: ignore[index]

        if oH == tH:
            # Both sides agree (incl. both added same / both deleted).
            if o is not None:
                auto[k] = o
            continue
        if bH == oH:
            # ours unchanged vs base → take theirs (incl. theirs delete).
            if t is not None:
                auto[k] = t
            continue
        if bH == tH:
            # theirs unchanged vs base → take ours.
            if o is not None:
                auto[k] = o
            continue

        # Diverged on both sides → conflict; classify.
        if b is None and o is not None and t is not None:
            cc = "add_add"
        elif (o is None) != (t is None):
            cc = "edit_delete"
        else:
            cc = "modify_modify"
        conflicts.append(
            MergeConflict(
                key=k, kind=kind, conflict_class=cc,
                base_hash=bH, ours_hash=oH, theirs_hash=tH,
            )
        )

    return MergeOutcome(
        auto=auto,
        conflicts=sorted(conflicts, key=lambda c: c.key),
        scanned_partitions=scanned,
        total_partitions=len(all_idx),
    )


def apply_resolutions(
    outcome: MergeOutcome,
    resolutions: Mapping[str, tuple[str, str] | None],
) -> EntryMap:
    """Combine the auto-merged entries with human conflict choices.

    ``resolutions[key]`` = the chosen ``(kind, content_hash)`` to keep,
    or ``None`` to resolve as a delete. Every conflict key MUST be
    present, else :class:`UnresolvedConflictsError`."""
    merged: EntryMap = dict(outcome.auto)
    conflict_keys = {c.key for c in outcome.conflicts}
    missing = conflict_keys - set(resolutions)
    if missing:
        raise UnresolvedConflictsError(
            f"{len(missing)} unresolved conflict(s): {sorted(missing)[:5]}"
        )
    for key, choice in resolutions.items():
        if key not in conflict_keys:
            continue  # ignore stray resolutions
        if choice is None:
            merged.pop(key, None)
        else:
            merged[key] = choice
    return merged


def check_referential_integrity(
    merged: EntryMap,
    *,
    get_edge_endpoints: EdgeEndpointResolver,
    containment_edge_types: frozenset[str] = frozenset(),
) -> list[IntegrityViolation]:
    """Post-merge integrity over the MERGED result. Returns every
    violation (caller raises / surfaces). Must be re-run after conflict
    resolution — a resolution can reintroduce a dangling edge."""
    node_keys = {k for k, (kind, _h) in merged.items() if kind == "node"}
    violations: list[IntegrityViolation] = []

    # Containment graph for cycle detection (only containment-typed
    # edges among surviving nodes).
    adj: dict[str, list[str]] = {}

    for k, (kind, chash) in merged.items():
        if kind != "edge":
            continue
        src, tgt, etype = get_edge_endpoints(chash)
        if src not in node_keys:
            violations.append(
                IntegrityViolation(
                    "dangling_edge",
                    f"edge {k!r} source {src!r} is not a surviving node",
                    k,
                )
            )
        if tgt not in node_keys:
            violations.append(
                IntegrityViolation(
                    "dangling_edge",
                    f"edge {k!r} target {tgt!r} is not a surviving node",
                    k,
                )
            )
        if etype and etype in containment_edge_types:
            adj.setdefault(src, []).append(tgt)

    # Cycle detection over the containment subgraph (DFS, 3-colour).
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def _dfs(n: str, stack: list[str]) -> bool:
        color[n] = GREY
        stack.append(n)
        for m in adj.get(n, ()):
            if color.get(m, WHITE) == GREY:
                cyc = " → ".join(stack[stack.index(m):] + [m])
                violations.append(
                    IntegrityViolation(
                        "containment_cycle",
                        f"containment cycle: {cyc}",
                        m,
                    )
                )
                return True
            if color.get(m, WHITE) == WHITE and _dfs(m, stack):
                stack.pop()
                return True
        stack.pop()
        color[n] = BLACK
        return False

    for n in list(adj):
        if color.get(n, WHITE) == WHITE:
            _dfs(n, [])

    return violations


__all__ = [
    "EntryMap",
    "EdgeEndpointResolver",
    "MergeConflict",
    "MergeOutcome",
    "IntegrityViolation",
    "UnresolvedConflictsError",
    "MergeIntegrityError",
    "three_way_merge",
    "apply_resolutions",
    "check_referential_integrity",
]
