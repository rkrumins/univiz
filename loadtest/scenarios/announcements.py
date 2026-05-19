"""Locust task for the announcements endpoint.

The frontend currently polls this every 15s in lockstep across all
users (no jitter). Plan WS-6 fixes the frontend; this scenario
reproduces the *backend pressure* that lockstep polling generates so we
can see whether the backend itself is the limit.

For the production-mix workload we just call it like any other read.
For a dedicated "polling pressure" run, point Locust at this scenario
alone with ``LOCUST_LOCUSTFILE=loadtest/scenarios/announcements.py``.
"""
from __future__ import annotations

from locust import TaskSet, task


class AnnouncementsTasks(TaskSet):
    @task
    def get_announcements(self) -> None:
        self.client.get("/api/v1/announcements", name="announcements:list")
