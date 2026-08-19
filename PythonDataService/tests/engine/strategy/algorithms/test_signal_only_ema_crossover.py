"""Regression coverage for the EMA signal/execution migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order_sizer import FixedShares
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import (
    SignalIntentExecutionContext,
)
from app.engine.live.action_plan_signal_executor import StockActionPlanSignalExecutor
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.algorithms.ema_crossover_2_bps import EmaCrossover2BpsAlgorithm
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import Strategy, StrategyContext
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


@dataclass
class _ReadyIndicator:
    current_value: Decimal
    is_ready: bool = True

    def update(self, _time: datetime, _value: Decimal) -> None:
        """Keep a controlled ready value for a one-bar decision test."""


@dataclass
class _RecordingExecutionContext:
    calls: list[tuple[str, str, Decimal | float | None]] = field(default_factory=list)

    def set_holdings(self, symbol: str, fraction: Decimal | float) -> None:
        self.calls.append(("set_holdings", symbol, fraction))

    def liquidate(self, symbol: str) -> None:
        self.calls.append(("liquidate", symbol, None))


@dataclass
class _RecordingSignalIntentExecutor:
    intents: list[SignalIntent] = field(default_factory=list)

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


class _SingleEnterSignalStrategy(Strategy):
    """Small live-runtime probe for the policy-executor binding."""

    def __init__(self) -> None:
        super().__init__()
        self._emitted = False

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if self._emitted:
            return
        self._emitted = True
        self.ctx.emit_signal_intent(
            SignalIntent(
                kind=SignalIntentKind.ENTER,
                bar_close_ms=int(bar.end_time.timestamp() * 1000),
                intended_price=bar.close,
            )
        )


def _signal_bar() -> TradeBar:
    time = datetime(2026, 7, 17, 14, 45, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=time,
        end_time=time + timedelta(minutes=15),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


def test_ema_strategy_emits_an_asset_free_enter_intent() -> None:
    strategy = EmaCrossoverSignalAlgorithm(symbol="SPY")
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    strategy._ema5 = _ReadyIndicator(Decimal("500.50"))  # type: ignore[assignment]
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    strategy._prev_ema5_above_ema10 = False
    executor = _RecordingSignalIntentExecutor()
    context.set_signal_intent_executor(executor)

    bar = _signal_bar()
    context.current_time = bar.end_time
    strategy._on_fifteen_minute_bar(bar)

    assert executor.intents == [
        SignalIntent(
            kind=SignalIntentKind.ENTER,
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            intended_price=Decimal("500"),
        )
    ]
    assert not hasattr(executor.intents[0], "symbol")
    assert context.portfolio.pending_orders == []


def test_two_bps_variant_changes_only_the_gap_gate() -> None:
    """A 3 bps relative gap passes 2 bps but remains below the $0.20 control."""
    bar = _signal_bar()

    original = EmaCrossoverSignalAlgorithm(symbol="SPY")
    original_context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    original.ctx = original_context
    original.initialize()
    original._ema5 = _ReadyIndicator(Decimal("500.15"))  # type: ignore[assignment]
    original._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    original._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    original._prev_ema5_above_ema10 = False
    original_executor = _RecordingSignalIntentExecutor()
    original_context.set_signal_intent_executor(original_executor)
    original_context.current_time = bar.end_time

    two_bps = EmaCrossover2BpsAlgorithm(symbol="SPY")
    two_bps_context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    two_bps.ctx = two_bps_context
    two_bps.initialize()
    two_bps._ema5 = _ReadyIndicator(Decimal("500.15"))  # type: ignore[assignment]
    two_bps._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    two_bps._rsi14 = _ReadyIndicator(Decimal("60"))  # type: ignore[assignment]
    two_bps._prev_ema5_above_ema10 = False
    two_bps_executor = _RecordingSignalIntentExecutor()
    two_bps_context.set_signal_intent_executor(two_bps_executor)
    two_bps_context.current_time = bar.end_time

    original._on_fifteen_minute_bar(bar)
    two_bps._on_fifteen_minute_bar(bar)

    assert original_executor.intents == []
    assert [intent.kind for intent in two_bps_executor.intents] == [SignalIntentKind.ENTER]


@pytest.mark.parametrize(
    ("gap_bps", "rsi_min", "rsi_max", "ema_fast", "rsi", "should_enter"),
    [
        (2.0, 50.0, 70.0, "500.15", "60", True),
        (4.0, 50.0, 70.0, "500.15", "60", False),
        (2.0, 61.0, 70.0, "500.15", "60", False),
        (2.0, 50.0, 59.0, "500.15", "60", False),
        (2.0, 60.0, 70.0, "500.15", "60", True),
    ],
)
def test_two_bps_variant_applies_configured_gap_and_inclusive_rsi_gates(
    gap_bps: float,
    rsi_min: float,
    rsi_max: float,
    ema_fast: str,
    rsi: str,
    should_enter: bool,
) -> None:
    strategy = EmaCrossover2BpsAlgorithm(
        symbol="SPY",
        gap_bps=gap_bps,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
    )
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    strategy._ema5 = _ReadyIndicator(Decimal(ema_fast))  # type: ignore[assignment]
    strategy._ema10 = _ReadyIndicator(Decimal("500.00"))  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(Decimal(rsi))  # type: ignore[assignment]
    strategy._prev_ema5_above_ema10 = False
    executor = _RecordingSignalIntentExecutor()
    context.set_signal_intent_executor(executor)
    bar = _signal_bar()
    context.current_time = bar.end_time

    strategy._on_fifteen_minute_bar(bar)

    assert [intent.kind for intent in executor.intents] == (
        [SignalIntentKind.ENTER] if should_enter else []
    )


def test_stock_action_plan_executor_selects_the_trade_asset() -> None:
    action_plan = {
        "on_enter": [
            {
                "leg_id": "nvda_long",
                "instrument": {"kind": "stock", "underlying": "NVDA"},
                "position": "long",
                "qty_ratio": 1,
            }
        ],
        "on_exit": [{"kind": "close_leg", "entry_leg_id": "nvda_long"}],
    }
    executor = StockActionPlanSignalExecutor.from_action_plan(action_plan)
    context = _RecordingExecutionContext()

    executor.execute(
        context,
        SignalIntent(SignalIntentKind.ENTER, bar_close_ms=1, intended_price=Decimal("500")),
    )
    executor.execute(
        context,
        SignalIntent(SignalIntentKind.EXIT, bar_close_ms=2, intended_price=Decimal("501")),
    )

    assert context.calls == [
        ("set_holdings", "NVDA", Decimal(1)),
        ("liquidate", "NVDA", None),
    ]


def _live_probe_bars() -> list[TradeBar]:
    start = datetime(2026, 7, 17, 14, 30, tzinfo=UTC)
    return [
        TradeBar(
            symbol="SPY",
            time=start + timedelta(minutes=index),
            end_time=start + timedelta(minutes=index + 1),
            open=Decimal("500"),
            high=Decimal("500"),
            low=Decimal("500"),
            close=Decimal("500"),
            volume=100,
        )
        for index in range(4)
    ]


def _nvda_action_plan() -> dict[str, object]:
    return {
        "on_enter": [
            {
                "leg_id": "nvda_long",
                "instrument": {"kind": "stock", "underlying": "NVDA"},
                "position": "long",
                "qty_ratio": 1,
            }
        ],
        "on_exit": [{"kind": "close_leg", "entry_leg_id": "nvda_long"}],
    }


async def test_live_engine_routes_policy_signal_to_action_plan_stock() -> None:
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None, sizing=FixedShares(value=1)),
        broker=broker,
        signal_intent_executor=StockActionPlanSignalExecutor.from_action_plan(_nvda_action_plan()),
    )

    result = await engine.run(_SingleEnterSignalStrategy(), iter_bars(_live_probe_bars()))

    assert [order.symbol for order in broker.orders] == ["NVDA"]
    assert result.open_positions == {"NVDA": 1}


async def test_live_engine_rejects_signal_without_execution_policy() -> None:
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None, sizing=FixedShares(value=1)),
        broker=broker,
    )

    with pytest.raises(RuntimeError, match="SignalIntent requires a bound SignalIntentExecutor"):
        await engine.run(_SingleEnterSignalStrategy(), iter_bars(_live_probe_bars()))

    assert broker.orders == []
