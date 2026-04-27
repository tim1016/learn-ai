"""Tests for HF two-component realized variance (Step 3 of IV-RV alignment)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.features_realtime.hf_realized_vol import (
    daily_two_component_rv_sq,
    hf_realized_vol_trd252,
)
from app.engine.edge.labels_oracle.hf_forward_rv import hf_forward_rv_trd252


def _make_bars(
    n_days: int,
    sigma_annual: float,
    *,
    bars_per_day: int = 64,  # ETH default
    session_start_hour_et: int = 4,
    seed: int = 7,
    overnight_factor: float = 1.0,
    s0: float = 100.0,
    skip_weekends: bool = True,
) -> pd.DataFrame:
    """Build a synthetic 15-min bar DataFrame at given annualized vol.

    Variance per intraday step: sigma² / (252 × bars_per_day).
    Overnight return scaled by ``overnight_factor`` (1.0 = same per-bar variance).
    """
    rng = np.random.default_rng(seed)
    per_step_var_intra = sigma_annual**2 / (252 * bars_per_day)
    per_step_std_intra = float(np.sqrt(per_step_var_intra))
    per_step_std_overnight = float(np.sqrt(per_step_var_intra) * overnight_factor)

    rows = []
    cur_price = s0
    last_close_per_day: float | None = None
    cur_date = pd.Timestamp("2024-03-04", tz="America/New_York")
    days_added = 0
    while days_added < n_days:
        if skip_weekends and cur_date.weekday() >= 5:
            cur_date = cur_date + pd.Timedelta(days=1)
            continue
        # overnight return (skip on day 0)
        if last_close_per_day is not None:
            r_overnight = rng.normal(0.0, per_step_std_overnight)
            cur_price = last_close_per_day * np.exp(r_overnight)
        for b in range(bars_per_day):
            ts_et = cur_date.replace(hour=session_start_hour_et) + pd.Timedelta(minutes=15 * b)
            r_intra = rng.normal(0.0, per_step_std_intra)
            new_price = cur_price * np.exp(r_intra)
            rows.append(
                {
                    "ts": ts_et.tz_convert("UTC"),
                    "open": cur_price,
                    "high": max(cur_price, new_price),
                    "low": min(cur_price, new_price),
                    "close": new_price,
                    "volume": 1000,
                }
            )
            cur_price = new_price
        last_close_per_day = cur_price
        cur_date = cur_date + pd.Timedelta(days=1)
        days_added += 1
    df = pd.DataFrame(rows).set_index("ts")
    return df


class TestDailyRvSq:
    def test_nonzero_on_synthetic_gbm(self):
        bars = _make_bars(n_days=5, sigma_annual=0.20, bars_per_day=64)
        daily = daily_two_component_rv_sq(bars, session="ETH")
        # 5 trading days, all positive variance.
        assert len(daily) == 5
        assert (daily > 0).all()

    def test_session_filter_produces_valid_output_for_both_modes(self):
        # The synthetic _make_bars covers 04:00–19:45 ET (full ETH session). RTH
        # mode masks out 04:00–09:29 and 16:00–19:45. Both modes should produce
        # one positive value per trading day, and the annualized estimator (next
        # test class) should recover sigma in both cases. We don't assert
        # ETH > RTH here because the synthetic-overnight construction inflates
        # RTH's "overnight" return (which spans the masked-out hours).
        bars = _make_bars(n_days=30, sigma_annual=0.20, bars_per_day=64)
        daily_eth = daily_two_component_rv_sq(bars, session="ETH")
        daily_rth = daily_two_component_rv_sq(bars, session="RTH")
        assert len(daily_eth) == len(daily_rth) == 30
        assert (daily_eth > 0).all()
        assert (daily_rth > 0).all()

    def test_empty_bars_returns_empty_series(self):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        empty.index = pd.DatetimeIndex([], tz="UTC")
        assert daily_two_component_rv_sq(empty).empty

    def test_zero_volume_bars_excluded_from_returns(self):
        # Mark every other bar as zero-volume. The estimator must produce output
        # built only from the surviving bars — no contribution from the masked bars.
        bars = _make_bars(n_days=3, sigma_annual=0.20, bars_per_day=64)
        bars_zv = bars.copy()
        bars_zv.iloc[::2, bars_zv.columns.get_loc("volume")] = 0
        result = daily_two_component_rv_sq(bars_zv, session="ETH")
        assert len(result) == 3
        assert (result > 0).all()
        # All-zero-volume → empty output (no surviving bars).
        bars_all_zero = bars.copy()
        bars_all_zero["volume"] = 0
        empty = daily_two_component_rv_sq(bars_all_zero, session="ETH")
        assert empty.empty

    def test_naive_index_raises(self):
        rng = pd.date_range("2024-03-04 09:30", periods=64, freq="15min")
        bars = pd.DataFrame(
            {
                "open": np.linspace(100, 101, 64),
                "high": np.linspace(100, 101, 64),
                "low": np.linspace(100, 101, 64),
                "close": np.linspace(100, 101, 64),
                "volume": [1000] * 64,
            },
            index=rng,
        )
        with pytest.raises(ValueError, match="tz-aware"):
            daily_two_component_rv_sq(bars)


class TestHfRealizedVolTrd252:
    def test_recovers_known_sigma_within_5pct(self):
        # ETH, 21-day window, 100 days of GBM at sigma=0.20.
        bars = _make_bars(n_days=100, sigma_annual=0.20, bars_per_day=64)
        rv = hf_realized_vol_trd252(bars, window_trading_days=21, session="ETH")
        valid = rv.dropna()
        assert len(valid) > 0
        mean_rv = valid.mean()
        # 5% relative tolerance — sample-size variance on 21-day windows is non-trivial.
        assert 0.19 <= mean_rv <= 0.21, f"expected ~0.20, got {mean_rv:.4f}"

    def test_warmup_region_is_nan(self):
        bars = _make_bars(n_days=30, sigma_annual=0.20, bars_per_day=64)
        rv = hf_realized_vol_trd252(bars, window_trading_days=21, session="ETH")
        # Bars in the first 20 trading days have no full 21-day window → NaN.
        first_day_bars = rv.iloc[:64]
        assert first_day_bars.isna().all()

    def test_rth_and_eth_recover_similar_sigma(self):
        bars = _make_bars(n_days=100, sigma_annual=0.20, bars_per_day=64)
        rv_eth = hf_realized_vol_trd252(bars, window_trading_days=21, session="ETH").dropna().mean()
        # Note: RTH on the same data will see only ~26 of the 64 ETH bars per day, plus
        # the same overnight return. The annualization factor is identical (252/W) so the
        # estimator should recover sigma similarly within Monte-Carlo error.
        rv_rth = hf_realized_vol_trd252(bars, window_trading_days=21, session="RTH").dropna().mean()
        # Both should land within 15% of the true sigma (small sample, RTH has fewer bars).
        assert 0.17 <= rv_eth <= 0.23
        assert 0.17 <= rv_rth <= 0.23

    def test_indexed_to_bars(self):
        bars = _make_bars(n_days=30, sigma_annual=0.20, bars_per_day=64)
        rv = hf_realized_vol_trd252(bars, window_trading_days=21, session="ETH")
        assert rv.index.equals(bars.index)
        assert rv.dtype == float

    def test_invalid_window_raises(self):
        bars = _make_bars(n_days=30, sigma_annual=0.20, bars_per_day=64)
        with pytest.raises(ValueError):
            hf_realized_vol_trd252(bars, window_trading_days=0)


class TestHfForwardRv:
    def test_forward_terminal_window_is_nan(self):
        bars = _make_bars(n_days=30, sigma_annual=0.20, bars_per_day=64)
        rv_fwd = hf_forward_rv_trd252(bars, window_trading_days=21, session="ETH")
        # Last 21 trading days should be NaN (forward not yet realized).
        last_day_bars = rv_fwd.iloc[-64:]
        assert last_day_bars.isna().all()

    def test_forward_recovers_sigma(self):
        bars = _make_bars(n_days=100, sigma_annual=0.20, bars_per_day=64)
        rv_fwd = hf_forward_rv_trd252(bars, window_trading_days=21, session="ETH")
        valid = rv_fwd.dropna()
        assert 0.19 <= valid.mean() <= 0.21
