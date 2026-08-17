"""Engine Lab behavioral and historical validation analytics.

Formula: horizon return = E_end / E_start - 1; bucket expectancy =
  arithmetic mean of closed-trade returns; bucket win rate = wins / trades;
  calendar-month return = product(1 + trade_return_i) - 1; rolling stability
  applies the same expectancy and win-rate formulas to the trailing N trades;
  rolling Sharpe = sqrt(252) * mean(r) / sample_std(r); cumulative P&L =
  E_t - E_0; divergence = slope(P&L) > 0 and slope(rolling Sharpe) < 0.
Reference: Pardo, *The Evaluation and Optimization of Trading Strategies*
  (2e), chapter 4 (trade performance ratios); Bacon, *Practical Portfolio
  Performance Measurement* (2e), chapter 2 (period returns); Sharpe (1994),
  *The Sharpe Ratio*; Lo (2002), *The Statistics of Sharpe Ratios*.
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
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.engine.results.statistics import compute_trade_statistics
from app.schemas.engine_validation import (
    EngineValidationAnalyticsResponse,
    PerformanceHorizonResponse,
    RollingTradePointResponse,
    SeasonalityMonthResponse,
    SharpePnlDivergencePointResponse,
    SharpePnlDivergenceResponse,
    TimingCellResponse,
)

_ET = ZoneInfo("America/New_York")
_DAY_MS = 86_400_000
_TRADING_SESSIONS_PER_YEAR = 252
_DEFAULT_SHARPE_WINDOW_SESSIONS = 20
_DEFAULT_DIVERGENCE_TREND_SESSIONS = 20
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


# Version stamp for the persisted ``ValidationAnalyticsJson`` envelope.
# Bump when the analytics shape changes so old rows stay interpretable
# (persisted analytics are frozen at run time, never recomputed).
VALIDATION_ANALYTICS_SCHEMA_VERSION = 2


def build_validation_analytics_envelope(
    analytics: EngineValidationAnalyticsResponse,
    *,
    engine: Literal["python", "lean"],
    computed_at_ms: int,
) -> dict[str, Any]:
    """Canonical persisted envelope for run validation analytics.

    Both persist paths (Python engine study save, LEAN sidecar persist)
    wrap their analytics through this single constructor so the
    ``ValidationAnalyticsJson`` column has exactly one shape. ``engine``
    is recorded because LEAN analytics derive from paired trades with
    synthetic-exit caveats — a reader must be able to tell.
    """
    return {
        "schema_version": VALIDATION_ANALYTICS_SCHEMA_VERSION,
        "computed_at_ms": computed_at_ms,
        "engine": engine,
        "analytics": analytics.model_dump(mode="json"),
    }


def compute_engine_validation_analytics(
    *,
    trades: Sequence[ValidationTrade],
    equity_curve: Sequence[ValidationEquityPoint],
    performance_equity_curve: Sequence[ValidationEquityPoint] | None = None,
    rolling_window: int = 20,
    sharpe_window_sessions: int = _DEFAULT_SHARPE_WINDOW_SESSIONS,
    divergence_trend_sessions: int = _DEFAULT_DIVERGENCE_TREND_SESSIONS,
) -> EngineValidationAnalyticsResponse:
    """Compute display-ready validation analytics from one canonical run."""
    _validate_inputs(trades=trades, equity_curve=equity_curve, rolling_window=rolling_window)
    native_equity = equity_curve if performance_equity_curve is None else performance_equity_curve
    if sharpe_window_sessions < 2:
        raise ValueError("sharpe_window_sessions must be at least 2")
    if divergence_trend_sessions < 2:
        raise ValueError("divergence_trend_sessions must be at least 2")

    try:
        _validate_equity_curve(native_equity)
        sharpe_pnl_divergence = _compute_sharpe_pnl_divergence(
            native_equity,
            sharpe_window_sessions=sharpe_window_sessions,
            divergence_trend_sessions=divergence_trend_sessions,
        )
    except ValueError as exc:
        sharpe_pnl_divergence = SharpePnlDivergenceResponse(
            error=f"Native equity curve rejected: {exc}",
            return_window_sessions=sharpe_window_sessions,
            trend_window_sessions=divergence_trend_sessions,
            annualization_sessions=_TRADING_SESSIONS_PER_YEAR,
            minimum_observation_count=(
                sharpe_window_sessions + divergence_trend_sessions
            ),
            daily_observation_count=0,
            eligible_observation_count=0,
            divergence_observation_count=0,
            longest_divergence_streak=0,
        )

    end_ms_utc = _resolve_end_ms(trades=trades, equity_curve=equity_curve)
    return EngineValidationAnalyticsResponse(
        horizons=_compute_horizons(trades, equity_curve, end_ms_utc),
        timing_cells=_compute_timing_cells(trades),
        seasonality=_compute_seasonality(trades),
        rolling_trade_stability=_compute_rolling_stability(trades, rolling_window),
        sharpe_pnl_divergence=sharpe_pnl_divergence,
    )


def build_compatibility_equity_curve(
    trades: Sequence[ValidationTrade],
    *,
    start_ms_utc: int,
    end_ms_utc: int,
    initial_equity: float,
) -> list[ValidationEquityPoint]:
    """Build the shared return-normalized curve used only by paired runs.

    Native Python and LEAN charts have different sampling contracts. This
    curve compounds the common closed-trade return ledger, combining trades
    that exit at the same millisecond, and pins both window endpoints. It is a
    platform analytics primitive, never a replacement for either native chart.
    """
    if start_ms_utc <= 0 or end_ms_utc <= start_ms_utc:
        raise ValueError("compatibility equity window must be positive and increasing")
    if initial_equity <= 0 or not math.isfinite(initial_equity):
        raise ValueError("compatibility initial equity must be positive and finite")

    returns_by_exit: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        if trade.exit_ms_utc < start_ms_utc or trade.exit_ms_utc > end_ms_utc:
            raise ValueError(f"trade {trade.trade_number} exits outside the compatibility window")
        returns_by_exit[trade.exit_ms_utc].append(trade.pnl_pct)

    equity = initial_equity
    points = [ValidationEquityPoint(timestamp_ms_utc=start_ms_utc, equity=equity)]
    for exit_ms_utc, returns in sorted(returns_by_exit.items()):
        equity *= math.prod(1.0 + value for value in returns)
        if exit_ms_utc == start_ms_utc:
            points[0] = ValidationEquityPoint(timestamp_ms_utc=start_ms_utc, equity=equity)
        else:
            points.append(ValidationEquityPoint(timestamp_ms_utc=exit_ms_utc, equity=equity))
    if points[-1].timestamp_ms_utc < end_ms_utc:
        points.append(ValidationEquityPoint(timestamp_ms_utc=end_ms_utc, equity=equity))
    return points


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
    _validate_equity_curve(equity_curve)


def _validate_equity_curve(equity_curve: Sequence[ValidationEquityPoint]) -> None:
    prior_timestamp = 0
    for index, point in enumerate(equity_curve):
        if point.timestamp_ms_utc <= prior_timestamp:
            raise ValueError(f"equity point {index} is not strictly increasing")
        if point.equity <= 0 or not math.isfinite(point.equity):
            raise ValueError(f"equity point {index} has invalid equity")
        prior_timestamp = point.timestamp_ms_utc


def _compute_sharpe_pnl_divergence(
    equity_curve: Sequence[ValidationEquityPoint],
    *,
    sharpe_window_sessions: int,
    divergence_trend_sessions: int,
) -> SharpePnlDivergenceResponse:
    """Compare rolling daily Sharpe with the native cumulative P&L path.

    Formula: r_t = E_t/E_(t-1)-1; SR_t = sqrt(252) * mean(r) /
      sample_std(r) over the trailing return window; PnL_t = E_t-E_0;
      divergence_t is true when the OLS slope of trailing P&L is positive
      and the OLS slope of trailing rolling Sharpe is negative.
    Reference: Sharpe (1994), *The Sharpe Ratio*; Lo (2002), *The
      Statistics of Sharpe Ratios*. The paired-slope flag is a documented
      repository exploratory diagnostic, not a significance test.
    Canonical implementation: this function.
    Validated against:
      PythonDataService/tests/services/test_engine_validation_analytics.py::test_validation_analytics_matches_pandas_rolling_sharpe_and_flags_pnl_divergence.
    """
    daily_equity = _last_equity_per_new_york_session(equity_curve)
    if not daily_equity:
        return SharpePnlDivergenceResponse(
            return_window_sessions=sharpe_window_sessions,
            trend_window_sessions=divergence_trend_sessions,
            annualization_sessions=_TRADING_SESSIONS_PER_YEAR,
            minimum_observation_count=(
                sharpe_window_sessions + divergence_trend_sessions
            ),
            daily_observation_count=0,
            eligible_observation_count=0,
            divergence_observation_count=0,
            longest_divergence_streak=0,
        )

    initial_equity = daily_equity[0].equity
    daily_returns = [
        daily_equity[index].equity / daily_equity[index - 1].equity - 1.0
        for index in range(1, len(daily_equity))
    ]
    points = [
        SharpePnlDivergencePointResponse(
            timestamp_ms_utc=point.timestamp_ms_utc,
            cumulative_pnl=point.equity - initial_equity,
            rolling_sharpe=_rolling_sharpe_at(
                daily_returns,
                equity_index=index,
                window=sharpe_window_sessions,
            ),
        )
        for index, point in enumerate(daily_equity)
    ]

    for index in range(divergence_trend_sessions - 1, len(points)):
        window = points[index - divergence_trend_sessions + 1 : index + 1]
        sharpes = [point.rolling_sharpe for point in window]
        if any(value is None for value in sharpes):
            continue
        pnl_slope = _ordinary_least_squares_slope(
            [point.cumulative_pnl for point in window]
        )
        sharpe_slope = _ordinary_least_squares_slope(
            [float(value) for value in sharpes if value is not None]
        )
        points[index].is_trend_eligible = True
        points[index].is_divergence = pnl_slope > 0 and sharpe_slope < 0

    eligible_points = [point for point in points if point.is_trend_eligible]
    divergence_count = sum(point.is_divergence for point in eligible_points)
    latest = points[-1]
    return SharpePnlDivergenceResponse(
        return_window_sessions=sharpe_window_sessions,
        trend_window_sessions=divergence_trend_sessions,
        annualization_sessions=_TRADING_SESSIONS_PER_YEAR,
        minimum_observation_count=(
            sharpe_window_sessions + divergence_trend_sessions
        ),
        daily_observation_count=len(points),
        eligible_observation_count=len(eligible_points),
        divergence_observation_count=divergence_count,
        divergence_ratio=(
            divergence_count / len(eligible_points) if eligible_points else None
        ),
        longest_divergence_streak=_longest_divergence_streak(points),
        latest_rolling_sharpe=latest.rolling_sharpe,
        latest_cumulative_pnl=latest.cumulative_pnl,
        currently_diverging=(latest.is_divergence if latest.is_trend_eligible else None),
        points=points,
    )


def _last_equity_per_new_york_session(
    equity_curve: Sequence[ValidationEquityPoint],
) -> list[ValidationEquityPoint]:
    daily: dict[tuple[int, int, int], ValidationEquityPoint] = {}
    for point in equity_curve:
        local = datetime.fromtimestamp(
            point.timestamp_ms_utc / 1000,
            tz=UTC,
        ).astimezone(_ET)
        daily[(local.year, local.month, local.day)] = point
    return list(daily.values())


def _rolling_sharpe_at(
    daily_returns: Sequence[float],
    *,
    equity_index: int,
    window: int,
) -> float | None:
    if equity_index < window:
        return None
    sample = daily_returns[equity_index - window : equity_index]
    mean_return = sum(sample) / window
    variance = sum((value - mean_return) ** 2 for value in sample) / (window - 1)
    if variance <= 0:
        return None
    return math.sqrt(_TRADING_SESSIONS_PER_YEAR) * mean_return / math.sqrt(variance)


def _ordinary_least_squares_slope(values: Sequence[float]) -> float:
    count = len(values)
    x_mean = (count - 1) / 2
    y_mean = sum(values) / count
    numerator = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    denominator = sum((index - x_mean) ** 2 for index in range(count))
    return numerator / denominator


def _longest_divergence_streak(
    points: Sequence[SharpePnlDivergencePointResponse],
) -> int:
    longest = 0
    current = 0
    for point in points:
        current = current + 1 if point.is_divergence else 0
        longest = max(longest, current)
    return longest


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
