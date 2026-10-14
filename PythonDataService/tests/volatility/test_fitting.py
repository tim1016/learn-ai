"""
Tests for smile fitting methods (SABR, SVI, variance interpolation).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.volatility.fitting import (
    ArbitrageReport,
    FitMethod,
    FitResult,
    SmileSlice,
    _sabr_vol,
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


class TestSmileSliceValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"ivs": np.array([0.2, 0.2])}, "length mismatch"),
            ({"ttm": 0.0}, "ttm must be positive"),
            ({"ttm": float("nan")}, "ttm must be positive"),
            ({"forward": -1.0}, "forward must be positive"),
        ],
        ids=["length_mismatch", "zero_ttm", "nan_ttm", "negative_forward"],
    )
    def test_invalid_slice_raises_value_error(self, kwargs: dict, message: str) -> None:
        """An explicit raise, so `python -O` cannot strip the guard (assert would)."""
        args = {"strikes": np.array([90.0, 100.0, 110.0]), "ivs": np.full(3, 0.2), "ttm": 0.25, "forward": 100.0}
        with pytest.raises(ValueError, match=message):
            SmileSlice(**{**args, **kwargs})


class TestSabrVol:
    @pytest.mark.parametrize("offset", [-1e-9, -5e-10, -2e-10, 2e-10, 5e-10, 1e-9])
    def test_strike_just_off_the_money_is_the_atm_limit(self, offset: float) -> None:
        """z / x(z) -> 1 as K -> F, so the smile is continuous through the money.

        Hagan et al. (2002) eq. (2.18) is the closed-form ATM limit. At |F-K|
        just above the 1e-10 ATM switch, ``x_z`` is below its own guard and the
        ratio must be the limit 1, not ``z`` (which put the vol at ~1e-13).
        Tolerance abs=1e-9 on a vol of 0.03: the observed error is below 1e-11.
        """
        forward, ttm, alpha, beta, rho, nu = 100.0, 0.25, 0.3, 0.5, -0.3, 0.4

        f1b = forward ** (1.0 - beta)
        closed_form = (alpha / f1b) * (
            1.0
            + ((1.0 - beta) ** 2 / 24.0 * alpha**2 / f1b**2 + rho * beta * nu * alpha / (4.0 * f1b) + (2.0 - 3.0 * rho**2) / 24.0 * nu**2)
            * ttm
        )

        vol = _sabr_vol(forward + offset, forward, ttm, alpha, beta, rho, nu)

        assert vol == pytest.approx(closed_form, abs=1e-9)


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

    def test_shuffled_strikes_report_the_same_violations_as_sorted(self) -> None:
        """Convexity is a statement about ascending log-moneyness, so the
        checker sorts: the order the caller happened to collect strikes in
        must not hide a butterfly violation."""
        ordered = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        shuffled = np.array([100.0, 90.0, 110.0, 95.0, 105.0])
        ivs = np.array([0.30, 0.20, 0.35, 0.20, 0.30])  # W-shaped smile
        fit = fit_variance_interp(SmileSlice(strikes=ordered, ivs=ivs, ttm=0.25, forward=100.0))

        sorted_report = check_smile_arbitrage(fit, ordered, 100.0)
        shuffled_report = check_smile_arbitrage(fit, shuffled, 100.0)

        assert sorted_report.butterfly_violations == 1
        assert shuffled_report.butterfly_violations == sorted_report.butterfly_violations
        assert not shuffled_report.passed

    @pytest.mark.parametrize(
        "vol_fn",
        [None, lambda strike: float("nan")],
        ids=["no_vol_function", "nan_vol_function"],
    )
    def test_fit_that_evaluates_nowhere_does_not_pass(self, vol_fn) -> None:
        """No evaluable vol means nothing was checked, which is not 'passed'."""
        strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        fit = FitResult(method=FitMethod.SABR, ttm=0.25, _vol_fn=vol_fn)

        report = check_smile_arbitrage(fit, strikes, 100.0)

        assert report.unevaluable == len(strikes)
        assert report.total_checks == len(strikes)
        assert not report.passed

    def test_a_single_unevaluable_strike_does_not_pass(self) -> None:
        """A convex smile with one strike the fit cannot price leaves that
        strike's triples unchecked, so the report fails closed."""
        strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])

        def vol_fn(strike: float) -> float:
            if strike == 100.0:
                raise ValueError("no vol at the money")
            return 0.25

        report = check_smile_arbitrage(FitResult(method=FitMethod.SABR, ttm=0.25, _vol_fn=vol_fn), strikes, 100.0)

        assert report.unevaluable == 1
        assert report.butterfly_violations == 0
        assert not report.passed

    def test_empty_strike_list_does_not_pass(self, flat_smile: SmileSlice) -> None:
        report = check_smile_arbitrage(fit_variance_interp(flat_smile), np.array([]), flat_smile.forward)

        assert report.total_checks == 0
        assert not report.passed

    def test_a_call_and_a_put_at_every_strike_are_checked_once_each(self) -> None:
        """A chain carries one record per call and put, so each strike arrives
        twice. The repeats collapse; they must not skip every triple."""
        ordered = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        ivs = np.array([0.30, 0.20, 0.35, 0.20, 0.30])  # W-shaped smile
        fit = fit_variance_interp(SmileSlice(strikes=ordered, ivs=ivs, ttm=0.25, forward=100.0))

        report = check_smile_arbitrage(fit, np.repeat(ordered, 2), 100.0)

        assert not report.passed
        assert report.butterfly_violations == 1
        assert report.total_checks == len(ordered)

    @pytest.mark.parametrize(
        "strikes",
        [[100.0], [95.0, 105.0], [100.0, 100.0], [95.0, 95.0, 105.0, 105.0]],
        ids=["one_strike", "two_strikes", "one_strike_twice", "two_strikes_twice"],
    )
    def test_fewer_than_three_distinct_strikes_cannot_pass(self, strikes: list[float], flat_smile: SmileSlice) -> None:
        """No butterfly triple exists, so nothing was tested: not 'passed'."""
        report = check_smile_arbitrage(fit_variance_interp(flat_smile), np.array(strikes), flat_smile.forward)

        assert not report.passed
        assert report.unevaluable == 0
        assert report.butterfly_triples_tested == 0
        assert any("no butterfly triple" in detail for detail in report.details)

    def test_a_report_that_checked_nothing_does_not_read_passed(self) -> None:
        assert not ArbitrageReport().passed
