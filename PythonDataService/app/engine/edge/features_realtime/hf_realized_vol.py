"""High-frequency two-component realized variance — Step 3 of IV-RV alignment.

Estimator
---------
For each trading day d:

    RV²_d = Σ_{i in session(d)} r²_i  +  r²_overnight(d)

where ``r_i = ln(close_i / close_{i-1})`` for consecutive bars within the
session (no return crosses a session boundary) and
``r_overnight(d) = ln(open_first_d / close_last_{d-1})`` connects yesterday's
last bar to today's first.

Aggregated over a window of ``W`` trading days:

    RV²_window = Σ_{d in window} RV²_d
    σ²_TRD252 = RV²_window · 252 / W
    σ_TRD252  = √σ²_TRD252

This is the headline realized vol that drives VRP after Step 1's IV→252
conversion. The four daily estimators (CtC/Parkinson/GK/YZ) on the chart
chip-row remain visualization, not VRP-driving.

Session selection
-----------------
- ``RTH`` — 09:30 → 16:00 ET, 26 fifteen-minute bars per day, 17.5h overnight gap.
- ``ETH`` — 04:00 → 20:00 ET, 64 fifteen-minute bars per day, 8h overnight gap.

Zero-volume bars are dropped before computing returns (Polygon ETH bars
in the wee hours often have no trades, biasing returns toward zero).

Realtime guarantee
------------------
This module is in ``features_realtime/`` and never applies ``.shift(-N)``.
The forward variant lives in ``labels_oracle/hf_forward_rv.py``.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

Session = Literal["ETH", "RTH"]


def _et_session_mask(index: pd.DatetimeIndex, session: Session) -> np.ndarray:
    """Boolean array — True for bars whose ET timestamp falls in the chosen session."""
    et = index.tz_convert("America/New_York")
    hour = np.asarray(et.hour)
    minute = np.asarray(et.minute)
    if session == "RTH":
        # 09:30 inclusive → 16:00 exclusive
        return ((hour > 9) | ((hour == 9) & (minute >= 30))) & (hour < 16)
    if session == "ETH":
        # 04:00 inclusive → 20:00 exclusive
        return (hour >= 4) & (hour < 20)
    raise ValueError(f"session must be 'RTH' or 'ETH', got {session!r}")


def _et_trading_date(index: pd.DatetimeIndex) -> np.ndarray:
    """Map each tz-aware UTC timestamp to its ET calendar date (numpy datetime64[D] array)."""
    et = index.tz_convert("America/New_York")
    return np.asarray(et.normalize().tz_localize(None))


def daily_two_component_rv_sq(
    bars: pd.DataFrame,
    session: Session = "ETH",
) -> pd.Series:
    """Per-trading-day realized variance (sum of squared returns + overnight²).

    Output indexed by trading-date (tz-naive, ET calendar). Each value is the
    raw RV² for that day — not annualized, not rolled.
    """
    if bars.empty:
        return pd.Series(dtype=float)
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise TypeError("bars.index must be a DatetimeIndex")
    if bars.index.tz is None:
        raise ValueError("bars.index must be tz-aware (UTC)")
    for col in ("close", "open", "volume"):
        if col not in bars.columns:
            raise KeyError(f"bars must have column '{col}'")

    in_session = bars.loc[_et_session_mask(bars.index, session) & (bars["volume"] > 0)].copy()
    if in_session.empty:
        return pd.Series(dtype=float)

    in_session["trading_date"] = _et_trading_date(in_session.index)

    # Intraday: log returns within each trading_date — diff() at session break = NaN, dropped.
    log_close = np.log(in_session["close"])
    intra_ret = log_close.groupby(in_session["trading_date"]).diff()
    intra_sq = (intra_ret**2).fillna(0.0)
    daily_intra_sq = intra_sq.groupby(in_session["trading_date"]).sum()

    # Overnight: today's first close vs yesterday's last close. (Open is acceptable but
    # close-to-close keeps a single price stream, avoiding open-print artifacts on Polygon.)
    daily_first_close = in_session.groupby("trading_date")["close"].first()
    daily_last_close = in_session.groupby("trading_date")["close"].last()
    overnight_ret = np.log(daily_first_close / daily_last_close.shift(1))
    overnight_sq = (overnight_ret**2).fillna(0.0)

    daily_rv_sq = daily_intra_sq.add(overnight_sq, fill_value=0.0)
    daily_rv_sq.index.name = "trading_date"
    return daily_rv_sq


def hf_realized_vol_trd252(
    bars: pd.DataFrame,
    *,
    window_trading_days: int = 21,
    session: Session = "ETH",
) -> pd.Series:
    """Trailing HF realized vol on TRD/252 basis, indexed by bar timestamp.

    Computes per-day RV², rolls a sum over ``window_trading_days``, annualizes
    with the standard ``× 252 / W`` factor, and ffills onto the bar grid.
    NaN for bars before the first complete window.
    """
    if window_trading_days <= 0:
        raise ValueError(f"window_trading_days must be positive: {window_trading_days}")

    daily_rv_sq = daily_two_component_rv_sq(bars, session=session)
    if daily_rv_sq.empty:
        return pd.Series(np.nan, index=bars.index, dtype=float)

    rolling_var = daily_rv_sq.rolling(
        window=window_trading_days, min_periods=window_trading_days
    ).sum()
    annual_vol = np.sqrt(rolling_var * 252.0 / window_trading_days)

    bar_dates = pd.Series(_et_trading_date(bars.index), index=bars.index, name="trading_date")
    out = annual_vol.reindex(bar_dates.values).to_numpy()
    return pd.Series(out, index=bars.index, dtype=float, name=f"rv_hf_{window_trading_days}d_{session.lower()}")
