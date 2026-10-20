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
    def test_exact_recovery_at_input_strikes_and_no_negative_variance(self, flat_smile: SmileSlice) -> None:
        """Interpolation should exactly recover input vols at known strikes, and never produce negative variance."""
        fit = fit_variance_interp(flat_smile)
        assert fit.success, "variance interpolation fit succeeds"
        assert fit.method == FitMethod.VARIANCE, "fit method is variance interpolation"

        for k, iv in zip(flat_smile.strikes, flat_smile.ivs, strict=False):
            recovered = fit.volatility(float(k))
            assert abs(recovered - iv) < 1e-10, f"strike {k}: input vol is exactly recovered"
        for k in np.linspace(80, 120, 100):
            v = fit.volatility(float(k))
            assert v > 0, f"strike {k}: variance interpolation never produces negative variance"

    def test_interpolation_between_strikes(self, skewed_smile: SmileSlice) -> None:
        """Interpolated vol should be between neighboring vols."""
        fit = fit_variance_interp(skewed_smile)

        mid_k = float(skewed_smile.strikes[5] + skewed_smile.strikes[6]) / 2
        v = fit.volatility(mid_k)

        assert v > 0
        assert math.isfinite(v)


class TestSABR:
    def test_fits_flat_smile(self, flat_smile: SmileSlice) -> None:
        """SABR should fit a flat smile with low RMSE."""
        fit = fit_sabr(flat_smile)
        assert fit.success
        assert fit.method == FitMethod.SABR
        assert fit.residual_rmse < 0.01

    def test_fits_skewed_smile(self, skewed_smile: SmileSlice) -> None:
        """SABR should fit a skewed smile reasonably, with params in valid ranges and finite vols across the strike
        range."""
        fit = fit_sabr(skewed_smile)
        assert fit.success, "SABR fit of a skewed smile succeeds"
        assert fit.residual_rmse < 0.02, "SABR fits a skewed smile reasonably"  # SABR may not perfectly fit SVI-like smiles
        assert fit.params["alpha"] > 0, "fitted alpha is positive"
        assert fit.params["nu"] > 0, "fitted nu is positive"
        assert -1 < fit.params["rho"] < 1, "fitted rho is in (-1, 1)"
        assert fit.params["beta"] == 0.5, "beta stays at 0.5"
        for k in np.linspace(80, 120, 50):
            v = fit.volatility(float(k))
            assert math.isfinite(v), f"strike {k}: fitted SABR vol is finite"
            assert v > 0, f"strike {k}: fitted SABR vol is positive"


class TestSVI:
    def test_fits_flat_smile(self, flat_smile: SmileSlice) -> None:
        """SVI should fit a flat smile with low RMSE."""
        fit = fit_svi(flat_smile)
        assert fit.success
        assert fit.method == FitMethod.SVI
        assert fit.residual_rmse < 0.001

    def test_fits_skewed_smile(self, skewed_smile: SmileSlice) -> None:
        """SVI should fit a skewed smile well (SVI is designed for this), with params satisfying no-arbitrage
        constraints and finite vols across the strike range."""
        fit = fit_svi(skewed_smile)
        assert fit.success, "SVI fit of a skewed smile succeeds"
        assert fit.residual_rmse < 0.005, "SVI fits a skewed smile well"  # SVI should fit its own shape very well
        p = fit.params
        assert p["b"] > 0, "fitted b is positive"
        assert p["sigma"] > 0, "fitted sigma is positive"
        assert -1 < p["rho"] < 1, "fitted rho is in (-1, 1)"
        # SVI should satisfy butterfly no-arb: a + b*sigma*sqrt(1-rho^2) >= 0
        constraint = p["a"] + p["b"] * p["sigma"] * math.sqrt(1 - p["rho"] ** 2)
        assert constraint >= -1e-6, "butterfly constraint a + b*sigma*sqrt(1-rho^2) >= 0 holds"
        for k in np.linspace(80, 120, 50):
            v = fit.volatility(float(k))
            assert math.isfinite(v), f"strike {k}: fitted SVI vol is finite"
            assert v > 0, f"strike {k}: fitted SVI vol is positive"


def _vol_fn_failing_at_the_money(strike: float) -> float:
    if strike == 100.0:
        raise ValueError("no vol at the money")
    return 0.25


class TestArbitrageCheck:
    def test_flat_smile_passes(self, flat_smile: SmileSlice) -> None:
        """Flat smile should pass all arbitrage checks."""
        fit = fit_variance_interp(flat_smile)
        report = check_smile_arbitrage(fit, flat_smile.strikes, flat_smile.forward)
        assert report.passed
        assert report.butterfly_violations == 0
        assert report.negative_variance == 0

    def test_w_shaped_smile_violates_butterfly_however_the_strikes_arrive(self) -> None:
        """A deliberately pathological W-shaped smile (concavity at center) triggers a butterfly violation. Convexity
        is a statement about ascending log-moneyness, so the checker sorts: the order the caller happened to collect
        strikes in must not hide the violation. A chain carries one record per call and put, so each strike arrives
        twice; the repeats collapse and must not skip every triple."""
        ordered = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        shuffled = np.array([100.0, 90.0, 110.0, 95.0, 105.0])
        ivs = np.array([0.30, 0.20, 0.35, 0.20, 0.30])  # W-shaped smile
        fit = fit_variance_interp(SmileSlice(strikes=ordered, ivs=ivs, ttm=0.25, forward=100.0))

        sorted_report = check_smile_arbitrage(fit, ordered, 100.0)
        shuffled_report = check_smile_arbitrage(fit, shuffled, 100.0)
        doubled_report = check_smile_arbitrage(fit, np.repeat(ordered, 2), 100.0)

        assert sorted_report.butterfly_violations > 0, "a W-shaped smile triggers butterfly violations"
        assert not sorted_report.passed, "a W-shaped smile does not pass"
        assert sorted_report.butterfly_violations == 1, "sorted strikes report exactly one butterfly violation"
        assert shuffled_report.butterfly_violations == sorted_report.butterfly_violations, (
            "shuffled strikes report the same violations as sorted"
        )
        assert not shuffled_report.passed, "shuffled strikes do not hide the violation"
        assert not doubled_report.passed, "a call and a put at every strike do not pass"
        assert doubled_report.butterfly_violations == 1, "a call and a put at every strike report the one violation"
        assert doubled_report.total_checks == len(ordered), "a call and a put at every strike are checked once each"

    @pytest.mark.parametrize(
        ("vol_fn", "unevaluable"),
        [
            pytest.param(None, 5, id="no_vol_function"),
            pytest.param(lambda strike: float("nan"), 5, id="nan_vol_function"),
            pytest.param(_vol_fn_failing_at_the_money, 1, id="single_unevaluable_strike"),
        ],
    )
    def test_fit_that_cannot_price_strikes_does_not_pass(self, vol_fn, unevaluable: int) -> None:
        """No evaluable vol means nothing was checked, which is not 'passed'. A convex smile with one strike the fit
        cannot price leaves that strike's triples unchecked, so the report fails closed."""
        strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        fit = FitResult(method=FitMethod.SABR, ttm=0.25, _vol_fn=vol_fn)

        report = check_smile_arbitrage(fit, strikes, 100.0)

        assert report.unevaluable == unevaluable, "every strike the fit cannot price is counted as unevaluable"
        assert report.total_checks == len(strikes), "every strike is counted as a check"
        assert report.butterfly_violations == 0, "no violation is claimed on strikes that could not be priced"
        assert not report.passed, "the report fails closed"

    @pytest.mark.parametrize(
        ("strikes", "total_checks"),
        [
            ((), 0),
            ((100.0,), 1),
            ((95.0, 105.0), 2),
            ((100.0, 100.0), 1),
            ((95.0, 95.0, 105.0, 105.0), 2),
        ],
        ids=["empty_strike_list", "one_strike", "two_strikes", "one_strike_twice", "two_strikes_twice"],
    )
    def test_fewer_than_three_distinct_strikes_cannot_pass(
        self, strikes: tuple[float, ...], total_checks: int, flat_smile: SmileSlice
    ) -> None:
        """No butterfly triple exists, so nothing was tested: not 'passed'."""
        report = check_smile_arbitrage(fit_variance_interp(flat_smile), np.array(strikes), flat_smile.forward)

        assert report.total_checks == total_checks, "total checks is the number of distinct strikes"
        assert not report.passed, "no butterfly triple was tested, so the report is not 'passed'"
        assert report.unevaluable == 0, "no strike is unevaluable"
        assert report.butterfly_triples_tested == 0, "no butterfly triple was tested"
        assert any("no butterfly triple" in detail for detail in report.details), "the details say why"

    def test_a_report_that_checked_nothing_does_not_read_passed(self) -> None:
        assert not ArbitrageReport().passed
