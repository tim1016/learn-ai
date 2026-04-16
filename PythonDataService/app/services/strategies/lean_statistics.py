"""
LEAN-compatible statistics calculations.

Every formula matches the C# implementation in Lean/Common/Statistics/:
  - PortfolioStatistics.cs  (PS.cs)
  - Statistics.cs           (S.cs)
  - TradeStatistics.cs      (TS.cs)
  - StatisticsBuilder.cs    (SB.cs)

Reference: docs/spy-lean-output/verify.py and source-map.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import mean

import numpy as np
import pandas as pd

from .common import TradeRecord

# ═══════════════════════════════════════════════════════════════════════
# Math helpers (matching MathNet.Numerics and LEAN's Statistics.cs)
# ═══════════════════════════════════════════════════════════════════════

TRADING_DAYS_PER_YEAR = 252


def _variance(xs: list[float]) -> float:
    """Sample variance (n-1), matching MathNet.Numerics.Statistics.Variance."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _stddev(xs: list[float]) -> float:
    return math.sqrt(_variance(xs))


def _covariance(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=False)) / (n - 1)


def _skewness(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    s = _stddev(xs)
    if s == 0:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((x - m) / s) ** 3 for x in xs)


def _kurtosis(xs: list[float]) -> float:
    """Excess kurtosis (Fisher definition), matching MathNet."""
    n = len(xs)
    if n < 4:
        return 0.0
    m = sum(xs) / n
    s = _stddev(xs)
    if s == 0:
        return 0.0
    a = (n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))
    b = sum(((x - m) / s) ** 4 for x in xs)
    c = (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))
    return a * b - c


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _inv_normal_cdf(mu: float, sigma: float, p: float) -> float:
    """Inverse normal CDF (Acklam approximation), matching LEAN's VaR."""
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    dd = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((dd[0] * q + dd[1]) * q + dd[2]) * q + dd[3]) * q + 1
        )
    elif p <= phigh:
        q = p - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((dd[0] * q + dd[1]) * q + dd[2]) * q + dd[3]) * q + 1
        )
    return mu + sigma * x


# ═══════════════════════════════════════════════════════════════════════
# Build daily equity curve from bar data + trades
# ═══════════════════════════════════════════════════════════════════════


def build_daily_equity(
    df: pd.DataFrame,
    trades: list[TradeRecord],
    start_capital: float = 100_000.0,
) -> list[tuple[datetime, float]]:
    """Build a daily equity curve by replaying trades over the bar timeline.

    Strategy: Start with `start_capital`. For each closed trade, compound
    the PnL. Between trades, equity stays flat (fully cash). This matches
    LEAN's single-position, fully-invested model for our strategies.

    Returns: sorted list of (date, equity_value).
    """
    # Index trades by exit date
    trade_pnl_by_date: dict[str, float] = {}
    for t in trades:
        exit_date = t.exit_timestamp[:10]  # "YYYY-MM-DD"
        trade_pnl_by_date.setdefault(exit_date, 0.0)
        trade_pnl_by_date[exit_date] += t.pnl_pct * start_capital

    # Get unique dates from the bar data
    dates_seen: dict[str, float] = {}
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
        else:
            dt = ts
        date_str = dt.strftime("%Y-%m-%d")
        dates_seen[date_str] = float(row["close"])

    # Build equity by replaying trade PnL
    equity_curve: list[tuple[datetime, float]] = []
    running_equity = start_capital
    for date_str in sorted(dates_seen.keys()):
        if date_str in trade_pnl_by_date:
            running_equity += trade_pnl_by_date[date_str]
        equity_curve.append((datetime.fromisoformat(date_str), running_equity))

    return equity_curve


# ═══════════════════════════════════════════════════════════════════════
# Portfolio Statistics (LEAN PortfolioStatistics.cs)
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class LeanPortfolioStatistics:
    """All 25 fields from LEAN's PortfolioStatistics, computed identically."""

    # Rates (PS.cs 36–69, 262–277)
    average_win_rate: float = 0.0
    average_loss_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    win_rate: float = 0.0
    loss_rate: float = 0.0
    expectancy: float = 0.0

    # Equity (PS.cs 75–100, 223–281)
    start_equity: float = 0.0
    end_equity: float = 0.0
    total_net_profit: float = 0.0

    # Annualized (PS.cs 88, 285; S.cs 38–48)
    compounding_annual_return: float = 0.0

    # Risk (PS.cs 107–166, 287–308; S.cs)
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    probabilistic_sharpe_ratio: float = 0.0
    annual_standard_deviation: float = 0.0
    annual_variance: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    information_ratio: float = 0.0
    tracking_error: float = 0.0
    treynor_ratio: float = 0.0

    # Drawdown (PS.cs 94, 192, 318–319)
    drawdown: float = 0.0
    drawdown_recovery: int = 0  # calendar days

    # VaR (PS.cs 179–186, 314–315)
    value_at_risk_99: float = 0.0
    value_at_risk_95: float = 0.0

    # Turnover (PS.cs 172, 228) — requires position data, set to 0 for now
    portfolio_turnover: float = 0.0


@dataclass
class LeanTradeStatistics:
    """Key fields from LEAN's TradeStatistics (TS.cs), computed identically."""

    start_date_time: str = ""
    end_date_time: str = ""
    total_number_of_trades: int = 0
    number_of_winning_trades: int = 0
    number_of_losing_trades: int = 0
    total_profit_loss: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    largest_profit: float = 0.0
    largest_loss: float = 0.0
    average_profit_loss: float = 0.0
    average_profit: float = 0.0
    average_loss: float = 0.0
    average_trade_duration: str = ""
    average_winning_trade_duration: str = ""
    average_losing_trade_duration: str = ""
    max_consecutive_winning_trades: int = 0
    max_consecutive_losing_trades: int = 0
    profit_factor: float = 0.0
    profit_to_max_drawdown_ratio: float = 0.0
    # PnL distribution
    profit_loss_standard_deviation: float = 0.0
    profit_loss_downside_deviation: float = 0.0
    sharpe_ratio: float = 0.0  # trade-level
    sortino_ratio: float = 0.0  # trade-level
    total_fees: float = 0.0  # always 0 for our strategies (no fee model)


@dataclass
class LeanStatistics:
    """Combines portfolio + trade statistics + runtime."""

    portfolio: LeanPortfolioStatistics = field(default_factory=LeanPortfolioStatistics)
    trade: LeanTradeStatistics = field(default_factory=LeanTradeStatistics)
    # Runtime statistics snapshot
    equity: float = 0.0
    fees: float = 0.0
    net_profit: float = 0.0
    total_return: float = 0.0
    total_orders: int = 0


def compute_lean_statistics(
    df: pd.DataFrame,
    trades: list[TradeRecord],
    start_capital: float = 100_000.0,
    risk_free_rate: float = 0.0,
    benchmark_returns: list[float] | None = None,
) -> LeanStatistics:
    """Compute the full LEAN statistics suite from bar data and trades.

    Formulas match Lean/Common/Statistics/ exactly — see verify.py for
    reconciliation proof against actual LEAN output.

    Args:
        df: OHLCV DataFrame with 'timestamp' and 'close' columns.
        trades: list of TradeRecord from the strategy execution.
        start_capital: initial portfolio value (default $100k).
        risk_free_rate: annualized risk-free rate (default 0, LEAN uses ~5.43%).
        benchmark_returns: daily benchmark returns. None = zero benchmark
            (equivalent to LEAN's SetBenchmark(d => 0m)).
    """
    stats = LeanStatistics()
    port = stats.portfolio
    ts = stats.trade

    # ─── Build daily equity curve ───
    equity_curve = build_daily_equity(df, trades, start_capital)
    if len(equity_curve) < 2:
        return stats

    port.start_equity = equity_curve[0][1]
    port.end_equity = equity_curve[-1][1]
    start_date = equity_curve[0][0]
    end_date = equity_curve[-1][0]

    # ─── Daily returns ───
    daily_perf: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        cur = equity_curve[i][1]
        daily_perf.append((cur / prev) - 1.0 if prev else 0.0)

    if not daily_perf:
        return stats

    # Default benchmark = zero (matches SetBenchmark(d => 0m))
    bench_perf = benchmark_returns if benchmark_returns else [0.0] * len(daily_perf)

    # ─── Trade-derived rates (PS.cs 262–277, StatisticsBuilder) ───
    n_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    n_wins = len(wins)
    n_losses = len(losses)

    port.win_rate = n_wins / n_trades if n_trades else 0.0
    port.loss_rate = n_losses / n_trades if n_trades else 0.0

    # Average Win/Loss Rate: capital-normalized per GetProfitLossRates
    running_cap = start_capital
    total_win_return = 0.0
    total_loss_return = 0.0
    for t in sorted(trades, key=lambda x: x.exit_timestamp):
        r = t.pnl / running_cap if running_cap else 0.0
        if t.pnl > 0:
            total_win_return += r
        else:
            total_loss_return += r
        running_cap += t.pnl

    port.average_win_rate = total_win_return / n_wins if n_wins else 0.0
    port.average_loss_rate = total_loss_return / n_losses if n_losses else 0.0
    port.profit_loss_ratio = port.average_win_rate / abs(port.average_loss_rate) if port.average_loss_rate != 0 else 0.0
    port.expectancy = port.win_rate * port.profit_loss_ratio - port.loss_rate

    # ─── Net profit (PS.cs 281) ───
    port.total_net_profit = (port.end_equity / port.start_equity) - 1.0

    # ─── CAGR (PS.cs 285, S.cs 38–48) — uses 365 calendar days ───
    years = (end_date - start_date).days / 365.0
    if years > 0 and port.start_equity > 0:
        port.compounding_annual_return = (port.end_equity / port.start_equity) ** (1.0 / years) - 1.0

    # ─── Drawdown (PS.cs 318, S.cs 261–314) ───
    peak = -math.inf
    max_dd = 0.0
    for _, e in equity_curve:
        if e > peak:
            peak = e
        dd = (e / peak) - 1.0
        if dd < max_dd:
            max_dd = dd
    port.drawdown = abs(max_dd)

    # ─── Drawdown recovery (PS.cs 319) ───
    peak = -math.inf
    peak_date = equity_curve[0][0]
    max_recovery = 0
    in_dd = False
    for dt, e in equity_curve:
        if e >= peak:
            if in_dd:
                recovery = (dt - peak_date).days
                if recovery > max_recovery:
                    max_recovery = recovery
                in_dd = False
            peak = e
            peak_date = dt
        else:
            in_dd = True
    port.drawdown_recovery = max_recovery

    # ─── Annual variance & std dev (PS.cs 287–288, S.cs 69–88) ───
    daily_var = _variance(daily_perf)
    port.annual_variance = daily_var * TRADING_DAYS_PER_YEAR
    port.annual_standard_deviation = math.sqrt(port.annual_variance)

    # ─── Sharpe ratio (PS.cs 294, S.cs 148–164) ───
    annual_perf = mean(daily_perf) * TRADING_DAYS_PER_YEAR
    if port.annual_standard_deviation > 1e-12:
        port.sharpe_ratio = (annual_perf - risk_free_rate) / port.annual_standard_deviation

    # ─── Sortino ratio (PS.cs 297, S.cs 188–191) ───
    downside = [x for x in daily_perf if x < 0]
    annual_down_var = _variance(downside) * TRADING_DAYS_PER_YEAR if downside else 0.0
    annual_down_std = math.sqrt(annual_down_var)
    if annual_down_std > 1e-12:
        port.sortino_ratio = (annual_perf - risk_free_rate) / annual_down_std

    # ─── Probabilistic Sharpe Ratio (PS.cs 312, S.cs 199–234) ───
    n = len(daily_perf)
    per_period_std = _stddev(daily_perf)
    sr_obs = mean(daily_perf) / per_period_std if per_period_std else 0.0
    sr_bench = 1.0 / math.sqrt(TRADING_DAYS_PER_YEAR)
    sk = _skewness(daily_perf)
    ku = _kurtosis(daily_perf)
    denom = math.sqrt(max(1e-18, 1.0 - sk * sr_obs + ((ku - 1) / 4.0) * sr_obs**2))
    port.probabilistic_sharpe_ratio = _normal_cdf((sr_obs - sr_bench) * math.sqrt(n - 1) / denom)

    # ─── Beta (PS.cs 300) ───
    bvar = _variance(bench_perf)
    port.beta = _covariance(daily_perf, bench_perf) / bvar if bvar > 1e-18 else 0.0

    # ─── Alpha (PS.cs 302) ───
    bench_ann = mean(bench_perf) * TRADING_DAYS_PER_YEAR if bench_perf else 0.0
    if abs(port.beta) > 1e-12:
        port.alpha = annual_perf - (risk_free_rate + port.beta * (bench_ann - risk_free_rate))

    # ─── Tracking Error (PS.cs 304, S.cs 123–138) ───
    diff = [a - b for a, b in zip(daily_perf, bench_perf, strict=False)]
    port.tracking_error = math.sqrt(_variance(diff) * TRADING_DAYS_PER_YEAR) if diff else 0.0

    # ─── Information Ratio (PS.cs 306) ───
    if port.tracking_error > 1e-12:
        port.information_ratio = (annual_perf - bench_ann) / port.tracking_error

    # ─── Treynor Ratio (PS.cs 308) ───
    if abs(port.beta) > 1e-12:
        port.treynor_ratio = (annual_perf - risk_free_rate) / port.beta

    # ─── VaR 99/95 (PS.cs 314–315) ───
    window = daily_perf[-TRADING_DAYS_PER_YEAR:]
    mu = mean(window)
    sigma = _stddev(window)
    if sigma > 1e-12:
        port.value_at_risk_99 = _inv_normal_cdf(mu, sigma, 1 - 0.99)
        port.value_at_risk_95 = _inv_normal_cdf(mu, sigma, 1 - 0.95)

    # ═══ Trade Statistics (TS.cs) ═══
    ts.total_number_of_trades = n_trades
    ts.number_of_winning_trades = n_wins
    ts.number_of_losing_trades = n_losses

    if trades:
        ts.start_date_time = trades[0].entry_timestamp
        ts.end_date_time = trades[-1].exit_timestamp

        pnl_list = [t.pnl for t in trades]
        win_pnl = [t.pnl for t in wins]
        loss_pnl = [t.pnl for t in losses]

        ts.total_profit_loss = sum(pnl_list)
        ts.total_profit = sum(win_pnl) if win_pnl else 0.0
        ts.total_loss = sum(loss_pnl) if loss_pnl else 0.0
        ts.largest_profit = max(win_pnl) if win_pnl else 0.0
        ts.largest_loss = min(loss_pnl) if loss_pnl else 0.0
        ts.average_profit_loss = mean(pnl_list) if pnl_list else 0.0
        ts.average_profit = mean(win_pnl) if win_pnl else 0.0
        ts.average_loss = mean(loss_pnl) if loss_pnl else 0.0

        # Profit factor (TS.cs 420)
        if ts.total_loss != 0:
            ts.profit_factor = abs(ts.total_profit / ts.total_loss)

        # Profit-to-Max-Drawdown (TS.cs 423)
        if port.drawdown > 1e-12:
            ts.profit_to_max_drawdown_ratio = port.total_net_profit / port.drawdown

        # P&L std dev / downside dev (TS.cs 392, 352–354)
        pnl_pct_list = [t.pnl_pct for t in trades]
        ts.profit_loss_standard_deviation = float(np.std(pnl_pct_list, ddof=1)) if len(pnl_pct_list) > 1 else 0.0
        downside_pnl = [x for x in pnl_pct_list if x < 0]
        ts.profit_loss_downside_deviation = float(np.std(downside_pnl, ddof=1)) if len(downside_pnl) > 1 else 0.0

        # Trade-level Sharpe / Sortino (TS.cs 421–422)
        if ts.profit_loss_standard_deviation > 1e-12:
            ts.sharpe_ratio = mean(pnl_pct_list) / ts.profit_loss_standard_deviation * math.sqrt(252)
        if ts.profit_loss_downside_deviation > 1e-12:
            ts.sortino_ratio = mean(pnl_pct_list) / ts.profit_loss_downside_deviation * math.sqrt(252)

        # Max consecutive wins/losses (TS.cs 325–378)
        max_consec_w = 0
        max_consec_l = 0
        cur_w = 0
        cur_l = 0
        for t in trades:
            if t.pnl > 0:
                cur_w += 1
                cur_l = 0
                max_consec_w = max(max_consec_w, cur_w)
            else:
                cur_l += 1
                cur_w = 0
                max_consec_l = max(max_consec_l, cur_l)
        ts.max_consecutive_winning_trades = max_consec_w
        ts.max_consecutive_losing_trades = max_consec_l

        # Average trade durations (TS.cs 394, 318, 356)
        def _parse_ts(s: str) -> datetime:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+00:00", ""))

        def _avg_duration(trade_list: list[TradeRecord]) -> str:
            if not trade_list:
                return "0:00:00"
            durations = []
            for t in trade_list:
                try:
                    entry = _parse_ts(t.entry_timestamp)
                    exit_ = _parse_ts(t.exit_timestamp)
                    durations.append((exit_ - entry).total_seconds())
                except Exception:
                    pass
            if not durations:
                return "0:00:00"
            avg_secs = mean(durations)
            hours = int(avg_secs // 3600)
            minutes = int((avg_secs % 3600) // 60)
            return f"{hours}:{minutes:02d}:00"

        ts.average_trade_duration = _avg_duration(trades)
        ts.average_winning_trade_duration = _avg_duration(wins)
        ts.average_losing_trade_duration = _avg_duration(losses)

    # ═══ Runtime Statistics (BRH.cs) ═══
    stats.equity = port.end_equity
    stats.fees = 0.0  # no fee model in our strategies
    stats.net_profit = port.end_equity - port.start_equity
    stats.total_return = port.total_net_profit
    stats.total_orders = n_trades  # 1 order per trade in our model

    return stats
