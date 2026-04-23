"""Tests for indicator reliability analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.indicator_reliability import (
    MAX_DECAY_HORIZON,
    MIN_REGIME_BARS,
    HorizonICAnalysis,
    apply_multiple_testing_correction,
    bars_per_year,
    compute_direction_label,
    compute_forward_return,
    compute_ic_decay_curve,
    compute_indicator_reliability_with_oos,
    compute_ir_proxy,
    compute_random_baseline_ic,
    compute_regime_ic,
    compute_retention_delta_pct,
    compute_slope_decisions,
    compute_stability_label,
    compute_strength_label,
    compute_tradeability,
    find_best_horizon,
    format_indicator_display_name,
    generate_info_footnotes,
    generate_next_steps,
    get_indicator_category,
    split_by_volatility_regime,
)


def _create_test_df(n_bars: int = 500, seed: int = 42) -> pd.DataFrame:
    """Create a test DataFrame with OHLCV + RSI-like indicator.

    Uses a short synthetic intraday session (~60 bars/day) to guarantee
    multi-day coverage even for small n_bars. This is what the daily-IC
    group-by needs: without enough distinct dates it short-circuits to
    effective_n=0 and the pipeline metrics collapse.
    """
    np.random.seed(seed)

    # Generate random walk price
    returns = np.random.randn(n_bars) * 0.001
    close = 100 * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(np.random.randn(n_bars)) * 0.002)
    low = close * (1 - np.abs(np.random.randn(n_bars)) * 0.002)
    open_ = low + (high - low) * np.random.rand(n_bars)

    # Generate timestamps — short sessions to force multi-day coverage.
    base_ts = pd.Timestamp("2024-01-02 09:30:00", tz="US/Eastern")
    timestamps = []
    current = base_ts
    for _ in range(n_bars):
        timestamps.append(int(current.timestamp() * 1000))
        current += pd.Timedelta(minutes=1)
        if current.hour >= 10 and current.minute >= 30:  # ~60 bars/day
            current = (current + pd.Timedelta(days=1)).replace(hour=9, minute=30)

    # Create mock RSI (slightly correlated with future returns for testing)
    rsi = 50 + 30 * np.tanh(returns * 50) + np.random.randn(n_bars) * 5

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.randint(1000, 10000, n_bars),
            "rsi_14": rsi,
        }
    )


def _make_analysis(
    horizon: int = 10,
    is_mean_ic: float = 0.05,
    fdr_p: float = 0.05,
    oos_mean_ic: float | None = None,
    oos_p_value: float | None = None,
    oos_retention: float | None = None,
    strength_label: str = "Weak",
    stability_label: str = "Moderate",
) -> HorizonICAnalysis:
    """Build a HorizonICAnalysis with sensible defaults for tests."""
    return HorizonICAnalysis(
        horizon=horizon,
        is_mean_ic=is_mean_ic,
        is_t_stat=2.0,
        is_p_value=0.05,
        is_nw_t_stat=1.8,
        is_nw_p_value=0.07,
        is_effective_n=100,
        oos_mean_ic=oos_mean_ic,
        oos_t_stat=None if oos_mean_ic is None else 1.5,
        oos_p_value=oos_p_value,
        oos_effective_n=None if oos_mean_ic is None else 40,
        oos_retention=oos_retention,
        fdr_p=fdr_p,
        strength_label=strength_label,
        stability_label=stability_label,
    )


class TestComputeForwardReturn:
    """Tests for compute_forward_return function."""

    def test_basic_forward_return(self):
        df = _create_test_df(100)
        fwd = compute_forward_return(df, horizon=5)

        assert len(fwd) == len(df)
        assert fwd.iloc[-5:].isna().all()
        assert fwd.iloc[:-10].notna().sum() > 0

    def test_variable_horizons(self):
        df = _create_test_df(100)

        for horizon in [1, 5, 10, 15, 30]:
            fwd = compute_forward_return(df, horizon=horizon)
            assert len(fwd) == len(df)
            assert fwd.iloc[-horizon:].isna().all()

    def test_cross_day_masking(self):
        df = _create_test_df(500)
        fwd = compute_forward_return(df, horizon=15, mask_overnight=True)
        valid_count = fwd.notna().sum()
        assert valid_count < len(df) - 15


class TestComputeIndicatorReliabilityWithOos:
    """Tests for compute_indicator_reliability_with_oos (IS/OOS pipeline)."""

    def test_single_horizon(self):
        df = _create_test_df(500)
        results, slope_results, metadata = compute_indicator_reliability_with_oos(
            df=df,
            indicator_column="rsi_14",
            horizons=[10],
            include_slope=False,
        )

        assert len(results) == 1
        assert slope_results is None

        r = results[0]
        assert r.horizon == 10
        assert -1 <= r.is_mean_ic <= 1
        assert r.is_effective_n > 0
        assert r.strength_label in {"Noise", "Weak", "Moderate", "Strong"}
        assert r.stability_label in {"Low", "Moderate", "High"}
        assert r.direction_label in {"Mean-Reversion", "Momentum", "None"}
        assert 0.0 <= r.is_hit_rate <= 1.0

        assert "train_bars" in metadata
        assert "test_bars" in metadata

    def test_multiple_horizons_apply_fdr(self):
        df = _create_test_df(500)
        results, _, _ = compute_indicator_reliability_with_oos(
            df=df,
            indicator_column="rsi_14",
            horizons=[1, 5, 10, 15, 30],
            include_slope=False,
        )

        assert [r.horizon for r in results] == [1, 5, 10, 15, 30]
        # Corrections are applied to the NW p-value (or standard p if NW unavailable).
        # A corrected value is always >= the underlying raw value (correction can
        # only make things more conservative, never less).
        for r in results:
            raw_p = r.is_nw_p_value if r.is_nw_p_value is not None else r.is_p_value
            assert r.fdr_p >= raw_p - 1e-9
            assert r.bonferroni_p >= raw_p - 1e-9

    def test_with_slope(self):
        df = _create_test_df(500)
        results, slope_results, _ = compute_indicator_reliability_with_oos(
            df=df,
            indicator_column="rsi_14",
            horizons=[5, 10],
            include_slope=True,
        )

        assert len(results) == 2
        assert slope_results is not None
        assert len(slope_results) == 2
        # Slope flags are computed by the router; dataclass default is None here.
        assert slope_results[0].slope_adds_value is None

    def test_retention_delta_populated_when_oos_present(self):
        df = _create_test_df(500)
        results, _, _ = compute_indicator_reliability_with_oos(
            df=df,
            indicator_column="rsi_14",
            horizons=[10],
        )
        r = results[0]
        if r.oos_mean_ic is not None and abs(r.is_mean_ic) > 1e-10:
            assert r.retention_delta_pct is not None

    def test_missing_column_raises(self):
        df = _create_test_df(100)
        with pytest.raises(ValueError, match="not found"):
            compute_indicator_reliability_with_oos(
                df=df,
                indicator_column="nonexistent",
                horizons=[10],
            )


class TestFindBestHorizon:
    """Tests for find_best_horizon function (OOS-priority selection)."""

    def test_picks_oos_significant(self):
        results = [
            _make_analysis(horizon=1, is_mean_ic=0.01, fdr_p=0.32),
            _make_analysis(
                horizon=10,
                is_mean_ic=0.04,
                fdr_p=0.001,
                oos_mean_ic=0.03,
                oos_p_value=0.02,
                oos_retention=0.75,
            ),
            _make_analysis(horizon=30, is_mean_ic=0.02, fdr_p=0.14),
        ]
        assert find_best_horizon(results) == 10

    def test_falls_back_to_fdr_when_no_oos(self):
        results = [
            _make_analysis(horizon=5, is_mean_ic=0.04, fdr_p=0.02),
            _make_analysis(horizon=15, is_mean_ic=0.01, fdr_p=0.50),
        ]
        assert find_best_horizon(results) == 5

    def test_returns_none_if_nothing_significant(self):
        results = [_make_analysis(horizon=5, is_mean_ic=0.005, fdr_p=0.69)]
        assert find_best_horizon(results) is None


class TestMultipleTestingCorrection:
    def test_bonferroni_and_fdr_preserve_order(self):
        p_values = [0.01, 0.04, 0.20, 0.50]
        bonferroni, fdr = apply_multiple_testing_correction(p_values)
        # Bonferroni: p * n
        assert bonferroni[0] == pytest.approx(0.04)
        # Both corrections are >= raw p
        for raw, b, f in zip(p_values, bonferroni, fdr, strict=True):
            assert b >= raw - 1e-9
            assert f >= raw - 1e-9


class TestVerdictLabels:
    def test_strength_buckets(self):
        assert compute_strength_label(0.005) == "Noise"
        assert compute_strength_label(0.04) == "Weak"
        assert compute_strength_label(0.10) == "Moderate"
        assert compute_strength_label(0.20) == "Strong"

    def test_stability_buckets(self):
        assert compute_stability_label(0.50) == "Low"
        assert compute_stability_label(0.55) == "Moderate"
        assert compute_stability_label(0.60) == "High"

    def test_direction_from_signed_ic(self):
        assert compute_direction_label(-0.05) == "Mean-Reversion"
        assert compute_direction_label(0.05) == "Momentum"
        assert compute_direction_label(0.01) == "None"

    def test_retention_delta_none_when_oos_missing(self):
        assert compute_retention_delta_pct(0.10, None) is None

    def test_retention_delta_is_percent_change(self):
        assert compute_retention_delta_pct(0.10, 0.15) == pytest.approx(50.0)
        assert compute_retention_delta_pct(0.10, 0.06) == pytest.approx(-40.0)


class TestSlopeDecisions:
    def test_slope_with_oos_validated(self):
        raw = _make_analysis(is_mean_ic=0.05, fdr_p=0.05)
        slope = _make_analysis(
            is_mean_ic=0.09,
            fdr_p=0.01,
            oos_mean_ic=0.08,
            oos_p_value=0.02,
            oos_retention=0.8,
        )
        adds, recommended = compute_slope_decisions(raw, slope)
        assert adds is True
        assert recommended is True

    def test_slope_weaker_than_raw(self):
        raw = _make_analysis(is_mean_ic=0.10, fdr_p=0.02)
        slope = _make_analysis(
            is_mean_ic=0.03,
            fdr_p=0.30,
            oos_mean_ic=0.02,
            oos_p_value=0.40,
            oos_retention=0.5,
        )
        adds, recommended = compute_slope_decisions(raw, slope)
        assert adds is False
        assert recommended is False

    def test_recommended_none_without_oos(self):
        raw = _make_analysis(is_mean_ic=0.05, fdr_p=0.05)
        slope = _make_analysis(is_mean_ic=0.10, fdr_p=0.02)
        adds, recommended = compute_slope_decisions(raw, slope)
        assert adds is True
        assert recommended is None


class TestFormatDisplayName:
    def test_rsi(self):
        assert format_indicator_display_name("rsi", {"length": 14}) == "RSI (14)"

    def test_macd(self):
        name = format_indicator_display_name(
            "macd", {"fast": 12, "slow": 26, "signal": 9}
        )
        assert name == "MACD (12, 26, 9)"

    def test_ema(self):
        assert format_indicator_display_name("ema", {"length": 20}) == "EMA (20)"

    def test_no_params(self):
        assert format_indicator_display_name("obv", {}) == "OBV"


class TestGetIndicatorCategory:
    def test_known_indicator(self):
        assert get_indicator_category("rsi") == "momentum"

    def test_trend_indicator(self):
        assert get_indicator_category("ema") == "overlap"

    def test_unknown_indicator(self):
        assert get_indicator_category("not_an_indicator") is None


class TestICDecayCurve:
    def test_returns_one_point_per_horizon(self):
        df = _create_test_df(500)
        curve = compute_ic_decay_curve(df, "rsi_14", max_horizon=15)
        assert len(curve) == 15
        assert [p.horizon for p in curve] == list(range(1, 16))
        for p in curve:
            assert -1 <= p.ic <= 1
            assert 0 <= p.p_value <= 1
            assert p.ic_stderr >= 0

    def test_clamps_to_max(self):
        df = _create_test_df(500)
        curve = compute_ic_decay_curve(df, "rsi_14", max_horizon=9999)
        assert len(curve) == MAX_DECAY_HORIZON


class TestVolatilityRegimeSplit:
    def test_masks_are_disjoint_and_exclude_warmup(self):
        df = _create_test_df(500)
        high, low = split_by_volatility_regime(df, window=20)
        assert len(high) == len(df)
        assert not (high & low).any()
        # First `window` bars have NaN rolling vol and must be excluded.
        assert not high.iloc[:20].any()
        assert not low.iloc[:20].any()

    def test_balanced_split(self):
        df = _create_test_df(500)
        high, low = split_by_volatility_regime(df, window=20)
        # Roughly balanced: neither bucket should be grossly dominant
        assert 0.3 < high.sum() / (high.sum() + low.sum()) < 0.7


class TestRegimeIC:
    def test_returns_results_per_regime_with_required_fields(self):
        df = _create_test_df(500)
        results = compute_regime_ic(df, "rsi_14", horizons=[5, 10], window=20)

        assert set(results.keys()) == {"high_vol", "low_vol"}
        # Both buckets should have enough bars in this synthetic data
        for regime, points in results.items():
            assert points is not None, f"{regime} bucket unexpectedly empty"
            assert len(points) == 2  # one per horizon
            for p in points:
                assert p.horizon in {5, 10}
                assert p.bars_in_regime >= MIN_REGIME_BARS
                assert -1 <= p.mean_ic <= 1
                assert 0 <= p.hit_rate <= 1

    def test_tiny_bucket_returns_none(self):
        # Tiny frame — both regime buckets will be < 50 bars
        df = _create_test_df(60)
        results = compute_regime_ic(df, "rsi_14", horizons=[5], window=20)
        assert results["high_vol"] is None
        assert results["low_vol"] is None


class TestBarsPerYear:
    def test_minute(self):
        # 252 * 390 / 1 ≈ 98,280
        assert bars_per_year("minute", 1) == pytest.approx(252 * 390)

    def test_multiplier_scales_inversely(self):
        # 5-minute bars: 1/5 as many bars per day
        assert bars_per_year("minute", 5) == pytest.approx(252 * 390 / 5)

    def test_day(self):
        assert bars_per_year("day", 1) == pytest.approx(252)


class TestIRProxy:
    def test_zero_ic_yields_zero_ir(self):
        ir, sharpe, breadth = compute_ir_proxy(0.0, 10, 252 * 390)
        assert ir == 0.0
        assert sharpe == 0.0
        assert breadth > 0

    def test_positive_ic_yields_positive_ir(self):
        ir, sharpe, _ = compute_ir_proxy(0.05, 30, 252 * 390)
        assert ir > 0
        assert sharpe == ir

    def test_negative_ic_yields_negative_ir(self):
        ir, sharpe, _ = compute_ir_proxy(-0.05, 30, 252 * 390)
        assert ir < 0
        assert sharpe == ir

    def test_longer_horizon_reduces_breadth(self):
        _, _, breadth_short = compute_ir_proxy(0.05, 5, 252 * 390)
        _, _, breadth_long = compute_ir_proxy(0.05, 50, 252 * 390)
        assert breadth_short > breadth_long


class TestTradeability:
    def test_strong_and_stable_is_tradeable(self):
        assert compute_tradeability(1.5, "High") == "Likely tradeable"

    def test_strong_but_unstable_is_marginal(self):
        # |sharpe| >= 1 but stability Low → bumped down to Marginal
        assert compute_tradeability(1.5, "Low") == "Marginal"

    def test_mid_range_is_marginal(self):
        assert compute_tradeability(0.7, "High") == "Marginal"

    def test_low_is_unlikely(self):
        assert compute_tradeability(0.2, "High") == "Unlikely"

    def test_uses_absolute_value(self):
        # Negative IC → short the signal → still tradeable
        assert compute_tradeability(-1.5, "High") == "Likely tradeable"


class TestRandomBaselineReturnsDistribution:
    def test_distribution_length_matches_n_simulations(self):
        df = _create_test_df(500)
        mean, std, dist = compute_random_baseline_ic(df, horizon=10, n_simulations=25)
        assert len(dist) == 25
        assert std >= 0
        # Mean of the distribution should equal the returned mean
        assert mean == pytest.approx(sum(dist) / len(dist), rel=1e-6)


class TestNextSteps:
    def _r(self, **kwargs) -> HorizonICAnalysis:
        defaults = {"horizon": 10, "is_mean_ic": 0.05}
        defaults.update(kwargs)
        return _make_analysis(**defaults)

    def test_flags_missing_oos(self):
        results = [self._r(oos_mean_ic=None)]
        steps = generate_next_steps(results, None, None, 10)
        assert any("out-of-sample" in s.lower() for s in steps)

    def test_suggests_threshold_when_strong_stable_validated(self):
        results = [
            self._r(
                is_mean_ic=0.10,
                strength_label="Strong",
                stability_label="High",
                oos_mean_ic=0.09,
                oos_p_value=0.02,
            )
        ]
        steps = generate_next_steps(results, None, None, 10)
        assert any("threshold" in s.lower() for s in steps)

    def test_no_best_horizon_returns_prompt(self):
        results = [self._r()]
        steps = generate_next_steps(results, None, None, None)
        assert len(steps) >= 1
        assert "significance" in steps[0].lower() or "cleared" in steps[0].lower()

    def test_flags_oos_gap_when_is_strong_but_not_validated(self):
        results = [
            self._r(
                is_mean_ic=0.08,
                strength_label="Moderate",
                stability_label="High",
                oos_mean_ic=0.05,
                oos_p_value=0.23,
            )
        ]
        steps = generate_next_steps(results, None, None, 10)
        combined = " ".join(steps).lower()
        assert "validate" in combined or "out-of-sample" in combined

    def test_caps_at_four_items(self):
        # Engineer a scenario that triggers every rule
        results = [
            self._r(
                is_mean_ic=0.05,
                strength_label="Moderate",
                stability_label="Low",  # triggers "try a longer horizon"
                oos_mean_ic=None,  # triggers "collect more OOS"
            )
        ]
        steps = generate_next_steps(results, None, None, 10)
        assert len(steps) <= 4


class TestInfoFootnotes:
    def test_always_includes_single_asset_caveat(self):
        notes = generate_info_footnotes([1])
        assert any("single-asset" in n.lower() for n in notes)

    def test_adds_overlap_caveat_for_multi_bar_horizons(self):
        notes = generate_info_footnotes([1, 5, 10])
        assert any("overlapping" in n.lower() for n in notes)

    def test_no_overlap_caveat_for_horizon_one_only(self):
        notes = generate_info_footnotes([1])
        assert not any("overlapping" in n.lower() for n in notes)
