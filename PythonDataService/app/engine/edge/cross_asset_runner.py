"""Cross-asset strategy runner for the Edge feature.

Wires a registered strategy (placeholder buy-and-hold for v1) across a list
of symbols, runs the trade simulator on each, and aggregates results.

The real strategy registry will be wired in alongside the three TV strategies
from `three_strategies_roadmap.md`. For v1 we ship a placeholder so the
endpoint contract and aggregation layer can be exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.engine.edge.period_splitter import (
    TimePeriod,
    calendar_year_buckets,
    rolling_windows,
)
from app.engine.edge.portfolio_aggregator import (
    composite_stats,
    equal_weight_returns,
    vol_weighted_returns,
)
from app.engine.edge.robustness_stats import (
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    robustness_score,
)
from app.engine.edge.trade_simulator import TradeSimConfig, simulate


@dataclass(frozen=True)
class CrossAssetRunRequest:
    strategy_name: str
    symbols: list[str]
    start_ms: int
    end_ms: int
    bar_size: str
    split_mode: str  # "rolling" | "calendar" | "walkforward" | "all"


def placeholder_buy_and_hold_signals(bars: pd.DataFrame) -> pd.Series:
    """v1 placeholder: long on the first bar, hold to the end."""
    sig = pd.Series(0, index=bars.index, dtype=int)
    if not sig.empty:
        sig.iloc[0] = 1
    return sig


STRATEGY_REGISTRY = {
    "placeholder_buy_and_hold": placeholder_buy_and_hold_signals,
}


async def _run_one(symbol: str, period: TimePeriod, bars: pd.DataFrame, strategy_name: str) -> dict:
    fn = STRATEGY_REGISTRY[strategy_name]
    window = bars.loc[(bars.index >= period.start_ms) & (bars.index < period.end_ms)]
    if window.empty:
        return {"symbol": symbol, "period": period.label, "stats": {"n_trades": 0}, "equity_curve": []}
    signals = fn(window)
    sim_res = simulate(bars=window, signals=signals, config=TradeSimConfig(time_stop_bars=10**9))  # buy-and-hold proxy
    eq = sim_res.equity_curve
    return {
        "symbol": symbol,
        "period": period.label,
        "stats": sim_res.stats,
        "equity_curve": [] if eq is None else eq.assign(ts=eq["ts"].astype(int)).to_dict(orient="records"),
    }


async def run_cross_asset(
    request: CrossAssetRunRequest,
    bars_by_symbol: dict[str, pd.DataFrame],
) -> dict:
    """Orchestrate parallel per-asset/per-period runs and aggregate."""
    if request.strategy_name not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown strategy: {request.strategy_name}")

    periods: list[TimePeriod] = []
    if request.split_mode in ("rolling", "all"):
        periods += rolling_windows(start_ms=request.start_ms, end_ms=request.end_ms)
    if request.split_mode in ("calendar", "all"):
        periods += calendar_year_buckets(start_ms=request.start_ms, end_ms=request.end_ms)

    tasks = [
        _run_one(sym, period, bars_by_symbol[sym], request.strategy_name)
        for sym in request.symbols
        if sym in bars_by_symbol
        for period in periods
    ]
    raw = await asyncio.gather(*tasks)

    by_asset: dict[str, list[dict]] = {sym: [] for sym in request.symbols}
    for entry in raw:
        by_asset.setdefault(entry["symbol"], []).append(entry)

    returns_by_symbol = _assemble_returns(by_asset, bars_by_symbol)
    composites = {}
    if returns_by_symbol:
        composites["equal_weight"] = composite_stats(equal_weight_returns(returns_by_symbol))
        composites["vol_weighted"] = composite_stats(vol_weighted_returns(returns_by_symbol))

    sharpe_matrix = _sharpe_matrix(by_asset, periods)
    return {
        "by_asset": by_asset,
        "composites": composites,
        "robustness": {
            "score": robustness_score(sharpe_matrix),
            "pbo": probability_of_backtest_overfitting(sharpe_matrix) if sharpe_matrix.size else 0.0,
            "dsr_by_asset": _dsr_by_asset(by_asset),
        },
    }


def _assemble_returns(by_asset: dict, bars_by_symbol: dict) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for sym, bars in bars_by_symbol.items():
        if bars.empty:
            continue
        out[sym] = bars["close"].pct_change().fillna(0.0)
    return out


def _sharpe_matrix(by_asset: dict, periods: list[TimePeriod]) -> np.ndarray:
    if not by_asset or not periods:
        return np.array([[]])
    rows = []
    for runs in by_asset.values():
        period_to_sharpe = {r["period"]: r["stats"].get("sharpe", 0.0) for r in runs}
        rows.append([period_to_sharpe.get(p.label, 0.0) for p in periods])
    return np.array(rows, dtype=np.float64)


def _dsr_by_asset(by_asset: dict) -> dict[str, float]:
    out = {}
    for sym, runs in by_asset.items():
        sharpes = np.array([r["stats"].get("sharpe", 0.0) for r in runs], dtype=np.float64)
        if sharpes.size < 2:
            out[sym] = 0.0
            continue
        n_trades_total = sum(r["stats"].get("n_trades", 0) for r in runs)
        out[sym] = deflated_sharpe_ratio(
            observed_sharpe=float(sharpes.mean()),
            n_trials=len(sharpes),
            skew=0.0,
            kurtosis=3.0,
            n_observations=max(n_trades_total, 30),
        )
    return out
