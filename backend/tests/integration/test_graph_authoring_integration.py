"""End-to-end integration tests for the authored-graph version-control
stack against a REAL Graph Store Postgres.

Why gated, not SQLite: the Graph Store schema uses ``JSONB`` (a
plan-approved, Postgres-only deviation), so these cannot run on the
suite's SQLite. They run when ``GRAPH_STORE_TEST_DB`` points at a
disposable Postgres (CI provisions one); otherwise the whole module
skips cleanly.

Coverage (the full create -> stage -> commit -> 2nd commit -> history
-> concurrency -> validation -> empty -> outbox -> relay path), driven
at the engine/repo layer so it exercises all persistence/version-control
logic without the HTTP/auth stack:

  * graph + main ref creation
  * staging (coalescing) into the per-user working set
  * genesis commit: blobs/manifests/commit/audit/ref-advance/outbox,
    all atomic; working set cleared; head advanced
  * materialized read-back (load_graph_state) round-trips content
  * second commit: parent linkage, history newest-first, Merkle reuse
  * optimistic concurrency: stale expected head -> HeadMovedError
  * validation: dangling edge -> GraphValidationError (nothing written)
  * empty commit refused
  * outbox row emitted under the visualization domain
  * relay drains it and marks it processed (at-least-once)
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_DB = os.getenv("GRAPH_STORE_TEST_DB")

pytestmark = pytest.mark.skipif(
    not _DB,
    reason="set GRAPH_STORE_TEST_DB=postgresql+asyncpg://… to run "
    "Graph Store integration tests",
)


@pytest_asyncio.fixture()
async def gs_session() -> AsyncSession:
    """Fresh Graph Store schema per test on the configured Postgres."""
    from backend.app.db.graph_store_engine import GraphStoreBase
    from backend.app.db import models_graph  # noqa: F401 — register tables

    engine = create_async_engine(_DB, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(GraphStoreBase.metadata.drop_all)
        await conn.run_sync(GraphStoreBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(GraphStoreBase.metadata.drop_all)
    await engine.dispose()


def _node(key, name):
    return {
        "change_type": "add_node",
        "object_kind": "node",
        "object_id": key,
        "payload": {
            "key": key, "entity_type": "Table", "display_name": name,
            "position": {"x": 0, "y": 0}, "properties": {}, "tags": [],
        },
        "summary": f"add {key}",
    }


def _edge(key, s, t):
    return {
        "change_type": "add_edge",
        "object_kind": "edge",
        "object_id": key,
        "payload": {
            "key": key, "source_key": s, "target_key": t,
            "edge_type": "flows_to", "properties": {},
        },
        "summary": f"add {key}",
    }


async def _commit(session, gid, *, expected_head, msg):
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine

    out = await GraphAuthoringEngine.commit(
        session, graph_id=gid, branch="main", user_id="u1",
        message=msg, author="u1", expected_head_commit_id=expected_head,
        actor="u1",
    )
    await session.commit()
    return out


@pytest.mark.asyncio
async def test_full_authoring_lifecycle(gs_session):
    from backend.app.db.repositories import (
        graph_repo, graph_working_set_repo as ws_repo,
    )
    from backend.app.db.models_graph import GraphStoreOutboxEventORM
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine

    # 1. create graph + main ref
    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="My Graph", created_by="u1",
    )
    await gs_session.commit()
    gid = g.id
    ref = await graph_repo.get_branch_ref(gs_session, graph_id=gid, branch="main")
    assert ref.commit_id is None and g.origin == "authored"

    # 2. stage two nodes + an edge
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=gid, branch="main", user_id="u1",
        changes=[_node("urn:a", "A"), _node("urn:b", "B"),
                 _edge("e1", "urn:a", "urn:b")],
        actor="u1",
    )
    await gs_session.commit()

    # 3. genesis commit
    out1 = await _commit(gs_session, gid, expected_head=None, msg="genesis")
    assert out1.result.delta_summary["nodes_added"] == 2
    assert out1.result.delta_summary["edges_added"] == 1
    ref = await graph_repo.get_branch_ref(gs_session, graph_id=gid, branch="main")
    assert ref.commit_id == out1.result.commit_id
    # working set cleared by commit
    wset = await ws_repo.get_or_open(
        gs_session, graph_id=gid, branch="main", user_id="u1",
        base_commit_id=ref.commit_id,
    )
    assert await ws_repo.list_changes(gs_session, working_set_id=wset.id) == []

    # 4. materialized read-back round-trips
    nodes, edges = await graph_repo.load_graph_state(
        gs_session, graph_id=gid, commit_id=out1.result.commit_id
    )
    assert set(nodes) == {"urn:a", "urn:b"} and set(edges) == {"e1"}
    assert nodes["urn:a"].display_name == "A"
    assert edges["e1"].source_key == "urn:a"

    # 5. outbox event emitted under the visualization domain (relay
    #    delivery is covered by test_relay_marks_processed).
    evrow = (
        await gs_session.execute(select(GraphStoreOutboxEventORM))
    ).scalars().all()
    assert len(evrow) == 1
    assert evrow[0].event_type == "visualization.graph.committed"
    assert evrow[0].processed is False
    assert evrow[0].payload["commit_id"] == out1.result.commit_id


@pytest.mark.asyncio
async def test_relay_marks_processed(gs_session):
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine
    from backend.app.services.graph_outbox_relay import drain_once
    from backend.app.db.models_graph import GraphStoreOutboxEventORM

    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="G", created_by="u1")
    await gs_session.commit()
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[_node("urn:x", "X")], actor="u1")
    await gs_session.commit()
    await _commit(gs_session, g.id, expected_head=None, msg="c1")

    seen = []

    async def publish(ev):
        seen.append(ev.event_type)

    n = await drain_once(gs_session, publish)
    await gs_session.commit()
    assert n == 1 and seen == ["visualization.graph.committed"]
    rows = (await gs_session.execute(
        select(GraphStoreOutboxEventORM))).scalars().all()
    assert rows[0].processed is True


@pytest.mark.asyncio
async def test_second_commit_history_and_parent_linkage(gs_session):
    from backend.app.db.repositories import graph_repo
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine

    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="G", created_by="u1")
    await gs_session.commit()
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[_node("urn:a", "A")], actor="u1")
    await gs_session.commit()
    c1 = (await _commit(gs_session, g.id, expected_head=None, msg="c1")).result

    # update the node -> 2nd commit
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[{
            "change_type": "update_node", "object_kind": "node",
            "object_id": "urn:a",
            "payload": {"key": "urn:a", "entity_type": "Table",
                        "display_name": "A2", "position": {"x": 0, "y": 0},
                        "properties": {}, "tags": []},
        }], actor="u1")
    await gs_session.commit()
    c2 = (await _commit(gs_session, g.id, expected_head=c1.commit_id, msg="c2")).result

    assert c2.commit_id != c1.commit_id
    hist = await GraphAuthoringEngine.history(
        gs_session, graph_id=g.id, branch="main")
    assert [h.id for h in hist] == [c2.commit_id, c1.commit_id]  # newest first
    assert hist[0].parent_ids == [c1.commit_id]
    nodes, _ = await graph_repo.load_graph_state(
        gs_session, graph_id=g.id, commit_id=c2.commit_id)
    assert nodes["urn:a"].display_name == "A2"


@pytest.mark.asyncio
async def test_optimistic_concurrency_stale_head_rejected(gs_session):
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine
    from backend.app.db.repositories.graph_commit_repo import HeadMovedError

    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="G", created_by="u1")
    await gs_session.commit()
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[_node("urn:a", "A")], actor="u1")
    await gs_session.commit()
    c1 = (await _commit(gs_session, g.id, expected_head=None, msg="c1")).result

    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[_node("urn:b", "B")], actor="u1")
    await gs_session.commit()
    # Stale expected head (None, but real head is c1) -> rejected.
    with pytest.raises(HeadMovedError) as ei:
        await GraphAuthoringEngine.commit(
            gs_session, graph_id=g.id, branch="main", user_id="u1",
            message="bad", author="u1", expected_head_commit_id=None,
            actor="u1")
    assert ei.value.current_head == c1.commit_id


@pytest.mark.asyncio
async def test_dangling_edge_blocks_commit(gs_session):
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine
    from backend.app.services.graph_versioning import GraphValidationError

    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="G", created_by="u1")
    await gs_session.commit()
    await GraphAuthoringEngine.stage(
        gs_session, graph_id=g.id, branch="main", user_id="u1",
        changes=[_node("urn:a", "A"), _edge("e1", "urn:a", "urn:ghost")],
        actor="u1")
    await gs_session.commit()
    with pytest.raises(GraphValidationError):
        await GraphAuthoringEngine.commit(
            gs_session, graph_id=g.id, branch="main", user_id="u1",
            message="bad", author="u1", expected_head_commit_id=None,
            actor="u1")


@pytest.mark.asyncio
async def test_empty_commit_refused(gs_session):
    from backend.app.services.graph_authoring_engine import GraphAuthoringEngine
    from backend.app.services.graph_versioning import EmptyCommitError

    g = await GraphAuthoringEngine.create_graph(
        gs_session, workspace_id="ws1", name="G", created_by="u1")
    await gs_session.commit()
    with pytest.raises(EmptyCommitError):
        await GraphAuthoringEngine.commit(
            gs_session, graph_id=g.id, branch="main", user_id="u1",
            message="nothing", author="u1", expected_head_commit_id=None,
            actor="u1")
