"""Tests covering the real IBKR boundary inside LiveEngine.

These live alongside the FakeBroker tests because they exercise the
same engine class — what differs is the input shape (IBKR wire types
instead of engine types) and the broker surface (a stub adapter with
the IBKR event-stream protocol instead of the deterministic fake).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrMinuteBar,
    IbkrOrderAck,
    IbkrOrderEvent,
    IbkrOrderSpec,
    IbkrPositionsSnapshot,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.live.broker_callbacks import BrokerCallbackWal, broker_callbacks_wal_path
from app.engine.live.live_engine import LiveEngine
from app.engine.live.live_portfolio import LiveBrokerEventStreamError
from app.engine.strategy.base import Strategy


class _StubIbkrBroker:
    """In-memory stand-in for IbkrBrokerAdapter.

    Implements the protocols ``LiveEngine`` cares about: ``BrokerAdapter``
    (account / positions / place_order / cancel_open_orders) plus the
    optional IBKR event-stream hooks. Buffers ``IbkrOrderEvent`` values
    for the engine to drain.
    """

    def __init__(self) -> None:
        self.cash = Decimal("100000")
        self.placed_specs: list[IbkrOrderSpec] = []
        self._next_order_id = 100
        self._owned: set[int] = set()
        self._buffer: list[IbkrOrderEvent] = []
        self.stream_started = False
        self.stream_stopped = False
        self.stream_failure: BaseException | None = None

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return IbkrAccountSummary(
            account_id="DU123",
            is_paper=True,
            cash_balance=float(self.cash),
            net_liquidation=float(self.cash),
            fetched_at_ms=1,
        )

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return IbkrPositionsSnapshot(
            account_id="DU123",
            is_paper=True,
            positions=[],
            fetched_at_ms=1,
        )

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        self.placed_specs.append(spec)
        order_id = self._next_order_id
        self._next_order_id += 1
        self._owned.add(order_id)
        return IbkrOrderAck(
            account_id="DU123",
            is_paper=True,
            order_id=order_id,
            client_id=1,
            con_id=756733,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )

    async def cancel_open_orders(self) -> list[int]:
        return []

    async def start_event_stream(self) -> None:
        self.stream_started = True

    async def stop_event_stream(self) -> None:
        self.stream_stopped = True

    def drain_broker_events(self) -> list[IbkrOrderEvent]:
        events = list(self._buffer)
        self._buffer.clear()
        return events

    def push_fill(self, *, order_id: int, fill_quantity: float, price: float, ts_ms: int) -> None:
        """Test helper: queue an IBKR fill event."""
        self._buffer.append(
            IbkrOrderEvent(
                account_id="DU123",
                order_id=order_id,
                event_type="fill",
                status="Filled",
                fill_quantity=fill_quantity,
                avg_fill_price=price,
                last_fill_price=price,
                cumulative_filled=fill_quantity,
                remaining=0.0,
                ts_ms=ts_ms,
            )
        )

    def push_status(self, *, order_id: int, status: str, ts_ms: int) -> None:
        """Test helper: queue an IBKR order-status event."""
        self._buffer.append(
            IbkrOrderEvent(
                account_id="DU123",
                order_id=order_id,
                event_type="status",
                status=status,
                ts_ms=ts_ms,
            )
        )


def _ibkr_bar(start: datetime, *, open_: str = "500", close: str = "500") -> IbkrMinuteBar:
    start_ms = int(start.astimezone(UTC).timestamp() * 1000)
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(open_),
        high=max(Decimal(open_), Decimal(close)),
        low=min(Decimal(open_), Decimal(close)),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start_ms + 60_000,
    )


async def _iter_ibkr(bars: Iterable[IbkrMinuteBar]) -> AsyncIterator[IbkrMinuteBar]:
    for bar in bars:
        yield bar


class _RecordingStrategy(Strategy):
    """Captures the bars consolidated by the engine."""

    def __init__(self) -> None:
        super().__init__()
        self.consolidated: list[TradeBar] = []
        self.events = []

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        self.consolidated.append(bar)

    def on_order_event(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_run_with_ibkr_bars_feeds_tradebars_into_strategy() -> None:
    """Regression: the engine must convert IbkrMinuteBar → TradeBar.

    Before the fix, the live engine fed ``IbkrMinuteBar`` directly into
    a path that expects ``TradeBar`` (consolidator.update, force-flat
    branch, equity_curve.append on ``minute_bar.end_time``). Those
    calls fail because ``IbkrMinuteBar`` has neither ``time`` nor
    ``end_time`` — only ``start_ms`` / ``end_ms``.
    """
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)
    strategy = _RecordingStrategy()
    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]

    result = await engine.run(strategy, ibkr_bars=_iter_ibkr(bars))

    # Consolidator received TradeBars, not IbkrMinuteBar instances.
    assert all(isinstance(b, TradeBar) for b in strategy.consolidated)
    assert all(b.time.tzinfo is not None for b in strategy.consolidated)
    # Equity curve is populated per minute; bars retained on the result.
    assert len(result.bars) == 2
    assert all(isinstance(b, TradeBar) for b in result.bars)
    # Adapter event stream was started/stopped around the run.
    assert broker.stream_started is True
    assert broker.stream_stopped is True


class _OneEntryStrategy(Strategy):
    """Submits a single SetHoldings on the first consolidator emission."""

    def __init__(self) -> None:
        super().__init__()
        self.events = []
        self._sent = False

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self._on_bar)

    def _on_bar(self, _bar: TradeBar) -> None:
        assert self.ctx is not None
        if not self._sent:
            self.ctx.set_holdings("SPY", Decimal("1"))
            self._sent = True

    def on_order_event(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_real_broker_fill_event_updates_portfolio_and_strategy() -> None:
    """A buffered IBKR fill is routed through portfolio + strategy."""
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)
    strategy = _OneEntryStrategy()

    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 32, tzinfo=UTC)),
    ]

    # Inject a fill that the engine will pick up on the third bar's
    # drain step. The engine assigns order_id=100 (first placement);
    # we mirror the broker's accounting in the fill event.
    fill_ts_ms = int(datetime(2026, 5, 4, 14, 32, tzinfo=UTC).timestamp() * 1000)
    bars_iter = _gated_ibkr_bars(bars, broker, on_index=2, fill_kwargs=dict(
        order_id=100, fill_quantity=200.0, price=500.50, ts_ms=fill_ts_ms,
    ))

    result = await engine.run(strategy, ibkr_bars=bars_iter)

    # Engine submitted exactly one entry order via the broker boundary.
    assert len(broker.placed_specs) == 1
    assert broker.placed_specs[0].action == "BUY"
    assert broker.placed_specs[0].quantity == 200

    # The fill arrived → portfolio updated, strategy notified, and the
    # event was retained in LiveRunResult.order_events.
    assert len(result.order_events) == 1
    fill_event = result.order_events[0]
    assert fill_event.symbol == "SPY"
    assert fill_event.fill_quantity == 200
    assert fill_event.fill_price == Decimal("500.50")
    assert fill_event.tag == "SetHoldings"
    assert len(strategy.events) == 1
    assert strategy.events[0] is fill_event
    # Position state reflects the fill.
    assert result.open_positions == {"SPY": 200}
    assert result.pending_orders == 0


@pytest.mark.asyncio
async def test_real_broker_fill_is_written_to_raw_callback_wal(tmp_path: Path) -> None:
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker, output_dir=tmp_path)
    strategy = _OneEntryStrategy()
    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 32, tzinfo=UTC)),
    ]
    fill_ts_ms = int(datetime(2026, 5, 4, 14, 32, tzinfo=UTC).timestamp() * 1000)
    bars_iter = _gated_ibkr_bars(
        bars,
        broker,
        on_index=2,
        fill_kwargs=dict(
            order_id=100,
            fill_quantity=200.0,
            price=500.50,
            ts_ms=fill_ts_ms,
        ),
    )

    await engine.run(strategy, ibkr_bars=bars_iter)

    records = BrokerCallbackWal(broker_callbacks_wal_path(tmp_path)).read_all()
    assert len(records) == 1
    assert records[0].seq == 1
    assert records[0].callback_type == "fill"
    assert records[0].event.order_id == 100
    assert records[0].event.fill_quantity == 200.0
    assert records[0].idempotency_key.endswith(f"|{fill_ts_ms}")


@pytest.mark.asyncio
async def test_real_broker_status_is_written_to_raw_callback_wal(tmp_path: Path) -> None:
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker, output_dir=tmp_path)
    strategy = _RecordingStrategy()
    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]
    status_ts_ms = int(datetime(2026, 5, 4, 14, 31, tzinfo=UTC).timestamp() * 1000)

    async def _push_status_before_second_bar() -> AsyncIterator[IbkrMinuteBar]:
        for idx, bar in enumerate(bars):
            if idx == 1:
                broker.push_status(order_id=100, status="Submitted", ts_ms=status_ts_ms)
            yield bar

    result = await engine.run(strategy, ibkr_bars=_push_status_before_second_bar())

    records = BrokerCallbackWal(broker_callbacks_wal_path(tmp_path)).read_all()
    assert result.order_events == []
    assert len(records) == 1
    assert records[0].seq == 1
    assert records[0].callback_type == "status"
    assert records[0].event.status == "Submitted"
    assert records[0].idempotency_key.endswith(f"|{status_ts_ms}")


@pytest.mark.asyncio
async def test_real_broker_fill_signs_quantity_for_sell_orders() -> None:
    """A SELL order's IBKR fill must produce a negative engine quantity."""
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)

    class _SellOnceStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__()
            self._done = False
            self.events = []

        def initialize(self) -> None:
            assert self.ctx is not None
            self.set_cash(Decimal("100000"))
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=1), self._on_bar)
            # Pre-load a long position via the portfolio so liquidate produces a SELL.
            self.ctx.portfolio.get_position("SPY").quantity = 50

        def _on_bar(self, _bar: TradeBar) -> None:
            assert self.ctx is not None
            if not self._done:
                self.ctx.liquidate("SPY")
                self._done = True

        def on_order_event(self, event) -> None:
            self.events.append(event)

    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 32, tzinfo=UTC)),
    ]
    fill_ts_ms = int(datetime(2026, 5, 4, 14, 32, tzinfo=UTC).timestamp() * 1000)
    bars_iter = _gated_ibkr_bars(bars, broker, on_index=2, fill_kwargs=dict(
        order_id=100, fill_quantity=50.0, price=500.50, ts_ms=fill_ts_ms,
    ))

    strategy = _SellOnceStrategy()
    result = await engine.run(strategy, ibkr_bars=bars_iter)

    assert broker.placed_specs[0].action == "SELL"
    assert len(result.order_events) == 1
    assert result.order_events[0].fill_quantity == -50
    assert result.order_events[0].tag == "Liquidate"


@pytest.mark.asyncio
async def test_real_broker_drops_fill_for_unknown_order() -> None:
    """Fills for order IDs we never placed are ignored, never applied."""
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)
    strategy = _RecordingStrategy()

    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]
    bars_iter = _gated_ibkr_bars(bars, broker, on_index=1, fill_kwargs=dict(
        order_id=99999,  # never placed by this runner
        fill_quantity=10.0,
        price=500.0,
        ts_ms=int(datetime(2026, 5, 4, 14, 31, tzinfo=UTC).timestamp() * 1000),
    ))

    result = await engine.run(strategy, ibkr_bars=bars_iter)

    assert result.order_events == []
    assert result.open_positions == {}


@pytest.mark.asyncio
async def test_run_aborts_when_event_stream_terminates() -> None:
    """A dead event-stream task must fail the run, not be silently ignored.

    If the broker's stream exits via an unhandled exception, fills stop
    arriving but order submission keeps going — the engine's portfolio
    state would silently desync from broker reality. The engine reads
    ``stream_failure`` each iteration and raises.
    """
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)
    strategy = _RecordingStrategy()

    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]

    async def _failing_after_first(
        source: list[IbkrMinuteBar],
    ) -> AsyncIterator[IbkrMinuteBar]:
        for idx, bar in enumerate(source):
            if idx == 1:
                broker.stream_failure = ConnectionError("simulated stream death")
            yield bar

    with pytest.raises(LiveBrokerEventStreamError) as excinfo:
        await engine.run(strategy, ibkr_bars=_failing_after_first(bars))

    # Original cause is preserved on the chained exception.
    assert isinstance(excinfo.value.__cause__, ConnectionError)
    # The engine still tore down the stream on the way out.
    assert broker.stream_stopped is True


@pytest.mark.asyncio
async def test_final_drain_captures_fill_after_last_bar() -> None:
    """A fill queued after the last per-bar drain must still be applied.

    Common on finite test/replay streams and on shutdown: IBKR reports
    the fill *after* the last bar's drain step. Without a final drain
    the fill stays buffered, the order count in the result is short by
    one, and the strategy never sees the event.
    """
    broker = _StubIbkrBroker()
    engine = LiveEngine(None, broker=broker)
    strategy = _OneEntryStrategy()

    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]
    fill_ts_ms = int(datetime(2026, 5, 4, 14, 32, tzinfo=UTC).timestamp() * 1000)

    async def _push_fill_after_last_bar() -> AsyncIterator[IbkrMinuteBar]:
        for bar in bars:
            yield bar
        # Source is now exhausted from the engine's perspective; queue
        # the fill that must still be drained before run() returns.
        broker.push_fill(order_id=100, fill_quantity=200.0, price=500.50, ts_ms=fill_ts_ms)

    result = await engine.run(strategy, ibkr_bars=_push_fill_after_last_bar())

    assert len(result.order_events) == 1
    assert result.order_events[0].fill_quantity == 200
    assert result.order_events[0].fill_price == Decimal("500.50")
    assert strategy.events == result.order_events
    assert result.open_positions == {"SPY": 200}


async def _gated_ibkr_bars(
    bars: list[IbkrMinuteBar],
    broker: _StubIbkrBroker,
    *,
    on_index: int,
    fill_kwargs: dict,
) -> AsyncIterator[IbkrMinuteBar]:
    """Yield bars, queueing a broker fill before the bar at ``on_index``.

    Mimics the real-broker timing: the order is placed during one bar
    and IBKR reports the fill before the next bar's drain. The drain
    runs at the start of each bar, so queueing right before yielding
    bar N puts the fill in front of bar N's drain step.
    """
    for idx, bar in enumerate(bars):
        if idx == on_index:
            broker.push_fill(**fill_kwargs)
        yield bar
