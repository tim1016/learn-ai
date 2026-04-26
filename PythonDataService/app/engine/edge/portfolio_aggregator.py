"""Cross-asset portfolio aggregation.

Two composites (per docs/architecture/edge-feature-design.md §5.1):
- Equal-weight: w_i = 1/N
- Vol-weighted: w_i ∝ 1/σ_i^(60d), monthly rebalanced
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def equal_weight_returns(returns_by_asset: dict[str, pd.Series]) -> pd.Series:
    """R_t^port = (1/N) Σ R_t^(i). Aligned on the union index, NaN-safe."""
    if not returns_by_asset:
        raise ValueError("returns_by_asset must contain at least one symbol")
    df = pd.concat(returns_by_asset, axis=1)
    return df.mean(axis=1, skipna=True)


def vol_weighted_returns(
    returns_by_asset: dict[str, pd.Series],
    *,
    vol_lookback: int = 60,
    rebalance_freq: str = "ME",
) -> pd.Series:
    """Inverse-vol portfolio: w_i ∝ 1/σ_i.

    Vol estimated with a rolling 60-bar standard deviation. Weights are held
    constant within each rebalance bucket (default month-end) — no look-ahead
    because the vol used for bucket [t, t+rebalance) is computed up to t.
    """
    df = pd.concat(returns_by_asset, axis=1)
    rolling_vol = df.rolling(vol_lookback, min_periods=vol_lookback).std(ddof=1)
    rebal_dates = df.resample(rebalance_freq).first().index
    weights = pd.DataFrame(0.0, index=df.index, columns=df.columns)

    for d in rebal_dates:
        snap = rolling_vol.asof(d)
        if snap is None or snap.isna().all():
            continue
        inv = 1.0 / snap.replace(0, np.nan)
        w = inv / inv.sum(skipna=True)
        weights.loc[d:] = w.values

    weights = weights.shift(1).fillna(0.0)
    return (df * weights).sum(axis=1, skipna=True)


def composite_stats(returns: pd.Series) -> dict:
    r = returns.dropna().to_numpy(dtype=np.float64)
    if r.size < 2:
        return {"n": int(r.size), "total_return": 0.0, "ann_sharpe": 0.0, "ann_vol": 0.0, "max_dd": 0.0}
    eq = (1.0 + r).cumprod()
    dd = (eq - eq.cummax()) / eq.cummax()
    ann_sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(252)) if r.std(ddof=1) > 0 else 0.0
    return {
        "n": int(r.size),
        "total_return": float(eq[-1] - 1.0),
        "ann_sharpe": ann_sharpe,
        "ann_vol": float(r.std(ddof=1) * np.sqrt(252)),
        "max_dd": float(dd.min()),
    }
