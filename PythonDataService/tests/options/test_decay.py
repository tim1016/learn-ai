"""A structure's daily time decay: each leg's own, signed by its side, summed. And each way it has none.

The sums are worked by hand from per-share decays written here, so that what
is proven is the composition. What one contract loses in a day is the
pricer's own figure, proven against QuantLib in its own tests (fixtures
AM-004 and AM-005).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.options import decay
from app.options.american import ONE_DAY_MS, AmericanDailyDecay, CashDividend, american_daily_decay, years_from_ms
from app.options.contract import OptionContract
from app.options.decay import (
    DecayNotEstimated,
    LegDecay,
    decay_not_estimated,
    goes_ex_soon,
    picks_daily_decay,
    structure_daily_decay,
)
from app.options.requirement import EntrySize
from app.options.resolver import DividendOn
from app.options.structure import ResolvedLeg, ResolvedStructure

# The resolver's own scenes: SPY at 585 on a dollar-wide ladder, selected on a Tuesday at 10:31:02.
from tests.options import test_resolver as scenes

_ONE = EntrySize(multiplier=100, units=1)
# The underlying's price every leg here was priced against: SPY's mid in the resolver's scenes.
_SPOT = 585.11


def _lost(per_share: float, *, cash_gone_ex: float = 0.0) -> LegDecay:
    """A contract worth 5.00 a share now that the model prices ``per_share`` away from that a day later."""
    return LegDecay(
        AmericanDailyDecay(
            price_now=5.0, price_a_day_later=5.0 + per_share, later_price_is_exercise_value=False, cash_gone_ex=cash_gone_ex
        )
    )


def _structure(*legs: tuple[str, str, str, int, int]) -> ResolvedStructure:
    """Legs as (leg id, bought or sold, call or put, strike, ratio), all expiring the same Friday."""
    return ResolvedStructure(
        legs=tuple(
            ResolvedLeg(leg_id=leg_id, contract=scenes._contract(right, strike), position=position, ratio=ratio)
            for leg_id, position, right, strike, ratio in legs
        )
    )


def _close(actual: float, expected: float) -> None:
    np.testing.assert_allclose(actual, expected, atol=1e-9, rtol=0, equal_nan=False)


_SPREAD = _structure(("bought", "long", "call", 585, 1), ("sold", "short", "call", 590, 1))
_CONDOR = _structure(
    ("long_put", "long", "put", 570, 1),
    ("short_put", "short", "put", 575, 1),
    ("short_call", "short", "call", 595, 1),
    ("long_call", "long", "call", 600, 1),
)


def test_a_bought_spread_loses_what_its_bought_leg_loses_less_what_its_sold_leg_loses() -> None:
    """The 585 call loses 50 cents a share in a day and the 590 call 30. Bought one and sold the other:
    -0.50 + 0.30 = -0.20 a share, which is $20.00 a structure, and $60.00 for three."""
    decays = {"bought": _lost(-0.50), "sold": _lost(-0.30)}

    one = structure_daily_decay(_SPREAD, _ONE, decays, underlying_price=_SPOT)
    three = structure_daily_decay(_SPREAD, EntrySize(multiplier=100, units=3), decays, underlying_price=_SPOT)

    assert one.estimated and one.why_not is None and one.legs_without == ()
    _close(one.per_share, -0.20)
    _close(one.per_structure, -20.00)
    _close(one.total, -20.00)
    _close(three.per_structure, -20.00)
    _close(three.total, -60.00)


def test_an_iron_condor_gains_with_a_day_what_its_sold_legs_lose_less_what_its_bought_legs_lose() -> None:
    """Bought wings losing 20 and 25 cents, sold legs losing 35 and 40:
    -0.20 + 0.35 + 0.40 - 0.25 = +0.30 a share, $30.00 a structure gained."""
    decays = {"long_put": _lost(-0.20), "short_put": _lost(-0.35), "short_call": _lost(-0.40), "long_call": _lost(-0.25)}

    seen = structure_daily_decay(_CONDOR, _ONE, decays, underlying_price=_SPOT)

    _close(seen.per_share, 0.30)
    _close(seen.per_structure, 30.00)
    _close(seen.total, 30.00)


def test_a_sold_leg_gains_what_its_contract_loses() -> None:
    sold = _structure(("sold", "short", "call", 590, 1))

    _close(structure_daily_decay(sold, _ONE, {"sold": _lost(-0.30)}, underlying_price=_SPOT).total, 30.00)


def test_a_legs_ratio_and_the_contracts_own_multiplier_both_count() -> None:
    """A butterfly: one, two sold, one. -0.10 + 2 x 0.45 - 0.12 = +0.68 a share. At ten shares a contract, $6.80."""
    butterfly = _structure(
        ("lower", "long", "call", 580, 1), ("body", "short", "call", 585, 2), ("upper", "long", "call", 590, 1)
    )
    decays = {"lower": _lost(-0.10), "body": _lost(-0.45), "upper": _lost(-0.12)}

    seen = structure_daily_decay(butterfly, EntrySize(multiplier=10, units=2), decays, underlying_price=_SPOT)

    _close(seen.per_share, 0.68)
    _close(seen.per_structure, 6.80)
    _close(seen.total, 13.60)


def test_a_position_that_gains_with_a_day_is_shown_gaining_and_never_held_at_zero() -> None:
    """A put deep in the money ahead of an ex-date gains the interest on its strike: +0.035 a share was measured."""
    bought_put = _structure(("deep", "long", "put", 600, 1))

    seen = structure_daily_decay(bought_put, _ONE, {"deep": _lost(0.035)}, underlying_price=_SPOT)

    _close(seen.total, 3.50)
    assert seen.total > 0


@pytest.mark.parametrize("why", list(DecayNotEstimated))
def test_one_leg_with_no_figure_leaves_the_structure_with_none_for_that_legs_reason(why: DecayNotEstimated) -> None:
    """No part-sum: three legs of four priced is not the structure's decay."""
    decays = {
        "long_put": _lost(-0.20),
        "short_put": LegDecay(None, why, "no figure"),
        "short_call": _lost(-0.40),
        "long_call": LegDecay(None, DecayNotEstimated.NOT_PRICED, "another reason, later in the structure"),
    }

    seen = structure_daily_decay(_CONDOR, _ONE, decays, underlying_price=_SPOT)

    assert not seen.estimated
    assert (seen.per_share, seen.per_structure, seen.total) == (None, None, None)
    # The first leg without one, in the structure's order, gives the reason. Every such leg is named.
    assert (seen.why_not, seen.legs_without) == (why, ("short_put", "long_call"))


def test_a_structure_priced_on_inputs_nobody_checked_has_no_decay_for_any_leg() -> None:
    seen = decay_not_estimated(_SPREAD, DecayNotEstimated.INPUTS_NOT_KNOWN)

    assert (seen.estimated, seen.why_not, seen.legs_without) == (False, DecayNotEstimated.INPUTS_NOT_KNOWN, ("bought", "sold"))


_SOLD_580_BOUGHT_590 = _structure(("sold", "short", "call", 580, 1), ("bought", "long", "call", 590, 1))
_GOING_EX = {"sold": _lost(-1.20, cash_gone_ex=1.75), "bought": _lost(-0.70, cash_gone_ex=1.75)}


@pytest.mark.parametrize(
    ("price", "named"),
    [
        pytest.param(580.01, ("sold",), id="a cent in the money"),
        pytest.param(580.0, (), id="at the money exactly"),
        pytest.param(579.99, (), id="a cent out of the money"),
        pytest.param(540.0, (), id="far out of the money"),
    ],
)
def test_a_sold_call_is_named_only_while_its_strike_is_below_the_price(price: float, named: tuple[str, ...]) -> None:
    """The model counts no fall by the dividend for a call at or out of the money, so there is nothing to warn
    of. The figure does not turn on the price handed in: only the naming does."""
    seen = structure_daily_decay(_SOLD_580_BOUGHT_590, _ONE, _GOING_EX, underlying_price=price)

    assert seen.sold_calls_in_the_money_at_an_ex_date == named
    _close(seen.total, 50.00)


def test_only_a_sold_call_is_named_never_a_bought_call_or_a_put_however_deep_in_the_money() -> None:
    """An iron condor with the stock above every strike: the sold call and the bought call are both in the
    money, and both puts are out of it. Only the sold call can be assigned."""
    decays = {
        "long_put": _lost(0.60, cash_gone_ex=1.75),
        "short_put": _lost(0.90, cash_gone_ex=1.75),
        "short_call": _lost(-1.20, cash_gone_ex=1.75),
        "long_call": _lost(-0.70, cash_gone_ex=1.75),
    }

    above_every_strike = structure_daily_decay(_CONDOR, _ONE, decays, underlying_price=610.0)
    below_every_strike = structure_daily_decay(_CONDOR, _ONE, decays, underlying_price=560.0)

    assert above_every_strike.sold_calls_in_the_money_at_an_ex_date == ("short_call",)
    assert below_every_strike.sold_calls_in_the_money_at_an_ex_date == ()
    # 0.60 - 0.90 + 1.20 - 0.70 = +0.20 a share, wherever the stock is.
    _close(above_every_strike.total, 20.00)
    _close(below_every_strike.total, 20.00)


def test_with_no_dividend_going_ex_inside_the_day_no_sold_call_is_named() -> None:
    decays = {"sold": _lost(-1.20), "bought": _lost(-0.70)}

    seen = structure_daily_decay(_SOLD_580_BOUGHT_590, _ONE, decays, underlying_price=_SPOT)

    assert seen.sold_calls_in_the_money_at_an_ex_date == ()


@pytest.mark.parametrize("no_price", [0.0, -585.11, float("nan"), float("inf")])
def test_the_price_the_legs_were_priced_against_is_a_number_above_zero(no_price: float) -> None:
    with pytest.raises(ValueError, match="above zero"):
        structure_daily_decay(_SPREAD, _ONE, {"bought": _lost(-0.50), "sold": _lost(-0.30)}, underlying_price=no_price)


def test_a_dividend_going_ex_inside_the_day_takes_about_itself_off_a_call_deep_in_the_money_and_nothing_off_one_out_of_it() -> None:
    """What the warning's words stand on, measured with the pricer itself: SPY at 585.11, 3.2 days to expiry,
    a volatility of 0.20, a rate of 0.045, $1.75 going ex. Each call's decay with the ex-date 23 hours off,
    inside the day, less its decay with the ex-date 25 hours off, outside it.

    Deep in the money the call falls by most of the dividend. At or out of the money it does not move, because
    its price now already has the dividend taken off. So the fall counted for a sold call is "up to" the
    dividend, and is never more.
    """
    hour_ms = ONE_DAY_MS // 24

    def moved(strike: int) -> float:
        def decay_with_the_ex_date(hours_off: int) -> float:
            worked = american_daily_decay(
                spot=_SPOT,
                strike=float(strike),
                years_to_expiry=years_from_ms(ONE_DAY_MS * 32 // 10),
                rate=0.045,
                volatility=0.20,
                right="call",
                dividends=(CashDividend(years_from_ms(hours_off * hour_ms), 1.75),),
            )
            assert worked is not None
            return worked.decay

        return decay_with_the_ex_date(23) - decay_with_the_ex_date(25)

    np.testing.assert_allclose(moved(540), -1.60, atol=5e-3, rtol=0, equal_nan=False)
    np.testing.assert_allclose(moved(575), -0.74, atol=5e-3, rtol=0, equal_nan=False)
    for strike in (580, 585, 590, 595):
        np.testing.assert_allclose(moved(strike), 0.0, atol=0.02, rtol=0, equal_nan=False)
    # Never more than the dividend, at any strike.
    assert all(-1.75 <= moved(strike) <= 0.02 for strike in (540, 560, 570, 575, 580, 585, 590, 595))


def test_a_structures_decay_is_a_figure_or_a_reason_and_never_both_or_neither() -> None:
    with pytest.raises(ValueError, match="never both"):
        decay.StructureDecay(None, None, None)
    with pytest.raises(ValueError, match="never both"):
        decay.StructureDecay(-0.2, -20.0, -20.0, why_not=DecayNotEstimated.NOT_PRICED)


def test_a_decay_for_a_leg_the_structure_does_not_have_or_none_for_one_it_has_is_the_callers_mistake() -> None:
    with pytest.raises(ValueError, match="one decay for each of the legs"):
        structure_daily_decay(_SPREAD, _ONE, {"bought": _lost(-0.50)}, underlying_price=_SPOT)
    with pytest.raises(ValueError, match="one decay for each of the legs"):
        structure_daily_decay(_SPREAD, _ONE, {"bought": _lost(-0.50), "sold": _lost(-0.30), "third": _lost(-0.1)}, underlying_price=_SPOT)


def test_a_legs_decay_is_a_figure_or_a_reason_and_never_both_or_neither() -> None:
    with pytest.raises(ValueError):
        LegDecay(None)
    with pytest.raises(ValueError):
        LegDecay(_lost(-0.5).worked, DecayNotEstimated.NOT_PRICED)


# ---- Each pick's own decay, as the search works it out ----------------------------------------


def _plan_of_two_calls():
    return scenes._plan(
        scenes._leg("bought", "call", {"selector": "absolute", "strike": 585.0}),
        scenes._leg("sold", "call", {"selector": "absolute", "strike": 590.0}, position="short"),
    )


def _priced_apart(scene: scenes._Scene, contract: OptionContract, volatility: float, dividends: tuple[DividendOn, ...] = ()):
    """The pricer's own answer for one contract, with every input written out again here: the underlying's mid
    of 585.11, the time from the selection to the expiry day's close, and the scene's rate, tree and dividends."""
    return american_daily_decay(
        spot=585.11,
        strike=float(contract.strike),
        years_to_expiry=years_from_ms(session_close_ms_utc(contract.expiry_date) - scene.selection_ms),
        rate=0.045,
        volatility=volatility,
        right=contract.right,
        dividends=tuple(CashDividend(years_from_ms(paid.ex_ms - scene.selection_ms), paid.cash) for paid in dividends),
        steps=101,
    )


def test_each_pick_is_priced_a_day_on_at_its_own_solved_volatility_against_the_underlyings_mid() -> None:
    scene = scenes._scene()
    resolution = scene.resolve(_plan_of_two_calls())
    solved = {leg.leg_id: leg.candidates for leg in resolution.evidence.legs}

    decays = picks_daily_decay(resolution)

    assert list(decays) == ["bought", "sold"]
    for leg_id, contract in resolution.picked.items():
        (pick,) = [candidate for candidate in solved[leg_id] if candidate.passed_over is None]
        apart = _priced_apart(scene, contract, pick.priced.implied_volatility)
        worked = decays[leg_id].worked
        assert decays[leg_id].why_not is None
        _close(worked.price_now, apart.price_now)
        _close(worked.price_a_day_later, apart.price_a_day_later)
        _close(worked.decay, apart.decay)
        # A call three days from expiry loses value with a day, and the model's price now is its quote's mid.
        assert worked.decay < 0 and worked.cash_gone_ex == 0.0
        _close(worked.price_now, pick.priced.mid)


def test_a_dividend_going_ex_inside_the_day_reaches_each_picks_decay() -> None:
    """$1.75 going ex at Wednesday's open, 23 hours after the Tuesday 10:31 selection."""
    going_ex = (DividendOn(ex_ms=session_open_ms_utc(scenes._WEDNESDAY), cash=1.75),)
    scene = scenes._scene(model=replace(scenes._MODEL, dividends=going_ex))
    resolution = scene.resolve(_plan_of_two_calls())
    (pick,) = [c for c in resolution.evidence.legs[0].candidates if c.passed_over is None]

    decays = picks_daily_decay(resolution)

    worked = decays["bought"].worked
    apart = _priced_apart(scene, resolution.picked["bought"], pick.priced.implied_volatility, going_ex)
    assert (worked.cash_gone_ex, apart.cash_gone_ex) == (1.75, 1.75)
    _close(worked.decay, apart.decay)
    # Left out, the later price would be worked at the stock's price today: another number altogether.
    without = _priced_apart(scene, resolution.picked["bought"], pick.priced.implied_volatility)
    assert abs(apart.decay - without.decay) > 1e-3


def test_a_dividend_is_inside_the_day_by_the_moment_the_picks_were_priced_at_not_the_bars_close() -> None:
    """The picks are priced at the selection, two seconds after the decision bar closed. A dividend going ex one
    second short of 24 hours after the selection is inside the day. Counted from the bar's close it would be a
    second outside it, and the later price would be worked at the wrong stock price."""
    scene = scenes._scene()
    going_ex = (DividendOn(ex_ms=scene.selection_ms + ONE_DAY_MS - 1_000, cash=1.75),)
    assert going_ex[0].ex_ms - scene.decision_bar_close_ms > ONE_DAY_MS
    resolution = replace(scene, model=replace(scenes._MODEL, dividends=going_ex)).resolve(_plan_of_two_calls())

    decays = picks_daily_decay(resolution)

    assert [leg.worked.cash_gone_ex for leg in decays.values()] == [1.75, 1.75]


def test_a_pick_whose_quote_no_volatility_explains_has_no_decay_and_says_why() -> None:
    """The 590 call quoted far above anything the model can reach. A rule by strike still picks it."""
    scene = scenes._scene()
    sold = scenes._contract("call", 590)
    resolution = scene.holding(sold, scenes._quote("400.00", "400.04", scene.selection_ms - 400)).resolve(_plan_of_two_calls())

    decays = picks_daily_decay(resolution)

    assert decays["bought"].worked is not None
    assert (decays["sold"].worked, decays["sold"].why_not) == (None, DecayNotEstimated.NO_VOLATILITY)


def test_a_pick_the_model_has_no_price_for_a_day_later_has_no_decay_and_is_never_a_zero() -> None:
    """A call ten dollars out of the money that expires tomorrow, looked at two minutes before today's close.
    Today the tree resolves its strike. With two minutes left a day later the strike is some forty standard
    deviations away, the tree does not resolve it, and the pricer answers nothing."""
    scene = scenes._scene(selected_at=(15, 58, 0))
    far = scenes._leg(
        "far", "call", {"selector": "absolute", "strike": 595.0},
        expiry={"selector": "absolute", "expiration_ms": scenes.option_expiry_anchor_ms(scenes._WEDNESDAY)},
    )
    resolution = scene.resolve(scenes._plan(far))
    (pick,) = [c for c in resolution.evidence.legs[0].candidates if c.passed_over is None]
    assert pick.priced.implied_volatility is not None

    decays = picks_daily_decay(resolution)

    assert _priced_apart(scene, resolution.picked["far"], pick.priced.implied_volatility) is None
    assert (decays["far"].worked, decays["far"].why_not) == (None, DecayNotEstimated.NOT_PRICED)


def test_an_input_the_pricer_refuses_a_day_later_is_no_decay_with_the_pricers_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuses(**_inputs: object) -> None:
        raise ValueError("the dividends going ex inside the day are worth as much as the stock itself")

    monkeypatch.setattr(decay, "american_daily_decay", refuses)

    decays = picks_daily_decay(scenes._scene().resolve(_plan_of_two_calls()))

    assert {leg.why_not for leg in decays.values()} == {DecayNotEstimated.NOT_PRICED}
    assert decays["bought"].detail == "the dividends going ex inside the day are worth as much as the stock itself"


@pytest.mark.parametrize(
    ("ex_after_ms", "soon"),
    [
        pytest.param(1, True, id="a millisecond from now"),
        pytest.param(ONE_DAY_MS, True, id="a day out"),
        pytest.param(3 * ONE_DAY_MS, True, id="exactly three days out"),
        pytest.param(3 * ONE_DAY_MS + 1, False, id="a millisecond past three days"),
        pytest.param(0, False, id="going ex this instant: it has gone"),
        pytest.param(-ONE_DAY_MS, False, id="gone ex yesterday"),
    ],
)
def test_a_dividend_is_near_when_it_goes_ex_within_three_days_from_now(ex_after_ms: int, soon: bool) -> None:
    now = 1_791_302_400_000

    assert goes_ex_soon([now + 40 * ONE_DAY_MS, now + ex_after_ms], now) is soon
    assert goes_ex_soon([], now) is False
