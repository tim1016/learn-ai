from __future__ import annotations

import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.broker.ibkr import capability as capability_module
from app.broker.ibkr.api_evidence import get_ibkr_api_evidence_recorder
from app.broker.ibkr.capability import (
    classify_entitlement,
    parse_ibkr_schedule,
    probe_session_data_capability,
)
from app.utils.timestamps import to_ms_utc


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")))


def test_parse_ibkr_schedule_converts_segments_through_instrument_timezone() -> None:
    windows = parse_ibkr_schedule(
        "20260702:0400-20260702:2000;20260703:0930-20260703:1300;20260704:CLOSED",
        "America/New_York",
    )

    assert windows[0].open_ms == _ms(2026, 7, 2, 4, 0)
    assert windows[0].close_ms == _ms(2026, 7, 2, 20, 0)
    assert windows[1].open_ms == _ms(2026, 7, 3, 9, 30)
    assert windows[1].close_ms == _ms(2026, 7, 3, 13, 0)
    assert len(windows) == 2


def test_parse_ibkr_schedule_fails_loudly_on_malformed_segment() -> None:
    with pytest.raises(ValueError, match="malformed IBKR schedule segment"):
        parse_ibkr_schedule("20260702:0400", "America/New_York")


@pytest.mark.parametrize(
    ("market_data_type", "codes", "expected"),
    [
        (1, [], "live"),
        (2, [], "frozen"),
        (3, [], "delayed"),
        (4, [], "delayed_frozen"),
        (None, [10167], "delayed"),
        (None, [354], "none"),
        (None, [], "none"),
    ],
)
def test_classify_entitlement_maps_ibkr_market_data_type(
    market_data_type: int | None,
    codes: list[int],
    expected: str,
) -> None:
    assert classify_entitlement(market_data_type, codes) == expected


class _Stock:
    def __init__(self, *, symbol: str, exchange: str, currency: str) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.secType = "STK"
        self.conId = 101


class _LimitOrder:
    def __init__(self, *, action: str, totalQuantity: int, lmtPrice: float) -> None:
        self.action = action
        self.totalQuantity = totalQuantity
        self.lmtPrice = lmtPrice
        self.outsideRth = False
        self.whatIf = False


def _install_fake_ib_async(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("ib_async")
    module.Stock = _Stock
    module.LimitOrder = _LimitOrder
    monkeypatch.setitem(sys.modules, "ib_async", module)


@pytest.mark.asyncio
async def test_probe_session_data_capability_uses_what_if_and_never_places_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ib_async(monkeypatch)
    monkeypatch.setattr(capability_module, "_MARKET_DATA_SAMPLE_S", 0)
    get_ibkr_api_evidence_recorder().clear()
    qualified = _Stock(symbol="SPY", exchange="SMART", currency="USD")
    qualified.conId = 756733
    details = SimpleNamespace(
        contract=qualified,
        tradingHours="20260702:0400-20260702:2000",
        liquidHours="20260702:0930-20260702:1600",
        timeZoneId="America/New_York",
        validExchanges="SMART,ARCA,OVERNIGHT",
    )
    ib = SimpleNamespace(
        reqContractDetailsAsync=AsyncMock(return_value=[details]),
        reqMarketDataType=MagicMock(),
        reqMktData=MagicMock(return_value=SimpleNamespace(marketDataType=1)),
        cancelMktData=MagicMock(),
        whatIfOrderAsync=AsyncMock(return_value=SimpleNamespace(warningText="", commission="0.01")),
        placeOrder=MagicMock(),
    )
    client = SimpleNamespace(
        ib=ib,
        connected_account="U1234567",
        require_live=lambda: None,
    )

    snapshot = await probe_session_data_capability(
        client,
        symbol="spy",
        as_of_ms=_ms(2026, 7, 2, 12, 0),
    )

    submitted_order = ib.whatIfOrderAsync.call_args.args[1]
    assert submitted_order.whatIf is True
    assert submitted_order.outsideRth is True
    ib.placeOrder.assert_not_called()
    assert snapshot.symbol == "SPY"
    assert snapshot.account_mode == "live"
    assert snapshot.sessions["RTH"].data == "live"
    assert snapshot.sessions["PRE"].tradeable == "yes"
    assert snapshot.sessions["POST"].tradeable == "yes"
    assert snapshot.raw_evidence
