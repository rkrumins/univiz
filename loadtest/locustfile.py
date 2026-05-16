"""Default Locust entry point — the plan's production traffic mix.

Run with::

    cd loadtest
    pip install -r requirements.txt
    SYNODIC_HOST=https://staging.example.com \
    SYNODIC_BEARER_TOKEN=eyJhbGc... \
        locust -f locustfile.py --headless \
            --users 200 --spawn-rate 20 --run-time 5m \
            --csv results/run --html results/run.html

The defaults emulate the workload described in the perf plan:

* 40% ``GET /api/v1/views/?limit=20`` (the Explorer list page — the
  endpoint WS-1 fixes)
* 30% ``GET /api/v1/admin/workspaces/{ws}/datasources/{ds}/cached-stats``
  (the hammer endpoint from WS-2 / WS-7)
* 10% ``GET /api/v1/views/popular`` (Explorer trending strip)
* 10% ``GET /api/v1/announcements`` (the lockstep poll WS-6 fixes)
* 10% reserved for other read traffic (a placeholder slot — extend by
  adding TaskSets to ``MIXED_TASKS`` below)

To run a focused workload (e.g. just hammer the views endpoint to
validate WS-1), invoke a single scenario module instead::

    locust -f scenarios/views.py --headless --users 200 --run-time 2m

Each scenario module exports a self-contained TaskSet, so they compose
freely and can run standalone.
"""
from __future__ import annotations

import logging
import os
import sys

# When invoked as ``locust -f locustfile.py`` from the ``loadtest``
# directory, Python doesn't automatically have the parent on sys.path,
# so the relative imports below would fail. Add the package root
# explicitly so the same file works from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from locust import HttpUser, between, events  # noqa: E402

from config import SETTINGS  # noqa: E402
from lib.auth import authenticate, AuthError  # noqa: E402
from lib.data import discover, IdPool  # noqa: E402
from scenarios.announcements import AnnouncementsTasks  # noqa: E402
from scenarios.views import ViewsTasks  # noqa: E402
from scenarios.workspaces import CachedStatsTasks  # noqa: E402

logger = logging.getLogger("synodic.loadtest")
logging.basicConfig(level=logging.INFO)


# The mix from the perf plan, expressed as Locust task weights. Locust
# picks each task proportional to its weight, so 4:1:3:1 ≈ 44/11/33/11.
# Close enough to the plan's 40/10/30/10; the remaining 10% slot is
# left for the operator to add scenarios without re-balancing the
# others. Keep weights small so the ratios stay readable.
MIXED_TASKS = {
    ViewsTasks: 5,            # contains list (weight 4) + popular (weight 1) — see ViewsTasks
    CachedStatsTasks: 3,
    AnnouncementsTasks: 1,
}


class SynodicUser(HttpUser):
    """A single simulated user issuing the plan's production mix.

    ``host`` is taken from :data:`config.SETTINGS` (``SYNODIC_HOST`` env)
    so the same locustfile works against local, staging, and prod with
    no code changes. Each user authenticates once at start, then loops
    through the weighted task mix until the run ends.
    """

    host = SETTINGS.host
    wait_time = between(SETTINGS.think_min, SETTINGS.think_max)
    tasks = MIXED_TASKS

    # Filled in :meth:`on_start`. Scenarios read this via ``self.user.id_pool``.
    id_pool: IdPool

    def on_start(self) -> None:
        """Per-user setup: authenticate, then discover IDs.

        Run once when the user is spawned. Discovery is intentionally
        per-user (not global) so the load-gen box doesn't depend on
        cross-user shared state; if discovery fails for one user the
        rest carry on.
        """
        try:
            authenticate(self.client)
        except AuthError as e:
            # Raising aborts this user so it doesn't generate 401 noise.
            # Other users keep running so a transient backend hiccup
            # doesn't tank the whole swarm.
            logger.error("Auth failed for a user: %s", e)
            raise
        self.id_pool = discover(self.client)


# ---------------------------------------------------------------------------
# Event hooks — surface plan-relevant signals during a run
# ---------------------------------------------------------------------------

@events.test_start.add_listener
def _on_test_start(environment, **_kwargs):
    """Print the resolved config at the top of every run.

    Makes results reproducible: anyone looking at a CSV/HTML knows
    which host, auth mode, and think-time the numbers came from.
    """
    auth_mode = (
        "bearer" if SETTINGS.bearer_token
        else "cookie-login" if SETTINGS.username and SETTINGS.password
        else "UNCONFIGURED"
    )
    logger.info(
        "Synodic load test starting: host=%s auth=%s id_pool_limit=%d think=%.1f-%.1fs",
        SETTINGS.host, auth_mode, SETTINGS.id_pool_limit,
        SETTINGS.think_min, SETTINGS.think_max,
    )


@events.request.add_listener
def _on_request(
    request_type, name, response_time, response_length, response,
    context, exception, start_time, url, **_kwargs,
):
    """Optional per-request failure logging.

    Off by default so a misconfigured run doesn't fill the box with
    log spam — Locust's stats panel already surfaces error counts.
    Toggle via ``SYNODIC_LOG_FAILURES=true``.
    """
    if exception is None and (response is None or response.status_code < 400):
        return
    if not SETTINGS.log_failures:
        return
    status = response.status_code if response is not None else "exc"
    logger.warning(
        "%s %s -> %s (%.0f ms) %s",
        request_type, name, status, response_time, exception or "",
    )
