"""Locust tasks for the admin aggregation-jobs endpoint.

``GET /api/v1/admin/aggregation-jobs`` lists every aggregation job
across all data sources. Flagged as Tier-2 high-risk in the backend
audit because it's a full-table scan against the aggregation schema in
Postgres — under concurrent load it competes with the worker tier's
job-state writes.

Hits the endpoint with a bounded ``limit`` so a single request can't
melt the box, but the page is large enough (50) to exercise the SQL
plan you'd see in the admin UI.
"""
from __future__ import annotations

from locust import TaskSet, task


class AggregationJobsTasks(TaskSet):
    @task
    def list_jobs(self) -> None:
        self.client.get(
            "/api/v1/admin/aggregation-jobs?limit=50",
            name="aggregation-jobs:list",
        )
