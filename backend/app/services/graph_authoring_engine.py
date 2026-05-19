"""GraphAuthoringEngine — orchestrates create / stage / commit / history
for user-authored versioned graphs.

Composes the verified pure engine
(:mod:`backend.app.services.graph_versioning`) with the Graph Store
repositories. It is graph-store-only: it never touches the management
DB. Strict-mode ontology membership is resolved by the API layer (which
has the management session) and passed in as an
:class:`OntologySpec`, so this engine stays decoupled and testable.

All methods take a Graph Store ``AsyncSession``; the caller's session
scope owns the commit, so every write in ``commit`` (blobs, manifests,
commit row, audit, ref advance, outbox event) is one atomic
transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models_graph import GraphCommitORM
from backend.app.db.repositories import (
    graph_repo,
    graph_working_set_repo as ws_repo,
)
from backend.app.db.repositories.graph_commit_repo import (
    CommitResult,
    HeadMovedError,
    persist_commit,
)
from backend.app.services.graph_versioning import (
    EmptyCommitError,
    GraphValidationError,
    OntologySpec,
    apply_changes,
    plan_commit,
)
from backend.app.services.graph_versioning.snapshot_reader import WorkingSetError


@dataclass(frozen=True)
class CommitOutcome:
    result: CommitResult
    branch: str


class GraphAuthoringEngine:
    """Stateless façade — methods take the session explicitly so it
    composes with the request/transaction scope."""

    # ── lifecycle ───────────────────────────────────────────────────

    @staticmethod
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
        created_by: str | None = None,
    ):
        if schema_mode == "strict" and not ontology_id:
            raise ValueError("strict schema_mode requires an ontology_id")
        return await graph_repo.create_graph(
            session,
            workspace_id=workspace_id,
            name=name,
            description=description,
            origin=origin,
            source_data_source_id=source_data_source_id,
            ontology_id=ontology_id,
            schema_mode=schema_mode,
            created_by=created_by,
        )

    # ── staging ─────────────────────────────────────────────────────

    @staticmethod
    async def stage(
        session: AsyncSession,
        *,
        graph_id: str,
        branch: str,
        user_id: str,
        changes: Sequence[Mapping[str, Any]],
        actor: str | None = None,
    ) -> int:
        ref = await graph_repo.get_branch_ref(
            session, graph_id=graph_id, branch=branch
        )
        wset = await ws_repo.get_or_open(
            session,
            graph_id=graph_id,
            branch=branch,
            user_id=user_id,
            base_commit_id=ref.commit_id,
        )
        return await ws_repo.stage_changes(
            session, working_set=wset, changes=changes, actor=actor
        )

    # ── commit ──────────────────────────────────────────────────────

    @staticmethod
    async def commit(
        session: AsyncSession,
        *,
        graph_id: str,
        branch: str,
        user_id: str,
        message: str,
        author: str | None,
        expected_head_commit_id: str | None,
        ontology: OntologySpec | None = None,
        actor: str | None = None,
    ) -> CommitOutcome:
        """Resolve base + working set, plan, persist, clear the working
        set. Raises HeadMovedError / GraphValidationError /
        EmptyCommitError / WorkingSetError for the API to map to 409 /
        422 / 409 / 422 respectively."""
        graph = await graph_repo.get_graph(session, graph_id)
        ref = await graph_repo.get_branch_ref(
            session, graph_id=graph_id, branch=branch
        )
        if ref.commit_id != expected_head_commit_id:
            raise HeadMovedError(ref.commit_id)

        if graph.schema_mode == "strict" and ontology is None:
            raise ValueError("strict graph requires resolved ontology")

        base_commit_id = ref.commit_id
        base_nodes, base_edges = await graph_repo.load_graph_state(
            session, graph_id=graph_id, commit_id=base_commit_id
        )
        base_snapshot = await graph_repo.load_snapshot(
            session, graph_id=graph_id, commit_id=base_commit_id
        )

        wset = await ws_repo.get_or_open(
            session,
            graph_id=graph_id,
            branch=branch,
            user_id=user_id,
            base_commit_id=base_commit_id,
        )
        ops = await ws_repo.to_ordered_ops(session, working_set_id=wset.id)
        if not ops:
            raise EmptyCommitError("no staged changes to commit")

        final_nodes, final_edges = apply_changes(
            base_nodes=base_nodes, base_edges=base_edges, changes=ops
        )

        plan = plan_commit(
            base_snapshot=base_snapshot if base_commit_id else None,
            nodes=final_nodes,
            edges=final_edges,
            partition_count=graph.partition_count,
            schema_mode=graph.schema_mode,
            ontology=ontology,
        )

        result = await persist_commit(
            session,
            graph_id=graph_id,
            branch=branch,
            plan=plan,
            node_states=final_nodes,
            edge_states=final_edges,
            author=author,
            message=message,
            expected_head_commit_id=base_commit_id,
            actor=actor,
        )

        # Commit consumed the working set: clear it and re-pin to the
        # new head so the next edit session starts clean.
        await ws_repo.discard_all(session, working_set=wset)
        wset.base_commit_id = result.commit_id

        return CommitOutcome(result=result, branch=branch)

    # ── history ─────────────────────────────────────────────────────

    @staticmethod
    async def history(
        session: AsyncSession,
        *,
        graph_id: str,
        branch: str,
        limit: int = 50,
    ) -> list[GraphCommitORM]:
        """Walk the parent chain from the branch head (newest first)."""
        ref = await graph_repo.get_branch_ref(
            session, graph_id=graph_id, branch=branch
        )
        out: list[GraphCommitORM] = []
        cur = ref.commit_id
        seen: set[str] = set()
        while cur and cur not in seen and len(out) < limit:
            seen.add(cur)
            commit = (
                await session.execute(
                    select(GraphCommitORM).where(GraphCommitORM.id == cur)
                )
            ).scalar_one_or_none()
            if commit is None:
                break
            out.append(commit)
            parents = commit.parent_ids or []
            cur = parents[0] if parents else None
        return out


__all__ = [
    "GraphAuthoringEngine",
    "CommitOutcome",
    "HeadMovedError",
    "GraphValidationError",
    "EmptyCommitError",
    "WorkingSetError",
]
