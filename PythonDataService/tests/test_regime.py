"""Tests for app.research.signal.regime."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.signal.regime import (
    compute_bar_regime_gate,
    compute_daily_regime_labels,
)

# Canonical `int64 ms UTC` timestamp for 2024-01-01 09:30 UTC — avoids tz-ambiguity
# in tests and satisfies the timestamp policy in .claude/rules/numerical-rigor.md.
JAN_1_2024_0930_UTC_MS = 1_704_101_400_000
DAY_MS = 86_400_000
MINUTE_MS = 60_000
BARS_PER_DAY = 5  # 5 intraday bars per day — enough for groupby.std() to be defined


def _bar(ts_ms: int, close: float) -> dict:
    return {
        "timestamp": ts_ms,
        "open": close - 0.1,
        "high": close + 0.1,
        "low": close - 0.2,
        "close": close,
        "volume": 1_000_000,
    }


def _synthetic_daily_bars(closes: list[float], intraday_sigma: list[float] | None = None) -> list[dict]:
    """Build `BARS_PER_DAY` bars per day, with per-day log-return noise scale.

    The regime calculator groups by date and takes std of intra-day log returns,
    so we must emit multiple bars per day to exercise the vol-regime buckets.
    """
    rng = np.random.default_rng(seed=123)
    bars: list[dict] = []
    for i, daily_close in enumerate(closes):
        sigma = intraday_sigma[i] if intraday_sigma is not None else 0.0
        for k in range(BARS_PER_DAY):
            noise = rng.normal(scale=sigma) if sigma > 0 else 0.0
            price = float(daily_close) + noise
            bars.append(_bar(JAN_1_2024_0930_UTC_MS + i * DAY_MS + k * MINUTE_MS, price))
    return bars


def test_compute_daily_regime_labels_short_series_returns_normal_vol():
    bars = _synthetic_daily_bars([100.0, 101.0])

    daily = compute_daily_regime_labels(bars)

    assert set(daily.columns) == {"date", "vol_regime", "trend_regime"}
    assert (daily["vol_regime"] == "Normal Vol").all()


def test_compute_daily_regime_labels_assigns_three_vol_buckets():
    # 60 days; per-day intraday noise scale rises across three cohorts so
    # realized_vol (std of intraday log returns) splits cleanly by tercile.
    closes = [100.0 + i * 0.01 for i in range(60)]  # near-flat level
    sigma = [0.001] * 20 + [0.05] * 20 + [1.0] * 20

    daily = compute_daily_regime_labels(bars=_synthetic_daily_bars(closes, intraday_sigma=sigma))

    vols = set(daily["vol_regime"].unique())
    assert vols == {"Low Vol", "Normal Vol", "High Vol"}


def test_compute_daily_regime_labels_trending_up_detected():
    # Steadily rising closes → MA slope positive → Trending Up
    closes = [100.0 + i for i in range(60)]
    bars = _synthetic_daily_bars(closes)

    daily = compute_daily_regime_labels(bars, ma_window=20, ma_slope_diff=5)

    # After the MA window + slope lookback the label should converge to Trending Up.
    tail_labels = daily["trend_regime"].iloc[-10:].unique().tolist()
    assert tail_labels == ["Trending Up"], tail_labels


def test_compute_daily_regime_labels_trending_down_detected():
    closes = [200.0 - i for i in range(60)]
    bars = _synthetic_daily_bars(closes)

    daily = compute_daily_regime_labels(bars, ma_window=20, ma_slope_diff=5)

    tail_labels = daily["trend_regime"].iloc[-10:].unique().tolist()
    assert tail_labels == ["Trending Down"], tail_labels


def test_compute_bar_regime_gate_shape_and_values():
    closes = [100.0] * 5
    bars = _synthetic_daily_bars(closes)
    timestamps = pd.Series([b["timestamp"] for b in bars])

    gate = compute_bar_regime_gate(bars, timestamps)

    assert len(gate) == len(timestamps)
    # Values must be {0.0, 1.0} floats.
    assert set(np.unique(gate.to_numpy())).issubset({0.0, 1.0})
    # Index is preserved.
    assert list(gate.index) == list(timestamps.index)


def test_compute_bar_regime_gate_unknown_date_defaults_to_zero():
    bars = _synthetic_daily_bars([100.0, 101.0, 102.0])
    # A timestamp from a day not present in the bars → gate must be 0.
    future_ts = pd.Series([JAN_1_2024_0930_UTC_MS + 365 * DAY_MS])

    gate = compute_bar_regime_gate(bars, future_ts)

    assert gate.iloc[0] == pytest.approx(0.0, abs=0, rel=0)
