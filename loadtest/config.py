"""Runtime config for the load tester.

All settings are env-driven so the harness can run in CI, locally, or
against staging without code edits. The defaults target a local dev
backend at http://localhost:8000.

This module is intentionally pure stdlib so the harness has zero
dependencies on the backend package.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    host: str
    bearer_token: Optional[str]
    username: Optional[str]
    password: Optional[str]
    # Path of the cookie-login endpoint. Defaults to the production
    # FastAPI mount (``/api/v1/auth/login``). Some deployments mount the
    # auth router at the root (``/auth/login``) or strip the prefix at
    # a reverse proxy — override here without editing code.
    login_path: str
    # How many workspace/datasource IDs to discover at startup. Caps the
    # discovery cost on huge tenants; sampling is fine for load tests.
    id_pool_limit: int
    # How many workspaces to fetch graph node URNs from. Each adds one
    # POST /nodes/query call to startup, so keep small.
    urn_pool_workspaces: int
    # Per-workspace URN sample size for the graph stress scenarios.
    urns_per_workspace: int
    # Per-task think time (seconds). Locust's ``between(min, max)`` is
    # uniform; users may override per-scenario.
    think_min: float
    think_max: float
    # If true, log every non-2xx response. Off by default to avoid
    # drowning the load-gen box in IO; rely on Locust's stats panel.
    log_failures: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=_env("SYNODIC_HOST", "http://localhost:8000"),
            bearer_token=_env("SYNODIC_BEARER_TOKEN") or None,
            username=_env("SYNODIC_USER") or None,
            password=_env("SYNODIC_PASSWORD") or None,
            login_path=_env("SYNODIC_LOGIN_PATH", "/api/v1/auth/login"),
            id_pool_limit=_env_int("SYNODIC_ID_POOL_LIMIT", 50),
            urn_pool_workspaces=_env_int("SYNODIC_URN_POOL_WORKSPACES", 5),
            urns_per_workspace=_env_int("SYNODIC_URNS_PER_WORKSPACE", 20),
            think_min=_env_float("SYNODIC_THINK_MIN", 0.5),
            think_max=_env_float("SYNODIC_THINK_MAX", 2.5),
            log_failures=_env("SYNODIC_LOG_FAILURES", "false").lower() == "true",
        )


SETTINGS = Settings.from_env()
