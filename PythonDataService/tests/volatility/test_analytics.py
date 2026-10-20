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

    def test_health_score_components_total_and_good_surface(self, built_surface: VolSurface) -> None:
        """All 4 component scores are between 0 and 100, the total is approximately their average, and exact
        Black-Scholes prices off a smooth skew earn full marks: every contract solves, the fit RMSE is under 0.005,
        none is rejected and there is no arbitrage."""
        score = compute_health_score(built_surface)

        avg = (score.convergence_score + score.rmse_score + score.rejection_score + score.arbitrage_score) / 4.0

        assert 0 <= score.convergence_score <= 100, "convergence score is between 0 and 100"
        assert 0 <= score.rmse_score <= 100, "rmse score is between 0 and 100"
        assert 0 <= score.rejection_score <= 100, "rejection score is between 0 and 100"
        assert 0 <= score.arbitrage_score <= 100, "arbitrage score is between 0 and 100"
        assert abs(score.total - avg) < 1.0, "total is approximately the average of the 4 components"  # Allow rounding tolerance
        assert score.total == 100, "exact Black-Scholes prices off a smooth skew earn full marks"


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

    @pytest.mark.parametrize(
        ("kinds", "expected_score"),
        [
            pytest.param(("checked", "checked"), 100.0, id="all_checked_slices_score_full"),
            pytest.param(("checked", "unchecked"), 50.0, id="score_is_the_checked_share_of_slices"),
            pytest.param(("unchecked", "unchecked"), 0.0, id="all_unchecked_slices_score_zero"),
            pytest.param(
                ("checked", "missing"), 50.0, id="a_slice_with_no_arbitrage_report_counts_as_unchecked_beside_a_checked_one"
            ),
            pytest.param(
                ("missing", "missing"), 0.0, id="a_slice_with_no_arbitrage_report_counts_as_unchecked_when_all_lack_one"
            ),
        ],
    )
    def test_arbitrage_score(self, kinds: tuple[str, ...], expected_score: float) -> None:
        reports = {"checked": self._checked_report, "unchecked": self._unchecked_report, "missing": lambda: None}

        assert self._arbitrage_score([reports[kind]() for kind in kinds]) == pytest.approx(expected_score, abs=1e-9)


class TestSkewMetrics:
    """Skew and smile metrics tests."""

    def test_skew_metrics_per_expiry(self, built_surface: VolSurface) -> None:
        """At every fitted expiry: ATM IV is positive and reasonable, dte_days matches round(ttm * 365), the 25D RR is
        negative for equity skew (puts > calls), the 25D butterfly is positive or near zero, and the 25D put and call
        exist in reasonable cases."""
        for fit in built_surface.fits:
            metrics = compute_skew_metrics(built_surface, fit.ttm)

            assert metrics.atm_iv > 0.0, f"ttm={fit.ttm}: ATM IV is positive"
            assert 0.01 <= metrics.atm_iv <= 2.0, f"ttm={fit.ttm}: ATM IV is in a reasonable vol range"
            expected_dte = round(fit.ttm * 365)
            assert metrics.dte_days == expected_dte, f"ttm={fit.ttm}: dte_days matches round(ttm * 365)"
            if metrics.rr_25d is not None:
                assert metrics.rr_25d < 0.0, f"ttm={fit.ttm}: for equity skew, 25D RR is negative (puts > calls)"
            if metrics.bf_25d is not None:
                # Allow small numerical noise
                assert metrics.bf_25d >= -0.001, f"ttm={fit.ttm}: 25D butterfly is positive (smile curvature) or near zero"
            if fit.ttm >= 30 / 365:
                assert metrics.put_25d is not None, f"ttm={fit.ttm}: 25D put exists"
                assert metrics.call_25d is not None, f"ttm={fit.ttm}: 25D call exists"


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

    def test_find_delta_strike_located_strikes(self, built_surface: VolSurface) -> None:
        """25D call strike exists and is > forward, 25D put strike exists and is < forward, 50D call delta ≈ 0.5 gives
        strike ≈ forward, and the IV at the found strike matches surface.volatility()."""
        ttm = 90 / 365
        forward = built_surface.spot * math.exp(built_surface.rate * ttm)

        call_25d = find_delta_strike(built_surface, ttm, 0.25, is_call=True)
        put_25d = find_delta_strike(built_surface, ttm, -0.25, is_call=False)
        call_50d = find_delta_strike(built_surface, ttm, 0.5, is_call=True)

        assert call_25d is not None, "25D call strike exists"
        assert call_25d.strike > forward, "25D call strike is > forward"
        assert call_25d.converged is True, "25D call strike converged"
        assert put_25d is not None, "25D put strike exists"
        assert put_25d.strike < forward, "25D put strike is < forward"
        assert put_25d.converged is True, "25D put strike converged"
        if call_50d is not None:
            assert abs(call_50d.strike - forward) < forward * 0.05, "50D call strike is within 5% of forward"
        surface_vol = built_surface.volatility(call_25d.strike, ttm)
        assert abs(call_25d.iv - surface_vol) < 0.001, "IV at the found 25D call strike matches surface.volatility()"

    def test_find_delta_strike_out_of_range_and_boundary_deltas(self, built_surface: VolSurface) -> None:
        """|delta| > 1 returns None; delta near the boundaries behaves sensibly."""
        ttm = 90 / 365

        result_high = find_delta_strike(built_surface, ttm, 1.5, is_call=True)
        result_low = find_delta_strike(built_surface, ttm, -1.5, is_call=False)
        boundary_high = find_delta_strike(built_surface, ttm, 0.75, is_call=True)
        boundary_low = find_delta_strike(built_surface, ttm, -0.75, is_call=False)

        assert result_high is None, "call delta 1.5 (|delta| > 1) returns None"
        assert result_low is None, "put delta -1.5 (|delta| > 1) returns None"
        # Higher delta calls are typically ITM (lower strikes), lower delta calls are OTM
        # Just verify convergence - don't assume moneyness
        if boundary_high is not None:
            assert boundary_high.converged is True, "call delta 0.75 converged"
        if boundary_low is not None:
            assert boundary_low.converged is True, "put delta -0.75 converged"


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

    @pytest.mark.parametrize(
        "calls",
        [
            pytest.param((), id="empty_option_list_returns_empty_dict"),
            pytest.param(((100.0, 5.0), (105.0, 2.0)), id="only_calls_no_puts_returns_no_forwards"),
        ],
    )
    def test_put_call_parity_forward_without_a_pair_returns_empty_dict(
        self, rate: float, calls: tuple[tuple[float, float], ...]
    ) -> None:
        """No call/put pair at an expiry means no implied forward."""
        options = [{"strike": strike, "ttm": 0.1, "option_price": price, "is_call": True} for strike, price in calls]

        forwards = compute_put_call_parity_forward(options, rate=rate)

        assert forwards == {}


class TestSkewMetricsEdgeCases:
    """Edge case tests for skew metrics."""

    @pytest.mark.parametrize(
        "ttm",
        [
            pytest.param(3 / 365, id="short_ttm_under_7_days"),
            pytest.param(2.0, id="long_ttm_over_1_year"),
        ],
    )
    def test_skew_metrics_extreme_ttm(self, built_surface: VolSurface, ttm: float) -> None:
        """Metrics for very short TTM (< 7 days) and long TTM (> 1 year)."""
        metrics = compute_skew_metrics(built_surface, ttm)

        assert metrics.ttm == ttm, "metrics report the queried ttm"
        assert metrics.atm_iv > 0.0, "ATM IV is positive"

    def test_find_delta_strike_short_ttm(self, built_surface: VolSurface) -> None:
        """Delta strike at very short TTM."""
        ttm = 1 / 365

        result = find_delta_strike(built_surface, ttm, 0.25, is_call=True)

        if result is not None:
            assert result.converged is True
