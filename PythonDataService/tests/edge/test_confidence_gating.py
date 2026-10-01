"""Tests for the confidence module + its Step E integration.

These tests pin:

1. The formula itself (multiplicative, clamped to [0, 1]).
2. The continuous gating in ``vrp_signal`` (back-compat when no
   confidence supplied; scaled gating + hard floor when supplied).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.confidence import (
    DEFAULT_CONFIDENCE_FLOOR,
    compute_confidence,
    confidence_with_explanation,
)
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
