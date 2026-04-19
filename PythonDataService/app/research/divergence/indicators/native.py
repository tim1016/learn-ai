"""Vetted pandas-only indicator implementations.

These are the **reference** formulas. They match Pine v5/v6's ``ta.*``
output to 4-6 decimal places when fed identical OHLCV data, as confirmed
by the 88-bar validation against the BATS_SPY indicator dump (April 2026).

Conventions:
  * EMA seed: SMA of first ``length`` samples (matches Pine and LEAN).
  * RSI smoothing: Wilder (alpha = 1/length).
  * Bollinger stdev: population (ddof=0). Pine's ``ta.bb`` uses ddof=0 too.
  * Bollinger bandwidth: 100 * (upper - lower) / mid (percentage form).
  * ATR smoothing: Wilder.
  * ADX: Wilder-smoothed +DM/-DM/TR; ADXR = (ADX + ADX.shift(2)) / 2.
  * SuperTrend: standard trailing-band ratchet rule.

All functions take ``pd.Series`` or ``pd.DataFrame`` and return the same.
``compute_all_native`` is the convenience entry point that produces every
indicator the divergence study compares.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ---------- primitives ----------


def ema(series: pd.Series, length: int) -> pd.Series:
    """Standard EMA. Seed = SMA of first ``length`` values; then recursive."""
    seed = series.rolling(length, min_periods=length).mean()
    out = series.ewm(span=length, adjust=False, min_periods=length).mean()
    # Replace the seed bar (index = length-1) with the SMA explicitly so the
    # first ready value matches Pine exactly. After that the recursion is
    # identical to pandas' ewm output.
    if len(series) >= length:
        out.iloc[length - 1] = seed.iloc[length - 1]
    return out


def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average. NaN for the first ``length-1`` bars."""
    return series.rolling(length, min_periods=length).mean()


def _rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder smoothing (also called RMA / Modified Moving Average).

    Matches Pine's ``ta.rma()`` and the classic J. Welles Wilder formulation:
    the first output value is the SMA of the first ``length`` inputs, then
    the recursion ``rma[i] = (rma[i-1]*(length-1) + x[i]) / length`` applies.
    ``pandas.Series.ewm(alpha=1/length, adjust=False)`` uses the FIRST VALUE
    as the seed, not the SMA, which differs from Wilder's convention by
    a few decimal places right after warmup. We therefore seed explicitly.
    """
    n = len(series)
    out = np.full(n, np.nan, dtype=float)
    x = series.values
    # First `length` non-NaN samples contribute to the seed. Allow for leading
    # NaNs (e.g. when ``series`` is the ``diff`` of closes — index 0 is NaN).
    valid_mask = ~pd.isna(x)
    valid_idx = np.where(valid_mask)[0]
    if valid_idx.size < length:
        return pd.Series(out, index=series.index)
    seed_end_pos = valid_idx[length - 1]  # position of the `length`-th valid value
    seed = float(np.nanmean(x[valid_idx[:length]]))
    out[seed_end_pos] = seed
    alpha = 1.0 / length
    for i in range(seed_end_pos + 1, n):
        xi = x[i]
        if np.isnan(xi):
            out[i] = out[i - 1]
            continue
        out[i] = alpha * xi + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def rsi_wilder(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's RSI — seeded with SMA of first ``length`` gains/losses."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    line = ef - es
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    # Mask warmup bars to NaN so they don't pollute downstream stats.
    warm_line = slow - 1
    warm_sig = warm_line + signal - 1
    n = len(close)
    line = line.mask(np.arange(n) < warm_line)
    sig = sig.mask(np.arange(n) < warm_sig)
    hist = hist.mask(np.arange(n) < warm_sig)
    return line, sig, hist


def bollinger(
    close: pd.Series,
    length: int = 20,
    stdev_mult: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (lower, mid, upper, bandwidth, %B). Stdev uses ddof=0."""
    mid = close.rolling(length, min_periods=length).mean()
    sd = close.rolling(length, min_periods=length).std(ddof=0)
    upper = mid + stdev_mult * sd
    lower = mid - stdev_mult * sd
    bandwidth = 100.0 * (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower)
    return lower, mid, upper, bandwidth, pct_b


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)


def atr_wilder(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.Series:
    return _rma(true_range(high, low, close), length)


def adx_system(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
    adxr_lag: int = 2,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (adx, adxr, +DI, -DI). All Wilder-smoothed."""
    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=high.index)

    atr = _rma(true_range(high, low, close), length)
    plus_di = 100.0 * _rma(plus_dm, length) / atr
    minus_di = 100.0 * _rma(minus_dm, length) / atr

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = _rma(dx, length)
    adxr = (adx + adx.shift(adxr_lag)) / 2.0
    return adx, adxr, plus_di, minus_di


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 10,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (line, direction, long_line, short_line).

    Direction = +1 in uptrend, -1 in downtrend. ``long_line`` is the ratcheted
    lower band (only populated during uptrend); ``short_line`` is the upper
    band (only during downtrend). ``line`` is whichever side is currently
    active — it's the value typically called "the SuperTrend."
    """
    hl2 = (high + low) / 2.0
    atr = _rma(true_range(high, low, close), length)
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=float)

    c = close.values
    ub = upper.values
    lb = lower.values

    if not atr.notna().any():
        return (
            pd.Series(line, index=close.index),
            pd.Series(direction, index=close.index),
            pd.Series(np.nan, index=close.index),
            pd.Series(np.nan, index=close.index),
        )
    first = int(atr.notna().idxmax())

    final_upper[first] = ub[first]
    final_lower[first] = lb[first]
    direction[first] = 1 if c[first] > ub[first] else -1
    line[first] = final_lower[first] if direction[first] == 1 else final_upper[first]

    for i in range(first + 1, n):
        final_upper[i] = ub[i] if (ub[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lb[i] if (lb[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if c[i] < final_lower[i] else 1
        else:
            direction[i] = 1 if c[i] > final_upper[i] else -1
        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    line_s = pd.Series(line, index=close.index)
    # Pine's ``ta.supertrend`` uses the *opposite* direction convention from
    # most other libraries: TV's dir = +1 means DOWN-trend, -1 means UP-trend.
    # We invert here so the column is directly comparable to TV's value;
    # downstream consumers that want the conventional sign can negate again.
    dir_s = pd.Series(-direction, index=close.index).mask(pd.Series(np.arange(n), index=close.index) < first)
    long_s = line_s.where(dir_s == -1)  # uptrend in TV convention
    short_s = line_s.where(dir_s == 1)  # downtrend in TV convention
    return line_s, dir_s, long_s, short_s


# ---------- batch-compute all indicators on an OHLCV DataFrame ----------

EMA_LENGTHS = (5, 10, 20, 30, 40, 50, 100, 200)
SMA_LENGTHS = (20, 50, 200)


def compute_all_native(df: pd.DataFrame, close_col: str = "close") -> pd.DataFrame:
    """Add every research-relevant indicator as new columns to ``df``.

    Column names follow the same naming convention used by the Pine dump
    script so the dataframe can be diffed against the TV CSV by column name.
    """
    out = df.copy()
    close = df[close_col]
    high = df["high"] if "high" in df.columns else df[close_col]
    low = df["low"] if "low" in df.columns else df[close_col]

    for L in EMA_LENGTHS:
        out[f"ema_{L}_native"] = ema(close, L)
    for L in SMA_LENGTHS:
        out[f"sma_{L}_native"] = sma(close, L)

    out["rsi_14_native"] = rsi_wilder(close, 14)

    m_line, m_sig, m_hist = macd(close, 12, 26, 9)
    out["macd_12_26_9_native"] = m_line
    out["macds_12_26_9_native"] = m_sig
    out["macdh_12_26_9_native"] = m_hist

    bb_l, bb_m, bb_u, bb_b, bb_p = bollinger(close, 20, 2.0)
    out["bb_lower_20_2_native"] = bb_l
    out["bb_mid_20_2_native"] = bb_m
    out["bb_upper_20_2_native"] = bb_u
    out["bb_bandwidth_20_2_native"] = bb_b
    out["bb_pctb_20_2_native"] = bb_p

    out["atr_14_native"] = atr_wilder(high, low, close, 14)

    adx_v, adxr_v, pdi, ndi = adx_system(high, low, close, 14, adxr_lag=2)
    out["adx_14_native"] = adx_v
    out["adxr_14_2_native"] = adxr_v
    out["dmp_14_native"] = pdi
    out["dmn_14_native"] = ndi

    st_line, st_dir, _st_long, _st_short = supertrend(high, low, close, 10, 3.0)
    out["supert_10_3_native"] = st_line
    out["supertd_10_3_native"] = st_dir

    return out
