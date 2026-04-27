"""Real-time features for regime clustering.

Hard rule: every column produced uses .shift(N) with N >= 0 only.
Never .shift(-N). Never imports from app.engine.edge.labels_oracle.

OHLCV-only feature set (v1):
- trend_slope:   OLS slope of close on time, 20-bar window, normalized by ATR
- rv_yz:         Yang-Zhang annualized vol, 20-bar window
- atr_pct:       ATR(14) / Close
- volume_z:      rolling z-score of volume, 20-bar
All features are then rolling-z-scored on a 60-bar lookback before clustering.

IV-derived features (added in step 5): iv30_atm_50d, skew_25d, term_slope, d_iv, iv_vol.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.edge.features_realtime.realized_vol import yang_zhang


def _trend_slope(close: pd.Series, window: int) -> pd.Series:
    """OLS slope of close on time index over a rolling window."""
    x = np.arange(window, dtype=np.float64)
    x_demean = x - x.mean()
    denom = (x_demean * x_demean).sum()

    def slope(arr: np.ndarray) -> float:
        if np.isnan(arr).any():
            return np.nan
        y_demean = arr - arr.mean()
        return float((x_demean * y_demean).sum() / denom)

    return close.rolling(window).apply(slope, raw=True)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder ATR — true range = max(H-L, |H-C_{t-1}|, |L-C_{t-1}|)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def _rolling_zscore(series: pd.Series, lookback: int) -> pd.Series:
    """Rolling z-score; constant windows (std==0) collapse to 0 rather than NaN
    so downstream features survive flat stretches in volume / trend / range."""
    mean = series.rolling(lookback, min_periods=lookback).mean()
    std = series.rolling(lookback, min_periods=lookback).std(ddof=1)
    z = (series - mean) / std
    z = z.where(std > 0, 0.0)
    return z.where(~mean.isna(), np.nan)


def build_ohlcv_features(
    bars: pd.DataFrame,
    *,
    feature_window: int = 20,
    atr_window: int = 14,
    z_lookback: int = 60,
) -> pd.DataFrame:
    """Build the OHLCV-only regime feature matrix.

    Returns a DataFrame indexed identically to `bars` with columns:
        trend_slope_z, rv_yz_z, atr_pct_z, volume_z_z
    Each is a rolling-z-scored version of its raw feature; rows lacking
    enough warmup contain NaN. Caller drops NaN before fitting a model.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")

    trend_slope = _trend_slope(bars["close"], feature_window)
    rv_yz_value = yang_zhang(bars, window=feature_window)
    atr = _atr(bars["high"], bars["low"], bars["close"], window=atr_window)
    atr_pct = atr / bars["close"]
    trend_slope_norm = trend_slope / atr.replace(0, np.nan)
    volume_z_raw = _rolling_zscore(bars["volume"].astype(np.float64), feature_window)

    return pd.DataFrame(
        {
            "trend_slope_z": _rolling_zscore(trend_slope_norm, z_lookback),
            "rv_yz_z": _rolling_zscore(rv_yz_value, z_lookback),
            "atr_pct_z": _rolling_zscore(atr_pct, z_lookback),
            "volume_z_z": _rolling_zscore(volume_z_raw, z_lookback),
        },
        index=bars.index,
    )


_IV_DERIVED_COLUMNS = ("d_iv_z", "iv_vol_z", "iv30_z", "skew_25d_z", "term_slope_z")


def build_full_features(
    bars: pd.DataFrame,
    *,
    iv30: pd.Series | None = None,
    skew_25d: pd.Series | None = None,
    term_slope: pd.Series | None = None,
    feature_window: int = 20,
    atr_window: int = 14,
    z_lookback: int = 60,
    iv_feature_weight: float | pd.Series | None = None,
) -> pd.DataFrame:
    """Full regime feature matrix with optional IV-derived features.

    When iv30 is supplied, three derived columns are appended:
        iv30_z       — z-score of IV30
        d_iv_z       — z-score of ΔIV30 (vol-of-vol pace)
        iv_vol_z     — z-score of rolling-std(IV30, 20) (vol-of-vol level)
    skew_25d and term_slope are passed through z-scored if supplied.

    All inputs must share the bars index; columns absent or all-NaN are dropped.

    Step F of the IV-ownership plan: ``iv_feature_weight`` ∈ [0, 1] (or a
    Series of per-bar weights) scales every IV-derived column by the
    weight before returning. Weight = 0 collapses the IV features to 0;
    weight = 1 leaves them unchanged. The single source of truth for
    this weight is ``app.engine.edge.confidence.regime_feature_weight``.
    """
    base = build_ohlcv_features(
        bars,
        feature_window=feature_window,
        atr_window=atr_window,
        z_lookback=z_lookback,
    )
    extras: dict[str, pd.Series] = {}
    if iv30 is not None and iv30.notna().any():
        d_iv = iv30.diff()
        iv_v = iv30.rolling(feature_window, min_periods=feature_window).std(ddof=1)
        extras["d_iv_z"] = _rolling_zscore(d_iv, z_lookback)
        extras["iv_vol_z"] = _rolling_zscore(iv_v, z_lookback)
        extras["iv30_z"] = _rolling_zscore(iv30, z_lookback)
    if skew_25d is not None and skew_25d.notna().any():
        extras["skew_25d_z"] = _rolling_zscore(skew_25d, z_lookback)
    if term_slope is not None and term_slope.notna().any():
        extras["term_slope_z"] = _rolling_zscore(term_slope, z_lookback)

    if not extras:
        return base

    if iv_feature_weight is not None:
        weight = _coerce_iv_feature_weight(iv_feature_weight, bars.index)
        for col_name in extras:
            if col_name in _IV_DERIVED_COLUMNS:
                extras[col_name] = extras[col_name] * weight

    return pd.concat([base, pd.DataFrame(extras, index=bars.index)], axis=1)


def _coerce_iv_feature_weight(
    weight: float | pd.Series, index: pd.Index
) -> pd.Series:
    """Accept either a scalar weight or a per-bar Series; emit a clamped Series."""
    if isinstance(weight, pd.Series):
        w = weight.reindex(index).fillna(0.0)
    else:
        w = pd.Series(float(weight), index=index)
    return w.clip(lower=0.0, upper=1.0)
