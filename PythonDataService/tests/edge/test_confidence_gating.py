"""Tests for the shared confidence module + Step E/F integration.

The confidence formula is the single source of truth shared between the
VRP signal generator (Step E) and the regime classifier (Step F). These
tests pin:

1. The formula itself (multiplicative, clamped to [0, 1]).
2. The continuous gating in ``vrp_signal`` (back-compat when no
   confidence supplied; scaled gating + hard floor when supplied).
3. The ``regime_feature_weight`` ramp (zero at health=0.5, one at 1.0).
4. ``build_full_features`` honors ``iv_feature_weight`` by scaling
   IV-derived columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.confidence import (
    DEFAULT_CONFIDENCE_FLOOR,
    compute_confidence,
    confidence_with_explanation,
    regime_feature_weight,
)
from app.engine.edge.features_realtime.regime_features import build_full_features
from app.engine.edge.vrp import vrp_signal


class TestConfidenceFormula:
    @pytest.mark.parametrize(
        "h,vcs,expected",
        [
            (1.0, 0.0, 1.0),
            (0.5, 0.5, 0.25),
            (1.0, 1.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.8, 0.2, 0.64),
        ],
    )
    def test_compute_confidence_values(self, h, vcs, expected):
        assert compute_confidence(
            health_score=h, variance_contribution_synthetic=vcs
        ) == pytest.approx(expected)

    def test_compute_confidence_clamps_inputs(self):
        # Out-of-range inputs are clamped, not propagated.
        c = compute_confidence(health_score=1.5, variance_contribution_synthetic=-0.1)
        assert c == pytest.approx(1.0)
        c = compute_confidence(health_score=-0.5, variance_contribution_synthetic=2.0)
        assert c == 0.0

    def test_explanation_returns_none_when_above_floor(self):
        b = confidence_with_explanation(
            health_score=0.9, variance_contribution_synthetic=0.0
        )
        assert b.confidence == pytest.approx(0.9)
        assert b.reason is None

    def test_explanation_synthetic_dominant(self):
        b = confidence_with_explanation(
            health_score=0.95, variance_contribution_synthetic=0.95,
            floor=DEFAULT_CONFIDENCE_FLOOR,
        )
        # 0.95 * 0.05 = 0.0475 < 0.1 → gated
        assert b.confidence < DEFAULT_CONFIDENCE_FLOOR
        assert "synthetic" in b.reason.lower()

    def test_explanation_health_dominant(self):
        b = confidence_with_explanation(
            health_score=0.1, variance_contribution_synthetic=0.0,
            floor=DEFAULT_CONFIDENCE_FLOOR,
        )
        # 0.1 * 1.0 = 0.1; equality with floor — not gated.
        # Drop slightly to gate it.
        b = confidence_with_explanation(
            health_score=0.05, variance_contribution_synthetic=0.0,
            floor=DEFAULT_CONFIDENCE_FLOOR,
        )
        assert b.confidence < DEFAULT_CONFIDENCE_FLOOR
        assert "unstable" in b.reason.lower()


class TestRegimeFeatureWeight:
    def test_health_at_half_yields_zero_weight(self):
        # The ramp is max(0, 2h - 1); h=0.5 → 0
        w = regime_feature_weight(health_score=0.5, variance_contribution_synthetic=0.0)
        assert w == 0.0

    def test_health_at_one_with_zero_synth_yields_one(self):
        w = regime_feature_weight(health_score=1.0, variance_contribution_synthetic=0.0)
        assert w == pytest.approx(1.0)

    def test_synthetic_share_attenuates_linearly(self):
        # h=1.0, vcs=0.5 → max(0, 1) * 0.5 = 0.5
        w = regime_feature_weight(health_score=1.0, variance_contribution_synthetic=0.5)
        assert w == pytest.approx(0.5)

    def test_health_below_half_clamps_to_zero(self):
        w = regime_feature_weight(health_score=0.3, variance_contribution_synthetic=0.0)
        assert w == 0.0


class TestVrpSignalContinuousGating:
    """Backward-compat first, then the continuous path."""

    def _make_iv_rv(self, n: int = 300):
        rng = np.random.default_rng(seed=42)
        idx = pd.RangeIndex(n)
        # IV around 20% with slight drift; RV around 18% with noise.
        iv = pd.Series(0.20 + rng.normal(0, 0.005, n).cumsum() * 0.001, index=idx)
        rv = pd.Series(0.18 + rng.normal(0, 0.01, n), index=idx)
        return iv.abs(), rv.abs()

    def test_backward_compat_no_confidence(self):
        iv, rv = self._make_iv_rv()
        sig = vrp_signal(iv=iv, rv=rv, lookback=100)
        # Old fields populated, new fields None.
        assert sig.confidence is None
        assert sig.vrp_z_scaled is None
        assert sig.floor_gated is None
        assert sig.side.dtype == int

    def test_full_confidence_matches_legacy_when_threshold_unchanged(self):
        iv, rv = self._make_iv_rv()
        sig_legacy = vrp_signal(iv=iv, rv=rv, lookback=100)
        # confidence = 1.0 across the board → z_scaled == z
        conf = pd.Series(1.0, index=iv.index)
        sig_full = vrp_signal(iv=iv, rv=rv, lookback=100, confidence=conf)
        # Sides match where legacy is non-zero (continuous path uses scaled-z;
        # at confidence=1, scaled = raw, so threshold logic is identical).
        # Allow disagreement only where the legacy used > vs. >= (boundary).
        agreed = (sig_legacy.side == sig_full.side).sum()
        assert agreed >= len(iv) - 5  # within a handful of boundary cases

    def test_below_floor_forces_zero(self):
        iv, rv = self._make_iv_rv()
        # Confidence below floor everywhere → no actions even with extreme z.
        conf = pd.Series(0.05, index=iv.index)
        sig = vrp_signal(
            iv=iv, rv=rv, lookback=100, confidence=conf,
            confidence_floor=DEFAULT_CONFIDENCE_FLOOR,
        )
        assert (sig.side == 0).all()
        assert sig.floor_gated.all()

    def test_low_confidence_attenuates_signal(self):
        iv, rv = self._make_iv_rv()
        conf_high = pd.Series(1.0, index=iv.index)
        conf_low = pd.Series(0.3, index=iv.index)  # > floor (0.1) but degraded
        sig_high = vrp_signal(iv=iv, rv=rv, lookback=100, confidence=conf_high)
        sig_low = vrp_signal(iv=iv, rv=rv, lookback=100, confidence=conf_low)
        # Low confidence should fire fewer (or equal) actions because
        # |z * 0.3| < |z * 1.0|, so threshold is harder to hit.
        n_actions_high = (sig_high.side != 0).sum()
        n_actions_low = (sig_low.side != 0).sum()
        assert n_actions_low <= n_actions_high


class TestBuildFullFeaturesWeighted:
    def _make_bars(self, n: int = 200):
        rng = np.random.default_rng(seed=11)
        idx = pd.RangeIndex(n)
        close = 100 + rng.normal(0, 1, n).cumsum()
        bars = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1000 + rng.integers(0, 100, n),
            },
            index=idx,
        )
        iv30 = pd.Series(0.20 + rng.normal(0, 0.005, n).cumsum() * 0.001, index=idx).abs()
        return bars, iv30

    def test_weight_one_unchanged_vs_no_weight(self):
        bars, iv30 = self._make_bars()
        a = build_full_features(bars, iv30=iv30)
        b = build_full_features(bars, iv30=iv30, iv_feature_weight=1.0)
        # IV-derived columns should match exactly when weight=1.
        for col in ("iv30_z", "d_iv_z", "iv_vol_z"):
            mask = a[col].notna() & b[col].notna()
            np.testing.assert_allclose(
                a.loc[mask, col].to_numpy(),
                b.loc[mask, col].to_numpy(),
                atol=1e-12, rtol=0,
            )

    def test_weight_zero_collapses_iv_features(self):
        bars, iv30 = self._make_bars()
        feats = build_full_features(bars, iv30=iv30, iv_feature_weight=0.0)
        # Where the underlying value would have been non-NaN, it is now exactly 0.
        # NaN region is unchanged (warmup).
        for col in ("iv30_z", "d_iv_z", "iv_vol_z"):
            non_nan = feats[col].dropna()
            assert (non_nan == 0.0).all()

    def test_weight_half_scales_linearly(self):
        bars, iv30 = self._make_bars()
        full = build_full_features(bars, iv30=iv30)
        half = build_full_features(bars, iv30=iv30, iv_feature_weight=0.5)
        for col in ("iv30_z", "d_iv_z", "iv_vol_z"):
            mask = full[col].notna() & half[col].notna()
            np.testing.assert_allclose(
                half.loc[mask, col].to_numpy(),
                full.loc[mask, col].to_numpy() * 0.5,
                atol=1e-12, rtol=0,
            )

    def test_per_bar_weight_series(self):
        bars, iv30 = self._make_bars()
        # First half weight=1.0, second half weight=0.0
        n = len(bars)
        weight = pd.Series([1.0] * (n // 2) + [0.0] * (n - n // 2), index=bars.index)
        feats = build_full_features(bars, iv30=iv30, iv_feature_weight=weight)
        # First half non-zero (where warmup permits), second half zero.
        first_half_iv30_z = feats["iv30_z"].iloc[: n // 2].dropna()
        second_half_iv30_z = feats["iv30_z"].iloc[n // 2 :].dropna()
        assert (second_half_iv30_z == 0.0).all()
        # First half should have at least some non-zero values.
        assert (first_half_iv30_z != 0.0).any()

    def test_ohlcv_features_untouched_by_iv_weight(self):
        bars, iv30 = self._make_bars()
        a = build_full_features(bars, iv30=iv30, iv_feature_weight=1.0)
        b = build_full_features(bars, iv30=iv30, iv_feature_weight=0.0)
        for col in ("trend_slope_z", "rv_yz_z", "atr_pct_z", "volume_z_z"):
            mask = a[col].notna() & b[col].notna()
            np.testing.assert_allclose(
                a.loc[mask, col].to_numpy(),
                b.loc[mask, col].to_numpy(),
                atol=1e-12, rtol=0,
            )
