"""Bar-level regime classification for signal gating."""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_daily_regime_labels(
    bars: list[dict],
    vol_low_pct: float = 0.33,
    vol_high_pct: float = 0.67,
    ma_window: int = 20,
    ma_slope_diff: int = 5,
) -> pd.DataFrame:
    """Classify each trading day into vol and trend regimes.

    Returns DataFrame with columns: date, vol_regime, trend_regime.
    """
    bar_df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)
    bar_df["date"] = pd.to_datetime(bar_df["timestamp"], unit="ms").dt.date
    bar_df["log_return"] = np.log(bar_df["close"] / bar_df["close"].shift(1))

    daily = (
        bar_df.groupby("date")
        .agg(close=("close", "last"), realized_vol=("log_return", "std"))
        .reset_index()
    )

    # Volatility regimes: tercile split
    vol_series = daily["realized_vol"].dropna()
    if len(vol_series) < 3:
        daily["vol_regime"] = "Normal Vol"
    else:
        p_low = vol_series.quantile(vol_low_pct)
        p_high = vol_series.quantile(vol_high_pct)
        daily["vol_regime"] = "Normal Vol"
        daily.loc[daily["realized_vol"] <= p_low, "vol_regime"] = "Low Vol"
        daily.loc[daily["realized_vol"] > p_high, "vol_regime"] = "High Vol"

    # Trend regimes: MA slope classification
    if len(daily) >= ma_window:
        ma = daily["close"].rolling(window=ma_window, min_periods=ma_window).mean()
        slope = ma.diff(ma_slope_diff) / ma_slope_diff
        threshold = slope.dropna().abs().median() * 0.5 if len(slope.dropna()) > 0 else 0.001

        daily["trend_regime"] = "Sideways"
        daily.loc[slope > threshold, "trend_regime"] = "Trending Up"
        daily.loc[slope < -threshold, "trend_regime"] = "Trending Down"
    else:
        daily["trend_regime"] = "Sideways"

    return daily[["date", "vol_regime", "trend_regime"]]


def compute_bar_regime_gate(
    bars: list[dict],
    timestamps: pd.Series,
) -> pd.Series:
    """Return per-bar boolean mask: 1 if Low Vol AND Sideways, else 0."""
    daily_regimes = compute_daily_regime_labels(bars)

    bar_dates = pd.to_datetime(timestamps, unit="ms").dt.date
    date_to_active = {}
    for _, row in daily_regimes.iterrows():
        is_active = row["vol_regime"] == "Low Vol" and row["trend_regime"] == "Sideways"
        date_to_active[row["date"]] = 1.0 if is_active else 0.0

    gate = bar_dates.map(date_to_active).fillna(0.0)
    return pd.Series(gate.values, index=timestamps.index, dtype=float)
