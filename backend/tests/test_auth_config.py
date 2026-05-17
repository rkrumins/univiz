"""Phase 0 — JWT signing secret must be explicit and strong.

There is no ephemeral fallback: an unset or too-weak ``JWT_SECRET_KEY``
fails fast at import (``_resolve_secret``) so the process never starts
in an insecure state.
"""
from __future__ import annotations

import pytest

from backend.auth_service.core import config


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(config.MissingSigningSecret):
        config._resolve_secret()


def test_weak_secret_raises(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "short")
    with pytest.raises(config.MissingSigningSecret):
        config._resolve_secret()


def test_strong_secret_accepted(monkeypatch):
    strong = "x" * config._MIN_SECRET_LENGTH
    monkeypatch.setenv("JWT_SECRET_KEY", strong)
    assert config._resolve_secret() == strong


def test_no_ephemeral_fallback_symbol():
    # The old random-key fallback imported ``secrets``; assert it's gone
    # so a future edit can't silently reintroduce an ephemeral key.
    assert not hasattr(config, "secrets")
