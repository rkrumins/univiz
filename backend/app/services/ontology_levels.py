"""Level-map derivation + digest for the AGGREGATED edge stamp system.

The AGGREGATED edge schema carries denormalized
``(sourceLevel, targetLevel, levelDigest)`` properties so the trace fast
path can filter by ontology level without a per-query join. To keep that
denormalized state honest:

  - **Single source of truth for the map**: runtime, backfill, and on-ingest
    materialization all derive the entity-type → level map from the same
    function (``derive_level_map``) — so they stamp with the same values.

  - **Digest tagging**: every stamped edge carries ``r.levelDigest``, the
    SHA-256 of the level map at stamping time. The probe at startup
    compares stamps' digest to current — mismatch means the map drifted
    (ontology was edited) and stamps are stale; backfill re-stamps them.

  - **Unstampable sentinel**: when an edge's endpoint label is not in the
    current level map (entity type with no declared hierarchy), we stamp
    ``-1`` rather than NULL. This makes the backfill converge (a re-run
    over a -1-stamped edge sees the same digest, no further work). The
    trace fast path treats -1 as "unknown level" and falls back to the
    label-scan path for those edges only.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Set


def derive_level_map(ontology: Any) -> Dict[str, int]:
    """Build the entity-type → integer level map from an ontology.

    Priority:
      1. Declared ``hierarchy.level`` on the entity type.
      2. Derived from ``can_contain`` / ``can_be_contained_by`` (roots = 0,
         children = max(parent_levels) + 1, iterated to a fixed point).
      3. Empty map (the caller decides whether to refuse or use sentinels).

    The two sources merge: declared levels take precedence on conflict;
    derived levels fill in entity types that don't declare one. This means
    an ontology that declares only some types' levels still gets a usable
    map for the rest, via the parent/child structure.
    """
    defs = getattr(ontology, "entity_type_definitions", None) or {}
    if not defs:
        return {}

    declared: Dict[str, int] = {}
    for et_id, et_def in defs.items():
        hierarchy = getattr(et_def, "hierarchy", None)
        level = getattr(hierarchy, "level", None) if hierarchy else None
        if isinstance(level, int):
            declared[et_id] = level

    derived = _derive_from_containment(defs)

    # Merge: declared wins on conflict.
    merged: Dict[str, int] = dict(derived)
    merged.update(declared)
    return merged


def _derive_from_containment(defs: Mapping[str, Any]) -> Dict[str, int]:
    """Project the ontology's containment DAG onto integer levels.

    Roots (no incoming containment) are level 0; each child's level is
    max(parent_levels) + 1, iterated to a fixed point. Cycle-safe via an
    iteration cap = ``len(all_types) + 1`` — any non-cyclic containment
    DAG converges within the length of its longest chain.
    """
    all_types: Set[str] = set(defs.keys())
    parents: Dict[str, Set[str]] = {}

    for et_id, et_def in defs.items():
        hierarchy = getattr(et_def, "hierarchy", None)
        if hierarchy is None:
            continue
        can_contain = list(getattr(hierarchy, "can_contain", None) or [])
        can_be_contained_by = list(getattr(hierarchy, "can_be_contained_by", None) or [])

        for parent in can_be_contained_by:
            if parent and parent != et_id and parent in all_types:
                parents.setdefault(et_id, set()).add(parent)
        for child in can_contain:
            if child and child != et_id and child in all_types:
                parents.setdefault(child, set()).add(et_id)

    if not parents:
        return {}

    levels: Dict[str, int] = {t: 0 for t in all_types if t not in parents}

    max_iterations = len(all_types) + 1
    for _ in range(max_iterations):
        changed = False
        for t in all_types:
            ps = parents.get(t)
            if not ps:
                continue
            parent_levels = [levels[p] for p in ps if p in levels]
            if not parent_levels:
                continue
            new_level = max(parent_levels) + 1
            if levels.get(t) != new_level:
                levels[t] = new_level
                changed = True
        if not changed:
            break

    return levels


def compute_level_digest(level_map: Mapping[str, int]) -> str:
    """SHA-256 of the level map as canonical JSON.

    Used as the ``r.levelDigest`` stamp on every AGGREGATED edge. Two
    semantically-identical maps produce identical digests regardless of
    dict ordering; any change to the map (entity type added, removed,
    re-leveled) produces a different digest, triggering the re-stamp
    path on the next probe.

    Empty map returns a stable empty-map digest — useful when the
    ontology declares no levels at all (the probe will then never report
    drift, and the trace path falls back to label-scan everywhere).
    """
    canonical = json.dumps(
        sorted(dict(level_map).items()),
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Sentinel used on the wire AND in Cypher for "label has no declared
# level in the current map." Kept as a module constant so backfill and
# trace paths agree.
UNKNOWN_LEVEL: int = -1


def filter_unstampable_labels(
    level_map: Mapping[str, int], labels: Mapping[str, Optional[Any]] = None,
) -> Dict[str, int]:
    """Return a copy of ``level_map`` keyed only by labels that have a
    real (non-None) integer level. The Cypher backfill expects every key
    in the param map to resolve to an integer when looked up; this guards
    against accidental None values slipping in from a half-populated
    ontology.
    """
    return {k: int(v) for k, v in level_map.items() if isinstance(v, int)}
