"""Tests for completed-order sweeps and what-if previews."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.models import IbkrOrderSpec
from app.broker.ibkr.order_history import list_completed_orders
from app.broker.ibkr.order_previews import preview_paper_order


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
        "order_ref": None,
        "manual_order": True,
    }
    base.update(overrides)
    return IbkrOrderSpec(**base)


def _client(*, account: str = "DU1234567") -> SimpleNamespace:
    settings = IbkrSettings(mode="paper", port=4002, readonly=False, _env_file=None)
    qualified = SimpleNamespace(conId=12345)
    ib = SimpleNamespace(
        qualifyContractsAsync=AsyncMock(return_value=[qualified]),
        reqCompletedOrdersAsync=AsyncMock(return_value=[]),
        whatIfOrderAsync=AsyncMock(
            return_value=SimpleNamespace(
                initMarginChange="125.50",
                maintMarginChange="75.25",
                equityWithLoanChange="-125.50",
                commission="1.23",
                warningText="",
            )
        ),
    )
    return SimpleNamespace(
        ib=ib,
        settings=settings,
        connected_account=account,
        is_connected=lambda: True,
        require_connected=lambda: None,
        require_live=lambda: None,
    )


def _trade(
    *,
    account: str = "",
    order_id: int = 42,
    status: str = "Filled",
    order_ref: str | None = "learn-ai/bot-a/v1:intent-a",
) -> SimpleNamespace:
    contract = SimpleNamespace(secType="STK", conId=12345, symbol="SPY")
    order = SimpleNamespace(
        account=account,
        orderId=order_id,
        permId=9001,
        action="BUY",
        totalQuantity=1.0,
        lmtPrice=0.0,
        tif="DAY",
        orderRef=order_ref or "",
    )
    order_status = SimpleNamespace(
        status=status,
        filled=1.0,
        remaining=0.0,
        avgFillPrice=450.0,
    )
    return SimpleNamespace(
        contract=contract,
        order=order,
        orderStatus=order_status,
        fills=[],
    )


@pytest.mark.asyncio
async def test_list_completed_orders_returns_terminal_order_evidence() -> None:
    client = _client()
    client.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[_trade()])

    rows = await list_completed_orders(client)

    assert len(rows) == 1
    assert rows[0].status == "Filled"
    assert rows[0].order_ref == "learn-ai/bot-a/v1:intent-a"
    assert rows[0].ibkr_evidence is not None
    assert rows[0].ibkr_evidence.request is not None
    assert rows[0].ibkr_evidence.request.call == "reqCompletedOrdersAsync"
    assert rows[0].ibkr_evidence.response is not None
    assert rows[0].ibkr_evidence.response.callback == "completedOrder"


@pytest.mark.asyncio
async def test_list_completed_orders_filters_other_accounts() -> None:
    client = _client()
    client.ib.reqCompletedOrdersAsync = AsyncMock(
        return_value=[
            _trade(account="DU1234567", order_id=1),
            _trade(account="DU9999999", order_id=2),
        ]
    )

    rows = await list_completed_orders(client)

    assert [row.order_id for row in rows] == [1]


@pytest.mark.asyncio
async def test_list_completed_orders_reports_missing_api_as_broker_error() -> None:
    client = _client()
    delattr(client.ib, "reqCompletedOrdersAsync")

    with pytest.raises(BrokerError, match="reqCompletedOrdersAsync"):
        await list_completed_orders(client)


@pytest.mark.asyncio
async def test_list_completed_orders_reports_unparseable_rows_as_broker_error() -> None:
    client = _client()
    bad_trade = _trade()
    bad_trade.contract.conId = "not-an-int"
    client.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[bad_trade])

    with pytest.raises(BrokerError, match="unparseable row"):
        await list_completed_orders(client)


@pytest.mark.asyncio
async def test_preview_paper_order_uses_non_submitting_what_if_path() -> None:
    client = _client()

    preview = await preview_paper_order(client, _spec())

    submitted_order = client.ib.whatIfOrderAsync.call_args.args[1]
    assert submitted_order.whatIf is True
    assert preview.init_margin_change == 125.50
    assert preview.maint_margin_change == 75.25
    assert preview.equity_with_loan_change == -125.50
    assert preview.commission == 1.23
    assert preview.ibkr_evidence is not None
    assert preview.ibkr_evidence.request is not None
    assert preview.ibkr_evidence.request.call == "whatIfOrderAsync"
