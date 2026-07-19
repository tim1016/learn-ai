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
    SignalSymbolExecutor,
)
from app.engine.live.action_plan_signal_executor import StockActionPlanSignalExecutor
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.live.run import (
    _signal_intent_executor_for_live_start,
    _strategy_param_resolution_from_live_config,
)
from app.engine.strategy.algorithms.ema_crossover_signal import EmaCrossoverSignalAlgorithm
from app.engine.strategy.base import Strategy, StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
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


def test_live_binding_keeps_signal_symbol_outside_strategy_trade_params() -> None:
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

    resolution = _strategy_param_resolution_from_live_config(
        _STRATEGY_REGISTRY["ema_crossover_signal"],
        LiveConfig(symbol="SPY"),
        {"action": action_plan},
    )

    assert resolution.kwargs == {"symbol": "SPY"}
    assert resolution.effective_trade_symbol == "NVDA"


def test_legacy_ema_binding_executes_intents_on_its_signal_symbol() -> None:
    executor = _signal_intent_executor_for_live_start(
        _STRATEGY_REGISTRY["spy_ema_crossover"],
        LiveConfig(symbol="SPY"),
        {},
    )
    context = _RecordingExecutionContext()

    assert isinstance(executor, SignalSymbolExecutor)
    executor.execute(
        context,
        SignalIntent(SignalIntentKind.ENTER, bar_close_ms=1, intended_price=Decimal("500")),
    )

    assert context.calls == [("set_holdings", "SPY", Decimal(1))]


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
