"""Locust tasks for single-direction lineage walks.

Pair of read endpoints hit from node-detail pages:
- ``GET /api/v1/{ws_id}/graph/nodes/{urn}/ancestors``
- ``GET /api/v1/{ws_id}/graph/nodes/{urn}/descendants``

Both are simpler than ``trace/v2`` (no AGGREGATED-edge orchestration)
but still walk the graph from a starting node and return ``List[GraphNode]``,
so they isolate raw traversal cost from trace's response-building.

Tasks are equal-weight: the UI typically loads both panels concurrently
on a node-detail view, so a 50/50 split matches real usage.
"""
from __future__ import annotations

from locust import TaskSet, task


class GraphWalksTasks(TaskSet):
    @task(1)
    def ancestors(self) -> None:
        target = self.user.id_pool.pick_ws_urn()
        if not target:
            self.client.get("/__no_node__", name="graph-walks:no-node")
            return
        ws_id, urn = target
        self.client.get(
            f"/api/v1/{ws_id}/graph/nodes/{urn}/ancestors",
            name="graph-ancestors:get",
        )

    @task(1)
    def descendants(self) -> None:
        target = self.user.id_pool.pick_ws_urn()
        if not target:
            self.client.get("/__no_node__", name="graph-walks:no-node")
            return
        ws_id, urn = target
        self.client.get(
            f"/api/v1/{ws_id}/graph/nodes/{urn}/descendants",
            name="graph-descendants:get",
        )
