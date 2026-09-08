"""The shadow world's ports: live reads, synthesized fills, no submission (ADR 0059 D2, D5.5)."""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.broker.alpaca.clerk.shadow_broker as shadow_broker_module
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW, ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.account_authority import shadow_evidence_account_id_for_strategy
from app.broker.alpaca.clerk.shadow_broker import (
    SHADOW_BROKER_ID,
    NoSubmitAlpacaTradePort,
    ShadowFillBindingError,
    ShadowNamespacePoisoned,
    ShadowNamespaceUnproven,
    ShadowPorts,
    compose_shadow_ports,
    verify_shadow_namespace_empty,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    OrderSide,
    OrderType,
    TimeInForce,
)
from app.marketdata.feed import MarketDataBar
from app.services.session_authority import declared_session_bounds, et_minute_of_day_ms
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarLedger

DAY = date(2026, 9, 8)  # a Tuesday; a full NYSE session
SID = "ema-shadow-1"
EVIDENCE = shadow_evidence_account_id_for_strategy(SID)
MINUTE_MS = 60_000


class _Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def _snapshot() -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id="9LIVE0001",
        account_mode="live",
        account_status="ACTIVE",
        currency="USD",
        cash=25_000.0,
        equity=25_000.0,
        buying_power=25_000.0,
        portfolio_value=25_000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1,
        observed_at_ms=2,
    )


def _vendor_order(client_order_id: str | None) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"vendor-{client_order_id}",
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=1.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=100.0,
        status="filled",
        submitted_at_ms=1,
        created_at_ms=1,
        updated_at_ms=1,
        filled_at_ms=1,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=1,
    )


class _LiveRead:
    """A live read port double: real account truth, and it never serves positions."""

    broker_id = "alpaca"

    def __init__(self, orders: list[BrokerOrder] | None = None) -> None:
        self.orders = orders or []
        self.order_calls: list[str | None] = []

    def capabilities(self) -> Any:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return _snapshot()

    async def list_positions(self) -> list:
        raise AssertionError("the shadow world never reads the live account's positions")

    async def list_orders(self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None) -> list[BrokerOrder]:
        del after_ms
        self.order_calls.append(status)
        return self.orders[:limit]

    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list:
        del after_ms, limit
        return []

    async def list_assets(self, *, status: str | None = None, limit: int | None = 100) -> list:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> None:
        del symbol
        return None

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return BrokerClockEvidence(broker="alpaca", is_open=True, vendor_timestamp_ms=7, next_open_ms=None, next_close_ms=None, observed_at_ms=7)

    async def get_portfolio_history(self, history_range: Any) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(timestamps=[], equity=[], profit_loss=[], base_value=None, timeframe="1D")


def _retain(
    bars: SourceBarLedger,
    *,
    minute: int,
    close: str,
    low: str | None = None,
    high: str | None = None,
    phase: str = "RTH",
) -> RetainedSourceBar:
    start_ms = et_minute_of_day_ms(DAY, minute)
    return bars.append(
        MarketDataBar(
            feed_id="fixture",
            symbol="SPY",
            start_ms=start_ms,
            end_ms=start_ms + MINUTE_MS,
            open=Decimal(close),
            high=Decimal(high or close),
            low=Decimal(low or close),
            close=Decimal(close),
            volume=1,
            fetched_at_ms=start_ms + MINUTE_MS,
            session_phase=phase,
        ),
        run_id="run-1",
    )


def _market_leg() -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side=OrderSide.BUY, quantity=1.0, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY)


def _extended_leg(limit: float) -> BrokerOrderLeg:
    return BrokerOrderLeg(
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=1.0,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit,
        extended_hours=True,
    )


@pytest.fixture
def world(tmp_path: Path) -> tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]:
    clock = _Clock(et_minute_of_day_ms(DAY, 600))
    live = _LiveRead()
    ports = compose_shadow_ports(live_read=live, live_account_id="9LIVE0001", artifacts_root=tmp_path, clock=clock)
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=EVIDENCE)
    return ports, bars, live, clock


async def test_regular_session_leg_fills_at_the_decision_bar_close(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, _clock = world
    decision = _retain(bars, minute=600, close="100.25")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:a", decision)

    order = await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:a")

    assert order.broker == SHADOW_BROKER_ID
    assert order.order_id == "shadow-order:learn-ai/ema-shadow-1/v1:a"
    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 100.25, decision.end_ms)
    assert order.events[0].execution_id == "shadow-execution:learn-ai/ema-shadow-1/v1:a"
    record = ports.book.record("learn-ai/ema-shadow-1/v1:a")
    assert record is not None and record.anchor is not None
    assert (record.anchor.fill_model, record.anchor.fill_bar_ref) == ("decision_bar_close", decision.bar_ref)
    assert [p.symbol for p in await ports.read.list_positions()] == ["SPY"]


async def test_submit_without_a_bound_decision_bar_is_refused(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, _clock = world
    _retain(bars, minute=600, close="100.25")
    with pytest.raises(ShadowFillBindingError, match="bound to this order"):
        await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:unbound")


async def test_extended_leg_rests_then_fills_at_the_limit_when_a_later_bar_touches(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1020, close="100.00", low="99.50", phase="POST")  # 17:00 ET
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:x", decision)

    resting = await ports.trade.submit(_extended_leg(100.50), client_order_id="learn-ai/ema-shadow-1/v1:x")
    assert (resting.status, resting.filled_quantity, resting.extended_hours) == ("new", 0.0, True)
    record = ports.book.record("learn-ai/ema-shadow-1/v1:x")
    assert record is not None and record.anchor is not None
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None and record.anchor.cancel_at_ms == bounds.close_ms

    _retain(bars, minute=1021, close="100.80", low="100.70", phase="POST")  # does not touch
    assert (await ports.read.list_orders())[0].status == "new"

    touching = _retain(bars, minute=1022, close="100.60", low="100.40", phase="POST")
    clock.now_ms = touching.end_ms + 1
    order = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:x")
    assert order is not None
    assert (order.status, order.filled_avg_price, order.filled_at_ms) == ("filled", 100.5, touching.end_ms)
    settled = ports.book.record("learn-ai/ema-shadow-1/v1:x")
    assert settled is not None and settled.anchor is not None
    assert (settled.anchor.fill_model, settled.anchor.fill_bar_ref) == ("limit_touch", touching.bar_ref)


async def test_extended_leg_cancels_one_bucket_after_the_declared_close_when_untouched(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1190, close="100.00", low="100.00", phase="POST")  # 19:50 ET
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:y", decision)
    await ports.trade.submit(_extended_leg(99.00), client_order_id="learn-ai/ema-shadow-1/v1:y")
    for minute in range(1191, 1200):
        _retain(bars, minute=minute, close="100.00", low="99.90", phase="POST")
    bounds = declared_session_bounds(DAY, ALPACA_EXTENDED_HOURS_WINDOW)
    assert bounds is not None

    clock.now_ms = bounds.close_ms + MINUTE_MS - 1
    assert (await ports.read.list_orders())[0].status == "new"

    clock.now_ms = bounds.close_ms + MINUTE_MS
    order = (await ports.read.list_orders())[0]
    assert (order.status, order.canceled_at_ms, order.filled_quantity) == ("canceled", bounds.close_ms, 0.0)
    assert await ports.read.list_positions() == []


async def test_cancel_marks_a_resting_order_canceled_and_is_a_no_op_once_filled(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, bars, _live, clock = world
    decision = _retain(bars, minute=1020, close="100.00", phase="POST")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:c", decision)
    resting = await ports.trade.submit(_extended_leg(99.00), client_order_id="learn-ai/ema-shadow-1/v1:c")
    clock.now_ms = decision.end_ms + 5

    await ports.trade.cancel(resting.order_id)
    canceled = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:c")
    assert canceled is not None and (canceled.status, canceled.canceled_at_ms) == ("canceled", decision.end_ms + 5)

    # Later than the resting order's decision bar: one stream's retained
    # delivery is monotonic, so the second decision cannot precede the first.
    filled_decision = _retain(bars, minute=1030, close="100.25", phase="POST")
    ports.trade.bind_evaluated_bar("learn-ai/ema-shadow-1/v1:f", filled_decision)
    filled = await ports.trade.submit(_market_leg(), client_order_id="learn-ai/ema-shadow-1/v1:f")
    await ports.trade.cancel(filled.order_id)
    still_filled = await ports.trade.get_order_by_client_order_id("learn-ai/ema-shadow-1/v1:f")
    assert still_filled is not None and still_filled.status == "filled"


async def test_read_port_serves_live_account_truth_and_synthesized_custody(world: tuple[ShadowPorts, SourceBarLedger, _LiveRead, _Clock]) -> None:
    ports, _bars, _live, _clock = world
    assert (await ports.read.get_account()).account_mode == "live"
    assert (await ports.read.get_clock_evidence()).vendor_timestamp_ms == 7
    assert ports.read.capabilities() is ALPACA_LIVE_CAPABILITIES
    assert await ports.read.list_positions() == []
    assert await ports.read.list_orders() == []
    assert ports.account_id == "shadow:9LIVE0001"
    assert ports.read.broker_id == SHADOW_BROKER_ID == ports.trade.broker_id


async def test_namespace_check_passes_clean_accounts_and_refuses_poison_or_full_pages() -> None:
    await verify_shadow_namespace_empty(_LiveRead([_vendor_order(None), _vendor_order("operator-ticket-7")]))

    with pytest.raises(ShadowNamespacePoisoned) as poisoned:
        await verify_shadow_namespace_empty(_LiveRead([_vendor_order("learn-ai/ema-1/v1:abc")]))
    assert poisoned.value.reason_code == "SHADOW_NAMESPACE_POISONED"
    assert poisoned.value.order_ids == ("vendor-learn-ai/ema-1/v1:abc",)

    full_page = _LiveRead([_vendor_order(None)] * 500)
    with pytest.raises(ShadowNamespaceUnproven) as unproven:
        await verify_shadow_namespace_empty(full_page)
    assert unproven.value.reason_code == "SHADOW_NAMESPACE_UNPROVEN"
    assert full_page.order_calls == ["all"]


def test_bars_after_returns_one_stream_in_open_order(tmp_path: Path) -> None:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id=EVIDENCE)
    first = _retain(bars, minute=1020, close="1")
    second = _retain(bars, minute=1021, close="2")
    third = _retain(bars, minute=1022, close="3")

    assert bars.bars_after(provider="fixture", symbol="SPY", start_ms=first.end_ms) == [second, third]
    assert bars.bars_after(provider="fixture", symbol="QQQ", start_ms=0) == []


def test_the_trade_port_holds_no_vendor_client() -> None:
    source = inspect.getsource(shadow_broker_module)
    assert "TradingClient" not in source and "AlpacaBroker" not in source
    assert list(inspect.signature(NoSubmitAlpacaTradePort.__init__).parameters) == ["self", "book"]
