"""
Tests for app.volatility.analytics module.

Tests health scores, skew metrics, delta strikes, and put-call parity.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest

from app.volatility.analytics import (
    compute_health_score,
    compute_put_call_parity_forward,
    compute_skew_metrics,
    find_delta_strike,
)
from app.volatility.fitting import (
    ArbitrageReport,
    FitMethod,
    FitResult,
    SmileSlice,
    check_smile_arbitrage,
    fit_variance_interp,
)
from app.volatility.surface import (
    SliceDiagnostics,
    SurfaceDiagnostics,
    SurfaceMethod,
    VolSurface,
    VolSurfaceBuilder,
)
from tests.volatility.conftest import bs_price


@pytest.fixture
def built_surface(spot: float, rate: float, skewed_chain: list[dict]) -> VolSurface:
    """Build a VolSurface from skewed option chain for analytics tests."""
    builder = VolSurfaceBuilder(spot=spot, rate=rate)
    return builder.build(skewed_chain, method=SurfaceMethod.SVI)


class TestHealthScore:
    """Surface health score tests."""

    def test_health_score_range(self, built_surface: VolSurface) -> None:
        """Total health score is between 0 and 100."""
        score = compute_health_score(built_surface)

        assert 0 <= score.total <= 100

    def test_health_score_components_in_range(self, built_surface: VolSurface) -> None:
        """All 4 component scores are between 0 and 100."""
        score = compute_health_score(built_surface)

        assert 0 <= score.convergence_score <= 100
        assert 0 <= score.rmse_score <= 100
        assert 0 <= score.rejection_score <= 100
        assert 0 <= score.arbitrage_score <= 100

    def test_health_score_total_is_average(self, built_surface: VolSurface) -> None:
        """Total is approximately the average of the 4 components."""
        score = compute_health_score(built_surface)

        avg = (score.convergence_score + score.rmse_score + score.rejection_score + score.arbitrage_score) / 4.0

        assert abs(score.total - avg) < 1.0  # Allow rounding tolerance

    def test_health_score_good_surface(self, built_surface: VolSurface) -> None:
        """Exact Black-Scholes prices off a smooth skew earn full marks: every contract solves, the fit RMSE is
        under 0.005, none is rejected and there is no arbitrage."""
        score = compute_health_score(built_surface)

        assert score.total == 100


class TestHealthScoreArbitrageCoverage:
    """A smile the arbitrage check could not test must not score as arbitrage-free."""

    @staticmethod
    def _checked_report() -> ArbitrageReport:
        strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
        smile = SmileSlice(strikes=strikes, ivs=np.full(5, 0.25), ttm=0.25, forward=100.0)
        report = check_smile_arbitrage(fit_variance_interp(smile), strikes, 100.0)
        assert report.passed
        return report

    @staticmethod
    def _unchecked_report() -> ArbitrageReport:
        return ArbitrageReport(total_checks=5, unevaluable=5)

    def _arbitrage_score(self, reports: list[ArbitrageReport | None]) -> float:
        slices = [
            SliceDiagnostics(
                ttm=0.1 * (i + 1), n_contracts=5, n_solved=5, n_failed=0, fit_method="variance", fit_rmse=0.0, arbitrage=r
            )
            for i, r in enumerate(reports)
        ]
        diagnostics = SurfaceDiagnostics(
            n_expiries=len(slices), n_total_contracts=5 * len(slices), n_total_solved=5 * len(slices), slices=slices
        )
        surface = VolSurface(method=SurfaceMethod.VARIANCE, spot=100.0, rate=0.0, dividend=0.0, diagnostics=diagnostics)
        return compute_health_score(surface).arbitrage_score

    def test_all_checked_slices_score_full(self) -> None:
        assert self._arbitrage_score([self._checked_report()] * 2) == pytest.approx(100.0, abs=1e-9)

    def test_score_is_the_checked_share_of_slices(self) -> None:
        score = self._arbitrage_score([self._checked_report(), self._unchecked_report()])

        assert score == pytest.approx(50.0, abs=1e-9)

    def test_all_unchecked_slices_score_zero(self) -> None:
        assert self._arbitrage_score([self._unchecked_report()] * 2) == pytest.approx(0.0, abs=1e-9)

    def test_a_slice_with_no_arbitrage_report_counts_as_unchecked(self) -> None:
        assert self._arbitrage_score([self._checked_report(), None]) == pytest.approx(50.0, abs=1e-9)
        assert self._arbitrage_score([None, None]) == pytest.approx(0.0, abs=1e-9)


class TestSkewMetrics:
    """Skew and smile metrics tests."""

    def test_skew_metrics_atm_iv_positive(self, built_surface: VolSurface) -> None:
        """ATM IV is positive and reasonable."""
        for fit in built_surface.fits:
            metrics = compute_skew_metrics(built_surface, fit.ttm)

            assert metrics.atm_iv > 0.0
            assert 0.01 <= metrics.atm_iv <= 2.0  # Reasonable vol range

    def test_skew_metrics_dte_days(self, built_surface: VolSurface) -> None:
        """dte_days matches round(ttm * 365)."""
        for fit in built_surface.fits:
            metrics = compute_skew_metrics(built_surface, fit.ttm)

            expected_dte = round(fit.ttm * 365)
            assert metrics.dte_days == expected_dte

    def test_skew_metrics_rr_negative_equity_skew(self, built_surface: VolSurface) -> None:
        """For equity skew, 25D RR should be negative (puts > calls)."""
        for fit in built_surface.fits:
            metrics = compute_skew_metrics(built_surface, fit.ttm)

            if metrics.rr_25d is not None:
                assert metrics.rr_25d < 0.0

    def test_skew_metrics_bf_positive(self, built_surface: VolSurface) -> None:
        """25D butterfly should be positive (smile curvature) or near zero."""
        for fit in built_surface.fits:
            metrics = compute_skew_metrics(built_surface, fit.ttm)

            if metrics.bf_25d is not None:
                assert metrics.bf_25d >= -0.001  # Allow small numerical noise

    def test_skew_metrics_puts_exist(self, built_surface: VolSurface) -> None:
        """25D put should exist in reasonable cases."""
        for fit in built_surface.fits:
            if fit.ttm >= 30 / 365:
                metrics = compute_skew_metrics(built_surface, fit.ttm)

                assert metrics.put_25d is not None

    def test_skew_metrics_calls_exist(self, built_surface: VolSurface) -> None:
        """25D call should exist in reasonable cases."""
        for fit in built_surface.fits:
            if fit.ttm >= 30 / 365:
                metrics = compute_skew_metrics(built_surface, fit.ttm)

                assert metrics.call_25d is not None


class TestSkewSlope:
    """``skew_slope`` separates a measured flat skew from an unmeasurable one."""

    @staticmethod
    def _surface(vol_fn: Callable[[float], float]) -> VolSurface:
        fit = FitResult(method=FitMethod.VARIANCE, ttm=0.25, _vol_fn=vol_fn)
        return VolSurface(method=SurfaceMethod.VARIANCE, spot=100.0, rate=0.0, dividend=0.0, fits=[fit])

    def test_flat_surface_has_zero_slope(self) -> None:
        metrics = compute_skew_metrics(self._surface(lambda strike: 0.20), 0.25)

        assert metrics.skew_slope == pytest.approx(0.0, abs=1e-12)

    def test_slope_is_unknown_when_the_surface_fails_off_the_forward(self) -> None:
        def vol_only_at_the_forward(strike: float) -> float:
            if strike != 100.0:
                raise ValueError(f"no vol at {strike}")
            return 0.20

        metrics = compute_skew_metrics(self._surface(vol_only_at_the_forward), 0.25)

        assert metrics.atm_iv == pytest.approx(0.20, abs=1e-12)
        assert metrics.skew_slope is None


class TestFindDeltaStrike:
    """Delta strike finding tests."""

    def test_find_delta_strike_call_25d(self, built_surface: VolSurface) -> None:
        """25D call strike exists and is > forward."""
        ttm = 90 / 365
        forward = built_surface.spot * math.exp(built_surface.rate * ttm)

        result = find_delta_strike(built_surface, ttm, 0.25, is_call=True)

        assert result is not None
        assert result.strike > forward
        assert result.converged is True

    def test_find_delta_strike_put_25d(self, built_surface: VolSurface) -> None:
        """25D put strike exists and is < forward."""
        ttm = 90 / 365
        forward = built_surface.spot * math.exp(built_surface.rate * ttm)

        result = find_delta_strike(built_surface, ttm, -0.25, is_call=False)

        assert result is not None
        assert result.strike < forward
        assert result.converged is True

    def test_find_delta_strike_50d_atm(self, built_surface: VolSurface) -> None:
        """50D call delta ≈ 0.5 gives strike ≈ forward."""
        ttm = 90 / 365
        forward = built_surface.spot * math.exp(built_surface.rate * ttm)

        result = find_delta_strike(built_surface, ttm, 0.5, is_call=True)

        if result is not None:
            assert abs(result.strike - forward) < forward * 0.05  # Within 5%

    def test_find_delta_strike_iv_matches_surface(self, built_surface: VolSurface) -> None:
        """IV at found strike matches surface.volatility()."""
        ttm = 90 / 365

        result = find_delta_strike(built_surface, ttm, 0.25, is_call=True)

        if result is not None:
            surface_vol = built_surface.volatility(result.strike, ttm)
            assert abs(result.iv - surface_vol) < 0.001  # Within 0.1%

    def test_find_delta_strike_invalid_delta_returns_none(self, built_surface: VolSurface) -> None:
        """|delta| > 1 returns None."""
        ttm = 90 / 365

        result_high = find_delta_strike(built_surface, ttm, 1.5, is_call=True)
        result_low = find_delta_strike(built_surface, ttm, -1.5, is_call=False)

        assert result_high is None
        assert result_low is None

    def test_find_delta_strike_boundary_deltas(self, built_surface: VolSurface) -> None:
        """Delta near boundaries behave sensibly."""
        ttm = 90 / 365

        result_high = find_delta_strike(built_surface, ttm, 0.75, is_call=True)
        result_low = find_delta_strike(built_surface, ttm, -0.75, is_call=False)

        # Higher delta calls are typically ITM (lower strikes), lower delta calls are OTM
        # Just verify convergence - don't assume moneyness
        if result_high is not None:
            assert result_high.converged is True
        if result_low is not None:
            assert result_low.converged is True


class TestPutCallParityForward:
    """Put-call parity implied forward tests."""

    @pytest.mark.parametrize("rate", [0.02, 0.08])
    def test_put_call_parity_forward_is_the_bsm_forward_at_the_rate_passed(
        self, spot: float, rate: float, base_vol: float
    ) -> None:
        """Matched BS call/put pairs imply F = S * exp(r*T) at every expiry."""
        ttms = [0.25, 0.5, 1.0]  # exact at the six decimals the function keys expiries by
        pairs = [
            {"strike": k, "ttm": ttm, "option_price": bs_price(spot, k, ttm, rate, base_vol, is_call), "is_call": is_call}
            for ttm in ttms
            for k in (90.0, 95.0, 100.0, 105.0, 110.0)
            for is_call in (True, False)
        ]

        forwards = compute_put_call_parity_forward(pairs, rate=rate)

        assert sorted(forwards) == ttms
        for ttm, implied_fwd in forwards.items():
            # Parity is an identity for BS prices with q = 0, so the strict-float default holds.
            assert implied_fwd == pytest.approx(spot * math.exp(rate * ttm), rel=0, abs=1e-9)

    def test_put_call_parity_forward_empty_returns_dict(self, rate: float) -> None:
        """Empty option list returns empty dict."""
        forwards = compute_put_call_parity_forward([], rate=rate)

        assert forwards == {}

    def test_put_call_parity_forward_only_calls_no_forwards(self, rate: float) -> None:
        """Only calls (no puts) returns no forwards."""
        call_only = [
            {"strike": 100.0, "ttm": 0.1, "option_price": 5.0, "is_call": True},
            {"strike": 105.0, "ttm": 0.1, "option_price": 2.0, "is_call": True},
        ]

        forwards = compute_put_call_parity_forward(call_only, rate=rate)

        assert forwards == {}


class TestSkewMetricsEdgeCases:
    """Edge case tests for skew metrics."""

    def test_skew_metrics_short_ttm(self, built_surface: VolSurface) -> None:
        """Metrics for very short TTM (< 7 days)."""
        ttm = 3 / 365

        metrics = compute_skew_metrics(built_surface, ttm)

        assert metrics.ttm == ttm
        assert metrics.atm_iv > 0.0

    def test_skew_metrics_long_ttm(self, built_surface: VolSurface) -> None:
        """Metrics for long TTM (> 1 year)."""
        ttm = 2.0

        metrics = compute_skew_metrics(built_surface, ttm)

        assert metrics.ttm == ttm
        assert metrics.atm_iv > 0.0

    def test_find_delta_strike_short_ttm(self, built_surface: VolSurface) -> None:
        """Delta strike at very short TTM."""
        ttm = 1 / 365

        result = find_delta_strike(built_surface, ttm, 0.25, is_call=True)

        if result is not None:
            assert result.converged is True

    def test_find_delta_strike_long_ttm(self, built_surface: VolSurface) -> None:
        """Delta strike at long TTM."""
        ttm = 2.0

        result = find_delta_strike(built_surface, ttm, 0.25, is_call=True)

        if result is not None:
            assert result.converged is True
