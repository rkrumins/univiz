"""Per-provider operation budgets read from environment variables.

The ABC docstring on GraphDataProvider mandates that every async I/O call
be bounded by a per-operation deadline. ProviderEnvBudget centralises the
defaults and the env-var naming convention so each provider reads the same
shape:

    SPANNER_QUERY_TIMEOUT      -- read-side wait_for budget (seconds)
    SPANNER_WRITE_TIMEOUT      -- write-side wait_for budget (seconds)
    SPANNER_INIT_TIMEOUT       -- connect/preflight budget (seconds)
    SPANNER_PURGE_BATCH_TIMEOUT -- per-batch budget for purge_aggregated_edges

Defaults are intentionally generous on the write side to absorb batched
MERGE/INSERT operations during ingestion sweeps, and tight on the read
side so a stalled downstream cannot hold a request thread open.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderEnvBudget:
    """Per-operation timeouts (seconds) for one provider type."""

    query: float
    write: float
    init: float
    purge_batch: float

    @classmethod
    def from_env(
        cls,
        provider_prefix: str,
        *,
        default_query: float = 5.0,
        default_write: float = 15.0,
        default_init: float = 3.0,
        default_purge_batch: float = 30.0,
    ) -> "ProviderEnvBudget":
        """Read ``<PREFIX>_QUERY_TIMEOUT`` etc. with documented fallbacks."""
        prefix = provider_prefix.upper()
        return cls(
            query=_read_float(f"{prefix}_QUERY_TIMEOUT", default_query),
            write=_read_float(f"{prefix}_WRITE_TIMEOUT", default_write),
            init=_read_float(f"{prefix}_INIT_TIMEOUT", default_init),
            purge_batch=_read_float(f"{prefix}_PURGE_BATCH_TIMEOUT", default_purge_batch),
        )


def _read_float(env_var: str, default: float) -> float:
    raw = os.getenv(env_var)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default
