from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from app.services.engine_validation_analytics import (
    ValidationEquityPoint,
    ValidationTrade,
    build_compatibility_equity_curve,
    compute_engine_validation_analytics,
)


def _ms(year: int, month: int, day: int, hour: int = 15) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def _trade(number: int, entry_ms: int, exit_ms: int, pnl_pct: float) -> ValidationTrade:
    return ValidationTrade(
        trade_number=number,
        entry_ms_utc=entry_ms,
        exit_ms_utc=exit_ms,
        pnl_pct=pnl_pct,
    )


def test_validation_analytics_computes_horizons_timing_and_seasonality() -> None:
    trades = [
        _trade(1, _ms(2025, 1, 6, 15), _ms(2025, 1, 6, 20), 0.10),
        _trade(2, _ms(2025, 1, 7, 18), _ms(2025, 1, 7, 20), -0.05),
        _trade(3, _ms(2026, 1, 5, 15), _ms(2026, 1, 5, 20), 0.02),
        _trade(4, _ms(2026, 7, 10, 19), _ms(2026, 7, 10, 20), 0.03),
    ]
    equity = [
        ValidationEquityPoint(_ms(2024, 7, 10), 100_000.0),
        ValidationEquityPoint(_ms(2025, 7, 10), 110_000.0),
        ValidationEquityPoint(_ms(2026, 7, 10, 20), 121_000.0),
    ]

    result = compute_engine_validation_analytics(trades=trades, equity_curve=equity, rolling_window=2)

    one_year = next(item for item in result.horizons if item.key == "1y")
    assert one_year.has_full_coverage is True
    assert one_year.net_return == pytest.approx(0.10, abs=1e-12)
    assert one_year.trade_count == 2
    assert one_year.win_rate == pytest.approx(1.0, abs=1e-12)

    monday_open = next(cell for cell in result.timing_cells if cell.weekday == 0 and cell.hour_et == 10)
    assert monday_open.trade_count == 2
    assert monday_open.average_return == pytest.approx(0.06, abs=1e-12)

    january = result.seasonality[0]
    assert january.observation_count == 2
    assert january.median_compounded_return == pytest.approx(0.0325, abs=1e-12)
    assert len(result.rolling_trade_stability) == 3


def test_validation_analytics_marks_uncovered_horizons_without_inventing_returns() -> None:
    end = _ms(2026, 7, 10, 20)
    result = compute_engine_validation_analytics(
        trades=[],
        equity_curve=[
            ValidationEquityPoint(end - 10 * 86_400_000, 100_000.0),
            ValidationEquityPoint(end, 101_000.0),
        ],
    )

    assert all(item.has_full_coverage is False for item in result.horizons)
    assert all(item.net_return is None for item in result.horizons)


def test_validation_analytics_rejects_non_monotonic_equity() -> None:
    timestamp = _ms(2026, 7, 10)

    with pytest.raises(ValueError, match="strictly increasing"):
        compute_engine_validation_analytics(
            trades=[],
            equity_curve=[
                ValidationEquityPoint(timestamp, 100_000.0),
                ValidationEquityPoint(timestamp, 101_000.0),
            ],
        )


def test_compatibility_equity_curve_compounds_common_trade_returns_and_pins_endpoints() -> None:
    start = _ms(2026, 1, 5, 0)
    end = _ms(2026, 1, 10, 0)
    trades = [
        _trade(1, _ms(2026, 1, 5), _ms(2026, 1, 6), 0.10),
        _trade(2, _ms(2026, 1, 7), _ms(2026, 1, 8), -0.05),
    ]

    curve = build_compatibility_equity_curve(
        trades,
        start_ms_utc=start,
        end_ms_utc=end,
        initial_equity=100_000,
    )

    assert [point.timestamp_ms_utc for point in curve] == [
        start,
        _ms(2026, 1, 6),
        _ms(2026, 1, 8),
        end,
    ]
    assert curve[-1].equity == pytest.approx(104_500, abs=1e-9)


def test_validation_analytics_matches_pandas_rolling_sharpe_and_flags_pnl_divergence() -> None:
    returns = [
        0.010,
        0.012,
        0.009,
        0.011,
        0.010,
        0.030,
        -0.008,
        0.018,
        -0.004,
        0.014,
        -0.002,
        0.006,
    ]
    equity = 100_000.0
    points = [ValidationEquityPoint(_ms(2026, 1, 2, 21), equity)]
    for offset, daily_return in enumerate(returns, start=1):
        equity *= 1.0 + daily_return
        points.append(ValidationEquityPoint(_ms(2026, 1, 2 + offset, 21), equity))

    result = compute_engine_validation_analytics(
        trades=[],
        equity_curve=points,
        sharpe_window_sessions=5,
        divergence_trend_sessions=4,
    )

    study = result.sharpe_pnl_divergence
    assert study is not None
    equity_returns = [
        points[index].equity / points[index - 1].equity - 1.0
        for index in range(1, len(points))
    ]
    expected = (
        pd.Series(equity_returns, dtype="float64")
        .rolling(window=5)
        .mean()
        .div(pd.Series(equity_returns, dtype="float64").rolling(window=5).std(ddof=1))
        .mul(252**0.5)
        .dropna()
        .tolist()
    )
    observed = [
        point.rolling_sharpe
        for point in study.points
        if point.rolling_sharpe is not None
    ]
    assert observed == pytest.approx(expected, abs=1e-12, rel=0)
    assert study.return_window_sessions == 5
    assert study.trend_window_sessions == 4
    assert study.daily_observation_count == len(points)
    assert study.eligible_observation_count > 0
    assert study.divergence_observation_count > 0
    assert study.points[-1].is_divergence is True
    assert study.currently_diverging is True
    assert study.divergence_ratio == pytest.approx(
        study.divergence_observation_count / study.eligible_observation_count,
        abs=1e-12,
        rel=0,
    )


def test_validation_analytics_uses_last_equity_observation_per_new_york_session() -> None:
    result = compute_engine_validation_analytics(
        trades=[],
        equity_curve=[
            ValidationEquityPoint(_ms(2026, 1, 2, 15), 100_000.0),
            ValidationEquityPoint(_ms(2026, 1, 2, 21), 101_000.0),
            ValidationEquityPoint(_ms(2026, 1, 3, 15), 100_500.0),
            ValidationEquityPoint(_ms(2026, 1, 3, 21), 102_000.0),
        ],
        sharpe_window_sessions=2,
        divergence_trend_sessions=2,
    )

    study = result.sharpe_pnl_divergence
    assert study is not None
    assert study.daily_observation_count == 2
    assert [point.cumulative_pnl for point in study.points] == pytest.approx(
        [0.0, 1_000.0],
        abs=1e-9,
        rel=0,
    )
    assert study.latest_rolling_sharpe is None
    assert study.currently_diverging is None


def test_validation_analytics_can_use_native_performance_curve_without_changing_compatibility_horizons() -> None:
    start = _ms(2026, 1, 2, 21)
    end = _ms(2026, 1, 6, 21)
    compatibility_curve = [
        ValidationEquityPoint(start, 100_000.0),
        ValidationEquityPoint(end, 105_000.0),
    ]
    native_curve = [
        ValidationEquityPoint(start, 100_000.0),
        ValidationEquityPoint(_ms(2026, 1, 3, 21), 101_000.0),
        ValidationEquityPoint(_ms(2026, 1, 4, 21), 100_500.0),
        ValidationEquityPoint(_ms(2026, 1, 5, 21), 102_000.0),
        ValidationEquityPoint(end, 103_000.0),
    ]

    result = compute_engine_validation_analytics(
        trades=[],
        equity_curve=compatibility_curve,
        performance_equity_curve=native_curve,
        sharpe_window_sessions=3,
        divergence_trend_sessions=2,
    )

    study = result.sharpe_pnl_divergence
    assert study is not None
    assert study.daily_observation_count == 5
    assert study.points[-1].cumulative_pnl == pytest.approx(3_000.0, abs=1e-9, rel=0)
