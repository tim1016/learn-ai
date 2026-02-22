"""Tests for the options strategy analysis engine."""
from __future__ import annotations

import math

import pytest

from app.models.strategy import StrategyLeg, StrategyAnalyzeRequest
from app.services.strategy_engine import (
    compute_payoff_at_expiry,
    compute_payoff_curve,
    find_breakevens,
    compute_max_profit_loss,
    weighted_iv,
    interpolate_iv_at_price,
    compute_d2,
    compute_pop,
    compute_expected_value,
    compute_strategy_cost,
    analyze_strategy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _leg(strike: float, opt_type: str, pos: str, premium: float, iv: float = 0.25, qty: int = 1) -> StrategyLeg:
    return StrategyLeg(strike=strike, option_type=opt_type, position=pos, premium=premium, iv=iv, quantity=qty)


def bull_call_spread() -> list[StrategyLeg]:
    """Buy 100C@$5, Sell 105C@$2 → net cost $3, max profit $2, BE $103."""
    return [
        _leg(100, "call", "long", 5.0),
        _leg(105, "call", "short", 2.0),
    ]


def bear_put_spread() -> list[StrategyLeg]:
    """Buy 105P@$4, Sell 100P@$1.5 → net cost $2.5."""
    return [
        _leg(105, "put", "long", 4.0),
        _leg(100, "put", "short", 1.5),
    ]


def long_straddle() -> list[StrategyLeg]:
    """Buy 100C@$5, Buy 100P@$4 → net cost $9."""
    return [
        _leg(100, "call", "long", 5.0),
        _leg(100, "put", "long", 4.0),
    ]


# ---------------------------------------------------------------------------
# Payoff at expiry
# ---------------------------------------------------------------------------

class TestPayoffAtExpiry:
    def test_single_long_call(self):
        legs = [_leg(100, "call", "long", 5.0)]
        assert compute_payoff_at_expiry(legs, 110) == pytest.approx(5.0)
        assert compute_payoff_at_expiry(legs, 100) == pytest.approx(-5.0)
        assert compute_payoff_at_expiry(legs, 90) == pytest.approx(-5.0)

    def test_single_short_put(self):
        legs = [_leg(95, "put", "short", 3.0)]
        assert compute_payoff_at_expiry(legs, 100) == pytest.approx(3.0)
        assert compute_payoff_at_expiry(legs, 90) == pytest.approx(-2.0)

    def test_bull_call_spread_below_lower(self):
        legs = bull_call_spread()
        assert compute_payoff_at_expiry(legs, 90) == pytest.approx(-3.0)

    def test_bull_call_spread_at_breakeven(self):
        legs = bull_call_spread()
        assert compute_payoff_at_expiry(legs, 103) == pytest.approx(0.0)

    def test_bull_call_spread_between_strikes(self):
        legs = bull_call_spread()
        assert compute_payoff_at_expiry(legs, 102) == pytest.approx(-1.0)

    def test_bull_call_spread_above_upper(self):
        legs = bull_call_spread()
        assert compute_payoff_at_expiry(legs, 120) == pytest.approx(2.0)

    def test_long_straddle_at_strike(self):
        legs = long_straddle()
        assert compute_payoff_at_expiry(legs, 100) == pytest.approx(-9.0)

    def test_long_straddle_profitable_up(self):
        legs = long_straddle()
        assert compute_payoff_at_expiry(legs, 115) == pytest.approx(6.0)

    def test_long_straddle_profitable_down(self):
        legs = long_straddle()
        assert compute_payoff_at_expiry(legs, 85) == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Strategy cost
# ---------------------------------------------------------------------------

class TestStrategyCost:
    def test_bull_call_spread_debit(self):
        assert compute_strategy_cost(bull_call_spread()) == pytest.approx(3.0)

    def test_long_straddle_debit(self):
        assert compute_strategy_cost(long_straddle()) == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# Breakevens
# ---------------------------------------------------------------------------

class TestBreakevens:
    def test_bull_call_spread_single_breakeven(self):
        bes = find_breakevens(bull_call_spread(), 102, 0.30)
        assert len(bes) == 1
        assert bes[0] == pytest.approx(103.0, abs=0.1)

    def test_long_straddle_two_breakevens(self):
        bes = find_breakevens(long_straddle(), 100, 0.30)
        assert len(bes) == 2
        assert bes[0] == pytest.approx(91.0, abs=0.1)
        assert bes[1] == pytest.approx(109.0, abs=0.1)

    def test_bear_put_spread_single_breakeven(self):
        bes = find_breakevens(bear_put_spread(), 102, 0.30)
        assert len(bes) == 1
        assert bes[0] == pytest.approx(102.5, abs=0.1)


# ---------------------------------------------------------------------------
# Max profit / loss
# ---------------------------------------------------------------------------

class TestMaxProfitLoss:
    def test_bull_call_spread(self):
        max_p, max_l = compute_max_profit_loss(bull_call_spread(), 102, 0.30)
        assert max_p == pytest.approx(2.0, abs=0.01)
        assert max_l == pytest.approx(-3.0, abs=0.01)

    def test_long_straddle(self):
        max_p, max_l = compute_max_profit_loss(long_straddle(), 100, 0.30)
        assert max_p > 0
        assert max_l == pytest.approx(-9.0, abs=0.05)


# ---------------------------------------------------------------------------
# Weighted IV
# ---------------------------------------------------------------------------

class TestWeightedIV:
    def test_equal_premiums(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(105, "call", "short", 5.0, iv=0.30),
        ]
        assert weighted_iv(legs) == pytest.approx(0.25, abs=0.001)

    def test_unequal_premiums(self):
        legs = [
            _leg(100, "call", "long", 10.0, iv=0.20),
            _leg(105, "call", "short", 2.0, iv=0.30),
        ]
        result = weighted_iv(legs)
        expected = (10.0 * 0.20 + 2.0 * 0.30) / 12.0
        assert result == pytest.approx(expected, abs=0.001)

    def test_zero_iv_skipped(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.0),
            _leg(105, "call", "short", 3.0, iv=0.25),
        ]
        assert weighted_iv(legs) == pytest.approx(0.25, abs=0.001)


# ---------------------------------------------------------------------------
# d2 computation
# ---------------------------------------------------------------------------

class TestD2:
    def test_atm(self):
        d2 = compute_d2(spot=100, strike=100, r=0.05, sigma=0.20, t=1.0)
        expected = (math.log(1.0) + (0.05 - 0.02) * 1.0) / 0.20
        assert d2 == pytest.approx(expected, abs=0.0001)

    def test_zero_sigma_returns_zero(self):
        assert compute_d2(100, 100, 0.05, 0.0, 1.0) == 0.0

    def test_zero_time_returns_zero(self):
        assert compute_d2(100, 100, 0.05, 0.20, 0.0) == 0.0


# ---------------------------------------------------------------------------
# Probability of Profit
# ---------------------------------------------------------------------------

class TestPOP:
    def test_returns_between_0_and_1(self):
        pop = compute_pop(bull_call_spread(), 102, 0.043, 30)
        assert 0.0 <= pop <= 1.0

    def test_deep_itm_high_pop(self):
        """If spot is well above both strikes, POP should be high."""
        pop = compute_pop(bull_call_spread(), 150, 0.043, 30)
        assert pop > 0.8

    def test_deep_otm_low_pop(self):
        """If spot is well below lower strike, POP should be low."""
        pop = compute_pop(bull_call_spread(), 80, 0.043, 30)
        assert pop < 0.2

    def test_at_expiry_profitable(self):
        pop = compute_pop(bull_call_spread(), 105, 0.043, 0)
        assert pop == 1.0

    def test_at_expiry_unprofitable(self):
        pop = compute_pop(bull_call_spread(), 95, 0.043, 0)
        assert pop == 0.0


# ---------------------------------------------------------------------------
# Expected Value
# ---------------------------------------------------------------------------

class TestExpectedValue:
    def test_is_finite(self):
        ev = compute_expected_value(bull_call_spread(), 102, 0.043, 30)
        assert math.isfinite(ev)

    def test_at_expiry(self):
        ev = compute_expected_value(bull_call_spread(), 105, 0.043, 0)
        assert ev == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# Payoff curve
# ---------------------------------------------------------------------------

class TestPayoffCurve:
    def test_default_length(self):
        curve = compute_payoff_curve(bull_call_spread(), 102, 0.30, 300)
        assert len(curve) == 300

    def test_all_points_have_values(self):
        curve = compute_payoff_curve(bull_call_spread(), 102, 0.30, 50)
        for pt in curve:
            assert math.isfinite(pt.price)
            assert math.isfinite(pt.pnl)


# ---------------------------------------------------------------------------
# Full analyze_strategy
# ---------------------------------------------------------------------------

class TestAnalyzeStrategy:
    def test_bull_call_spread_full(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=bull_call_spread(),
            expiration_date="2026-12-31",
            spot_price=102,
        )
        result = analyze_strategy(req)

        assert result.success is True
        assert result.symbol == "TEST"
        assert result.strategy_cost == pytest.approx(3.0)
        assert result.max_profit == pytest.approx(2.0, abs=0.1)
        assert result.max_loss == pytest.approx(-3.0, abs=0.1)
        assert len(result.breakevens) == 1
        assert result.breakevens[0] == pytest.approx(103.0, abs=0.2)
        assert 0.0 <= result.pop <= 1.0
        assert math.isfinite(result.expected_value)
        assert len(result.curve) == 300

    def test_long_straddle_full(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=long_straddle(),
            expiration_date="2026-12-31",
            spot_price=100,
        )
        result = analyze_strategy(req)

        assert result.success is True
        assert len(result.breakevens) == 2
        assert result.max_loss == pytest.approx(-9.0, abs=0.1)
        assert 0.0 <= result.pop <= 1.0


# ---------------------------------------------------------------------------
# IV interpolation
# ---------------------------------------------------------------------------

class TestInterpolateIV:
    def test_at_strike_returns_that_iv(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(110, "call", "short", 2.0, iv=0.30),
        ]
        assert interpolate_iv_at_price(legs, 100) == pytest.approx(0.20, abs=0.001)
        assert interpolate_iv_at_price(legs, 110) == pytest.approx(0.30, abs=0.001)

    def test_between_strikes(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(110, "call", "short", 2.0, iv=0.30),
        ]
        # Midpoint should be average
        assert interpolate_iv_at_price(legs, 105) == pytest.approx(0.25, abs=0.001)
        # 75% of the way
        assert interpolate_iv_at_price(legs, 107.5) == pytest.approx(0.275, abs=0.001)

    def test_below_all_strikes(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(110, "call", "short", 2.0, iv=0.30),
        ]
        # Below lowest strike → use lowest IV
        assert interpolate_iv_at_price(legs, 80) == pytest.approx(0.20, abs=0.001)

    def test_above_all_strikes(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(110, "call", "short", 2.0, iv=0.30),
        ]
        # Above highest strike → use highest IV
        assert interpolate_iv_at_price(legs, 130) == pytest.approx(0.30, abs=0.001)

    def test_zero_iv_legs_skipped(self):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.0),
            _leg(110, "call", "short", 2.0, iv=0.25),
        ]
        # Only one valid leg → returns that IV everywhere
        assert interpolate_iv_at_price(legs, 105) == pytest.approx(0.25, abs=0.001)

    def test_iron_condor_four_legs(self):
        legs = [
            _leg(90, "put", "long", 1.0, iv=0.35),
            _leg(95, "put", "short", 2.0, iv=0.30),
            _leg(105, "call", "short", 2.0, iv=0.25),
            _leg(110, "call", "long", 1.0, iv=0.22),
        ]
        # Between 95 and 105 should interpolate
        iv_at_100 = interpolate_iv_at_price(legs, 100)
        assert 0.25 < iv_at_100 < 0.30
        assert iv_at_100 == pytest.approx(0.275, abs=0.001)
