"""Content addressing — the dedup key for node/edge versions.

``content_hash`` covers exactly the *versioned content* of a node or
edge and nothing else (no surrogate id, no derived/projection fields).
Two objects with identical content therefore share one stored version
row no matter how many commits/branches reference them — this is what
makes "edit one node in a million-node graph" cost one row.

Versioned subsets (must match models_graph + the strategy doc):

* node: ``entity_type``, ``display_name``, ``position`` (layout IS
  versioned — confirmed product decision), ``properties``, ``tags``.
  Excluded: ``node_key``/urn (that is identity, carried by the
  manifest entry, not content), surrogate id, ``child_count``,
  ``last_synced_at`` and any projection-time field.
* edge: ``source_node_key``, ``target_node_key`` (endpoints are
  content — an endpoint change is a new version of the same edge),
  ``edge_type``, ``confidence``, ``properties``.

Canonicalization rules (deterministic across processes/versions):

* mappings serialized with sorted keys, recursively;
* ``tags`` order-insensitive (sorted, de-duplicated);
* floats normalized so ``1.0`` and ``1`` and ``1.00`` hash equal
  (positions especially — a no-op re-drag must not create a version);
* ``None`` omitted (absent == null) so optional fields don't perturb
  the hash when unset;
* UTF-8, no ASCII escaping, compact separators.

Algorithm: SHA-256 over the canonical JSON, hex digest. (SHA-256
mirrors the existing ``ontology_digest`` hashing in the codebase.)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _normalize(value: Any) -> Any:
    """Recursively canonicalize a value for stable hashing."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Collapse int/float that are equal (1 == 1.0) onto one form.
        return float(value) if False else value
    if isinstance(value, float):
        # Normalize -0.0 -> 0.0 and integral floats -> int form so
        # 1.0 and 1 canonicalize identically. NaN/inf are rejected
        # (not legitimate graph content; fail loudly rather than hash
        # an unstable value).
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite float is not valid graph content")
        if value == 0.0:
            return 0
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        # Drop None-valued keys (absent == null) and recurse, sorted.
        return {
            k: _normalize(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
            if v is not None
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_normalize(v) for v in value)
    # Fallback: stringify unknown types deterministically.
    return str(value)


def _canonical_bytes(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm_tags(tags: Any) -> list:
    if not tags:
        return []
    # Order-insensitive + de-duplicated.
    return sorted({str(t) for t in tags})


def node_content_hash(
    *,
    entity_type: str | None,
    display_name: str | None,
    position: Mapping[str, Any] | None,
    properties: Mapping[str, Any] | None,
    tags: Any = None,
) -> str:
    """Content hash for a node version (the ``graph_node_versions``
    dedup key within a graph)."""
    payload = {
        "entity_type": entity_type,
        "display_name": display_name,
        "position": position or None,
        "properties": properties or {},
        "tags": _norm_tags(tags),
    }
    return _sha256(_canonical_bytes(payload))


def edge_content_hash(
    *,
    source_node_key: str,
    target_node_key: str,
    edge_type: str | None,
    confidence: Any = None,
    properties: Mapping[str, Any] | None = None,
) -> str:
    """Content hash for an edge version. Endpoints are content, so
    re-pointing an edge yields a new version of the same edge_key."""
    payload = {
        "source_node_key": source_node_key,
        "target_node_key": target_node_key,
        "edge_type": edge_type,
        "confidence": confidence,
        "properties": properties or {},
    }
    return _sha256(_canonical_bytes(payload))


__all__ = ["node_content_hash", "edge_content_hash"]
