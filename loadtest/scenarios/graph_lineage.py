"""Locust tasks for the lineage trace v2 endpoint.

``POST /api/v1/{ws_id}/graph/trace/v2`` is the Explorer's headline
query — multi-hop FalkorDB traversal against AGGREGATED edges, capped
server-side at ``TRACE_MAX_NODES`` / ``TRACE_TIMEOUT_SECS`` but still
the most expensive thing the read tier serves.

Body is just ``{"urn": "<urn>"}``; the server-side defaults (level=0
skeleton, both directions, depth=99) match what the canvas actually
issues for a cold focus-load, so this scenario reproduces real
production traffic shape without us hand-tuning depth/direction.
"""
from __future__ import annotations

from locust import TaskSet, task


class GraphLineageTasks(TaskSet):
    @task
    def trace_v2(self) -> None:
        target = self.user.id_pool.pick_ws_urn()
        if not target:
            # No URNs in the pool — record a marker row so the operator
            # sees graph data is missing, but don't fail the user.
            self.client.get("/__no_node__", name="graph-trace:no-node")
            return
        ws_id, urn = target
        self.client.post(
            f"/api/v1/{ws_id}/graph/trace/v2",
            json={"urn": urn},
            name="graph-trace:v2",
        )
