"""Repository: user_graphs + graph_refs lifecycle, and the DB-backed
readers that feed the pure snapshot/commit engine.

Graph CRUD is ordinary OLTP. The interesting part is
:func:`load_graph_state` — it wires the *pure* ``rebuild_snapshot``
(injecting DB fetchers for the root + partition manifests) and then
loads the content-addressed version blobs for every live entry, giving
the engine the full materialized node/edge state at any commit. That
materialization is what ``plan_commit`` consumes for the next commit
and what history/checkout reads return.

Caller owns the transaction (Graph Store session).
"""
from __future__ import annotations

import gzip
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models_graph import (
    GraphCommitORM,
    GraphEdgeVersionORM,
    GraphNodeVersionORM,
    GraphPartitionManifestORM,
    GraphRefORM,
    UserGraphORM,
)
from backend.app.services.graph_versioning.commit import EdgeState, NodeState
from backend.app.services.graph_versioning.manifest import (
    ROOT_PARTITION_INDEX,
    decode_partition_entries,
)
from backend.app.services.graph_versioning.snapshot_reader import rebuild_snapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GraphNotFoundError(LookupError):
    pass


async def create_graph(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    description: str | None = None,
    origin: str = "authored",
    source_data_source_id: str | None = None,
    ontology_id: str | None = None,
    schema_mode: str = "schemaless",
    partition_count: int = 4096,
    created_by: str | None = None,
) -> UserGraphORM:
    """Create a graph + its empty ``main`` branch ref (commit_id NULL
    until the first commit). schema_mode/ontology consistency is the
    caller's concern (the API enforces it)."""
    gid = f"g_{uuid.uuid4().hex[:12]}"
    graph = UserGraphORM(
        id=gid,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        origin=origin,
        source_data_source_id=source_data_source_id,
        name=name,
        description=description,
        schema_mode=schema_mode,
        default_branch="main",
        partition_count=partition_count,
        created_by=created_by,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(graph)
    session.add(
        GraphRefORM(
            id=f"gref_{uuid.uuid4().hex[:12]}",
            graph_id=gid,
            name="main",
            ref_type="branch",
            commit_id=None,
            revision=0,
            created_by=created_by,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return graph


async def get_graph(session: AsyncSession, graph_id: str) -> UserGraphORM:
    g = (
        await session.execute(
            select(UserGraphORM).where(
                UserGraphORM.id == graph_id,
                UserGraphORM.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if g is None:
        raise GraphNotFoundError(graph_id)
    return g


async def list_graphs(
    session: AsyncSession, *, workspace_id: str, limit: int = 50
) -> Sequence[UserGraphORM]:
    return (
        (
            await session.execute(
                select(UserGraphORM)
                .where(
                    UserGraphORM.workspace_id == workspace_id,
                    UserGraphORM.deleted_at.is_(None),
                )
                .order_by(UserGraphORM.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def soft_delete_graph(
    session: AsyncSession, *, graph_id: str, deleted_by: str | None
) -> None:
    g = await get_graph(session, graph_id)
    g.deleted_at = _now()
    g.deleted_by = deleted_by


async def get_branch_ref(
    session: AsyncSession, *, graph_id: str, branch: str
) -> GraphRefORM:
    ref = (
        await session.execute(
            select(GraphRefORM).where(
                GraphRefORM.graph_id == graph_id,
                GraphRefORM.name == branch,
            )
        )
    ).scalar_one_or_none()
    if ref is None:
        raise GraphNotFoundError(f"{graph_id}@{branch}")
    return ref


async def create_branch(
    session: AsyncSession,
    *,
    graph_id: str,
    name: str,
    from_commit_id: str | None,
    created_by: str | None = None,
) -> GraphRefORM:
    ref = GraphRefORM(
        id=f"gref_{uuid.uuid4().hex[:12]}",
        graph_id=graph_id,
        name=name,
        ref_type="branch",
        commit_id=from_commit_id,
        revision=0,
        created_by=created_by,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(ref)
    return ref


# ── snapshot / state readers (wire the pure engine to the DB) ───────

async def _make_fetchers(session: AsyncSession, graph_id: str):
    """Build the (fetch_root, fetch_partition) callables rebuild_snapshot
    needs. Each does one indexed lookup by manifest_hash."""

    async def _load(manifest_hash: str):
        row = (
            await session.execute(
                select(GraphPartitionManifestORM).where(
                    GraphPartitionManifestORM.manifest_hash == manifest_hash
                )
            )
        ).scalar_one_or_none()
        return row

    # rebuild_snapshot is sync, so pre-resolve the root then memoize
    # partition lookups it asks for. Two-phase: load root, then load
    # exactly the partitions the root references.
    return _load


async def load_snapshot(session: AsyncSession, *, graph_id: str, commit_id: str | None):
    """Materialize the Merkle Snapshot at *commit_id* (None -> empty /
    pre-genesis)."""
    from backend.app.services.graph_versioning.manifest import Snapshot, _root_hash

    graph = await get_graph(session, graph_id)

    if commit_id is None:
        return Snapshot(
            partition_count=graph.partition_count,
            root_hash=_root_hash({}),
            partitions={},
        )

    commit = (
        await session.execute(
            select(GraphCommitORM).where(GraphCommitORM.id == commit_id)
        )
    ).scalar_one_or_none()
    if commit is None:
        raise GraphNotFoundError(f"commit {commit_id}")

    load = await _make_fetchers(session, graph_id)

    root_row = await load(commit.root_manifest_hash)
    if root_row is None:
        root_pairs: list[tuple[int, str]] | None = None
    else:
        root_pairs = [
            (int(i), h)
            for i, h in json.loads(gzip.decompress(root_row.entries).decode())
        ]

    # Pre-load every referenced partition so the (sync) rebuild can
    # resolve them from a dict (no awaits inside rebuild_snapshot).
    part_cache: dict[str, dict[str, tuple[str, str]]] = {}
    if root_pairs:
        for _idx, phash in root_pairs:
            prow = await load(phash)
            part_cache[phash] = (
                decode_partition_entries(gzip.decompress(prow.entries))
                if prow is not None
                else {}
            )

    return rebuild_snapshot(
        root_hash=commit.root_manifest_hash,
        partition_count=graph.partition_count,
        fetch_root=lambda _h: root_pairs,
        fetch_partition=lambda mh: part_cache.get(mh, {}),
    )


async def load_graph_state(
    session: AsyncSession, *, graph_id: str, commit_id: str | None
) -> tuple[dict[str, NodeState], dict[str, EdgeState]]:
    """Full materialized node/edge state at *commit_id*. Reconstructs
    the snapshot, then loads the content-addressed blob for every live
    entry. (O(graph) by design for the MVP — incremental
    materialization is a Phase-3 scale concern, documented in the
    strategy doc.)"""
    snap = await load_snapshot(session, graph_id=graph_id, commit_id=commit_id)

    node_hashes: dict[str, str] = {}
    edge_hashes: dict[str, str] = {}
    for pm in snap.partitions.values():
        for key, (kind, chash) in pm.entries.items():
            (node_hashes if kind == "node" else edge_hashes)[key] = chash

    nodes: dict[str, NodeState] = {}
    edges: dict[str, EdgeState] = {}

    if node_hashes:
        rows = (
            await session.execute(
                select(GraphNodeVersionORM).where(
                    GraphNodeVersionORM.graph_id == graph_id,
                    GraphNodeVersionORM.content_hash.in_(set(node_hashes.values())),
                )
            )
        ).scalars().all()
        by_hash = {r.content_hash: r for r in rows}
        for key, chash in node_hashes.items():
            r = by_hash[chash]
            nodes[key] = NodeState(
                key=key,
                entity_type=r.entity_type,
                display_name=r.display_name,
                position=r.position,
                properties=r.properties or {},
                tags=tuple(r.tags or ()),
            )

    if edge_hashes:
        rows = (
            await session.execute(
                select(GraphEdgeVersionORM).where(
                    GraphEdgeVersionORM.graph_id == graph_id,
                    GraphEdgeVersionORM.content_hash.in_(set(edge_hashes.values())),
                )
            )
        ).scalars().all()
        by_hash = {r.content_hash: r for r in rows}
        for key, chash in edge_hashes.items():
            r = by_hash[chash]
            edges[key] = EdgeState(
                key=key,
                source_key=r.source_node_key,
                target_key=r.target_node_key,
                edge_type=r.edge_type,
                confidence=r.confidence,
                properties=r.properties or {},
            )

    return nodes, edges


__all__ = [
    "GraphNotFoundError",
    "create_graph",
    "get_graph",
    "list_graphs",
    "soft_delete_graph",
    "get_branch_ref",
    "create_branch",
    "load_snapshot",
    "load_graph_state",
]
