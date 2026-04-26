"""Tests for Phase 1.1 additions to the options strategy engine.

Phase 1.1 of `docs/architecture/numerical-authority-migration-plan.md`:
``analyze_strategy`` gains opt-in current-time fields so
``OptionsStrategyLabComponent`` can stop computing them in TypeScript.

These tests verify:
- The new optional fields are ``None`` unless requested (no payload-shape
  drift for existing callers).
- When requested, the fields are populated and consistent with direct
  ``bs_greeks`` calls.
- ``what_if_time_shift_days`` and ``what_if_iv_shift`` flow through.
- Per-leg diagnostics agree with direct math at the request spot.

Math-validity coverage lives in ``test_bs_greeks.py``,
``test_bs_cross_engine_parity.py``, and the existing ``test_strategy_engine.py``.
This file is a wiring test for the new fields.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.strategy import StrategyAnalyzeRequest, StrategyLeg
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.services.strategy_engine import analyze_strategy


def _bull_call_spread(today_plus: int = 30) -> StrategyAnalyzeRequest:
    """Long 100C / Short 105C, IV 25%, 30 DTE, spot 102."""
    expiration = (date.today() + timedelta(days=today_plus)).strftime("%Y-%m-%d")
    return StrategyAnalyzeRequest(
        symbol="TEST",
        legs=[
            StrategyLeg(
                leg_id="long_100c", strike=100.0, option_type="call", position="long",
                premium=5.0, iv=0.25, quantity=1,
            ),
            StrategyLeg(
                leg_id="short_105c", strike=105.0, option_type="call", position="short",
                premium=2.0, iv=0.25, quantity=1,
            ),
        ],
        expiration_date=expiration,
        spot_price=102.0,
        risk_free_rate=0.04,
    )


class TestPayloadShapeStableByDefault:
    """Existing callers (no include_* flags) see exactly the old payload."""

    def test_default_request_returns_none_for_new_fields(self) -> None:
        request = _bull_call_spread()
        result = analyze_strategy(request)
        assert result.success
        assert result.current_curve is None
        assert result.greek_curves is None
        assert result.leg_diagnostics is None


class TestCurrentCurve:
    """include_current_curve populates a curve at today's vol surface (not at expiry)."""

    def test_current_curve_has_request_curve_points(self) -> None:
        request = _bull_call_spread()
        request.include_current_curve = True
        result = analyze_strategy(request)
        assert result.current_curve is not None
        assert len(result.current_curve) == request.curve_points

    def test_current_curve_differs_from_expiry_curve(self) -> None:
        """At today (30 DTE, 25% IV) the current value is not the same as at expiry."""
        request = _bull_call_spread()
        request.include_current_curve = True
        result = analyze_strategy(request)
        # At-the-money (price ~ 102), expiry payoff and current value differ:
        # expiry payoff = max(102-100,0) - 5 - max(102-105,0) + 2 = 2 - 5 - 0 + 2 = -1
        # current value carries time value, so theoretical_pnl ≠ -1 at price=102
        atm = min(result.current_curve, key=lambda p: abs(p.price - 102.0))
        atm_expiry = min(result.curve, key=lambda p: abs(p.price - 102.0))
        assert atm.theoretical_pnl != pytest.approx(atm_expiry.pnl, abs=0.01)

    def test_iv_shift_changes_current_curve(self) -> None:
        request = _bull_call_spread()
        request.include_current_curve = True
        request.what_if_iv_shift = 0.0
        baseline = analyze_strategy(request).current_curve
        request.what_if_iv_shift = 0.10
        shifted = analyze_strategy(request).current_curve
        # +10 vol points must change the curve. We compare across the whole
        # curve rather than at the ATM point alone, because for a bull call
        # spread the ATM net vega can be near zero (long-leg vega ≈ short-leg
        # vega) while OTM points show a much larger IV effect.
        baseline_values = [p.theoretical_value for p in baseline]
        shifted_values = [p.theoretical_value for p in shifted]
        max_diff = max(abs(b - s) for b, s in zip(baseline_values, shifted_values, strict=True))
        # Sanity: a 10-vol-point shift on a 30-DTE 25%-IV spread must move
        # *some* point on the curve by more than a cent.
        assert max_diff > 0.01, (
            f"IV shift 0→0.10 produced max curve change of only {max_diff:.6f}; "
            f"expected > 0.01"
        )


class TestGreekCurves:
    """include_greek_curves populates aggregate Greeks per spot grid point."""

    def test_greek_curves_populated_when_requested(self) -> None:
        request = _bull_call_spread()
        request.include_greek_curves = True
        result = analyze_strategy(request)
        assert result.greek_curves is not None
        assert len(result.greek_curves) == request.curve_points

    def test_aggregate_delta_matches_per_leg_sum(self) -> None:
        request = _bull_call_spread()
        request.include_greek_curves = True
        result = analyze_strategy(request)
        ttm = 30 / 365.0
        # Spot-check at request spot: delta should equal sum of per-leg
        # closed-form deltas with sign and quantity applied.
        atm = min(result.greek_curves, key=lambda p: abs(p.price - 102.0))
        long_g = black_scholes_greeks(
            spot=atm.price, strike=100.0, ttm_years=ttm, volatility=0.25,
            rate=0.04, dividend=0.0, is_call=True,
        )
        short_g = black_scholes_greeks(
            spot=atm.price, strike=105.0, ttm_years=ttm, volatility=0.25,
            rate=0.04, dividend=0.0, is_call=True,
        )
        expected_delta = long_g.delta * 1 - short_g.delta * 1
        assert atm.delta == pytest.approx(expected_delta, abs=1e-3)


class TestLegDiagnostics:
    """include_leg_diagnostics populates per-leg current value/Greeks."""

    def test_leg_diagnostics_one_row_per_leg(self) -> None:
        request = _bull_call_spread()
        request.include_leg_diagnostics = True
        result = analyze_strategy(request)
        assert result.leg_diagnostics is not None
        assert len(result.leg_diagnostics) == 2
        assert {row.leg_id for row in result.leg_diagnostics} == {"long_100c", "short_105c"}

    def test_leg_diagnostic_theoretical_matches_bs_european_price(self) -> None:
        request = _bull_call_spread()
        request.include_leg_diagnostics = True
        result = analyze_strategy(request)
        ttm = 30 / 365.0
        for row in result.leg_diagnostics:
            expected = bs_european_price(
                spot=request.spot_price, strike=row.strike, ttm_years=ttm,
                rate=request.risk_free_rate, volatility=row.iv,
                is_call=row.option_type == "call", dividend=0.0,
            )
            assert row.current_theoretical == pytest.approx(expected, abs=1e-4)


class TestZeroDTEHandling:
    """At expiration day (DTE=0) the current curve collapses to intrinsic."""

    def test_dte_zero_current_curve_equals_expiry_curve(self) -> None:
        request = _bull_call_spread(today_plus=0)
        request.include_current_curve = True
        result = analyze_strategy(request)
        # At DTE=0, theoretical_value == intrinsic.
        # So theoretical_pnl == expiry pnl on the same price grid.
        for cur, exp in zip(result.current_curve, result.curve, strict=True):
            assert cur.price == pytest.approx(exp.price, abs=1e-6)
            assert cur.theoretical_pnl == pytest.approx(exp.pnl, abs=1e-3)
