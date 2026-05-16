"""Locust tasks for the views endpoints.

Targets the two endpoints identified in the perf plan as load-bearing:
- ``GET /api/v1/views/`` — the Explorer list page (40% of mixed traffic)
- ``GET /api/v1/views/popular`` — the Explorer trending strip (10%)

Each request is registered under a stable Locust ``name`` so the stats
panel groups them correctly regardless of query-string variation
(otherwise different ``offset`` values would appear as separate URLs
and you couldn't read the p95 at a glance).
"""
from __future__ import annotations

from locust import TaskSet, task


class ViewsTasks(TaskSet):
    """Read-only Explorer traffic: list views + popular views."""

    @task(4)
    def list_views(self) -> None:
        """List views with the Explorer's default page size (limit=20)."""
        # The Explorer occasionally pages forward — emulate that lightly so
        # the count-query path is exercised, not just offset=0.
        offset = 0  # keep deterministic for now; randomise via env later if needed
        self.client.get(
            f"/api/v1/views/?limit=20&offset={offset}",
            name="views:list",
        )

    @task(1)
    def list_popular(self) -> None:
        """Trending strip — small, cacheable, frequently hit."""
        self.client.get(
            "/api/v1/views/popular?limit=10",
            name="views:popular",
        )
