"""Tests for the options strategy analysis engine."""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import patch

import pytest

from app.models.strategy import StrategyAnalyzeRequest, StrategyLeg
from app.options.expiry import option_expiry_anchor_ms, option_expiry_dte_days, time_to_expiry_years
from app.services.bs_greeks import bs_european_price
from app.services.strategy_engine import (
    MAX_PROFIT_CAP,
    analyze_strategy,
    compute_d2,
    compute_expected_value,
    compute_max_profit_loss,
    compute_payoff_at_expiry,
    compute_payoff_curve,
    compute_pop,
    compute_strategy_cost,
    find_breakevens,
    interpolate_iv_at_price,
    weighted_iv,
)
from app.utils.session_anchors import et_midnight_ms
from scripts.regenerate_strategy_payoff_parity_fixture import CASES as PARITY_CASES

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
    @pytest.mark.parametrize(
        ("legs_factory", "price", "expected"),
        [
            pytest.param(bull_call_spread, 90, -3.0, id="bull_call_spread_below_lower"),
            pytest.param(bull_call_spread, 103, 0.0, id="bull_call_spread_at_breakeven"),
            pytest.param(bull_call_spread, 102, -1.0, id="bull_call_spread_between_strikes"),
            pytest.param(bull_call_spread, 120, 2.0, id="bull_call_spread_above_upper"),
            pytest.param(long_straddle, 100, -9.0, id="long_straddle_at_strike"),
            pytest.param(long_straddle, 115, 6.0, id="long_straddle_profitable_up"),
            pytest.param(long_straddle, 85, 6.0, id="long_straddle_profitable_down"),
        ],
    )
    def test_spread_and_straddle_payoff(self, legs_factory, price, expected):
        assert compute_payoff_at_expiry(legs_factory(), price) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("strike", "opt_type", "pos", "premium", "price", "expected"),
        [
            pytest.param(100, "call", "long", 5.0, 110, 5.0, id="single_long_call_above_strike"),
            pytest.param(100, "call", "long", 5.0, 100, -5.0, id="single_long_call_at_strike"),
            pytest.param(100, "call", "long", 5.0, 90, -5.0, id="single_long_call_below_strike"),
            pytest.param(95, "put", "short", 3.0, 100, 3.0, id="single_short_put_above_strike"),
            pytest.param(95, "put", "short", 3.0, 90, -2.0, id="single_short_put_below_strike"),
        ],
    )
    def test_single_leg_payoff(self, strike, opt_type, pos, premium, price, expected):
        legs = [_leg(strike, opt_type, pos, premium)]
        assert compute_payoff_at_expiry(legs, price) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Strategy cost
# ---------------------------------------------------------------------------


class TestStrategyCost:
    @pytest.mark.parametrize(
        ("legs_factory", "expected"),
        [
            pytest.param(bull_call_spread, 3.0, id="bull_call_spread_debit"),
            pytest.param(long_straddle, 9.0, id="long_straddle_debit"),
        ],
    )
    def test_strategy_cost_debit(self, legs_factory, expected):
        assert compute_strategy_cost(legs_factory()) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Breakevens
# ---------------------------------------------------------------------------


class TestBreakevens:
    @pytest.mark.parametrize(
        ("legs_factory", "expected"),
        [
            pytest.param(bull_call_spread, 103.0, id="bull_call_spread_single_breakeven"),
            pytest.param(bear_put_spread, 102.5, id="bear_put_spread_single_breakeven"),
        ],
    )
    def test_single_breakeven(self, legs_factory, expected):
        bes = find_breakevens(legs_factory())
        assert len(bes) == 1, "exactly one breakeven"
        assert bes[0] == pytest.approx(expected, abs=0.1), "breakeven location"


# ---------------------------------------------------------------------------
# Max profit / loss
# ---------------------------------------------------------------------------


class TestMaxProfitLoss:
    def test_bull_call_spread(self):
        extremes = compute_max_profit_loss(bull_call_spread())
        max_p, max_l = extremes.max_profit, extremes.max_loss
        assert max_p == pytest.approx(2.0, abs=0.01)
        assert max_l == pytest.approx(-3.0, abs=0.01)


# Hand-derived per share, by case name (#3186): the most a structure can gain or
# lose is read at 0, at the strikes and from the slope above the highest strike,
# so a leg whose strike sits far from spot cannot hide its loss. The legs are the
# parity fixture's own (`scripts/regenerate_strategy_payoff_parity_fixture.py`),
# so the number the browser is held to is arithmetic here, not only the canonical's
# word. (max_profit, max_loss, breakevens); None is a side with no limit.
_WHOLE_PAYOFF_EXPECTED = {
    # S=0: 5 - 590. Above 590 the put expires worthless and the credit is kept.
    "short_put_590_at_5": (5.0, -585.0, [585.0]),
    "short_put_590_at_5_times_3": (15.0, -1755.0, [585.0]),
    "long_call_100_at_5": (None, -5.0, [105.0]),
    "short_call_100_at_5": (5.0, None, [105.0]),
    # Credit 3. At S<=580: 3 - 10. Breakeven where 590 - S = 3.
    "bull_put_spread_590_580": (3.0, -7.0, [587.0]),
    # Strikes 200 apart: debit 100, so -100 at S<=400 and 200 - 100 above 600.
    "wide_bull_call_spread_400_600": (100.0, -100.0, [500.0]),
    # Debit 20 between the strikes; breakevens 570 - 20 and 610 + 20; the call has no limit.
    "long_strangle_570_610_at_10": (None, -20.0, [550.0, 630.0]),
    # Debit 9 at 100; breakevens 100 -/+ 9; the call has no limit.
    "long_straddle_100": (None, -9.0, [91.0, 109.0]),
    # Payoff is S - 101 at every price: -101 at S=0, breakeven 101.
    "synthetic_long_100": (None, -101.0, [101.0]),
    # Debit 1. Peak 10 - 1 at 110, then the two short calls outrun the long one.
    "ratio_call_spread_100_110x2": (9.0, None, [101.0, 119.0]),
    # Wings 20 wide, credit 4: the loss is 20 - 4 on either side.
    "iron_condor_wide_wings": (4.0, -16.0, [496.0, 524.0]),
    # Reaches 0 at 105 and falls again: touches zero, never crosses.
    "touches_zero_at_a_strike": (0.0, None, []),
    # Zero from 105 to 110, then rising: the breakeven is where zero is first reached.
    "crosses_zero_along_a_flat_stretch": (None, -5.0, [105.0]),
    # Payoff at 0/100/105/110/120 is -5/-5/0/0/10: the zeros are left upward.
    "crosses_zero_along_an_interior_flat_stretch": (None, -5.0, [105.0]),
    # Zero up to 100 then rising: nothing to cross from.
    "zero_from_s_zero": (None, 0.0, []),
    # 105: 5 - 9.7 + 4.7 = 0 and flat above: the loss is 9.7 - 4.7, and zero is touched, not crossed.
    "debit_equals_width_call_spread_105_110": (0.0, -5.0, []),
    # 0 up to 100 (-2.1 + 3 x 0.7), 5 at 105, then slope 1 - 3 = -2: zero again at 105 + 5/2.
    "even_money_ratio_call_spread_100_105x3": (5.0, None, [107.5]),
    # Cost 14.9 + 7.95 - 18.0 - 4.85 = 0. 15 at 105, 5 at 95, slope 2 below: 5 - 2 x 95 = -185 at S=0,
    # zero at 95 - 5/2 = 92.5. At 120 it is 0 with 120 - S above below and S - 120 above: a touch.
    "zero_net_cost_touch_at_120": (None, -185.0, [92.5]),
}


def _parity_legs(name: str) -> list[StrategyLeg]:
    return [_leg(strike, kind, pos, premium, qty=qty) for strike, kind, pos, premium, qty in PARITY_CASES[name]]


class TestExtremesAndBreakevensFromTheWholePayoff:
    def test_every_parity_case_has_a_hand_derived_answer(self):
        assert set(_WHOLE_PAYOFF_EXPECTED) == set(PARITY_CASES)

    @pytest.mark.parametrize("name", list(_WHOLE_PAYOFF_EXPECTED))
    def test_max_profit_and_loss(self, name):
        max_profit, max_loss, _ = _WHOLE_PAYOFF_EXPECTED[name]
        got = compute_max_profit_loss(_parity_legs(name))
        want_profit = math.inf if max_profit is None else max_profit
        want_loss = -math.inf if max_loss is None else max_loss
        assert got.max_profit == pytest.approx(want_profit, abs=1e-9, rel=0)
        assert got.max_loss == pytest.approx(want_loss, abs=1e-9, rel=0)

    @pytest.mark.parametrize("name", list(_WHOLE_PAYOFF_EXPECTED))
    def test_breakevens(self, name):
        _, _, breakevens = _WHOLE_PAYOFF_EXPECTED[name]
        got = find_breakevens(_parity_legs(name))
        assert len(got) == len(breakevens)
        for g, w in zip(got, breakevens, strict=True):
            assert g == pytest.approx(w, abs=1e-9, rel=0)

    def test_the_response_carries_the_whole_loss_of_a_short_put(self):
        req = StrategyAnalyzeRequest(
            symbol="SPY",
            legs=_parity_legs("short_put_590_at_5"),
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
            spot_price=590,
        )
        result = analyze_strategy(req)
        assert result.max_loss == pytest.approx(-585.0, abs=1e-9, rel=0)
        assert result.max_profit == pytest.approx(5.0, abs=1e-9, rel=0)
        assert result.breakevens == pytest.approx([585.0], abs=1e-9, rel=0)

    def test_the_response_carries_a_side_with_no_limit_as_the_cap(self):
        req = StrategyAnalyzeRequest(
            symbol="SPY",
            legs=_parity_legs("synthetic_long_100"),
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
            spot_price=100,
        )
        result = analyze_strategy(req)
        assert result.max_profit == MAX_PROFIT_CAP
        assert result.max_loss == pytest.approx(-101.0, abs=1e-9, rel=0)


# ---------------------------------------------------------------------------
# Weighted IV
# ---------------------------------------------------------------------------


class TestWeightedIV:
    @pytest.mark.parametrize(
        ("long_premium", "long_iv", "short_premium", "short_iv", "expected"),
        [
            pytest.param(5.0, 0.20, 5.0, 0.30, 0.25, id="equal_premiums"),
            pytest.param(10.0, 0.20, 2.0, 0.30, (10.0 * 0.20 + 2.0 * 0.30) / 12.0, id="unequal_premiums"),
            pytest.param(5.0, 0.0, 3.0, 0.25, 0.25, id="zero_iv_skipped"),
        ],
    )
    def test_weighted_iv(self, long_premium, long_iv, short_premium, short_iv, expected):
        legs = [
            _leg(100, "call", "long", long_premium, iv=long_iv),
            _leg(105, "call", "short", short_premium, iv=short_iv),
        ]
        assert weighted_iv(legs) == pytest.approx(expected, abs=0.001)


# ---------------------------------------------------------------------------
# d2 computation
# ---------------------------------------------------------------------------


class TestD2:
    def test_atm(self):
        d2 = compute_d2(spot=100, strike=100, r=0.05, sigma=0.20, t=1.0)
        expected = (math.log(1.0) + (0.05 - 0.02) * 1.0) / 0.20
        assert d2 == pytest.approx(expected, abs=0.0001)

    @pytest.mark.parametrize(
        ("sigma", "t"),
        [
            pytest.param(0.0, 1.0, id="zero_sigma_returns_zero"),
            pytest.param(0.20, 0.0, id="zero_time_returns_zero"),
        ],
    )
    def test_degenerate_sigma_or_time_returns_zero(self, sigma, t):
        assert compute_d2(100, 100, 0.05, sigma, t) == 0.0


# ---------------------------------------------------------------------------
# Probability of Profit
# ---------------------------------------------------------------------------


class TestPOP:
    def test_zero_cost_call_spread_profits_above_its_lower_strike(self):
        legs = [_leg(116, "call", "short", 0.05), _leg(106, "call", "long", 0.05)]

        # The expiry payoff is zero through 106 and positive above it.
        # N(d2) at 106 for S=100, sigma=0.25, T=30/365, r=0 is 0.1979902692.
        assert compute_pop(legs, 100, 0.0, 30 / 365.0) == pytest.approx(0.1979902692, abs=1e-6, rel=0)

    def test_zero_payoff_rounding_residue_is_not_profit(self):
        legs = [
            _leg(116, "call", "short", 0.04),
            _leg(106, "call", "long", 0.03),
            _leg(126, "call", "long", 0.01),
        ]

        # Premium cancellation leaves a tiny positive float below 106, not a real profit.
        assert compute_pop(legs, 100, 0.0, 30 / 365.0) == pytest.approx(0.1979902692, abs=1e-6, rel=0)
        assert compute_pop(legs, 100, 0.0, 0.0) == 0.0

    def test_pop_by_moneyness(self):
        legs = bull_call_spread()
        pop = compute_pop(legs, 102, 0.043, 30 / 365.0)
        assert 0.0 <= pop <= 1.0, "returns between 0 and 1"
        # If spot is well above both strikes, POP should be high.
        pop = compute_pop(legs, 150, 0.043, 30 / 365.0)
        assert pop > 0.8, "deep ITM has a high POP"
        # If spot is well below lower strike, POP should be low.
        pop = compute_pop(legs, 80, 0.043, 30 / 365.0)
        assert pop < 0.2, "deep OTM has a low POP"

    @pytest.mark.parametrize(
        ("spot", "expected"),
        [
            pytest.param(105, 1.0, id="at_expiry_profitable"),
            pytest.param(95, 0.0, id="at_expiry_unprofitable"),
        ],
    )
    def test_at_expiry_pop(self, spot, expected):
        pop = compute_pop(bull_call_spread(), spot, 0.043, 0.0)
        assert pop == expected


# ---------------------------------------------------------------------------
# Expected Value
# ---------------------------------------------------------------------------


class TestExpectedValue:
    def test_is_finite(self):
        ev = compute_expected_value(bull_call_spread(), 102, 0.043, 30 / 365.0)
        assert math.isfinite(ev)

    def test_at_expiry(self):
        ev = compute_expected_value(bull_call_spread(), 105, 0.043, 0.0)
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
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
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
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
            spot_price=100,
        )
        result = analyze_strategy(req)

        assert result.success is True
        assert len(result.breakevens) == 2
        assert result.max_loss == pytest.approx(-9.0, abs=0.1)
        assert 0.0 <= result.pop <= 1.0


# ---------------------------------------------------------------------------
# Time to expiry — measured to the expiry anchor, the same instant the chain
# snapshot's rate tenor is measured to (#2789, #2934)
# ---------------------------------------------------------------------------

# 23:30 ET on 2027-03-05 (EST) is already 04:30 UTC on 2027-03-06.
_LATE_EVENING_ET_2027_03_05 = et_midnight_ms(date(2027, 3, 5)) + 23 * 3_600_000 + 30 * 60_000
# The expiry anchor is 16:00 New York time on 2027-03-06: 16.5 hours after the
# instant above.
_ANCHOR_2027_03_06 = option_expiry_anchor_ms(date(2027, 3, 6))


class TestTimeToExpiry:
    def test_measures_to_the_anchor_not_the_whole_day(self):
        assert time_to_expiry_years(_ANCHOR_2027_03_06, _LATE_EVENING_ET_2027_03_05) == pytest.approx(
            16.5 / 24 / 365, abs=1e-12, rel=0
        )

    def test_dte_days_is_the_same_instant_in_days(self):
        assert option_expiry_dte_days(_ANCHOR_2027_03_06, _LATE_EVENING_ET_2027_03_05) == pytest.approx(
            16.5 / 24, abs=1e-9, rel=0
        )

    def test_an_expired_contract_is_zero(self):
        assert time_to_expiry_years(option_expiry_anchor_ms(date(2027, 2, 19)), _LATE_EVENING_ET_2027_03_05) == 0.0

    def test_analysis_prices_over_the_same_anchor_ttm(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=[_leg(100, "call", "long", 5.0)],
            expiration_ms=_ANCHOR_2027_03_06,
            spot_price=102,
            risk_free_rate=0.04,
            include_leg_diagnostics=True,
        )
        with patch("app.services.strategy_engine.now_ms_utc", return_value=_LATE_EVENING_ET_2027_03_05):
            result = analyze_strategy(req)

        sixteen_and_a_half_hours = bs_european_price(
            spot=102, strike=100, ttm_years=16.5 / 24 / 365, rate=0.04, volatility=0.25, is_call=True, dividend=0.0
        )
        assert result.success is True
        assert result.leg_diagnostics[0].current_theoretical == round(sixteen_and_a_half_hours, 6)


# ---------------------------------------------------------------------------
# IV interpolation
# ---------------------------------------------------------------------------


class TestInterpolateIV:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            pytest.param(100, 0.20, id="at_strike_returns_that_iv_lower"),
            pytest.param(110, 0.30, id="at_strike_returns_that_iv_upper"),
            # Midpoint should be average
            pytest.param(105, 0.25, id="between_strikes_midpoint"),
            # 75% of the way
            pytest.param(107.5, 0.275, id="between_strikes_75_percent"),
            # Below lowest strike → use lowest IV
            pytest.param(80, 0.20, id="below_all_strikes"),
            # Above highest strike → use highest IV
            pytest.param(130, 0.30, id="above_all_strikes"),
        ],
    )
    def test_interpolates_between_two_strikes(self, price, expected):
        legs = [
            _leg(100, "call", "long", 5.0, iv=0.20),
            _leg(110, "call", "short", 2.0, iv=0.30),
        ]
        assert interpolate_iv_at_price(legs, price) == pytest.approx(expected, abs=0.001)

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
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            # Between the short strikes (95-105), full credit is kept.
            pytest.param(100, 2.0 + 2.0 - 0.50 - 0.50, id="max_profit_in_middle"),  # = 3.0
            # Below 90 (long put strike), max loss is capped: net credit 3 - spread width 5 = -2
            pytest.param(80, -2.0, id="max_loss_below_lower_wing"),
            # Above 110 (long call strike), max loss is capped.
            pytest.param(120, -2.0, id="max_loss_above_upper_wing"),
        ],
    )
    def test_payoff_at_expiry(self, price, expected):
        legs = iron_condor()
        pnl = compute_payoff_at_expiry(legs, price)
        assert pnl == pytest.approx(expected)

    def test_breakevens(self):
        """Iron condor has two breakevens."""
        bes = find_breakevens(iron_condor())
        assert len(bes) == 2
        assert bes[0] == pytest.approx(92.0, abs=0.2)
        assert bes[1] == pytest.approx(108.0, abs=0.2)

    def test_max_profit_loss_values(self):
        extremes = compute_max_profit_loss(iron_condor())
        max_p, max_l = extremes.max_profit, extremes.max_loss
        assert max_p == pytest.approx(3.0, abs=0.05)
        assert max_l == pytest.approx(-2.0, abs=0.05)


class TestIronCondorFull:
    def test_full_analysis(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=iron_condor(),
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
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
    def test_payoff_at_expiry(self):
        legs = naked_put()
        assert compute_payoff_at_expiry(legs, 100) == pytest.approx(2.0), "profit above strike"
        # At 90: put intrinsic = 5, short position: 2 - 5 = -3
        assert compute_payoff_at_expiry(legs, 90) == pytest.approx(-3.0), "loss below strike"
        # Naked put has theoretically large (if not unlimited) downside.
        pnl_at_50 = compute_payoff_at_expiry(legs, 50)
        assert pnl_at_50 < -40, "unlimited risk: large loss"

    def test_single_breakeven(self):
        bes = find_breakevens(naked_put())
        assert len(bes) == 1
        assert bes[0] == pytest.approx(93.0, abs=0.2)

    def test_strategy_cost_is_credit(self):
        assert compute_strategy_cost(naked_put()) == pytest.approx(-2.0)


class TestCoveredCall:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            # Above 110: long 100C earns 10+, short 110C costs the excess
            # Long 100C: 20 - 0 = 20, short 110C: 3 - 10 = -7 → net 13
            pytest.param(120, 13.0, id="profit_capped_at_upper_strike"),
            # At 100: both OTM, just net credit from short call (only short call premium)
            pytest.param(100, 3.0, id="at_lower_strike"),
            # Below 100: both OTM, still just keep the credit
            pytest.param(90, 3.0, id="below_lower_strike"),
        ],
    )
    def test_payoff_at_expiry(self, price, expected):
        legs = covered_call()
        pnl = compute_payoff_at_expiry(legs, price)
        assert pnl == pytest.approx(expected)


class TestBearPutSpread:
    def test_full_analysis(self):
        req = StrategyAnalyzeRequest(
            symbol="TEST",
            legs=bear_put_spread(),
            expiration_ms=option_expiry_anchor_ms(date(2026, 12, 31)),
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
    @pytest.mark.parametrize(
        ("pos", "sign"),
        [
            pytest.param("long", 1, id="long_call_positive_delta"),
            pytest.param("short", -1, id="short_call_negative_delta"),
        ],
    )
    def test_call_delta_sign(self, pos, sign):
        from app.services.strategy_engine import compute_strategy_greeks

        legs = [_leg(100, "call", pos, 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, ttm_years=30 / 365.0)
        assert greeks.delta * sign > 0

    def test_long_straddle_greeks(self):
        from app.services.strategy_engine import compute_strategy_greeks

        legs = long_straddle()
        greeks = compute_strategy_greeks(legs, spot=100, r=0.043, ttm_years=30 / 365.0)
        # ATM straddle is roughly delta neutral
        assert abs(greeks.delta) < 0.15, "straddle has near-zero delta"
        assert greeks.gamma > 0, "straddle has positive gamma (long options = long gamma)"
        assert greeks.theta < 0, "straddle has negative theta (long options = time decay)"

    def test_quantity_multiplier(self):
        from app.services.strategy_engine import compute_strategy_greeks

        single = [_leg(100, "call", "long", 5.0, iv=0.25, qty=1)]
        double = [_leg(100, "call", "long", 5.0, iv=0.25, qty=2)]
        g1 = compute_strategy_greeks(single, spot=100, r=0.043, ttm_years=30 / 365.0)
        g2 = compute_strategy_greeks(double, spot=100, r=0.043, ttm_years=30 / 365.0)
        assert g2.delta == pytest.approx(g1.delta * 2, abs=0.001)
        assert g2.gamma == pytest.approx(g1.gamma * 2, abs=0.001)

    def test_at_expiry_call_greeks(self):
        from app.services.strategy_engine import compute_strategy_greeks

        legs = [_leg(100, "call", "long", 5.0, iv=0.25)]
        greeks = compute_strategy_greeks(legs, spot=110, r=0.043, ttm_years=0.0)
        assert greeks.delta == pytest.approx(1.0, abs=0.001), "ITM call at expiry has delta one"
        assert greeks.gamma == 0.0, "at expiry gamma is zero"
