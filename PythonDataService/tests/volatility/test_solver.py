"""
Tests for the QuantLib IV solver.
"""

from __future__ import annotations

import math

import pytest
from scipy.stats import norm

from app.volatility.solver import (
    SolveStatus,
    implied_volatility,
    solve_iv_chain,
)


def bs_price(
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    vol: float,
    is_call: bool,
) -> float:
    """Reference Black-Scholes price for test data generation."""
    import math

    d1 = (math.log(spot / strike) + (rate + 0.5 * vol**2) * ttm) / (vol * math.sqrt(ttm))
    d2 = d1 - vol * math.sqrt(ttm)
    df = math.exp(-rate * ttm)
    if is_call:
        return spot * norm.cdf(d1) - strike * df * norm.cdf(d2)
    return strike * df * norm.cdf(-d2) - spot * norm.cdf(-d1)


class TestImpliedVolatility:
    """Tests for the single-contract IV solver."""

    def test_atm_call_recovers_vol(self, spot: float, rate: float) -> None:
        """ATM call priced at 25% vol should recover ~25% IV."""
        vol = 0.25
        ttm = 0.5
        strike = spot
        price = bs_price(spot, strike, ttm, rate, vol, is_call=True)

        result = implied_volatility(price, spot, strike, ttm, rate)

        assert result.status in (SolveStatus.NEWTON_OK, SolveStatus.QUANTLIB_OK, SolveStatus.BRENT_FALLBACK)
        assert result.iv is not None
        # QuantLib accuracy is on price, not vol; ~0.1% vol tolerance is realistic
        assert abs(result.iv - vol) < 0.005

    def test_atm_put_recovers_vol(self, spot: float, rate: float) -> None:
        """ATM put priced at 25% vol should recover ~25% IV."""
        vol = 0.25
        ttm = 0.5
        strike = spot
        price = bs_price(spot, strike, ttm, rate, vol, is_call=False)

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=False)

        assert result.iv is not None
        assert abs(result.iv - vol) < 0.005

    @pytest.mark.parametrize("vol", [0.05, 0.15, 0.30, 0.60, 1.0, 2.0])
    def test_range_of_vols(self, spot: float, rate: float, vol: float) -> None:
        """IV solver should handle a wide range of input vols."""
        ttm = 0.25
        strike = spot * 1.05
        price = bs_price(spot, strike, ttm, rate, vol, is_call=True)

        result = implied_volatility(price, spot, strike, ttm, rate)

        assert result.iv is not None
        # Tolerance scales with vol level: 0.5% relative or 0.005 absolute
        tol = max(vol * 0.005, 0.005)
        assert abs(result.iv - vol) < tol, f"Expected {vol}, got {result.iv}"

    @pytest.mark.parametrize("moneyness", [0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30])
    def test_range_of_strikes(self, spot: float, rate: float, moneyness: float) -> None:
        """IV solver should handle ITM through OTM options."""
        vol = 0.30
        ttm = 0.5
        strike = spot * moneyness
        is_call = moneyness >= 1.0
        price = bs_price(spot, strike, ttm, rate, vol, is_call=is_call)

        if price < 0.001:
            pytest.skip("Price too low for reliable recovery")

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=is_call)

        assert result.iv is not None
        assert abs(result.iv - vol) < 1e-3

    def test_expired_option_returns_expired(self, spot: float) -> None:
        """TTM below MIN_TIME_TO_EXPIRY (1 minute) should return EXPIRED.

        The solver's floor was lowered from 1 calendar day to 1 minute so
        the data-lab options companion can solve 0DTE intraday IVs (see
        ``MIN_TIME_TO_EXPIRY`` rationale). This test pins the new floor.
        """
        sub_minute_ttm = 1e-7  # ~3 seconds, well below 1/(365*24*60)
        result = implied_volatility(5.0, spot, spot, ttm=sub_minute_ttm, rate=0.05)
        assert result.status == SolveStatus.EXPIRED
        assert result.iv is None

    def test_zero_price_returns_price_too_low(self, spot: float) -> None:
        """Zero price should return PRICE_TOO_LOW."""
        result = implied_volatility(0.0, spot, spot, ttm=0.5, rate=0.05)
        assert result.status == SolveStatus.PRICE_TOO_LOW

    def test_negative_spot_returns_input_error(self) -> None:
        """Negative spot should return INPUT_ERROR."""
        result = implied_volatility(5.0, -100, 100, ttm=0.5, rate=0.05)
        assert result.status == SolveStatus.INPUT_ERROR

    def test_intrinsic_violation(self, spot: float, rate: float) -> None:
        """Price below intrinsic should return INTRINSIC_VIOLATION."""
        strike = 80.0  # deep ITM call
        ttm = 0.5
        intrinsic = max(spot - strike * math.exp(-rate * ttm), 0)
        price = intrinsic * 0.5  # below intrinsic

        result = implied_volatility(price, spot, strike, ttm, rate, is_call=True)
        assert result.status == SolveStatus.INTRINSIC_VIOLATION

    def test_deterministic(self, spot: float, rate: float) -> None:
        """Same inputs must produce identical outputs."""
        vol = 0.25
        ttm = 0.5
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)

        results = [implied_volatility(price, spot, spot, ttm, rate) for _ in range(10)]
        ivs = [r.iv for r in results]
        assert len(set(ivs)) == 1, f"Non-deterministic: {ivs}"


class TestSolveIvChain:
    """Tests for the batch IV solver."""

    def test_batch_solve(self, spot: float, rate: float) -> None:
        """Batch solver should process multiple records."""
        vol = 0.25
        ttm = 0.5
        records = [
            {
                "strike": spot * m,
                "ttm": ttm,
                "option_price": bs_price(spot, spot * m, ttm, rate, vol, m >= 1.0),
                "is_call": m >= 1.0,
            }
            for m in [0.90, 0.95, 1.00, 1.05, 1.10]
        ]

        results = solve_iv_chain(records, spot, rate)

        assert len(results) == 5
        for r in results:
            assert r["iv"] is not None
            assert r["iv_status"] in ("newton_ok", "quantlib_ok", "brent_fallback")
            assert abs(r["iv"] - vol) < 1e-3


class TestNewtonSolverPath:
    """Isolate the Newton solver path from QuantLib / Brent fallbacks.

    Newton is the primary solver — it must succeed on well-conditioned
    inputs (ATM, near-ATM, sub-day TTM) so the warm-start savings the
    options companion relies on are real.
    """

    def test_newton_succeeds_for_atm_well_conditioned(self, spot: float, rate: float) -> None:
        vol = 0.25
        ttm = 0.5
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)
        result = implied_volatility(price, spot, spot, ttm, rate, vol_guess=0.20)
        assert result.status == SolveStatus.NEWTON_OK
        assert result.iv == pytest.approx(vol, abs=1e-6)

    def test_newton_succeeds_for_sub_day_ttm(self, spot: float, rate: float) -> None:
        # 30-min TTM with ATM call — Newton handles this directly. (We use
        # ATM rather than ITM so vega is large and the recovered IV is
        # well-determined; mildly ITM at sub-day TTM has near-zero vega
        # which inflates per-call IV uncertainty even on a clean root.)
        vol = 0.30
        ttm = 30.0 / (365.0 * 24.0 * 60.0)
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)
        result = implied_volatility(price, spot, spot, ttm, rate, vol_guess=0.25)
        assert result.status == SolveStatus.NEWTON_OK
        assert result.iv == pytest.approx(vol, abs=1e-4)


class TestBrentFallbackPath:
    """When Newton's vega collapses (deep OTM with sub-day TTM the option
    price is essentially insensitive to vol), the solver must hand off
    to Brent without surfacing a convergence_failure to the caller."""

    def test_brent_handles_deep_otm_sub_day(self, spot: float, rate: float) -> None:
        # 1-hour TTM, mildly OTM call. We pin the strike close enough to
        # spot that the BS premium stays comfortably above MIN_OPTION_PRICE
        # so the solver path is actually exercised — a wider OTM strike
        # collapses the premium into the PRICE_TOO_LOW short-circuit and
        # the test would silently skip the path it claims to cover.
        vol = 0.30
        ttm = 1.0 / (365.0 * 24.0)
        strike = spot * 1.005
        price = bs_price(spot, strike, ttm, rate, vol, is_call=True)
        assert price >= 0.001, (
            f"test setup must keep premium above MIN_OPTION_PRICE "
            f"(got {price:.6f})"
        )
        result = implied_volatility(price, spot, strike, ttm, rate, is_call=True)
        assert result.status != SolveStatus.CONVERGENCE_FAILURE
        assert result.iv is not None
        assert result.iv == pytest.approx(vol, abs=1e-3)

    def test_quantlib_skipped_for_sub_day_ttm(self, spot: float, rate: float) -> None:
        """QuantLib's day-resolution arithmetic rounds sub-day TTMs to a
        full day, so the solver must skip QL entirely below 1/365 yr.
        Pin: any sub-day solve must return NEWTON_OK or BRENT_FALLBACK,
        never QUANTLIB_OK.
        """
        vol = 0.20
        ttm = 12.0 / (365.0 * 24.0)  # 12 hours
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)
        result = implied_volatility(price, spot, spot, ttm, rate, is_call=True)
        assert result.status in (SolveStatus.NEWTON_OK, SolveStatus.BRENT_FALLBACK)
        assert result.status != SolveStatus.QUANTLIB_OK
        assert result.iv == pytest.approx(vol, abs=1e-3)

    def test_warm_start_returns_same_iv_as_cold_start(self, spot: float, rate: float) -> None:
        """Warm-starting Newton with the prior bar's IV must converge to
        the same answer as a cold start — the seed only changes
        iteration count, not the final root.
        """
        vol = 0.25
        ttm = 0.25
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)
        cold = implied_volatility(price, spot, spot, ttm, rate, vol_guess=0.50)
        warm = implied_volatility(price, spot, spot, ttm, rate, vol_guess=0.249)
        assert cold.iv is not None
        assert warm.iv is not None
        # Newton's internal tol is 1e-7 on price, which corresponds to
        # ~1e-7 / vega absolute IV difference. At ATM 25 % vol on 0.25 yr,
        # vega ≈ 20, so 1e-6 absolute IV is well within bound.
        assert cold.iv == pytest.approx(warm.iv, abs=1e-6)


class TestZeroDteFloorBoundary:
    """0DTE / near-floor TTM regression tests.

    The solver's `MIN_TIME_TO_EXPIRY` floor is 1 minute (in years). Below
    that the solver returns `EXPIRED`; at or above it must return a
    finite, plausible IV. These tests pin the boundary so the constant
    can't silently regress to a coarser value (e.g. one calendar day,
    which would silently kill 0DTE signal recovery — see
    docs/references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md
    Finding 3.1).
    """

    @pytest.mark.parametrize(
        "minutes_to_expiry",
        [30.0, 5.0, 1.0],
        ids=["t_30min", "t_5min", "t_1min_at_floor"],
    )
    def test_iv_finite_at_or_near_floor(
        self,
        spot: float,
        rate: float,
        minutes_to_expiry: float,
    ) -> None:
        """At and just-above the 1-minute floor, the solver must return a
        finite, positive IV inside [MIN_IV, MAX_IV]. Uses ATM so vega is
        large; deep-OTM at sub-minute TTM is intentionally a separate
        regime (covered elsewhere)."""
        vol = 0.30
        ttm = minutes_to_expiry / (365.0 * 24.0 * 60.0)
        price = bs_price(spot, spot, ttm, rate, vol, is_call=True)

        result = implied_volatility(price, spot, spot, ttm, rate, is_call=True)

        assert result.iv is not None, f"solver returned None at T={minutes_to_expiry} min"
        assert math.isfinite(result.iv), f"non-finite IV at T={minutes_to_expiry} min"
        assert 0.005 <= result.iv <= 5.0, f"IV {result.iv} out of bounds at T={minutes_to_expiry} min"
        # Recovery tolerance widens as TTM shrinks (vega gets noisier near
        # the floor). 5 % at the floor is generous; tighter would be
        # over-fit to a single solver path.
        assert abs(result.iv - vol) < 0.05, (
            f"IV {result.iv} too far from input {vol} at T={minutes_to_expiry} min"
        )

    def test_iv_below_floor_returns_expired(self, spot: float, rate: float) -> None:
        """Just below the 1-minute floor (30 seconds) must return EXPIRED,
        not a degenerate IV. Pins the asymmetry: the boundary is closed
        on the floor side and open below it."""
        ttm = 30.0 / (365.0 * 24.0 * 60.0 * 60.0)  # 30 seconds in years
        # Construct a non-zero premium so we exercise the floor branch,
        # not the PRICE_TOO_LOW short-circuit.
        result = implied_volatility(0.50, spot, spot, ttm, rate, is_call=True)
        assert result.status == SolveStatus.EXPIRED
        assert result.iv is None
