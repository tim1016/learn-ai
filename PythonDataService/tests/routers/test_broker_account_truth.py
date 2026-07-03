"""Tests for the broker Account Truth router."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.models import IbkrConnectionHealth
from app.routers import broker_account_truth


def _health() -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id="DU1234567",
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _client() -> SimpleNamespace:
    return SimpleNamespace(health=_health)


@pytest.mark.asyncio
async def test_account_truth_endpoint_delegates_to_refresh_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    truth = SimpleNamespace(account_id="DU1234567")
    client = _client()

    async def fake_refresh_account_truth_now(*args, **kwargs) -> SimpleNamespace:
        captured["client"] = args[0]
        captured["context"] = kwargs["context"]
        return truth

    monkeypatch.setattr(broker_account_truth, "refresh_account_truth_now", fake_refresh_account_truth_now)

    result = await broker_account_truth.account_truth_endpoint(client)

    assert result is truth
    assert captured == {"client": client, "context": "account truth"}


@pytest.mark.asyncio
async def test_account_truth_endpoint_translates_broker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_refresh_account_truth_now(*_, **__) -> SimpleNamespace:
        raise BrokerError("broker sweep failed")

    monkeypatch.setattr(broker_account_truth, "refresh_account_truth_now", fake_refresh_account_truth_now)

    with pytest.raises(HTTPException) as exc_info:
        await broker_account_truth.account_truth_endpoint(_client())

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "broker sweep failed"
