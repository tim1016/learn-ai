from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.engine_validation_analytics import (
    ValidationEquityPoint,
    ValidationTrade,
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
