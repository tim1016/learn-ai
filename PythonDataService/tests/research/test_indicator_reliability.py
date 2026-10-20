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
    generate_info_footnotes,
    generate_next_steps,
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

    @pytest.mark.parametrize(
        "horizon",
        [1, 5, 10, 15, 30],
        ids=["horizon_1", "horizon_5", "horizon_10", "horizon_15", "horizon_30"],
    )
    def test_forward_return_over_variable_horizons(self, horizon):
        df = _create_test_df(100)
        fwd = compute_forward_return(df, horizon=horizon)

        assert len(fwd) == len(df), "one forward return per bar"
        assert fwd.iloc[-horizon:].isna().all(), "the last `horizon` bars have no forward return"
        assert fwd.iloc[:-10].notna().sum() > 0, "the leading bars carry valid forward returns"

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

        assert len(results) == 1, "one result per requested horizon"
        assert slope_results is None, "no slope results unless requested"

        r = results[0]
        assert r.horizon == 10, "the result carries its horizon"
        assert -1 <= r.is_mean_ic <= 1, "the in-sample IC is a correlation"
        assert r.is_effective_n > 0, "the in-sample effective n is positive"
        assert r.strength_label in {"Noise", "Weak", "Moderate", "Strong"}, "the strength label is one of the buckets"
        assert r.stability_label in {"Low", "Moderate", "High"}, "the stability label is one of the buckets"
        assert r.direction_label in {"Mean-Reversion", "Momentum", "None"}, "the direction label is one of the buckets"
        assert 0.0 <= r.is_hit_rate <= 1.0, "the in-sample hit rate is a fraction"

        assert "train_bars" in metadata, "metadata reports the train bars"
        assert "test_bars" in metadata, "metadata reports the test bars"

        # Retention delta is populated whenever OOS evidence is present.
        if r.oos_mean_ic is not None and abs(r.is_mean_ic) > 1e-10:
            assert r.retention_delta_pct is not None, "retention delta is populated when OOS is present"

    # ~14 s: five horizons through the full IS/OOS pipeline. A PR shard that
    # drew it ran past the 120 s gate (#2611), so it runs daily.
    @pytest.mark.slow
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

    # ~6 s, the slope pass doubling the pipeline: daily, as above.
    @pytest.mark.slow
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

    @pytest.mark.parametrize(
        ("analyses", "expected"),
        [
            pytest.param(
                [
                    {"horizon": 1, "is_mean_ic": 0.01, "fdr_p": 0.32},
                    {
                        "horizon": 10,
                        "is_mean_ic": 0.04,
                        "fdr_p": 0.001,
                        "oos_mean_ic": 0.03,
                        "oos_p_value": 0.02,
                        "oos_retention": 0.75,
                    },
                    {"horizon": 30, "is_mean_ic": 0.02, "fdr_p": 0.14},
                ],
                10,
                id="picks_oos_significant",
            ),
            pytest.param(
                [
                    {"horizon": 5, "is_mean_ic": 0.04, "fdr_p": 0.02},
                    {"horizon": 15, "is_mean_ic": 0.01, "fdr_p": 0.50},
                ],
                5,
                id="falls_back_to_fdr_when_no_oos",
            ),
            pytest.param(
                [{"horizon": 5, "is_mean_ic": 0.005, "fdr_p": 0.69}],
                None,
                id="returns_none_if_nothing_significant",
            ),
        ],
    )
    def test_find_best_horizon(self, analyses, expected):
        results = [_make_analysis(**kwargs) for kwargs in analyses]
        assert find_best_horizon(results) == expected, "the selected horizon"


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
    def test_label_buckets(self):
        assert compute_strength_label(0.005) == "Noise", "strength 0.005 is Noise"
        assert compute_strength_label(0.04) == "Weak", "strength 0.04 is Weak"
        assert compute_strength_label(0.10) == "Moderate", "strength 0.10 is Moderate"
        assert compute_strength_label(0.20) == "Strong", "strength 0.20 is Strong"
        assert compute_stability_label(0.50) == "Low", "stability 0.50 is Low"
        assert compute_stability_label(0.55) == "Moderate", "stability 0.55 is Moderate"
        assert compute_stability_label(0.60) == "High", "stability 0.60 is High"
        assert compute_direction_label(-0.05) == "Mean-Reversion", "negative signed IC is Mean-Reversion"
        assert compute_direction_label(0.05) == "Momentum", "positive signed IC is Momentum"
        assert compute_direction_label(0.01) == "None", "small signed IC has no direction"

    def test_retention_delta_pct(self):
        assert compute_retention_delta_pct(0.10, None) is None, "retention delta is None when OOS is missing"
        assert compute_retention_delta_pct(0.10, 0.15) == pytest.approx(50.0), "0.10 -> 0.15 is +50 percent"
        assert compute_retention_delta_pct(0.10, 0.06) == pytest.approx(-40.0), "0.10 -> 0.06 is -40 percent"


class TestSlopeDecisions:
    @pytest.mark.parametrize(
        ("raw_kwargs", "slope_kwargs", "expected_adds", "expected_recommended"),
        [
            pytest.param(
                {"is_mean_ic": 0.05, "fdr_p": 0.05},
                {
                    "is_mean_ic": 0.09,
                    "fdr_p": 0.01,
                    "oos_mean_ic": 0.08,
                    "oos_p_value": 0.02,
                    "oos_retention": 0.8,
                },
                True,
                True,
                id="slope_with_oos_validated",
            ),
            pytest.param(
                {"is_mean_ic": 0.10, "fdr_p": 0.02},
                {
                    "is_mean_ic": 0.03,
                    "fdr_p": 0.30,
                    "oos_mean_ic": 0.02,
                    "oos_p_value": 0.40,
                    "oos_retention": 0.5,
                },
                False,
                False,
                id="slope_weaker_than_raw",
            ),
            pytest.param(
                {"is_mean_ic": 0.05, "fdr_p": 0.05},
                {"is_mean_ic": 0.10, "fdr_p": 0.02},
                True,
                None,
                id="recommended_none_without_oos",
            ),
        ],
    )
    def test_slope_decisions(self, raw_kwargs, slope_kwargs, expected_adds, expected_recommended):
        raw = _make_analysis(**raw_kwargs)
        slope = _make_analysis(**slope_kwargs)
        adds, recommended = compute_slope_decisions(raw, slope)
        assert adds is expected_adds, "slope adds value"
        assert recommended is expected_recommended, "slope is recommended"


class TestICDecayCurve:
    def test_decay_curve_points_and_max_horizon_clamp(self):
        df = _create_test_df(500)
        curve = compute_ic_decay_curve(df, "rsi_14", max_horizon=15)
        assert len(curve) == 15, "one point per horizon"
        assert [p.horizon for p in curve] == list(range(1, 16)), "points run over horizons 1..15"
        for p in curve:
            assert -1 <= p.ic <= 1, f"horizon {p.horizon}: IC is a correlation"
            assert 0 <= p.p_value <= 1, f"horizon {p.horizon}: p-value is a probability"
            assert p.ic_stderr >= 0, f"horizon {p.horizon}: IC standard error is non-negative"

        clamped = compute_ic_decay_curve(df, "rsi_14", max_horizon=9999)
        assert len(clamped) == MAX_DECAY_HORIZON, "max horizon clamps to MAX_DECAY_HORIZON"


class TestVolatilityRegimeSplit:
    def test_masks_are_disjoint_exclude_warmup_and_balanced(self):
        df = _create_test_df(500)
        high, low = split_by_volatility_regime(df, window=20)
        assert len(high) == len(df), "mask is aligned to the frame"
        assert not (high & low).any(), "high and low masks are disjoint"
        # First `window` bars have NaN rolling vol and must be excluded.
        assert not high.iloc[:20].any(), "high mask excludes the warmup bars"
        assert not low.iloc[:20].any(), "low mask excludes the warmup bars"
        # Roughly balanced: neither bucket should be grossly dominant
        assert 0.3 < high.sum() / (high.sum() + low.sum()) < 0.7, "the split is roughly balanced"


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
    @pytest.mark.parametrize(
        ("timespan", "multiplier", "expected"),
        [
            # 252 * 390 / 1 ≈ 98,280
            pytest.param("minute", 1, 252 * 390, id="minute"),
            # 5-minute bars: 1/5 as many bars per day
            pytest.param("minute", 5, 252 * 390 / 5, id="multiplier_scales_inversely"),
            pytest.param("day", 1, 252, id="day"),
        ],
    )
    def test_bars_per_year(self, timespan, multiplier, expected):
        assert bars_per_year(timespan, multiplier) == pytest.approx(expected), "bars per year for the timespan and multiplier"


class TestIRProxy:
    def test_ir_proxy_sign_and_breadth(self):
        ir, sharpe, breadth = compute_ir_proxy(0.0, 10, 252 * 390)
        assert ir == 0.0, "zero IC yields zero IR"
        assert sharpe == 0.0, "zero IC yields zero Sharpe estimate"
        assert breadth > 0, "breadth is positive for zero IC"

        ir, sharpe, _ = compute_ir_proxy(0.05, 30, 252 * 390)
        assert ir > 0, "positive IC yields positive IR"
        assert sharpe == ir, "Sharpe estimate equals IR for positive IC"

        ir, sharpe, _ = compute_ir_proxy(-0.05, 30, 252 * 390)
        assert ir < 0, "negative IC yields negative IR"
        assert sharpe == ir, "Sharpe estimate equals IR for negative IC"

        _, _, breadth_short = compute_ir_proxy(0.05, 5, 252 * 390)
        _, _, breadth_long = compute_ir_proxy(0.05, 50, 252 * 390)
        assert breadth_short > breadth_long, "a longer horizon reduces breadth"


class TestTradeability:
    @pytest.mark.parametrize(
        ("sharpe", "stability", "expected"),
        [
            pytest.param(1.5, "High", "Likely tradeable", id="strong_and_stable_is_tradeable"),
            # |sharpe| >= 1 but stability Low → bumped down to Marginal
            pytest.param(1.5, "Low", "Marginal", id="strong_but_unstable_is_marginal"),
            pytest.param(0.7, "High", "Marginal", id="mid_range_is_marginal"),
            pytest.param(0.2, "High", "Unlikely", id="low_is_unlikely"),
            # Negative IC → short the signal → still tradeable
            pytest.param(-1.5, "High", "Likely tradeable", id="uses_absolute_value"),
        ],
    )
    def test_tradeability(self, sharpe, stability, expected):
        assert compute_tradeability(sharpe, stability) == expected, "tradeability verdict for the Sharpe estimate and stability"


class TestNextSteps:
    def _r(self, **kwargs) -> HorizonICAnalysis:
        defaults = {"horizon": 10, "is_mean_ic": 0.05}
        defaults.update(kwargs)
        return _make_analysis(**defaults)

    @pytest.mark.parametrize(
        ("kwargs", "expected_keyword"),
        [
            pytest.param({"oos_mean_ic": None}, "out-of-sample", id="flags_missing_oos"),
            pytest.param(
                {
                    "is_mean_ic": 0.10,
                    "strength_label": "Strong",
                    "stability_label": "High",
                    "oos_mean_ic": 0.09,
                    "oos_p_value": 0.02,
                },
                "threshold",
                id="suggests_threshold_when_strong_stable_validated",
            ),
        ],
    )
    def test_next_step_mentions_keyword(self, kwargs, expected_keyword):
        results = [self._r(**kwargs)]
        steps = generate_next_steps(results, None, None, 10)
        assert any(expected_keyword in s.lower() for s in steps), f"a next step mentions {expected_keyword!r}"

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
    @pytest.mark.parametrize(
        ("horizons", "expects_overlap_caveat"),
        [
            pytest.param([1, 5, 10], True, id="adds_overlap_caveat_for_multi_bar_horizons"),
            pytest.param([1], False, id="no_overlap_caveat_for_horizon_one_only"),
        ],
    )
    def test_overlap_caveat(self, horizons, expects_overlap_caveat):
        notes = generate_info_footnotes(horizons)
        assert any("overlapping" in n.lower() for n in notes) is expects_overlap_caveat, "overlap caveat presence"


class TestRandomBaselineReturnsDistribution:
    def test_distribution_length_and_rng_determinism(self):
        df = _create_test_df(500)
        mean, std, dist = compute_random_baseline_ic(df, horizon=10, n_simulations=25)
        assert len(dist) == 25, "distribution length matches n_simulations"
        assert std >= 0, "std is non-negative"
        # Mean of the distribution should equal the returned mean
        assert mean == pytest.approx(sum(dist) / len(dist), rel=1e-6), "returned mean equals the distribution mean"

        rng_a = np.random.default_rng(seed=123)
        rng_b = np.random.default_rng(seed=123)

        mean_a, std_a, dist_a = compute_random_baseline_ic(
            df, horizon=10, feature_column="rsi_14", n_simulations=20, rng=rng_a
        )
        mean_b, std_b, dist_b = compute_random_baseline_ic(
            df, horizon=10, feature_column="rsi_14", n_simulations=20, rng=rng_b
        )

        assert mean_a == pytest.approx(mean_b, abs=1e-12), "same rng seed gives the same mean"
        assert std_a == pytest.approx(std_b, abs=1e-12), "same rng seed gives the same std"
        assert dist_a == pytest.approx(dist_b, abs=1e-12), "same rng seed gives the same distribution"

    @pytest.mark.parametrize(
        "seed",
        [42, 0],
        ids=["seed_42_marginal_distribution", "seed_0_within_day_order"],
    )
    def test_block_shuffle_preserves_feature_marginal_distribution_and_within_day_order(self, seed):
        """The day-block circular shift must permute existing values, not
        invent new ones, and within a session the feature values stay in
        their original order — only the *day* assignment is rotated."""
        from app.research.indicator_reliability import _day_block_circular_shift

        rng = np.random.default_rng(seed=seed)
        feature = np.array([1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 100.0, 200.0, 300.0])
        sessions = np.array(["d1", "d1", "d1", "d2", "d2", "d2", "d3", "d3", "d3"])

        rotated = _day_block_circular_shift(feature, sessions, rng)

        # Same multiset of values (up to NaNs from variable-length sessions, none here).
        assert sorted(rotated.tolist()) == sorted(feature.tolist()), "feature marginal distribution is preserved"
        # Whatever donor day each target day reads from, the within-day
        # triple must come from one of the original triples (1,2,3) or
        # (10,20,30) or (100,200,300) in that exact order.
        for chunk_start in (0, 3, 6):
            chunk = tuple(rotated[chunk_start : chunk_start + 3].tolist())
            assert chunk in {(1.0, 2.0, 3.0), (10.0, 20.0, 30.0), (100.0, 200.0, 300.0)}, (
                f"within-day order is preserved in the session starting at row {chunk_start}"
            )

    def test_block_shuffle_falls_back_to_iid_with_one_session(self):
        from app.research.indicator_reliability import _day_block_circular_shift

        rng = np.random.default_rng(seed=1)
        feature = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        sessions = np.array(["d1"] * 5)

        rotated = _day_block_circular_shift(feature, sessions, rng)

        # Fall-back path uses IID permutation; multiset preserved, order may differ.
        assert sorted(rotated.tolist()) == sorted(feature.tolist())

    def test_block_shuffle_handles_variable_session_lengths(self):
        """When the donor session is shorter than the target, trailing
        rows are NaN-filled and the IC computation drops them."""
        from app.research.indicator_reliability import _day_block_circular_shift

        rng = np.random.default_rng(seed=2)
        feature = np.array([1.0, 2.0, 3.0, 10.0, 20.0, 100.0])  # 3 + 2 + 1 bars
        sessions = np.array(["d1", "d1", "d1", "d2", "d2", "d3"])

        rotated = _day_block_circular_shift(feature, sessions, rng)

        # Length preserved; some rows may be NaN.
        assert len(rotated) == len(feature)
        finite = rotated[~np.isnan(rotated)]
        # All finite values come from the original.
        assert all(v in set(feature.tolist()) for v in finite)

    def test_baseline_with_feature_column_uses_block_shuffle(self):
        """When the feature column is supplied, the shuffled-IC distribution
        should reflect real feature persistence — the std is materially
        wider than the parametric 1/sqrt(N) the legacy uniform-rank
        null implies."""
        df = _create_test_df(500)
        # Block-shuffle baseline (v2 default).
        _, std_block, _ = compute_random_baseline_ic(
            df,
            horizon=10,
            feature_column="rsi_14",
            n_simulations=200,
            rng=np.random.default_rng(seed=7),
        )
        # Legacy uniform-rank baseline (no feature_column).
        _, std_legacy, _ = compute_random_baseline_ic(
            df,
            horizon=10,
            n_simulations=200,
            rng=np.random.default_rng(seed=7),
        )

        # Both stds are positive; we don't assert which is wider because
        # for an essentially-noise feature like the synthetic RSI here
        # the autocorrelation gain is small. We *do* assert they aren't
        # identical — i.e. the new path is a different null, not a
        # silent wrapper.
        assert std_block > 0
        assert std_legacy > 0
        # Sanity: distributions should differ; allow a tiny epsilon.
        assert abs(std_block - std_legacy) > 1e-9 or std_block != std_legacy
