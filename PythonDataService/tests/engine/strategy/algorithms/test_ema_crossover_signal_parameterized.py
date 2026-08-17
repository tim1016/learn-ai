"""Regression coverage for parameterizing the EMA-crossover *signal* strategy.

The signal strategy historically hardcoded a 0.20 absolute gap and a 50–70
RSI band. Per the repo rule "any tunable that can be a parameter is one"
(Recency Chart), these become real parameters — with defaults preserved
exactly so the validated LEAN-parity behaviour at the default point is
unchanged (the golden-fixture parity suite is the regression gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.algorithms.ema_crossover_signal import (
    EmaCrossoverSignalAlgorithm,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind


@dataclass
class _ReadyIndicator:
    """Controlled indicator stub — bypasses real EMA/RSI math so a single
    bar's decision can be tested with hand-picked values."""

    current_value: Decimal
    is_ready: bool = True

    def update(self, _time: datetime, _value: Decimal) -> None:
        pass


@dataclass
class _RecordingSignalIntentExecutor:
    intents: list[SignalIntent] = field(default_factory=list)

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _entry_check_bar() -> TradeBar:
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


def _entry_check(*, gap: Decimal, rsi_min: Decimal, rsi_max: Decimal, ema_gap: Decimal, rsi: Decimal) -> list[SignalIntent]:
    """Drive one bar through the real entry-check body with hand-picked
    EMA/RSI values, bypassing indicator warmup entirely."""
    strategy = EmaCrossoverSignalAlgorithm(symbol="SPY", gap=gap, rsi_min=rsi_min, rsi_max=rsi_max)
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()

    strategy._ema10 = _ReadyIndicator(Decimal("100.00"))  # type: ignore[assignment]
    strategy._ema5 = _ReadyIndicator(Decimal("100.00") + ema_gap)  # type: ignore[assignment]
    strategy._rsi14 = _ReadyIndicator(rsi)  # type: ignore[assignment]
    strategy._prev_ema5_above_ema10 = False

    executor = _RecordingSignalIntentExecutor()
    context.set_signal_intent_executor(executor)

    strategy._on_fifteen_minute_bar(_entry_check_bar())
    return executor.intents


def test_gap_threshold_defaults_to_020_and_is_configurable() -> None:
    default = EmaCrossoverSignalAlgorithm()
    # 0.30 gap clears the default 0.20 threshold.
    assert default._gap_is_sufficient(Decimal("100.30"), Decimal("100.00")) is True

    tighter = EmaCrossoverSignalAlgorithm(gap=Decimal("0.50"))
    # Same 0.30 gap no longer clears a 0.50 threshold.
    assert tighter._gap_is_sufficient(Decimal("100.30"), Decimal("100.00")) is False


def test_rsi_band_defaults_to_50_70_and_is_configurable() -> None:
    assert EmaCrossoverSignalAlgorithm()._rsi_gate_bounds() == (Decimal(50), Decimal(70))

    custom = EmaCrossoverSignalAlgorithm(rsi_min=Decimal(55), rsi_max=Decimal(65))
    assert custom._rsi_gate_bounds() == (Decimal(55), Decimal(65))


def test_registration_exposes_gap_and_rsi_with_preserved_defaults() -> None:
    schema = _STRATEGY_REGISTRY["ema_crossover_signal"].param_schema
    params = schema()
    assert params.gap == 0.20
    assert params.rsi_min == 50.0
    assert params.rsi_max == 70.0


def test_registration_rejects_inverted_rsi_band() -> None:
    schema = _STRATEGY_REGISTRY["ema_crossover_signal"].param_schema
    with pytest.raises(ValidationError):
        schema(rsi_min=70, rsi_max=50)


def test_build_threads_configured_gates_into_algorithm() -> None:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    algorithm = registration.build(
        registration.param_schema(gap=0.5, rsi_min=55, rsi_max=65)
    )
    assert algorithm._gap_is_sufficient(Decimal("100.30"), Decimal("100.00")) is False
    assert algorithm._rsi_gate_bounds() == (Decimal(55), Decimal(65))


def test_entry_check_honors_a_configured_gap_the_default_would_reject() -> None:
    """A gap below the hardcoded 0.20 default must still enter when the
    strategy is configured with a looser threshold — proves the entry
    check reads self._gap_is_sufficient(), not a literal ``0.20``."""
    intents = _entry_check(
        gap=Decimal("0.01"),
        rsi_min=Decimal(50),
        rsi_max=Decimal(70),
        ema_gap=Decimal("0.05"),
        rsi=Decimal(60),
    )
    assert [i.kind for i in intents] == [SignalIntentKind.ENTER]


def test_entry_check_honors_a_configured_gap_tighter_than_the_default() -> None:
    """A gap that clears the hardcoded 0.20 default must NOT enter when the
    strategy is configured with a tighter threshold."""
    intents = _entry_check(
        gap=Decimal("0.50"),
        rsi_min=Decimal(50),
        rsi_max=Decimal(70),
        ema_gap=Decimal("0.30"),
        rsi=Decimal(60),
    )
    assert intents == []


def test_entry_check_honors_a_configured_rsi_band_the_default_would_reject() -> None:
    """An RSI outside the hardcoded 50-70 default must still enter when the
    strategy is configured with a wider band — proves the entry check reads
    self._rsi_gate_bounds(), not literal ``50``/``70``."""
    intents = _entry_check(
        gap=Decimal("0.20"),
        rsi_min=Decimal(30),
        rsi_max=Decimal(90),
        ema_gap=Decimal("0.30"),
        rsi=Decimal(80),
    )
    assert [i.kind for i in intents] == [SignalIntentKind.ENTER]
