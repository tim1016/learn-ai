from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import Direction, FillMode
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentValidationAlgorithm,
    DeploymentValidationConsecutiveGreen,
)

NY = ZoneInfo("America/New_York")


class _StaticBarReader:
    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        yield from self._bars


def _bar(hour: int, minute: int, open_: str, close: str) -> TradeBar:
    start = datetime(2026, 1, 5, hour, minute, tzinfo=NY)
    o = Decimal(open_)
    c = Decimal(close)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=10_000,
    )


def _run(bars: list[TradeBar]) -> tuple[DeploymentValidationConsecutiveGreen, list]:
    strategy = DeploymentValidationConsecutiveGreen()
    engine = BacktestEngine(
        data_source=_StaticBarReader(bars),
        fill_model=FillModel(
            mode=FillMode.NEXT_BAR_OPEN,
            commission_per_order=Decimal("0"),
            slippage_per_share=Decimal("0"),
        ),
    )
    result = engine.run(strategy)
    return strategy, result.order_events


def test_two_green_bars_from_0945_enter_next_bar_open_and_exit_cycle() -> None:
    bars = [
        _bar(9, 43, "100", "101"),  # green but before 09:45; ignored
        _bar(9, 44, "101", "102"),  # first eligible green, ends 09:45
        _bar(9, 45, "102", "103"),  # second eligible green, queues entry
        _bar(9, 46, "104", "104.5"),  # entry fill at this bar's open
        _bar(9, 47, "105", "105.5"),
        _bar(9, 48, "106", "106.5"),  # fifth bar; queues liquidate
        _bar(9, 49, "107", "107.5"),  # exit fill at next-bar open
        _bar(9, 50, "108", "108.5"),
    ]

    strategy, events = _run(bars)

    assert len(events) == 2
    assert events[0].direction is Direction.LONG
    assert events[0].time == datetime(2026, 1, 5, 9, 46, tzinfo=NY)
    assert events[0].fill_price == Decimal("104")
    assert events[1].direction is Direction.SHORT
    assert events[1].time == datetime(2026, 1, 5, 9, 49, tzinfo=NY)
    assert len(strategy.trade_log) == 1
    assert strategy.trade_log[0].signal_reason == "two_consecutive_green_minute_bars"


def test_red_bar_resets_green_detection() -> None:
    bars = [
        _bar(9, 44, "100", "101"),
        _bar(9, 45, "101", "100"),  # red resets
        _bar(9, 46, "100", "101"),
        _bar(9, 47, "101", "100"),  # red resets again
        _bar(9, 48, "100", "101"),
        _bar(9, 49, "101", "100"),
    ]

    _strategy, events = _run(bars)

    assert events == []


def test_detector_resets_after_exit_and_allows_many_trades_per_day() -> None:
    bars = [
        _bar(9, 44, "100", "101"),
        _bar(9, 45, "101", "102"),
        _bar(9, 46, "103", "103.5"),
        _bar(9, 47, "104", "104.5"),
        _bar(9, 48, "105", "105.5"),
        _bar(9, 49, "106", "106.5"),
        # Fresh pattern after the first exit fill.
        _bar(9, 50, "107", "108"),
        _bar(9, 51, "108", "109"),
        _bar(9, 52, "110", "110.5"),
        _bar(9, 53, "111", "111.5"),
        _bar(9, 54, "112", "112.5"),
        _bar(9, 55, "113", "113.5"),
    ]

    strategy, events = _run(bars)

    assert [e.direction for e in events] == [Direction.LONG, Direction.SHORT, Direction.LONG, Direction.SHORT]
    assert len(strategy.trade_log) == 2


def test_stops_detecting_and_flattens_at_1545() -> None:
    bars = [
        _bar(15, 44, "100", "101"),  # ends at 15:45, barrier fires before detection
        _bar(15, 45, "101", "102"),
        _bar(15, 46, "102", "103"),
        _bar(15, 47, "103", "104"),
    ]

    _strategy, events = _run(bars)

    assert events == []


def test_live_start_convention_alias_resolves_strategy_class() -> None:
    assert DeploymentValidationAlgorithm is DeploymentValidationConsecutiveGreen


def test_exposes_consolidator_period_for_indicator_hydration() -> None:
    # Regression: indicator_state.hydrate() reads ``strategy.CONSOLIDATOR_PERIOD_MIN``
    # unconditionally during live-run startup. The class previously omitted it,
    # so every live run of this strategy crashed with AttributeError before any
    # bar was processed (exit_code=3). The period must match the 1-minute
    # consolidator registered in initialize().
    strategy = DeploymentValidationConsecutiveGreen()

    assert strategy.CONSOLIDATOR_PERIOD_MIN == 1


def test_is_not_warm_startable() -> None:
    # deployment_validation reports no persistable state, so it is NOT
    # warm-startable. The live hydration ladder must treat hydrate_policy=require
    # as vacuous for it (no exit 4) — otherwise the canary can never start,
    # because a stateless strategy never writes the sidecar `require` demands.
    # Regression for the "zero clean sessions" blocker.
    assert DeploymentValidationConsecutiveGreen().is_warm_startable() is False


def test_spy_ema_remains_warm_startable() -> None:
    # Contrast: a strategy that overrides report_state_for_persistence IS
    # warm-startable, so `require` is still enforced for it (no regression to
    # the seed-day guarantee). Derived purely from the persistence-contract
    # override, so the two signals can never drift.
    from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm

    assert SpyEmaCrossoverAlgorithm().is_warm_startable() is True


def test_satisfies_live_persistence_contract() -> None:
    # Regression: the live engine's hydration ladder + shutdown checkpoint call
    # report_state_for_persistence / validate_state_payload /
    # restore_state_from_persistence on every strategy. This indicator-less
    # strategy defined none, so the shutdown checkpoint crashed mid-run with
    # AttributeError ('...has no attribute report_state_for_persistence').
    # Base-class defaults now satisfy the contract: no persistable state.
    strategy = DeploymentValidationConsecutiveGreen()

    # No warm-startable indicator state → cold start every session.
    assert strategy.report_state_for_persistence() is None

    # A strategy that models no state must refuse a foreign/stale payload.
    result = strategy.validate_state_payload({"anything": 1})
    assert result.failure_reason == "payload_mismatch"
    assert result.payload_shape_ok is False

    # restore is never reached on the happy path; reaching it is a bug.
    import pytest

    with pytest.raises(NotImplementedError):
        strategy.restore_state_from_persistence({})
