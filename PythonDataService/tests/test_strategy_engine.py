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


# ---------------------------------------------------------------------------
# Iron condor strategy (4 legs) — edge case coverage
# ---------------------------------------------------------------------------

def iron_condor() -> list[StrategyLeg]:
    """Short 95P@$2 / Long 90P@$0.50 / Short 105C@$2 / Long 110C@$0.50
    Net credit = $3, max loss = $2 per side.
    """
    return [
        _leg(90, "put", "long", 0.50, iv=0.35),
        _leg(95, "put", "short", 2.0, iv=0.30),
        _leg(105, "call", "short", 2.0, iv=0.25),
        _leg(110, "call", "long", 0.50, iv=0.22),
    ]


def covered_call() -> list[StrategyLeg]:
    """Synthetic: long 100C@$0 (stock proxy) + short 110C@$3."""
    return [
        _leg(100, "call", "long", 0.0, iv=0.25),
        _leg(110, "call", "short", 3.0, iv=0.22),
    ]


def naked_put() -> list[StrategyLeg]:
    """Short 95P@$2."""
    return [_leg(95, "put", "short", 2.0, iv=0.30)]


class TestIronCondorPayoff:
    def test_max_profit_in_middle(self):
        """Between the short strikes (95-105), full credit is kept."""
        legs = iron_condor()
        pnl = compute_payoff_at_expiry(legs, 100)
        expected_credit = 2.0 + 2.0 - 0.50 - 0.50  # = 3.0
        assert pnl == pytest.approx(expected_credit)

    def test_max_loss_below_lower_wing(self):
        """Below 90 (long put strike), max loss is capped."""
        legs = iron_condor()
        pnl = compute_payoff_at_expiry(legs, 80)
        # Net credit 3 - spread width 5 = -2
        assert pnl == pytest.approx(-2.0)

    def test_max_loss_above_upper_wing(self):
        """Above 110 (long call strike), max loss is capped."""
        legs = iron_condor()
        pnl = compute_payoff_at_expiry(legs, 120)
        assert pnl == pytest.approx(-2.0)

    def test_breakevens(self):
        """Iron condor has two breakevens."""
        bes = find_breakevens(iron_condor(), 100, 0.30)
        assert len(bes) == 2
        assert bes[0] == pytest.approx(92.0, abs=0.2)
        assert bes[1] == pytest.approx(108.0, abs=0.2)

    def test_max_profit_loss_values(self):
        max_p, max_l = compute_max_profit_loss(iron_condor(), 100, 0.30)
        assert max_p == pytest.approx(3.0, abs=0.05)
        assert max_l == pytest.approx(-2.0, abs=0.05)


class TestIronCondorFull:
    def test_full_analysis(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=iron_condor(),
            expiration_date="2026-12-31",
            spot_price=100,
        )
        result = analyze_strategy(req)
        assert result.success is True
        assert result.strategy_cost == pytest.approx(-3.0)  # credit
        assert len(result.breakevens) == 2
        assert 0.0 <= result.pop <= 1.0
        assert result.greeks.delta is not None
        # Iron condor should be roughly delta neutral
        assert abs(result.greeks.delta) < 0.3


class TestNakedPut:
    def test_profit_above_strike(self):
        legs = naked_put()
        assert compute_payoff_at_expiry(legs, 100) == pytest.approx(2.0)

    def test_loss_below_strike(self):
        legs = naked_put()
        # At 90: put intrinsic = 5, short position: 2 - 5 = -3
        assert compute_payoff_at_expiry(legs, 90) == pytest.approx(-3.0)

    def test_unlimited_risk(self):
        """Naked put has theoretically large (if not unlimited) downside."""
        legs = naked_put()
        pnl_at_50 = compute_payoff_at_expiry(legs, 50)
        assert pnl_at_50 < -40  # large loss

    def test_single_breakeven(self):
        bes = find_breakevens(naked_put(), 100, 0.50)
        assert len(bes) == 1
        assert bes[0] == pytest.approx(93.0, abs=0.2)

    def test_strategy_cost_is_credit(self):
        assert compute_strategy_cost(naked_put()) == pytest.approx(-2.0)


class TestCoveredCall:
    def test_profit_capped_at_upper_strike(self):
        legs = covered_call()
        # Above 110: long 100C earns 10+, short 110C costs the excess
        pnl_120 = compute_payoff_at_expiry(legs, 120)
        # Long 100C: 20 - 0 = 20, short 110C: 3 - 10 = -7 → net 13
        assert pnl_120 == pytest.approx(13.0)

    def test_at_lower_strike(self):
        legs = covered_call()
        # At 100: both OTM, just net credit from short call
        pnl = compute_payoff_at_expiry(legs, 100)
        assert pnl == pytest.approx(3.0)  # only short call premium

    def test_below_lower_strike(self):
        legs = covered_call()
        # Below 100: both OTM, still just keep the credit
        pnl = compute_payoff_at_expiry(legs, 90)
        assert pnl == pytest.approx(3.0)


class TestBearPutSpread:
    def test_full_analysis(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=bear_put_spread(),
            expiration_date="2026-12-31",
            spot_price=102,
        )
        result = analyze_strategy(req)
        assert result.success is True
        assert result.strategy_cost == pytest.approx(2.5)  # debit
        assert result.max_profit == pytest.approx(2.5, abs=0.1)
        assert result.max_loss == pytest.approx(-2.5, abs=0.1)
        assert len(result.breakevens) == 1


# ---------------------------------------------------------------------------
# Greeks edge cases
# ---------------------------------------------------------------------------

class TestGreeks:
    def test_long_call_positive_delta(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = [_leg(100, "call", "long", 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, days_to_expiry=30)
        assert greeks.delta > 0

    def test_short_call_negative_delta(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = [_leg(100, "call", "short", 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, days_to_expiry=30)
        assert greeks.delta < 0

    def test_straddle_near_zero_delta(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = long_straddle()
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, days_to_expiry=30)
        # ATM straddle is roughly delta neutral
        assert abs(greeks.delta) < 0.15

    def test_straddle_positive_gamma(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = long_straddle()
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, days_to_expiry=30)
        assert greeks.gamma > 0  # long options = long gamma

    def test_straddle_negative_theta(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = long_straddle()
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, days_to_expiry=30)
        assert greeks.theta < 0  # long options = time decay

    def test_quantity_multiplier(self):
        from app.services.strategy_engine import compute_strategy_greeks
        single = [_leg(100, "call", "long", 5.0, iv=0.25, qty=1)]
        double = [_leg(100, "call", "long", 5.0, iv=0.25, qty=2)]
        g1 = compute_strategy_greeks(single, spot=100, r=0.043, days_to_expiry=30)
        g2 = compute_strategy_greeks(double, spot=100, r=0.043, days_to_expiry=30)
        assert g2.delta == pytest.approx(g1.delta * 2, abs=0.001)
        assert g2.gamma == pytest.approx(g1.gamma * 2, abs=0.001)

    def test_at_expiry_call_itm_delta_one(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = [_leg(100, "call", "long", 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=110, r=0.043, days_to_expiry=0)
        assert greeks.delta == pytest.approx(1.0, abs=0.001)

    def test_at_expiry_zero_gamma(self):
        from app.services.strategy_engine import compute_strategy_greeks
        legs = [_leg(100, "call", "long", 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=110, r=0.043, days_to_expiry=0)
        assert greeks.gamma == 0.0
