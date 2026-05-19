"""Locust tasks for the workspace graph metadata-schema endpoint.

``GET /api/v1/{ws_id}/graph/metadata/schema`` returns the full schema
introspection for a workspace's graph — entity types, properties, edge
constraints, the lot. Flagged as Tier-2 high-risk in the backend audit:
under concurrent load it competes with graph write operations and can
trigger FalkorDB read-amplification on large graphs.

Picks a random workspace from the shared ID pool each call so traffic
isn't artificially concentrated on one graph.
"""
from __future__ import annotations

from locust import TaskSet, task


class GraphSchemaTasks(TaskSet):
    @task
    def get_schema(self) -> None:
        ws_id = self.user.id_pool.pick_workspace()
        if not ws_id:
            # No workspaces discovered — surface in stats but don't fail
            # the user. Same pattern as scenarios/workspaces.py.
            self.client.get("/__no_target__", name="graph-schema:no-target")
            return
        self.client.get(
            f"/api/v1/{ws_id}/graph/metadata/schema",
            name="graph-schema:get",
        )
