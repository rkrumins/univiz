"""Single-scenario Locust runner: aggregation-jobs only.

Used by ``make smoke-aggregation-jobs``. Wraps ``AggregationJobsTasks``
in an HttpUser so Locust can run it directly via ``-f``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from locust import HttpUser, between  # noqa: E402

from config import SETTINGS  # noqa: E402
from lib.auth import authenticate  # noqa: E402
from lib.data import discover, IdPool  # noqa: E402
from scenarios.aggregation_jobs import AggregationJobsTasks  # noqa: E402


class AggregationJobsUser(HttpUser):
    host = SETTINGS.host
    wait_time = between(SETTINGS.think_min, SETTINGS.think_max)
    tasks = {AggregationJobsTasks: 1}
    id_pool: IdPool

    def on_start(self) -> None:
        authenticate(self.client)
        self.id_pool = discover(self.client)
