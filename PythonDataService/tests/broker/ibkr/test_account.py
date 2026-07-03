"""Tests for app.broker.ibkr.account — summary parsing and position mapping.

ib_async types are stubbed with SimpleNamespace / MagicMock; nothing in
this module reaches the wire. The router is exercised separately in
test_router.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.broker.ibkr.account import (
    _coerce_float_or_none,
    _ibkr_position_to_model,
    fetch_account_summary,
    fetch_positions,
)
from app.broker.ibkr.client import BrokerError
from app.broker.ibkr.contracts import yyyymmdd_to_expiry_ms

# ── helpers ─────────────────────────────────────────────────────────────


def _value(account: str, tag: str, value: str, currency: str = "USD") -> SimpleNamespace:
    """Mirror the ``ib_async.AccountValue`` fields we read."""
    return SimpleNamespace(account=account, tag=tag, value=value, currency=currency)


def _stock_contract(symbol: str = "SPY", con_id: int = 756733) -> SimpleNamespace:
    return SimpleNamespace(
        secType="STK",
        conId=con_id,
        symbol=symbol,
        exchange="SMART",
        primaryExchange="ARCA",
        currency="USD",
        lastTradeDateOrContractMonth="",
        strike=0.0,
        right="",
        multiplier="",
    )


def _option_contract(
    *,
    symbol: str = "SPY",
    con_id: int = 700001,
    yyyymmdd: str = "20260619",
    strike: float = 580.0,
    right: str = "C",
) -> SimpleNamespace:
    return SimpleNamespace(
        secType="OPT",
        conId=con_id,
        symbol=symbol,
        exchange="SMART",
        primaryExchange="",
        currency="USD",
        lastTradeDateOrContractMonth=yyyymmdd,
        strike=strike,
        right=right,
        multiplier="100",
    )


def _fake_client(account_id: str, *, summary_rows=None, positions=None, cached_positions=None):
    """Build a minimal IbkrClient stand-in that the production code can use."""
    ib = SimpleNamespace(
        accountSummaryAsync=AsyncMock(return_value=list(summary_rows or [])),
        reqPositionsAsync=AsyncMock(return_value=list(positions or [])),
        positions=lambda: list(cached_positions or []),
        client=SimpleNamespace(cancelPositions=Mock()),
    )
    client = SimpleNamespace(
        ib=ib,
        connected_account=account_id,
        _last_event_ms=1,
        is_connected=lambda: True,
        require_connected=lambda: None,
    )
    return client


# ── _coerce_float_or_none ───────────────────────────────────────────────


def test_coerce_float_or_none_handles_strings_and_empties() -> None:
    assert _coerce_float_or_none("100000.5") == 100000.5
    assert _coerce_float_or_none(0.0) == 0.0
    assert _coerce_float_or_none(None) is None
    assert _coerce_float_or_none("") is None
    # Marker strings IBKR sometimes returns
    assert _coerce_float_or_none("BASE") is None
    assert _coerce_float_or_none("not-a-number") is None


# ── fetch_account_summary ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_account_summary_paper_account_maps_tags() -> None:
    rows = [
        _value("DU1234567", "TotalCashValue", "100000.50"),
        _value("DU1234567", "NetLiquidation", "100123.45"),
        _value("DU1234567", "BuyingPower", "400000"),
        _value("DU1234567", "InitMarginReq", "0"),
        _value("DU1234567", "MaintMarginReq", "0"),
        _value("DU1234567", "ExcessLiquidity", "100000"),
        _value("DU1234567", "EquityWithLoanValue", "100123.45"),
        _value("DU1234567", "AvailableFunds", "99987.65"),
        _value("DU1234567", "RealizedPnL", "0"),
        _value("DU1234567", "UnrealizedPnL", "123.45"),
        # A different account row should be ignored
        _value("DU9999999", "NetLiquidation", "999"),
    ]
    client = _fake_client("DU1234567", summary_rows=rows)
    out = await fetch_account_summary(client)

    assert out.account_id == "DU1234567"
    assert out.is_paper is True
    assert out.cash_balance == 100000.5
    assert out.net_liquidation == 100123.45
    assert out.buying_power == 400000.0
    assert out.excess_liquidity == 100000.0
    assert out.unrealized_pnl == 123.45
    assert out.realized_pnl == 0.0
    assert out.day_pnl is None  # Phase 2b will populate this from reqPnL
    assert out.fetched_at_ms > 0


@pytest.mark.asyncio
async def test_fetch_account_summary_filters_other_currencies() -> None:
    """Non-USD rows for the same account should not pollute the snapshot."""
    rows = [
        _value("DU1234567", "TotalCashValue", "1000", currency="USD"),
        _value("DU1234567", "TotalCashValue", "9999", currency="EUR"),  # ignore
        _value("DU1234567", "TotalCashValue", "8888", currency="JPY"),  # ignore
        _value("DU1234567", "NetLiquidation", "1500", currency="BASE"),  # accept
    ]
    client = _fake_client("DU1234567", summary_rows=rows)
    out = await fetch_account_summary(client)
    assert out.cash_balance == 1000.0
    assert out.net_liquidation == 1500.0


@pytest.mark.asyncio
async def test_fetch_account_summary_live_account_flag() -> None:
    rows = [_value("U7654321", "NetLiquidation", "50000")]
    client = _fake_client("U7654321", summary_rows=rows)
    out = await fetch_account_summary(client)
    assert out.is_paper is False
    assert out.account_id == "U7654321"


@pytest.mark.asyncio
async def test_fetch_account_summary_timeout_raises_broker_error() -> None:
    async def never_returns(_account_id: str):
        await asyncio.sleep(60)

    client = _fake_client("DU1234567")
    client.ib.accountSummaryAsync = AsyncMock(side_effect=never_returns)

    with pytest.raises(BrokerError, match="account summary request timed out"):
        await fetch_account_summary(client, timeout_s=0.001)


@pytest.mark.asyncio
async def test_fetch_account_summary_timeout_cancels_subscription_request() -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    wrapper = SimpleNamespace(acctSummary={}, startReq=Mock(return_value=future))
    ib_client = SimpleNamespace(
        getReqId=Mock(return_value=42),
        reqAccountSummary=Mock(),
        cancelAccountSummary=Mock(),
    )
    client = SimpleNamespace(
        ib=SimpleNamespace(client=ib_client, wrapper=wrapper),
        connected_account="DU1234567",
        require_connected=lambda: None,
    )

    with pytest.raises(BrokerError, match="account summary request timed out"):
        await fetch_account_summary(client, timeout_s=0.001)

    ib_client.reqAccountSummary.assert_called_once()
    ib_client.cancelAccountSummary.assert_called_once_with(42)


# ── _ibkr_position_to_model ─────────────────────────────────────────────


def test_position_model_for_stock() -> None:
    pos = SimpleNamespace(
        account="DU1234567",
        contract=_stock_contract("SPY", 756733),
        position=10,
        avgCost=590.5,
    )
    out = _ibkr_position_to_model(pos, "DU1234567", fetched_at_ms=1_800_000_000_000)
    assert out.symbol == "SPY"
    assert out.sec_type == "STK"
    assert out.con_id == 756733
    assert out.quantity == 10.0
    assert out.avg_cost == 590.5
    assert out.multiplier == 1
    assert out.expiry_ms is None
    assert out.strike is None
    assert out.right is None


def test_position_model_for_option_decodes_expiry() -> None:
    pos = SimpleNamespace(
        account="DU1234567",
        contract=_option_contract(yyyymmdd="20260619", strike=580.0, right="C"),
        position=-2,
        avgCost=350.0,
    )
    out = _ibkr_position_to_model(pos, "DU1234567", fetched_at_ms=1_800_000_000_000)
    assert out.sec_type == "OPT"
    assert out.right == "C"
    assert out.strike == 580.0
    assert out.expiry_ms == yyyymmdd_to_expiry_ms("20260619")
    assert out.multiplier == 100
    assert out.quantity == -2.0  # short
    assert out.avg_cost == 350.0


def test_position_model_handles_extended_yyyymmdd() -> None:
    """Some IBKR contracts use YYYYMMDD HH:MM:SS — we should still parse."""
    pos = SimpleNamespace(
        account="DU1234567",
        contract=_option_contract(yyyymmdd="20260619 16:00:00 US/Eastern"),
        position=1,
        avgCost=100.0,
    )
    out = _ibkr_position_to_model(pos, "DU1234567", fetched_at_ms=1_800_000_000_000)
    assert out.expiry_ms == yyyymmdd_to_expiry_ms("20260619")


# ── fetch_positions ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_positions_filters_by_account_and_skips_zero_quantity() -> None:
    positions = [
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("SPY", 756733),
            position=10,
            avgCost=590.0,
        ),
        SimpleNamespace(
            account="DU1234567",
            contract=_option_contract(con_id=700001),
            position=2,
            avgCost=350.0,
        ),
        # zero-quantity (closed position) — should be skipped
        SimpleNamespace(
            account="DU1234567",
            contract=_option_contract(con_id=700002),
            position=0,
            avgCost=0.0,
        ),
        # different account — should be ignored
        SimpleNamespace(
            account="DU9999999",
            contract=_stock_contract("AAPL", 12345),
            position=100,
            avgCost=180.0,
        ),
    ]
    client = _fake_client("DU1234567", positions=positions)
    snap = await fetch_positions(client)

    assert snap.account_id == "DU1234567"
    assert snap.is_paper is True
    assert len(snap.positions) == 2
    symbols = {p.symbol for p in snap.positions}
    assert symbols == {"SPY"}  # both rows are SPY (stock + option)
    sec_types = {p.sec_type for p in snap.positions}
    assert sec_types == {"STK", "OPT"}


@pytest.mark.asyncio
async def test_fetch_positions_continues_on_unparseable_row() -> None:
    """One bad row must not poison the rest of the snapshot."""
    bad_contract = SimpleNamespace(
        secType="OPT",
        conId="not-an-int",  # will trip int(contract.conId)
        symbol="SPY",
        exchange="SMART",
        primaryExchange="",
        currency="USD",
        lastTradeDateOrContractMonth="20260619",
        strike=580.0,
        right="C",
        multiplier="100",
    )
    positions = [
        SimpleNamespace(account="DU1234567", contract=bad_contract, position=1, avgCost=100.0),
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("AAPL", 12345),
            position=5,
            avgCost=180.0,
        ),
    ]
    client = _fake_client("DU1234567", positions=positions)
    snap = await fetch_positions(client)

    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_fetch_positions_timeout_fails_closed_by_default_even_with_cache() -> None:
    cached = [
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("SPY", 756733),
            position=3,
            avgCost=590.0,
        ),
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("MU", 9939),
            position=0,
            avgCost=0.0,
        ),
    ]

    async def never_returns():
        await asyncio.sleep(60)

    client = _fake_client("DU1234567", cached_positions=cached)
    client.ib.reqPositionsAsync = AsyncMock(side_effect=never_returns)

    with pytest.raises(BrokerError, match="live positions are unavailable"):
        await fetch_positions(client, timeout_s=0.001)

    client.ib.client.cancelPositions.assert_called_once_with()


@pytest.mark.asyncio
async def test_fetch_positions_cache_fallback_requires_known_cache_timestamp() -> None:
    async def never_returns():
        await asyncio.sleep(60)

    client = _fake_client(
        "DU1234567",
        cached_positions=[
            SimpleNamespace(
                account="DU1234567",
                contract=_stock_contract("SPY", 756733),
                position=3,
                avgCost=590.0,
            )
        ],
    )
    client.ib.reqPositionsAsync = AsyncMock(side_effect=never_returns)

    with pytest.raises(BrokerError, match="cache freshness is unknown"):
        await fetch_positions(client, timeout_s=0.001, allow_cache_fallback=True)

    client.ib.client.cancelPositions.assert_called_once_with()


@pytest.mark.asyncio
async def test_fetch_positions_read_only_cache_fallback_preserves_cache_timestamp() -> None:
    cached = [
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("SPY", 756733),
            position=3,
            avgCost=590.0,
        ),
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("MU", 9939),
            position=0,
            avgCost=0.0,
        ),
    ]

    async def never_returns():
        await asyncio.sleep(60)

    client = _fake_client("DU1234567", positions=cached, cached_positions=cached)
    live = await fetch_positions(client, timeout_s=1)
    client.ib.reqPositionsAsync = AsyncMock(side_effect=never_returns)

    snap = await fetch_positions(client, timeout_s=0.001, allow_cache_fallback=True)

    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "SPY"
    assert snap.used_cache_fallback is True
    assert snap.fetched_at_ms == live.fetched_at_ms
    assert snap.positions[0].fetched_at_ms == live.fetched_at_ms
    client.ib.client.cancelPositions.assert_called_once_with()


@pytest.mark.asyncio
async def test_fetch_positions_timeout_guard_blocks_retry_until_reconnect() -> None:
    live = [
        SimpleNamespace(
            account="DU1234567",
            contract=_stock_contract("AAPL", 12345),
            position=5,
            avgCost=180.0,
        )
    ]

    async def never_returns():
        await asyncio.sleep(60)

    client = _fake_client("DU1234567", cached_positions=live)
    client.ib.reqPositionsAsync = AsyncMock(side_effect=never_returns)

    with pytest.raises(BrokerError, match="live positions are unavailable"):
        await fetch_positions(client, timeout_s=0.001)
    with pytest.raises(BrokerError, match="previously timed out"):
        await fetch_positions(client, timeout_s=1)

    assert client.ib.reqPositionsAsync.await_count == 1

    client._last_event_ms = 2
    client.ib.reqPositionsAsync = AsyncMock(return_value=live)
    snap_again = await fetch_positions(client, timeout_s=1)

    assert len(snap_again.positions) == 1
    assert snap_again.positions[0].symbol == "AAPL"
    assert snap_again.used_cache_fallback is False
    assert client.ib.reqPositionsAsync.await_count == 1


@pytest.mark.asyncio
async def test_fetch_positions_serializes_req_positions_per_client() -> None:
    in_flight = 0
    max_in_flight = 0

    async def delayed_positions():
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return [
                SimpleNamespace(
                    account="DU1234567",
                    contract=_stock_contract("SPY", 756733),
                    position=3,
                    avgCost=590.0,
                )
            ]
        finally:
            in_flight -= 1

    client = _fake_client("DU1234567")
    client.ib.reqPositionsAsync = AsyncMock(side_effect=delayed_positions)

    first, second = await asyncio.gather(
        fetch_positions(client, timeout_s=1),
        fetch_positions(client, timeout_s=1),
    )

    assert len(first.positions) == 1
    assert len(second.positions) == 1
    assert client.ib.reqPositionsAsync.await_count == 2
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_fetch_positions_timeout_raises_when_cache_unavailable() -> None:
    async def never_returns():
        await asyncio.sleep(60)

    client = _fake_client("DU1234567")
    client.ib.reqPositionsAsync = AsyncMock(side_effect=never_returns)
    client._learn_ai_positions_cache_fetched_at_ms = 1_780_000_000_000
    delattr(client.ib, "positions")

    with pytest.raises(BrokerError, match="positions cache is unavailable"):
        await fetch_positions(client, timeout_s=0.001, allow_cache_fallback=True)
