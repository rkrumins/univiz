"""Unit tests for DeadlineGuard and ProviderEnvBudget."""
from __future__ import annotations

import asyncio
import os

import pytest

from backend.common.providers.config import ProviderEnvBudget
from backend.common.providers.deadlines import DeadlineGuard


# ---------------------------------------------------------------------------
# ProviderEnvBudget
# ---------------------------------------------------------------------------

def test_budget_uses_defaults_when_env_unset(monkeypatch):
    for var in (
        "SPANNER_QUERY_TIMEOUT", "SPANNER_WRITE_TIMEOUT",
        "SPANNER_INIT_TIMEOUT", "SPANNER_PURGE_BATCH_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)

    budget = ProviderEnvBudget.from_env("spanner")
    assert budget.query == 5.0
    assert budget.write == 15.0
    assert budget.init == 3.0
    assert budget.purge_batch == 30.0


def test_budget_reads_env_vars(monkeypatch):
    monkeypatch.setenv("SPANNER_QUERY_TIMEOUT", "7.5")
    monkeypatch.setenv("SPANNER_WRITE_TIMEOUT", "20")

    budget = ProviderEnvBudget.from_env("spanner")
    assert budget.query == 7.5
    assert budget.write == 20.0


def test_budget_falls_back_on_invalid_env(monkeypatch):
    monkeypatch.setenv("SPANNER_QUERY_TIMEOUT", "not-a-number")
    budget = ProviderEnvBudget.from_env("spanner", default_query=4.0)
    assert budget.query == 4.0


def test_budget_per_provider_isolation(monkeypatch):
    monkeypatch.setenv("FALKORDB_QUERY_TIMEOUT", "1.0")
    monkeypatch.setenv("NEO4J_QUERY_TIMEOUT", "2.0")
    monkeypatch.setenv("SPANNER_QUERY_TIMEOUT", "3.0")

    assert ProviderEnvBudget.from_env("falkordb").query == 1.0
    assert ProviderEnvBudget.from_env("neo4j").query == 2.0
    assert ProviderEnvBudget.from_env("spanner").query == 3.0


# ---------------------------------------------------------------------------
# DeadlineGuard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_returns_value_on_success():
    guard = DeadlineGuard(provider_name="test")

    async def _work():
        return 42

    assert await guard.run(_work(), op_name="ok", timeout_s=1.0) == 42


@pytest.mark.asyncio
async def test_run_raises_on_timeout():
    guard = DeadlineGuard(provider_name="test")

    async def _slow():
        await asyncio.sleep(0.5)

    with pytest.raises(asyncio.TimeoutError):
        await guard.run(_slow(), op_name="slow", timeout_s=0.05)


@pytest.mark.asyncio
async def test_block_yields_remaining_budget():
    guard = DeadlineGuard(provider_name="test")

    async with guard.block(op_name="multi", timeout_s=0.5) as ctx:
        # Just-after-entry remaining is close to the full budget.
        first = ctx.remaining_s()
        assert first <= 0.5
        assert first >= 0.45

        await asyncio.sleep(0.1)

        # Remaining shrinks monotonically.
        second = ctx.remaining_s()
        assert second < first
