"""Single-scenario Locust runner: views only.

Used by ``make smoke-views``. Locust can't run a bare TaskSet directly
(scenarios/views.py exports ``ViewsTasks`` as a TaskSet, not an
HttpUser), so this file provides the thin HttpUser wrapper.
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
from scenarios.views import ViewsTasks  # noqa: E402


class ViewsUser(HttpUser):
    host = SETTINGS.host
    wait_time = between(SETTINGS.think_min, SETTINGS.think_max)
    tasks = {ViewsTasks: 1}
    id_pool: IdPool

    def on_start(self) -> None:
        authenticate(self.client)
        self.id_pool = discover(self.client)
