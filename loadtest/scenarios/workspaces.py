"""Locust tasks for the workspace admin cached-stats endpoint.

This endpoint is the one the frontend was hitting in serial waterfalls
(see plan WS-5). Under the plan's target traffic mix it accounts for
~30% of requests. We pick a real (workspace, datasource) pair from the
ID pool each iteration so the backend's not-found path isn't
artificially exercised.

If the pool is empty (discovery failed and no fallback was set), the
task records a request with a clear ``no-target`` name so the operator
can spot it in stats without the user being marked failed.
"""
from __future__ import annotations

from locust import TaskSet, task


class CachedStatsTasks(TaskSet):
    """Read traffic on the admin cached-stats endpoint."""

    @task
    def cached_stats(self) -> None:
        target = self.user.id_pool.pick_ws_ds()
        if not target:
            # Surface in stats so the operator notices, but don't fail
            # the user — the pool may simply be unpopulated yet.
            self.client.get("/__no_target__", name="cached-stats:no-target")
            return
        ws_id, ds_id = target
        self.client.get(
            f"/api/v1/admin/workspaces/{ws_id}/datasources/{ds_id}/cached-stats",
            # Single stable name so stats don't fragment per-ID.
            name="cached-stats:get",
        )
