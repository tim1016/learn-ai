"""Tests for app.broker.ibkr.config — env-var validation and the
port-vs-mode safety layer."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.broker.ibkr.config import IbkrSettings


def test_defaults_are_paper_on_paper_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip every IBKR_-prefixed env var first — pydantic-settings reads
    # os.environ even when _env_file=None, and the polygon-data-service
    # container's process env carries values from the repo-root .env
    # (e.g., IBKR_CLIENT_ID=42). Without this scrub, an operator-set
    # client_id silently invalidates the "default is 1" assertion.
    for key in list(os.environ):
        if key.startswith("IBKR_"):
            monkeypatch.delenv(key, raising=False)

    s = IbkrSettings(_env_file=None)
    assert s.mode == "paper"
    assert s.port == 4002
    assert s.client_id == 1
    assert s.persist_ticks is False


def test_uppercase_ibkr_env_vars_are_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_MODE", "paper")
    monkeypatch.setenv("IBKR_HOST", "172.23.176.1")
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setenv("IBKR_CLIENT_ID", "7")
    monkeypatch.setenv("IBKR_PERSIST_TICKS", "true")

    s = IbkrSettings(_env_file=None)

    assert s.mode == "paper"
    assert s.host == "172.23.176.1"
    assert s.port == 7497
    assert s.client_id == 7
    assert s.persist_ticks is True


def test_paper_mode_with_live_gateway_port_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IbkrSettings(mode="paper", port=4001, _env_file=None)
    assert "LIVE port" in str(excinfo.value)


def test_paper_mode_with_live_tws_port_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IbkrSettings(mode="paper", port=7496, _env_file=None)
    assert "LIVE port" in str(excinfo.value)


def test_live_mode_with_paper_port_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        IbkrSettings(mode="live", port=4002, _env_file=None)
    assert "PAPER port" in str(excinfo.value)


def test_live_mode_with_live_port_accepted() -> None:
    s = IbkrSettings(mode="live", port=4001, _env_file=None)
    assert s.mode == "live"
    assert s.port == 4001


def test_paper_mode_with_paper_tws_port_accepted() -> None:
    s = IbkrSettings(mode="paper", port=7497, _env_file=None)
    assert s.port == 7497


def test_unrelated_port_not_rejected() -> None:
    """Custom port (e.g. behind a tunnel) bypasses the table-based check.

    The validator only rejects KNOWN-conflicting ports — it doesn't try
    to enumerate every possible legitimate routing.
    """
    s = IbkrSettings(mode="paper", port=14002, _env_file=None)
    assert s.port == 14002
