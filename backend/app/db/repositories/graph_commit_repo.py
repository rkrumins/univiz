"""Repository: persist a CommitPlan to the Graph Store DB.

The thin, lower-risk adapter between the *verified pure* version-control
engine (:mod:`backend.app.services.graph_versioning`) and durable
storage. All correctness/scaling logic lives in the planner; this
module only writes the plan transactionally and advances the ref under
an optimistic-concurrency guard.

Transaction contract (matches the codebase): the caller owns the
commit. Everything here — content-blob dedup-inserts, partition
manifest upserts, the commit row, change-event audit rows, the ref
advance, and the outbox event — is appended to the *one* Graph Store
session so it all commits atomically (the cross-DB-atomicity design).

Concurrency: the branch ref carries an optimistic ``revision``. The
advance is a conditional ``UPDATE ... WHERE revision = :expected``; a
zero rowcount means another commit landed first → :class:`HeadMovedError`
(the API maps this to the 409 the frontend's rebase flow expects).

NOTE (verification boundary): unit-tested for the pure commit-hash and
the head-moved guard via a fake session. Full persistence is exercised
by the Phase-1 integration tests, which require a live Graph Store
Postgres (not available in the authoring environment).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models_graph import (
    GraphChangeEventORM,
    GraphCommitORM,
    GraphEdgeVersionORM,
    GraphNodeVersionORM,
    GraphPartitionManifestORM,
    GraphRefORM,
)
from backend.app.db.repositories import graph_store_outbox_repo
from backend.app.services.graph_versioning.commit import (
    CommitPlan,
    EdgeState,
    NodeState,
)
from backend.app.services.graph_versioning.manifest import ROOT_PARTITION_INDEX


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HeadMovedError(RuntimeError):
    """The branch head advanced since the caller's base — the commit is
    refused and must be rebased. Carries the current head so the API can
    return it in the structured 409 body."""

    def __init__(self, current_head: str | None) -> None:
        self.current_head = current_head
        super().__init__(
            f"branch head moved (now {current_head!r}); rebase required"
        )


@dataclass(frozen=True)
class CommitResult:
    commit_id: str
    commit_hash: str
    root_hash: str
    delta_summary: Mapping[str, int]


def compute_commit_hash(
    *,
    root_hash: str,
    parent_ids: list[str],
    author: str | None,
    message: str | None,
    committed_at: str,
) -> str:
    """Deterministic content id of a commit (pure — unit-tested).
    Mirrors git: a commit is identified by its tree + parents +
    author + message + timestamp."""
    payload = json.dumps(
        {
            "root": root_hash,
            "parents": list(parent_ids),
            "author": author or "",
            "message": message or "",
            "at": committed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _root_entries_gzip(snapshot) -> tuple[bytes, int]:
    rows = sorted(
        [idx, p.manifest_hash] for idx, p in snapshot.partitions.items()
    )
    raw = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0), len(rows)


async def persist_commit(
    session: AsyncSession,
    *,
    graph_id: str,
    branch: str,
    plan: CommitPlan,
    node_states: Mapping[str, NodeState],
    edge_states: Mapping[str, EdgeState],
    author: str | None,
    message: str | None,
    expected_head_commit_id: str | None,
    actor: str | None = None,
) -> CommitResult:
    """Persist *plan* on *branch* and advance the ref. Raises
    :class:`HeadMovedError` if the branch moved since
    ``expected_head_commit_id``. ``session`` MUST be a Graph Store
    session; the caller commits it."""
    # 1. Load + guard the ref (optimistic concurrency).
    ref = (
        await session.execute(
            select(GraphRefORM).where(
                GraphRefORM.graph_id == graph_id,
                GraphRefORM.name == branch,
            )
        )
    ).scalar_one_or_none()
    if ref is None:
        raise LookupError(f"branch {branch!r} not found for graph {graph_id!r}")
    if ref.commit_id != expected_head_commit_id:
        raise HeadMovedError(ref.commit_id)
    expected_revision = ref.revision

    # 2. Dedup-insert new content blobs (ON CONFLICT (graph_id,
    #    content_hash) DO NOTHING — content-addressed storage).
    for v in plan.new_versions:
        if v.kind == "node":
            st = node_states[v.key]
            await session.execute(
                pg_insert(GraphNodeVersionORM)
                .values(
                    id=f"gnv_{uuid.uuid4().hex[:12]}",
                    graph_id=graph_id,
                    node_key=v.key,
                    content_hash=v.content_hash,
                    entity_type=st.entity_type,
                    display_name=st.display_name,
                    position=dict(st.position) if st.position else None,
                    properties=dict(st.properties),
                    tags=list(st.tags),
                    created_by=actor,
                    created_at=_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["graph_id", "content_hash"]
                )
            )
        else:
            e = edge_states[v.key]
            await session.execute(
                pg_insert(GraphEdgeVersionORM)
                .values(
                    id=f"gev_{uuid.uuid4().hex[:12]}",
                    graph_id=graph_id,
                    edge_key=v.key,
                    content_hash=v.content_hash,
                    source_node_key=e.source_key,
                    target_node_key=e.target_key,
                    edge_type=e.edge_type,
                    confidence=str(e.confidence) if e.confidence is not None else None,
                    properties=dict(e.properties),
                    created_by=actor,
                    created_at=_now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["graph_id", "content_hash"]
                )
            )

    # 3. Upsert changed partition manifests + the root manifest.
    #    manifest_hash is the PK and content-addressed, so DO NOTHING
    #    gives structural sharing across commits/branches for free.
    for idx in plan.changed_partitions:
        pm = plan.new_snapshot.partitions.get(idx)
        if pm is None:
            continue  # partition emptied — nothing to store
        await session.execute(
            pg_insert(GraphPartitionManifestORM)
            .values(
                manifest_hash=pm.manifest_hash,
                graph_id=graph_id,
                partition_index=idx,
                entries=pm.gzip_bytes(),
                entry_count=len(pm.entries),
                created_at=_now(),
            )
            .on_conflict_do_nothing(index_elements=["manifest_hash"])
        )
    root_bytes, root_count = _root_entries_gzip(plan.new_snapshot)
    await session.execute(
        pg_insert(GraphPartitionManifestORM)
        .values(
            manifest_hash=plan.root_hash,
            graph_id=graph_id,
            partition_index=ROOT_PARTITION_INDEX,
            entries=root_bytes,
            entry_count=root_count,
            created_at=_now(),
        )
        .on_conflict_do_nothing(index_elements=["manifest_hash"])
    )

    # 4. The commit row.
    committed_at = _now()
    parent_ids = [expected_head_commit_id] if expected_head_commit_id else []
    commit_hash = compute_commit_hash(
        root_hash=plan.root_hash,
        parent_ids=parent_ids,
        author=author,
        message=message,
        committed_at=committed_at,
    )
    commit_id = f"gcmt_{uuid.uuid4().hex[:12]}"
    session.add(
        GraphCommitORM(
            id=commit_id,
            graph_id=graph_id,
            commit_hash=commit_hash,
            parent_ids=parent_ids,
            merge_base_id=None,
            root_manifest_hash=plan.root_hash,
            author=author,
            message=message,
            delta_summary=dict(plan.delta_summary),
            committed_at=committed_at,
        )
    )

    # 5. Stamp audit events with this commit.
    for ev in plan.change_events:
        session.add(
            GraphChangeEventORM(
                id=f"gce_{uuid.uuid4().hex[:12]}",
                graph_id=graph_id,
                branch=branch,
                commit_id=commit_id,
                object_kind=ev.object_kind,
                object_id=ev.object_id,
                action=ev.action,
                prev_content_hash=ev.prev_content_hash,
                new_content_hash=ev.new_content_hash,
                actor=actor,
                created_at=committed_at,
            )
        )

    # 6. Optimistic ref advance — conditional UPDATE on revision.
    res = await session.execute(
        update(GraphRefORM)
        .where(
            GraphRefORM.id == ref.id,
            GraphRefORM.revision == expected_revision,
        )
        .values(
            commit_id=commit_id,
            revision=expected_revision + 1,
            updated_at=committed_at,
        )
    )
    if res.rowcount != 1:
        # Lost the race between the read-guard and here.
        raise HeadMovedError(None)

    # 7. Outbox event — same transaction (atomic with everything above).
    await graph_store_outbox_repo.emit(
        session,
        event_type="visualization.graph.committed",
        aggregate_id=graph_id,
        payload={
            "graph_id": graph_id,
            "branch": branch,
            "commit_id": commit_id,
            "commit_hash": commit_hash,
            "delta_summary": dict(plan.delta_summary),
        },
    )

    return CommitResult(
        commit_id=commit_id,
        commit_hash=commit_hash,
        root_hash=plan.root_hash,
        delta_summary=dict(plan.delta_summary),
    )


__all__ = [
    "HeadMovedError",
    "CommitResult",
    "compute_commit_hash",
    "persist_commit",
]
