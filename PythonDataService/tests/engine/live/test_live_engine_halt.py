"""Tests for LiveEngine § 7 fatal-halt wiring (Phase C-2c-b2-ii).

Drives the engine with a small async-iterable bar source and a fake
``IbkrEventAdapter``-shaped broker that lets us inject synthetic
``IbkrOrderEvent`` rows directly into the buffer. That gives us a
deterministic way to:

  - inject a foreign fill (no Python order) → outside-mutation halt
  - delay a Python-owned fill past the configured window → lost-fill
    halt
  - check that the writers flush and ``poisoned.flag`` lands before
    the FatalHaltError propagates

The fake broker satisfies the BrokerAdapter, IbkrEventAdapter, and
ReplayBrokerAdapter Protocols enough for the engine's run() loop.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrOrderAck,
    IbkrOrderEvent,
    IbkrOrderSpec,
    IbkrPositionsSnapshot,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.halt import (
    POISONED_FLAG_FILENAME,
    FatalHaltError,
    PoisonedHaltTrigger,
)
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy

# ──────────────────────────── Fake event broker ──────────────────────


class _FakeIbkrEventBroker:
    """Minimal stand-in for IbkrBrokerAdapter that backs both the
    place_order ack flow and the start_event_stream / drain pattern.

    Lets tests inject IbkrOrderEvent rows directly into the buffer
    via ``inject_event`` so the engine sees them on the next
    drain — no real ib_async / network involved.
    """

    def __init__(self) -> None:
        self._buffer: list[IbkrOrderEvent] = []
        self._next_order_id = 100
        self.cancel_calls: int = 0
        self.placed_specs: list[IbkrOrderSpec] = []
        self._stream_failure: BaseException | None = None
        self._started = False

    @property
    def stream_failure(self) -> BaseException | None:
        return self._stream_failure

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return IbkrAccountSummary(
            account_id="DU123",
            is_paper=True,
            cash_balance=100000.0,
            net_liquidation=100000.0,
            fetched_at_ms=1,
        )

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return IbkrPositionsSnapshot(
            account_id="DU123", is_paper=True, positions=[], fetched_at_ms=1,
        )

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        order_id = self._next_order_id
        self._next_order_id += 1
        self.placed_specs.append(spec)
        return IbkrOrderAck(
            account_id="DU123",
            is_paper=True,
            order_id=order_id,
            client_id=42,
            con_id=756733,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )

    async def cancel_open_orders(self) -> list[int]:
        self.cancel_calls += 1
        return []

    async def start_event_stream(self) -> None:
        self._started = True

    async def stop_event_stream(self) -> None:
        self._started = False

    def drain_broker_events(self) -> list[IbkrOrderEvent]:
        events = list(self._buffer)
        self._buffer.clear()
        return events

    def inject_event(self, event: IbkrOrderEvent) -> None:
        """Test-only helper: queue an event for the next drain."""
        self._buffer.append(event)


# ──────────────────────────── Helpers ────────────────────────────────


def _bar(minute: int, close: str = "500") -> TradeBar:
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


async def _iter_bars(bars, *, per_bar_sleep: float = 0.0):
    """Yield each bar with an optional real sleep so wall-clock-based
    halt checks (lost-fill) see measurable elapsed time even in fast
    test runs."""
    for b in bars:
        yield b
        if per_bar_sleep > 0:
            await asyncio.sleep(per_bar_sleep)
        else:
            # Yield control so background event-stream task can fire.
            await asyncio.sleep(0)


class _NoOpStrategy(Strategy):
    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        pass


# ──────────────────────────── Outside mutation ───────────────────────


@pytest.mark.asyncio
async def test_fatal_halt_on_foreign_execution(tmp_path: Path) -> None:
    """A foreign IbkrOrderEvent (no matching _order_meta) trips the
    outside-mutation halt; the engine writes poisoned.flag and raises
    FatalHaltError."""
    broker = _FakeIbkrEventBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),  # disable force-flat for this test
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    # Inject a fill for an order_id we never placed — simulates a
    # manual TWS click on the same DU account.
    foreign_fill = IbkrOrderEvent(
        account_id="DU123",
        order_id=999_999,  # not in engine._order_meta
        event_type="fill",
        status="Filled",
        exec_id="exec-foreign-1",
        client_id=0,
        fill_quantity=10.0,
        avg_fill_price=500.0,
        last_fill_price=500.0,
        cumulative_filled=10.0,
        remaining=0.0,
        ts_ms=1,
    )
    broker.inject_event(foreign_fill)

    bars = [_bar(m) for m in range(30, 50)]
    with pytest.raises(FatalHaltError) as exc_info:
        await engine.run(_NoOpStrategy(), _iter_bars(bars))

    assert exc_info.value.reason.trigger == PoisonedHaltTrigger.OUTSIDE_MUTATION
    assert exc_info.value.reason.details["exec_id"] == "exec-foreign-1"

    # poisoned.flag was written before the exception propagated.
    flag_path = tmp_path / POISONED_FLAG_FILENAME
    assert flag_path.exists()
    payload = json.loads(flag_path.read_text())
    assert payload["trigger"] == "outside_mutation"


@pytest.mark.asyncio
async def test_fatal_halt_writes_flag_even_when_cancel_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung broker.cancel_open_orders must NOT block poisoned.flag.

    The whole point of fatal-halt is to land the flag on disk so the
    operator knows the run is contaminated. Before the timeout fix,
    a broker stuck in cancel would block the await indefinitely and
    the flag would never get written. (CodeRabbit P1 from #194.)
    """
    import app.engine.live.live_engine as live_engine_module

    # Tiny timeout so the test doesn't actually wait 5s; the real
    # path uses FATAL_HALT_CANCEL_TIMEOUT_S.
    monkeypatch.setattr(live_engine_module, "FATAL_HALT_CANCEL_TIMEOUT_S", 0.1)

    class _HangingCancelBroker(_FakeIbkrEventBroker):
        async def cancel_open_orders(self) -> list[int]:
            # Simulate a broker that accepts the cancel call but
            # never returns — typical of a contaminated session
            # where the broker's TCP connection has wedged.
            await asyncio.sleep(60)  # well beyond the 0.1s timeout
            return []

    broker = _HangingCancelBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    foreign_fill = IbkrOrderEvent(
        account_id="DU123",
        order_id=999_999,
        event_type="fill",
        status="Filled",
        exec_id="exec-foreign-hanging",
        client_id=0,
        fill_quantity=10.0,
        avg_fill_price=500.0,
        last_fill_price=500.0,
        cumulative_filled=10.0,
        remaining=0.0,
        ts_ms=1,
    )
    broker.inject_event(foreign_fill)

    bars = [_bar(m) for m in range(30, 50)]
    with pytest.raises(FatalHaltError):
        await engine.run(_NoOpStrategy(), _iter_bars(bars))

    # Flag landed despite the cancel hanging — the timeout did its job.
    assert (tmp_path / POISONED_FLAG_FILENAME).exists()


@pytest.mark.asyncio
async def test_no_halt_when_disabled_via_no_output_dir(tmp_path: Path) -> None:
    """Without an output_dir, halt detection is off — replay tests
    don't need IBKR-state safety. Foreign fills flow through but no
    poisoned.flag is written and no FatalHaltError fires."""
    broker = _FakeIbkrEventBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=broker,
        # No output_dir → halt disabled.
        account_id="DU123",
    )
    foreign_fill = IbkrOrderEvent(
        account_id="DU123",
        order_id=999,
        event_type="fill",
        status="Filled",
        exec_id="exec-foreign-1",
        client_id=0,
        fill_quantity=10.0,
        avg_fill_price=500.0,
        last_fill_price=500.0,
        cumulative_filled=10.0,
        remaining=0.0,
        ts_ms=1,
    )
    broker.inject_event(foreign_fill)

    bars = [_bar(m) for m in range(30, 50)]
    # No FatalHaltError.
    await engine.run(_NoOpStrategy(), _iter_bars(bars))
    assert not (tmp_path / POISONED_FLAG_FILENAME).exists()


# ──────────────────────────── Lost fill ──────────────────────────────


class _SubmitOnceStrategy(Strategy):
    def __init__(self) -> None:
        super().__init__()
        self._submitted = False

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if not self._submitted:
            self.ctx.portfolio.submit_market_order(
                "SPY", 100, self.ctx.current_time, tag="entry"
            )
            self._submitted = True


@pytest.mark.asyncio
async def test_fatal_halt_on_lost_fill(tmp_path: Path) -> None:
    """A Python-owned order with no matching execution past the fill
    window trips the lost-fill halt."""
    broker = _FakeIbkrEventBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        fill_window_ms=1,  # Tiny window — any order is "lost" by the next bar.
    )

    bars = [_bar(m) for m in range(30, 90)]
    # Per-bar 5ms sleep ensures wall-clock advances measurably so the
    # fill_window_ms=1 budget is exceeded by the bar after order
    # submission.
    with pytest.raises(FatalHaltError) as exc_info:
        await engine.run(_SubmitOnceStrategy(), _iter_bars(bars, per_bar_sleep=0.005))

    assert exc_info.value.reason.trigger == PoisonedHaltTrigger.LOST_FILL
    assert exc_info.value.reason.details["client_order_id"].startswith("live-")
    assert (tmp_path / POISONED_FLAG_FILENAME).exists()


# ──────────────────────────── Cleanup invariants ─────────────────────


@pytest.mark.asyncio
async def test_writers_flush_before_poisoned_flag_write(tmp_path: Path) -> None:
    """A halt mid-run must leave on-disk parquets consistent with the
    poisoned.flag — partial writes flushed BEFORE the flag lands so
    an operator-side post-mortem sees both."""
    broker = _FakeIbkrEventBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    # Strategy publishes a decision snapshot (so the writer has rows
    # to flush) before the foreign fill triggers the halt.
    from app.engine.strategy.base import DecisionSnapshot

    class _SnapshotStrategy(Strategy):
        def initialize(self) -> None:
            assert self.ctx is not None
            self.ctx.add_equity("SPY")
            self.ctx.register_consolidator("SPY", timedelta(minutes=15), self._on_bar)

        def _on_bar(self, bar: TradeBar) -> None:
            self.last_decision_snapshot = DecisionSnapshot(
                bar_close_ms=int(bar.end_time.timestamp() * 1000),
                ema5=float(bar.close),
                ema10=float(bar.close),
                rsi=60.0,
                signal="HOLD",
                intended_price=float(bar.close),
            )

    # Foreign fill arrives after a few bars so a snapshot is buffered first.
    bars_pre = [_bar(m) for m in range(30, 50)]
    foreign_fill = IbkrOrderEvent(
        account_id="DU123",
        order_id=999,
        event_type="fill",
        status="Filled",
        exec_id="exec-foreign-final",
        client_id=0,
        fill_quantity=10.0,
        avg_fill_price=500.0,
        last_fill_price=500.0,
        cumulative_filled=10.0,
        remaining=0.0,
        ts_ms=1,
    )

    async def _bars_with_late_inject():
        for b in bars_pre[:18]:
            yield b
            await asyncio.sleep(0)
        broker.inject_event(foreign_fill)
        for b in bars_pre[18:]:
            yield b
            await asyncio.sleep(0)

    with pytest.raises(FatalHaltError):
        await engine.run(_SnapshotStrategy(), _bars_with_late_inject())

    # Both artifacts present — decisions parquet from the buffered
    # snapshots, poisoned.flag from the halt.
    decisions_path = tmp_path / "decisions.parquet"
    flag_path = tmp_path / POISONED_FLAG_FILENAME
    assert decisions_path.exists(), "writers must flush before flag write"
    assert flag_path.exists()
    df = pd.read_parquet(decisions_path)
    assert len(df) >= 1
