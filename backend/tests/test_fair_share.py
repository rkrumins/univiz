"""Unit tests for :mod:`backend.app.services.fair_share`."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from backend.app.services import fair_share
from backend.app.services.fair_share import (
    ENDPOINT_CHILDREN,
    WorkspaceTokenBucket,
)
from backend.common.adapters import ProviderBusy


def _make_redis(script_return: list) -> MagicMock:
    """Build a mock Redis whose `register_script` returns an AsyncMock
    that yields ``script_return`` (the [allowed, retry_ms] pair)."""
    redis = MagicMock()
    script = AsyncMock(return_value=script_return)
    redis.register_script = MagicMock(return_value=script)
    return redis


@pytest.fixture(autouse=True)
def _enable_fair_share(monkeypatch):
    monkeypatch.setattr(fair_share, "_ENABLED", True)


@pytest.mark.asyncio
async def test_take_allowed_returns_zero_retry() -> None:
    redis = _make_redis([1, 0])
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take(ENDPOINT_CHILDREN, "ws1")
    assert result.allowed is True
    assert result.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_take_denied_floors_retry_at_one_second() -> None:
    # 250 ms wait math should still round up to 1 s minimum
    redis = _make_redis([0, 250])
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take(ENDPOINT_CHILDREN, "ws1")
    assert result.allowed is False
    assert result.retry_after_seconds == 1


@pytest.mark.asyncio
async def test_take_denied_rounds_up_large_retry() -> None:
    redis = _make_redis([0, 3400])
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take(ENDPOINT_CHILDREN, "ws1")
    assert result.allowed is False
    assert result.retry_after_seconds == 4


@pytest.mark.asyncio
async def test_missing_workspace_id_bypasses_bucket() -> None:
    redis = _make_redis([0, 9999])
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take(ENDPOINT_CHILDREN, workspace_id="")
    assert result.allowed is True
    # Script was never invoked
    redis.register_script.return_value.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_endpoint_bypasses_bucket() -> None:
    redis = _make_redis([0, 9999])
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take("unknown-endpoint", "ws1")
    assert result.allowed is True
    redis.register_script.return_value.assert_not_called()


@pytest.mark.asyncio
async def test_redis_error_fails_open() -> None:
    redis = MagicMock()
    redis.register_script = MagicMock(
        return_value=AsyncMock(side_effect=RedisError("boom")),
    )
    bucket = WorkspaceTokenBucket(redis)
    result = await bucket.take(ENDPOINT_CHILDREN, "ws1")
    assert result.allowed is True
    assert result.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_enforce_raises_provider_busy_on_deny() -> None:
    redis = _make_redis([0, 2000])
    bucket = WorkspaceTokenBucket(redis)
    with pytest.raises(ProviderBusy) as exc_info:
        await bucket.enforce(ENDPOINT_CHILDREN, "ws1")
    assert exc_info.value.retry_after_seconds == 2
    assert "ws1" in exc_info.value.provider_name


@pytest.mark.asyncio
async def test_enforce_passes_through_on_allow() -> None:
    redis = _make_redis([1, 0])
    bucket = WorkspaceTokenBucket(redis)
    await bucket.enforce(ENDPOINT_CHILDREN, "ws1")  # should not raise
