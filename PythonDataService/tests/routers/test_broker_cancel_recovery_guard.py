"""Tests for Account Truth order-cancel recovery guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr import order_cancel_decision
from app.broker.ibkr.models import IbkrConnectionHealth, IbkrOpenOrder
from app.broker.ibkr.order_cancel_decision import account_truth_cancel_decision
from app.broker.ibkr.orders import OrderNotFoundError, OrderRefusedError
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    AccountInstanceBinding,
    write_account_freeze,
    write_account_instance_binding,
)


def _health(account_id: str | None = "DU1234567") -> IbkrConnectionHealth:
    return IbkrConnectionHealth(
        mode="paper",
        host="127.0.0.1",
        port=4002,
        client_id=7,
        connected=True,
        account_id=account_id,
        is_paper=True,
        fetched_at_ms=1_780_000_000_000,
        connection_state="connected",
        last_transition_ms=1_780_000_000_000,
    )


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id="DU1234567",
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_780_000_000_000,
        source="test",
    )


def _open_order(**overrides) -> IbkrOpenOrder:
    base = {
        "account_id": "DU1234567",
        "order_id": 42,
        "perm_id": 9001,
        "client_id": 7,
        "con_id": 12345,
        "symbol": "SPY",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 1.0,
        "order_type": "MKT",
        "limit_price": None,
        "time_in_force": "DAY",
        "status": "Submitted",
        "cumulative_filled": 0.0,
        "remaining": 1.0,
        "avg_fill_price": None,
        "order_ref": "learn-ai/bot-a/v1:intent-a",
        "fetched_at_ms": 1_780_000_000_100,
    }
    base.update(overrides)
    return IbkrOpenOrder(**base)


async def _decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    orders: list[IbkrOpenOrder] | None = None,
    health: IbkrConnectionHealth | None = None,
):
    monkeypatch.setattr(
        order_cancel_decision,
        "list_open_orders",
        AsyncMock(return_value=orders or [_open_order()]),
    )
    return await account_truth_cancel_decision(
        object(),  # type: ignore[arg-type]
        health=health or _health(),
        artifacts_root=tmp_path,
        order_id=42,
    )


@pytest.mark.asyncio
async def test_order_id_cancel_allows_owned_open_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_account_instance_binding(tmp_path, _binding())

    decision = await _decision(tmp_path, monkeypatch)

    assert decision.action.enabled is True
    decision.raise_if_blocked()


@pytest.mark.asyncio
async def test_order_id_cancel_refuses_active_account_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_account_instance_binding(tmp_path, _binding())
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id="DU1234567",
            reason="restart_intensity.threshold_breached",
            source="account_restart_intensity",
            recorded_at_ms=1_780_000_002_000,
            operator_next_step="STOP_RESTARTING_AND_RECOVER_ACCOUNT",
        ),
    )

    decision = await _decision(tmp_path, monkeypatch)

    assert decision.action.reason_code == "ACCOUNT_FROZEN"
    with pytest.raises(OrderRefusedError) as exc_info:
        decision.raise_if_blocked()
    message = str(exc_info.value)
    assert "ACCOUNT_FROZEN" in message
    assert "restart_intensity.threshold_breached" in message
    assert "STOP_RESTARTING_AND_RECOVER_ACCOUNT" in message


@pytest.mark.asyncio
async def test_order_id_cancel_refuses_when_freeze_state_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_account_instance_binding(tmp_path, _binding())
    account_root = tmp_path / "accounts" / "DU1234567"
    account_root.mkdir(parents=True, exist_ok=True)
    (account_root / "unresolved_exposure.flag").write_text("{not-json", encoding="utf-8")

    decision = await _decision(tmp_path, monkeypatch)

    assert decision.action.reason_code == "ACCOUNT_FREEZE_UNREADABLE"
    with pytest.raises(OrderRefusedError) as exc_info:
        decision.raise_if_blocked()
    assert "ACCOUNT_FREEZE_UNREADABLE" in str(exc_info.value)
    assert "Account freeze state is unreadable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_order_id_cancel_refuses_foreign_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = await _decision(tmp_path, monkeypatch)

    assert decision.action.reason_code == "FOREIGN_OR_UNCLAIMED"
    with pytest.raises(OrderRefusedError, match="FOREIGN_OR_UNCLAIMED"):
        decision.raise_if_blocked()


@pytest.mark.asyncio
async def test_order_id_cancel_reports_missing_open_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        order_cancel_decision,
        "list_open_orders",
        AsyncMock(return_value=[]),
    )

    with pytest.raises(OrderNotFoundError):
        await account_truth_cancel_decision(
            object(),  # type: ignore[arg-type]
            health=_health(),
            artifacts_root=tmp_path,
            order_id=42,
        )
