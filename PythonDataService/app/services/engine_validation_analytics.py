"""Engine Lab behavioral and historical validation analytics.

Formula: horizon return = E_end / E_start - 1; bucket expectancy =
  arithmetic mean of closed-trade returns; bucket win rate = wins / trades;
  calendar-month return = product(1 + trade_return_i) - 1; rolling stability
  applies the same expectancy and win-rate formulas to the trailing N trades.
Reference: Pardo, *The Evaluation and Optimization of Trading Strategies*
  (2e), chapter 4 (trade performance ratios); Bacon, *Practical Portfolio
  Performance Measurement* (2e), chapter 2 (period returns).
Canonical implementation: this file.
Validated against:
  PythonDataService/tests/services/test_engine_validation_analytics.py.

All temporal inputs and outputs are canonical int64 milliseconds UTC. ET is
used only as an in-function presentation bucket for weekday/hour seasonality.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from zoneinfo import ZoneInfo

from app.engine.results.statistics import compute_trade_statistics
from app.schemas.engine_validation import (
    EngineValidationAnalyticsResponse,
    PerformanceHorizonResponse,
    RollingTradePointResponse,
    SeasonalityMonthResponse,
    TimingCellResponse,
)

_ET = ZoneInfo("America/New_York")
_DAY_MS = 86_400_000
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri")
_MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_HORIZONS: tuple[tuple[str, str, int], ...] = (
    ("2w", "2 weeks", 14),
    ("1m", "1 month", 30),
    ("3m", "3 months", 90),
    ("6m", "6 months", 180),
    ("1y", "1 year", 365),
    ("2y", "2 years", 730),
)


@dataclass(frozen=True)
class ValidationTrade:
    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    pnl_pct: float

    @property
    def pnl_pts(self) -> float:
        return self.pnl_pct

    @property
    def result(self) -> str:
        return "WIN" if self.pnl_pct > 0 else "LOSS"


@dataclass(frozen=True)
class ValidationEquityPoint:
    timestamp_ms_utc: int
    equity: float


def compute_engine_validation_analytics(
    *,
    trades: Sequence[ValidationTrade],
    equity_curve: Sequence[ValidationEquityPoint],
    rolling_window: int = 20,
) -> EngineValidationAnalyticsResponse:
    """Compute display-ready validation analytics from one canonical run."""
    _validate_inputs(trades=trades, equity_curve=equity_curve, rolling_window=rolling_window)

    end_ms_utc = _resolve_end_ms(trades=trades, equity_curve=equity_curve)
    return EngineValidationAnalyticsResponse(
        horizons=_compute_horizons(trades, equity_curve, end_ms_utc),
        timing_cells=_compute_timing_cells(trades),
        seasonality=_compute_seasonality(trades),
        rolling_trade_stability=_compute_rolling_stability(trades, rolling_window),
    )


def _validate_inputs(
    *,
    trades: Sequence[ValidationTrade],
    equity_curve: Sequence[ValidationEquityPoint],
    rolling_window: int,
) -> None:
    if rolling_window < 1:
        raise ValueError("rolling_window must be positive")
    for index, trade in enumerate(trades):
        if trade.entry_ms_utc <= 0 or trade.exit_ms_utc <= trade.entry_ms_utc:
            raise ValueError(f"trade {index} has invalid canonical timestamps")
        if not math.isfinite(trade.pnl_pct):
            raise ValueError(f"trade {index} has non-finite pnl_pct")
    prior_timestamp = 0
    for index, point in enumerate(equity_curve):
        if point.timestamp_ms_utc <= prior_timestamp:
            raise ValueError(f"equity point {index} is not strictly increasing")
        if point.equity <= 0 or not math.isfinite(point.equity):
            raise ValueError(f"equity point {index} has invalid equity")
        prior_timestamp = point.timestamp_ms_utc


def _resolve_end_ms(
    *,
    trades: Sequence[ValidationTrade],
    equity_curve: Sequence[ValidationEquityPoint],
) -> int:
    if equity_curve:
        return equity_curve[-1].timestamp_ms_utc
    if trades:
        return max(trade.exit_ms_utc for trade in trades)
    return 0


def _compute_horizons(
    trades: Sequence[ValidationTrade],
    equity_curve: Sequence[ValidationEquityPoint],
    end_ms_utc: int,
) -> list[PerformanceHorizonResponse]:
    if end_ms_utc == 0:
        return []
    coverage_start = equity_curve[0].timestamp_ms_utc if equity_curve else end_ms_utc
    end_equity = equity_curve[-1].equity if equity_curve else None
    horizons: list[PerformanceHorizonResponse] = []
    for key, label, days in _HORIZONS:
        start_ms_utc = end_ms_utc - days * _DAY_MS
        has_full_coverage = bool(equity_curve) and coverage_start <= start_ms_utc
        start_equity = _equity_at_or_before(equity_curve, start_ms_utc) if has_full_coverage else None
        net_return = None
        if start_equity is not None and end_equity is not None:
            net_return = end_equity / start_equity - 1.0

        window_trades = [trade for trade in trades if start_ms_utc <= trade.exit_ms_utc <= end_ms_utc]
        trade_stats = compute_trade_statistics(window_trades) if window_trades else None
        profit_factor = None
        if trade_stats is not None and math.isfinite(trade_stats.profit_factor):
            profit_factor = trade_stats.profit_factor
        horizons.append(
            PerformanceHorizonResponse(
                key=key,
                label=label,
                start_ms_utc=start_ms_utc,
                end_ms_utc=end_ms_utc,
                has_full_coverage=has_full_coverage,
                net_return=net_return,
                trade_count=len(window_trades),
                win_rate=trade_stats.win_rate if trade_stats is not None else None,
                profit_factor=profit_factor,
            )
        )
    return horizons


def _equity_at_or_before(
    equity_curve: Sequence[ValidationEquityPoint],
    timestamp_ms_utc: int,
) -> float | None:
    value: float | None = None
    for point in equity_curve:
        if point.timestamp_ms_utc > timestamp_ms_utc:
            break
        value = point.equity
    return value


def _compute_timing_cells(trades: Sequence[ValidationTrade]) -> list[TimingCellResponse]:
    buckets: dict[tuple[int, int], list[ValidationTrade]] = defaultdict(list)
    for trade in trades:
        entry_et = datetime.fromtimestamp(trade.entry_ms_utc / 1000, tz=UTC).astimezone(_ET)
        if entry_et.weekday() <= 4:
            buckets[(entry_et.weekday(), entry_et.hour)].append(trade)

    cells: list[TimingCellResponse] = []
    for (weekday, hour_et), bucket_trades in sorted(buckets.items()):
        stats = compute_trade_statistics(bucket_trades)
        cells.append(
            TimingCellResponse(
                weekday=weekday,
                weekday_label=_WEEKDAY_LABELS[weekday],
                hour_et=hour_et,
                trade_count=stats.total_trades,
                win_rate=stats.win_rate,
                average_return=stats.avg_trade_pct,
            )
        )
    return cells


def _compute_seasonality(trades: Sequence[ValidationTrade]) -> list[SeasonalityMonthResponse]:
    year_month_returns: dict[tuple[int, int], list[float]] = defaultdict(list)
    for trade in trades:
        exit_et = datetime.fromtimestamp(trade.exit_ms_utc / 1000, tz=UTC).astimezone(_ET)
        year_month_returns[(exit_et.year, exit_et.month)].append(trade.pnl_pct)

    by_month: dict[int, list[float]] = defaultdict(list)
    for (_, month), returns in year_month_returns.items():
        compounded = math.prod(1.0 + value for value in returns) - 1.0
        by_month[month].append(compounded)

    return [
        SeasonalityMonthResponse(
            month=month,
            month_label=_MONTH_LABELS[month - 1],
            observation_count=len(by_month[month]),
            median_compounded_return=median(by_month[month]) if by_month[month] else None,
        )
        for month in range(1, 13)
    ]


def _compute_rolling_stability(
    trades: Sequence[ValidationTrade],
    rolling_window: int,
) -> list[RollingTradePointResponse]:
    points: list[RollingTradePointResponse] = []
    for end_index in range(rolling_window, len(trades) + 1):
        window = trades[end_index - rolling_window : end_index]
        stats = compute_trade_statistics(window)
        points.append(
            RollingTradePointResponse(
                trade_number=trades[end_index - 1].trade_number,
                end_ms_utc=trades[end_index - 1].exit_ms_utc,
                window_size=rolling_window,
                average_return=stats.avg_trade_pct,
                win_rate=stats.win_rate,
            )
        )
    return points

