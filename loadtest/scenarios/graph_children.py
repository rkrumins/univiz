"""Locust tasks for direct-children reads on the graph.

Pair of read endpoints hit from the canvas during user expansion:
- ``GET /api/v1/{ws_id}/graph/nodes/{urn}/children`` — direct children only.
- ``GET /api/v1/{ws_id}/graph/nodes/{urn}/children-with-edges`` — children
  plus their incident edges. Heavier because it serialises edge metadata.

The canvas expand-node interaction issues these constantly as users
drill into a graph, so they're the highest-volume graph-traversal calls
in production. Weighting favours the simpler ``children`` (3:1) since
the UI prefers it whenever edges aren't needed.
"""
from __future__ import annotations

from locust import TaskSet, task


class GraphChildrenTasks(TaskSet):
    @task(3)
    def children(self) -> None:
        target = self.user.id_pool.pick_ws_urn()
        if not target:
            self.client.get("/__no_node__", name="graph-children:no-node")
            return
        ws_id, urn = target
        self.client.get(
            f"/api/v1/{ws_id}/graph/nodes/{urn}/children",
            name="graph-children:get",
        )

    @task(1)
    def children_with_edges(self) -> None:
        target = self.user.id_pool.pick_ws_urn()
        if not target:
            self.client.get("/__no_node__", name="graph-children:no-node")
            return
        ws_id, urn = target
        self.client.get(
            f"/api/v1/{ws_id}/graph/nodes/{urn}/children-with-edges",
            name="graph-children-edges:get",
        )
