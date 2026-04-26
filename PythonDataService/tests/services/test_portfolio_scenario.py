"""Tests for `app.services.portfolio_scenario.evaluate_scenario` (Phase 2.1).

The scenario engine itself does not invent math — it composes
``bs_european_price`` and ``black_scholes_greeks`` from
``app/services/bs_greeks.py``. So these tests focus on **wiring** rather
than math validity:

- Per-leg theoretical price and Greeks match direct Hull-formula calls
  (the reference is a direct reapplication of ``bs_greeks``, run
  independently of the scenario engine).
- Aggregates equal the sum of per-leg values × quantity × multiplier.
- Stock legs contribute delta=1, gamma=0, theta=0, vega=0.
- Time-shift advances TTM correctly.
- Spot-shock applies as a fractional move.
- Expired legs (TTM ≤ 0) are intrinsic-only with all Greeks = 0 and
  surface a warning.

Math validity is covered by ``tests/services/test_bs_greeks.py`` and
``tests/services/test_bs_cross_engine_parity.py``.

Documentation: ``docs/architecture/numerical-authority-migration-plan.md`` Phase 2.1.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.models.portfolio import (
    OptionPosition,
    ScenarioGrid,
    ScenarioRequest,
    StockPosition,
)
from app.services.bs_greeks import black_scholes_greeks, bs_european_price
from app.services.portfolio_scenario import (
    DAYS_PER_YEAR,
    MS_PER_DAY,
    evaluate_scenario,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "golden"
    / "portfolio-scenario-3leg"
    / "cases.json"
)


@pytest.fixture(scope="module")
def fixture_payload() -> dict:
    with FIXTURE.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def request_obj(fixture_payload: dict) -> ScenarioRequest:
    """Reconstruct the ScenarioRequest from the fixture JSON."""
    return ScenarioRequest.model_validate(fixture_payload["request"])


def _expected_leg(
    *,
    position,
    effective_spot: float,
    scenario_ms: int,
    iv_shift: float,
    rate: float,
    q: float,
):
    """Direct Hull-formula reference for one leg.

    Mirrors what `evaluate_scenario` should do internally, computed here
    from the same primitives but routed independently. If `evaluate_scenario`
    is wired correctly, the two paths agree.
    """
    if isinstance(position, StockPosition):
        return {
            "price": effective_spot,
            "delta": 1.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
        }

    ttm_years = (position.expiration_ms - scenario_ms) / MS_PER_DAY / DAYS_PER_YEAR
    sigma = max(position.current_iv + iv_shift, 0.0)
    is_call = position.option_type == "call"

    if ttm_years <= 0.0 or sigma <= 0.0:
        intrinsic = (
            max(effective_spot - position.strike, 0.0)
            if is_call
            else max(position.strike - effective_spot, 0.0)
        )
        return {
            "price": intrinsic,
            "delta": 0.0,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
        }

    price = bs_european_price(
        spot=effective_spot,
        strike=position.strike,
        ttm_years=ttm_years,
        rate=rate,
        volatility=sigma,
        is_call=is_call,
        dividend=q,
    )
    g = black_scholes_greeks(
        spot=effective_spot,
        strike=position.strike,
        ttm_years=ttm_years,
        volatility=sigma,
        rate=rate,
        dividend=q,
        is_call=is_call,
    )
    return {
        "price": price,
        "delta": g.delta,
        "gamma": g.gamma,
        "theta": g.theta,
        "vega": g.vega,
        "rho": g.rho,
    }


class TestGridShape:
    """Output grid matches input grid: 5×5×1 = 25 points."""

    def test_point_count_matches_grid(self, request_obj: ScenarioRequest) -> None:
        result = evaluate_scenario(request_obj)
        n_expected = (
            len(request_obj.grid.spot_shocks)
            * len(request_obj.grid.time_shifts_days)
            * len(request_obj.grid.iv_shifts)
        )
        assert len(result.points) == n_expected == 25

    def test_each_point_has_one_leg_result_per_position(
        self, request_obj: ScenarioRequest
    ) -> None:
        result = evaluate_scenario(request_obj)
        for point in result.points:
            assert len(point.legs) == len(request_obj.positions)


class TestPerLegParity:
    """Per-leg theoretical price and Greeks match direct Hull-formula reference."""

    def test_all_legs_match_reference(
        self, request_obj: ScenarioRequest, fixture_payload: dict
    ) -> None:
        result = evaluate_scenario(request_obj)
        atol = fixture_payload["tolerance"]["atol"]
        rtol = fixture_payload["tolerance"]["rtol"]

        mismatches: list[str] = []
        for point in result.points:
            effective_spot = request_obj.spot_price * (1.0 + point.spot_shock)
            scenario_ms = request_obj.as_of_ms + int(point.time_shift_days * MS_PER_DAY)

            for position, leg in zip(request_obj.positions, point.legs, strict=True):
                expected = _expected_leg(
                    position=position,
                    effective_spot=effective_spot,
                    scenario_ms=scenario_ms,
                    iv_shift=point.iv_shift,
                    rate=request_obj.risk_free_rate,
                    q=request_obj.dividend_yield,
                )
                for key, actual in (
                    ("price", leg.theoretical_price),
                    ("delta", leg.delta),
                    ("gamma", leg.gamma),
                    ("theta", leg.theta),
                    ("vega", leg.vega),
                    ("rho", leg.rho),
                ):
                    diff = abs(actual - expected[key])
                    bound = atol + rtol * abs(expected[key])
                    if diff > bound:
                        mismatches.append(
                            f"shock={point.spot_shock:+.2f} t={point.time_shift_days}d "
                            f"leg={leg.leg_id} {key}: got {actual!r} expected {expected[key]!r} "
                            f"diff={diff:.2e} bound={bound:.2e}"
                        )

        assert not mismatches, "\n".join(mismatches[:10])


class TestAggregates:
    """Aggregates equal the sum of per-leg values × quantity × multiplier."""

    def test_aggregate_delta_equals_quantity_weighted_sum(
        self, request_obj: ScenarioRequest
    ) -> None:
        result = evaluate_scenario(request_obj)
        for point in result.points:
            expected_delta = 0.0
            for position, leg in zip(request_obj.positions, point.legs, strict=True):
                multiplier = (
                    position.multiplier if isinstance(position, OptionPosition) else 1.0
                )
                expected_delta += leg.delta * position.quantity * multiplier
            assert math.isclose(
                point.aggregate_delta, expected_delta, abs_tol=1e-9, rel_tol=0.0
            ), (
                f"shock={point.spot_shock:+.2f} t={point.time_shift_days}d: "
                f"aggregate_delta={point.aggregate_delta} sum_of_legs={expected_delta}"
            )

    def test_portfolio_pnl_is_per_share_pnl_times_scaled_quantity(
        self, request_obj: ScenarioRequest
    ) -> None:
        result = evaluate_scenario(request_obj)
        for point in result.points:
            expected_pnl = 0.0
            for position, leg in zip(request_obj.positions, point.legs, strict=True):
                multiplier = (
                    position.multiplier if isinstance(position, OptionPosition) else 1.0
                )
                per_share = leg.theoretical_price - position.entry_price
                expected_pnl += per_share * position.quantity * multiplier
            assert math.isclose(
                point.portfolio_pnl, expected_pnl, abs_tol=1e-7, rel_tol=0.0
            ), (
                f"shock={point.spot_shock:+.2f} t={point.time_shift_days}d: "
                f"portfolio_pnl={point.portfolio_pnl} expected={expected_pnl}"
            )


class TestCurrentStateZeroShockZeroTime:
    """At spot_shock=0, time_shift=0, iv_shift=0 the result is 'live Greeks'."""

    def test_zero_shock_zero_time_uses_input_spot(
        self, request_obj: ScenarioRequest
    ) -> None:
        result = evaluate_scenario(request_obj)
        zero_points = [
            p for p in result.points
            if p.spot_shock == 0.0 and p.time_shift_days == 0.0 and p.iv_shift == 0.0
        ]
        assert len(zero_points) == 1
        zero = zero_points[0]
        assert zero.spot == request_obj.spot_price


class TestExpiredOptionWarnings:
    """Time-shifts beyond expiration produce intrinsic-only valuation + warning."""

    def test_time_shift_past_expiration_emits_warning(self) -> None:
        # Build a request whose grid pushes the option into expiration.
        # 30-day option, time_shift_days=60 → already expired at scenario_ms.
        eval_ms = 1767225600000  # arbitrary
        req = ScenarioRequest(
            as_of_ms=eval_ms,
            spot_price=100.0,
            risk_free_rate=0.04,
            dividend_yield=0.0,
            positions=[
                OptionPosition(
                    symbol="TEST",
                    option_type="call",
                    strike=100.0,
                    expiration_ms=eval_ms + int(30 * MS_PER_DAY),
                    quantity=1.0,
                    multiplier=100.0,
                    entry_price=2.50,
                    current_iv=0.20,
                    leg_id="expired_call",
                )
            ],
            grid=ScenarioGrid(
                spot_shocks=[0.0],
                time_shifts_days=[60.0],  # past 30-day expiration
                iv_shifts=[0.0],
            ),
        )
        result = evaluate_scenario(req)
        assert len(result.warnings) >= 1
        assert "expired_call" in result.warnings[0] or "intrinsic" in result.warnings[0]
        # Greeks all zero, theoretical_price = intrinsic = max(100-100, 0) = 0
        leg = result.points[0].legs[0]
        assert leg.delta == 0.0
        assert leg.gamma == 0.0
        assert leg.theta == 0.0
        assert leg.vega == 0.0
        assert leg.theoretical_price == 0.0


class TestMixedUnderlyingsRejected:
    """Mixed-underlying portfolios must be split by caller; the endpoint refuses them."""

    def test_two_underlyings_raises_validation_error(self) -> None:
        with pytest.raises(ValueError):
            ScenarioRequest(
                as_of_ms=1767225600000,
                spot_price=100.0,
                positions=[
                    StockPosition(symbol="SPY", quantity=10.0, entry_price=100.0),
                    StockPosition(symbol="QQQ", quantity=10.0, entry_price=100.0),
                ],
            )
