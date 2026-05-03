"""Tests for app.broker.ibkr.orders — paper-mode safety + dispatch.

The four safety layers all live in ``_enforce_paper_safety``; each test
flips one layer to a bad value and asserts ``OrderRefusedError`` before
``placeOrder`` is reached. ib_async types are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.models import IbkrOrderSpec
from app.broker.ibkr.orders import (
    OrderRefusedError,
    _enforce_paper_safety,
    place_paper_order,
)


def _spec(**overrides) -> IbkrOrderSpec:
    base = {
        "symbol": "SPY",
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 1.0,
        "order_type": "MKT",
        "limit_price": None,
        "time_in_force": "DAY",
        "expiry_ms": None,
        "strike": None,
        "right": None,
        "multiplier": 100,
        "confirm_paper": True,
    }
    base.update(overrides)
    return IbkrOrderSpec(**base)


def _client(
    *,
    account: str = "DU1234567",
    mode: str = "paper",
    port: int = 4002,
    place_order_return=None,
) -> SimpleNamespace:
    settings = IbkrSettings(mode=mode, port=port, _env_file=None)
    qualified = SimpleNamespace(conId=12345)
    place_order_return = place_order_return or SimpleNamespace(
        order=SimpleNamespace(orderId=42, permId=99),
        orderStatus=SimpleNamespace(status="PendingSubmit"),
    )
    ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[qualified]),
        placeOrder=MagicMock(return_value=place_order_return),
    )
    return SimpleNamespace(
        ib=ib,
        settings=settings,
        connected_account=account,
        is_connected=lambda: True,
        require_connected=lambda: None,
    )


# ── safety layers ──────────────────────────────────────────────────────


def test_enforce_paper_safety_passes_in_clean_paper_mode() -> None:
    out = _enforce_paper_safety(_client(), _spec())
    assert out == "DU1234567"


def test_enforce_paper_safety_refuses_when_mode_is_live() -> None:
    with pytest.raises(OrderRefusedError, match="IBKR_MODE"):
        _enforce_paper_safety(_client(mode="live", port=4001), _spec())


def test_enforce_paper_safety_refuses_when_account_is_live_despite_paper_mode() -> None:
    """Even if env says paper, a U-prefix account must be refused."""
    with pytest.raises(OrderRefusedError, match="DU"):
        _enforce_paper_safety(_client(account="U7654321"), _spec())


def test_enforce_paper_safety_refuses_when_confirm_paper_is_false() -> None:
    with pytest.raises(OrderRefusedError, match="confirm_paper"):
        _enforce_paper_safety(_client(), _spec(confirm_paper=False))


# ── place_paper_order happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_place_paper_order_market_buy_returns_ack() -> None:
    client = _client()
    ack = await place_paper_order(client, _spec(action="BUY", quantity=10))

    assert ack.account_id == "DU1234567"
    assert ack.is_paper is True
    assert ack.order_id == 42
    assert ack.perm_id == 99
    assert ack.symbol == "SPY"
    assert ack.action == "BUY"
    assert ack.quantity == 10.0
    assert ack.order_type == "MKT"
    assert ack.limit_price is None
    assert ack.status == "PendingSubmit"
    assert ack.placed_at_ms > 0
    client.ib.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_place_paper_order_limit_sell_passes_price_through() -> None:
    client = _client()
    ack = await place_paper_order(
        client,
        _spec(action="SELL", quantity=2, order_type="LMT", limit_price=421.50),
    )
    assert ack.action == "SELL"
    assert ack.order_type == "LMT"
    assert ack.limit_price == 421.50

    # Inspect the order that was submitted
    submitted_order = client.ib.placeOrder.call_args.args[1]
    assert submitted_order.action == "SELL"
    assert submitted_order.totalQuantity == 2
    assert float(submitted_order.lmtPrice) == 421.50
    assert submitted_order.tif == "DAY"


@pytest.mark.asyncio
async def test_place_paper_order_option_requires_expiry_strike_right() -> None:
    client = _client()
    bad = _spec(sec_type="OPT")  # missing expiry_ms / strike / right
    with pytest.raises(OrderRefusedError, match="OPT order requires"):
        await place_paper_order(client, bad)


@pytest.mark.asyncio
async def test_place_paper_order_lmt_requires_limit_price() -> None:
    client = _client()
    bad = _spec(order_type="LMT", limit_price=None)
    # Pydantic itself rejects limit_price=None when order_type=LMT? Actually no,
    # we validate that in code. The model lets it through (gt=0 only when
    # provided), so the OrderRefusedError fires from _build_order.
    with pytest.raises(OrderRefusedError, match="LMT order requires limit_price"):
        await place_paper_order(client, bad)


# ── safety layers route into place_paper_order ────────────────────────


@pytest.mark.asyncio
async def test_place_paper_order_refuses_in_live_mode() -> None:
    client = _client(mode="live", port=4001)
    with pytest.raises(OrderRefusedError):
        await place_paper_order(client, _spec())
    # placeOrder must never have been called
    client.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_place_paper_order_refuses_when_confirm_missing() -> None:
    client = _client()
    with pytest.raises(OrderRefusedError, match="confirm_paper"):
        await place_paper_order(client, _spec(confirm_paper=False))
    client.ib.placeOrder.assert_not_called()
