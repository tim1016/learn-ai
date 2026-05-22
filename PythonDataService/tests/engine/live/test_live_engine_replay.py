"""Replay parity gate for the IBKR paper live runtime."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine, BacktestResult, EquitySnapshot
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode, OrderEvent
from app.engine.framework.insight import Insight
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine, LiveRunResult
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from app.engine.strategy.base import LoggedTrade
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars

LEAN_CACHE_ROOT = Path(__file__).resolve().parents[3] / "lean-cache"


def _ms_utc(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _assert_order_events_exact(backtest: list[OrderEvent], live: list[OrderEvent]) -> None:
    assert len(live) == len(backtest)
    for idx, (expected, actual) in enumerate(zip(backtest, live, strict=True), start=1):
        assert actual.symbol == expected.symbol, f"order {idx} symbol"
        assert actual.direction == expected.direction, f"order {idx} direction"
        assert actual.fill_quantity == expected.fill_quantity, f"order {idx} quantity"
        assert actual.fill_price - expected.fill_price == Decimal("0"), f"order {idx} fill_price"
        assert actual.fee - expected.fee == Decimal("0"), f"order {idx} fee"
        assert actual.tag == expected.tag, f"order {idx} tag"
        assert abs(_ms_utc(actual.time) - _ms_utc(expected.time)) <= 1, f"order {idx} fill time"


def _assert_equity_curve_exact(backtest: list[EquitySnapshot], live: list[EquitySnapshot]) -> None:
    assert len(live) == len(backtest)
    for idx, (expected, actual) in enumerate(zip(backtest, live, strict=True), start=1):
        assert _ms_utc(actual.timestamp) == _ms_utc(expected.timestamp), f"equity {idx} timestamp"
        assert actual.equity - expected.equity == Decimal("0"), f"equity {idx} equity"
        assert actual.cash - expected.cash == Decimal("0"), f"equity {idx} cash"
        assert actual.holdings_value - expected.holdings_value == Decimal("0"), f"equity {idx} holdings"


def _assert_trade_log_exact(backtest: list[LoggedTrade], live: list[LoggedTrade]) -> None:
    assert len(live) == len(backtest)
    for idx, (expected, actual) in enumerate(zip(backtest, live, strict=True), start=1):
        assert _ms_utc(actual.entry_time) == _ms_utc(expected.entry_time), f"trade {idx} entry_time"
        assert actual.entry_price - expected.entry_price == Decimal("0"), f"trade {idx} entry_price"
        assert _ms_utc(actual.exit_time) == _ms_utc(expected.exit_time), f"trade {idx} exit_time"
        assert actual.exit_price - expected.exit_price == Decimal("0"), f"trade {idx} exit_price"
        assert actual.pnl_pts - expected.pnl_pts == Decimal("0"), f"trade {idx} pnl_pts"
        assert actual.pnl_pct - expected.pnl_pct == Decimal("0"), f"trade {idx} pnl_pct"
        assert actual.result == expected.result, f"trade {idx} result"
        assert actual.indicators == expected.indicators, f"trade {idx} indicators"


def _insight_signature(insight: Insight) -> tuple:
    return (
        insight.symbol,
        insight.type,
        insight.direction,
        insight.period,
        insight.magnitude,
        insight.confidence,
        insight.source_model,
        insight.tag,
        _ms_utc(insight.generated_time),
        _ms_utc(insight.close_time),
        insight.reference_value,
        insight.reference_value_final,
        insight.score.direction,
        insight.score.magnitude,
        insight.score.is_final_score,
        _ms_utc(insight.score.updated_time_utc) if insight.score.updated_time_utc else None,
    )


def _assert_insights_exact(backtest: Iterable[Insight], live: Iterable[Insight]) -> None:
    assert [_insight_signature(i) for i in live] == [_insight_signature(i) for i in backtest]


def _run_backtest() -> tuple[BacktestResult, SpyEmaCrossoverAlgorithm]:
    reader = LeanMinuteDataReader(LEAN_CACHE_ROOT)
    strategy = SpyEmaCrossoverAlgorithm()
    result = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(mode=FillMode.NEXT_BAR_OPEN),
    ).run(strategy)
    return result, strategy


async def _run_live_from_backtest_window(
    backtest_strategy: SpyEmaCrossoverAlgorithm,
) -> tuple[LiveRunResult, SpyEmaCrossoverAlgorithm]:
    assert backtest_strategy.start_date is not None
    assert backtest_strategy.end_date is not None
    reader = LeanMinuteDataReader(LEAN_CACHE_ROOT)
    bars = list(
        reader.iter_bars(
            "SPY",
            backtest_strategy.start_date.date(),
            backtest_strategy.end_date.date(),
        )
    )
    strategy = SpyEmaCrossoverAlgorithm()
    # BacktestEngine's ExecutionConfig defaults ``force_flat_at`` to None;
    # explicitly disable the live force-flat barrier so the parity gate
    # compares apples-to-apples. The barrier itself is exercised by
    # ``test_live_engine.py::test_live_engine_force_flat_*``.
    config = LiveConfig(force_flat_at=None)
    result = await LiveEngine(None, config, broker=FakeBroker()).run(strategy, iter_bars(bars))
    return result, strategy


@pytest.mark.asyncio
async def test_live_engine_replays_spy_next_bar_open_backtest_exactly() -> None:
    # The replay gate runs against the local Polygon-sourced LEAN cache
    # (`PythonDataService/lean-cache/`), which is gitignored runtime data and
    # not materialized on CI runners. Skip cleanly there so the test fails
    # fast locally if the cache is missing while leaving CI green.
    if not LEAN_CACHE_ROOT.exists():
        pytest.skip(f"local LEAN cache missing at {LEAN_CACHE_ROOT}; run locally to exercise the parity gate")
    backtest_result, backtest_strategy = _run_backtest()
    live_result, live_strategy = await _run_live_from_backtest_window(backtest_strategy)

    assert len(live_result.bars) == len(backtest_result.bars)
    # Count dropped from 162 → 120 when the LeanMinuteDataReader started
    # honoring ``data_policy.session`` (Bug B fix, 2026-05-21). Before the
    # fix the reader returned 04:00-20:00 ET bars unconditionally and the
    # strategy fired entries on extended-hours signal bars; now both the
    # backtest and the replay see RTH-only bars and the trade count agrees
    # at 120 on this window. The parity invariant (backtest == live) is
    # asserted by ``_assert_order_events_exact`` below regardless of the
    # absolute count.
    assert len(live_result.order_events) == 120
    assert live_result.initial_cash == backtest_result.initial_cash
    assert live_result.final_equity - backtest_result.final_equity == Decimal("0")
    assert live_result.total_fees - backtest_result.total_fees == Decimal("0")

    _assert_order_events_exact(backtest_result.order_events, live_result.order_events)
    _assert_equity_curve_exact(backtest_result.equity_curve, live_result.equity_curve)
    _assert_trade_log_exact(backtest_strategy.trade_log, live_strategy.trade_log)
    _assert_insights_exact(backtest_result.insights, live_result.insights)
    assert live_result.insight_summary == backtest_result.insight_summary

    assert live_result.submitted_order_ids == sorted(live_result.submitted_order_ids)
    assert len(set(live_result.submitted_order_ids)) == len(live_result.submitted_order_ids)
    assert live_result.open_positions == {}
    assert live_result.pending_orders == 0
    assert [event.tag == "ForceFlat" for event in live_result.order_events] == [
        event.tag == "ForceFlat" for event in backtest_result.order_events
    ]
