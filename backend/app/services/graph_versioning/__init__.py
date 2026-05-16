"""Graph version-control core — content addressing + Merkle manifests.

Pure, dependency-free logic (stdlib only) so it is exhaustively
unit-testable without a database. Everything in the Phase-1 commit
path builds on these primitives:

* :mod:`content_address` — the canonical content hash that is the
  dedup key for ``graph_node_versions`` / ``graph_edge_versions``.
* :mod:`manifest` — the partitioned 2-level Merkle tree that makes
  commit / diff / checkout O(changed objects) instead of O(graph).
"""

from .content_address import (  # noqa: F401
    node_content_hash,
    edge_content_hash,
)
from .manifest import (  # noqa: F401
    ManifestEntry,
    PartitionManifest,
    Snapshot,
    SnapshotDiff,
    partition_for,
    build_snapshot,
    diff_snapshots,
)
from .validation import (  # noqa: F401
    NodeSpec,
    EdgeSpec,
    OntologySpec,
    Violation,
    GraphValidationError,
    validate_graph_state,
)

__all__ = [
    "node_content_hash",
    "edge_content_hash",
    "ManifestEntry",
    "PartitionManifest",
    "Snapshot",
    "SnapshotDiff",
    "partition_for",
    "build_snapshot",
    "diff_snapshots",
    "NodeSpec",
    "EdgeSpec",
    "OntologySpec",
    "Violation",
    "GraphValidationError",
    "validate_graph_state",
]
