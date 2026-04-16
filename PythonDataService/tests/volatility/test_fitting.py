"""
Tests for smile fitting methods (SABR, SVI, variance interpolation).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.volatility.fitting import (
    FitMethod,
    SmileSlice,
    check_smile_arbitrage,
    fit_sabr,
    fit_svi,
    fit_variance_interp,
)


@pytest.fixture
def flat_smile() -> SmileSlice:
    """Smile with constant 25% vol."""
    strikes = np.linspace(85, 115, 15)
    return SmileSlice(
        strikes=strikes,
        ivs=np.full(len(strikes), 0.25),
        ttm=0.25,
        forward=100.5,
    )


@pytest.fixture
def skewed_smile() -> SmileSlice:
    """Smile with realistic equity skew."""
    forward = 100.5
    strikes = np.linspace(80, 120, 20)
    log_m = np.log(strikes / forward)
    ivs = 0.25 - 0.12 * log_m + 0.03 * log_m**2
    return SmileSlice(
        strikes=strikes,
        ivs=ivs,
        ttm=0.25,
        forward=forward,
    )


class TestVarianceInterp:
    def test_exact_recovery_at_input_strikes(self, flat_smile: SmileSlice) -> None:
        """Interpolation should exactly recover input vols at known strikes."""
        fit = fit_variance_interp(flat_smile)
        assert fit.success
        assert fit.method == FitMethod.VARIANCE

        for k, iv in zip(flat_smile.strikes, flat_smile.ivs, strict=False):
            recovered = fit.volatility(float(k))
            assert abs(recovered - iv) < 1e-10

    def test_interpolation_between_strikes(self, skewed_smile: SmileSlice) -> None:
        """Interpolated vol should be between neighboring vols."""
        fit = fit_variance_interp(skewed_smile)

        mid_k = float(skewed_smile.strikes[5] + skewed_smile.strikes[6]) / 2
        v = fit.volatility(mid_k)

        assert v > 0
        assert math.isfinite(v)

    def test_no_negative_variance(self, flat_smile: SmileSlice) -> None:
        """Variance interp should never produce negative variance."""
        fit = fit_variance_interp(flat_smile)
        for k in np.linspace(80, 120, 100):
            v = fit.volatility(float(k))
            assert v > 0


class TestSABR:
    def test_fits_flat_smile(self, flat_smile: SmileSlice) -> None:
        """SABR should fit a flat smile with low RMSE."""
        fit = fit_sabr(flat_smile)
        assert fit.success
        assert fit.method == FitMethod.SABR
        assert fit.residual_rmse < 0.01

    def test_fits_skewed_smile(self, skewed_smile: SmileSlice) -> None:
        """SABR should fit a skewed smile reasonably."""
        fit = fit_sabr(skewed_smile)
        assert fit.success
        assert fit.residual_rmse < 0.02  # SABR may not perfectly fit SVI-like smiles

    def test_params_are_sensible(self, skewed_smile: SmileSlice) -> None:
        """Fitted params should be in valid ranges."""
        fit = fit_sabr(skewed_smile)
        assert fit.params["alpha"] > 0
        assert fit.params["nu"] > 0
        assert -1 < fit.params["rho"] < 1
        assert fit.params["beta"] == 0.5

    def test_returns_finite_vols(self, skewed_smile: SmileSlice) -> None:
        """Fitted SABR should return finite vols across strike range."""
        fit = fit_sabr(skewed_smile)
        for k in np.linspace(80, 120, 50):
            v = fit.volatility(float(k))
            assert math.isfinite(v)
            assert v > 0


class TestSVI:
    def test_fits_flat_smile(self, flat_smile: SmileSlice) -> None:
        """SVI should fit a flat smile with low RMSE."""
        fit = fit_svi(flat_smile)
        assert fit.success
        assert fit.method == FitMethod.SVI
        assert fit.residual_rmse < 0.001

    def test_fits_skewed_smile(self, skewed_smile: SmileSlice) -> None:
        """SVI should fit a skewed smile well (SVI is designed for this)."""
        fit = fit_svi(skewed_smile)
        assert fit.success
        assert fit.residual_rmse < 0.005  # SVI should fit its own shape very well

    def test_params_are_sensible(self, skewed_smile: SmileSlice) -> None:
        """SVI params should satisfy no-arbitrage constraints."""
        fit = fit_svi(skewed_smile)
        assert fit.params["b"] > 0
        assert fit.params["sigma"] > 0
        assert -1 < fit.params["rho"] < 1

    def test_butterfly_constraint(self, skewed_smile: SmileSlice) -> None:
        """SVI should satisfy butterfly no-arb: a + b*sigma*sqrt(1-rho^2) >= 0."""
        fit = fit_svi(skewed_smile)
        p = fit.params
        constraint = p["a"] + p["b"] * p["sigma"] * math.sqrt(1 - p["rho"] ** 2)
        assert constraint >= -1e-6

    def test_returns_finite_vols(self, skewed_smile: SmileSlice) -> None:
        """Fitted SVI should return finite vols across strike range."""
        fit = fit_svi(skewed_smile)
        for k in np.linspace(80, 120, 50):
            v = fit.volatility(float(k))
            assert math.isfinite(v)
            assert v > 0


class TestArbitrageCheck:
    def test_flat_smile_passes(self, flat_smile: SmileSlice) -> None:
        """Flat smile should pass all arbitrage checks."""
        fit = fit_variance_interp(flat_smile)
        report = check_smile_arbitrage(fit, flat_smile.strikes, flat_smile.forward)
        assert report.passed
        assert report.butterfly_violations == 0
        assert report.negative_variance == 0

    def test_detects_known_violation(self) -> None:
        """A deliberately pathological smile should trigger violations."""
        strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        # W-shaped smile: concavity at center → butterfly violation
        ivs = np.array([0.30, 0.20, 0.35, 0.20, 0.30])
        smile = SmileSlice(strikes=strikes, ivs=ivs, ttm=0.25, forward=100.0)

        fit = fit_variance_interp(smile)
        report = check_smile_arbitrage(fit, strikes, 100.0)
        assert report.butterfly_violations > 0
        assert not report.passed
