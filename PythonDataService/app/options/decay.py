"""What a day takes from an option structure: its daily time decay, in dollars per 24 hours.

Formula: with ``s_i`` = +1 for a bought leg and -1 for a sold one, ``r_i`` its
  ratio, ``θ_i`` its contract's daily decay per share (the model's price 24
  hours later less its price now), ``m`` the contract multiplier and ``N``
  the structures opened:
    per share of one structure = Σ s_i · r_i · θ_i
    one structure              = m · Σ s_i · r_i · θ_i      (dollars per 24 hours)
    the entry                  = N · m · Σ s_i · r_i · θ_i
  Below zero the position loses that much in 24 hours, with the stock's
  price, each leg's volatility and the rate held. A sold leg's sign is
  turned: it gains what its contract loses.
  Each ``θ_i`` is worked at the leg's own implied volatility, the one solved
  from its mid quote when it was picked, against the same stock price, rate
  and dividends that solve was given.
Reference: Hull, Options, Futures, and Other Derivatives, "The Greek
  Letters": a Greek of a portfolio is the sum of its positions' Greeks, each
  weighted by its quantity, as its delta is. Each ``θ_i`` is
  ``app/options/american.py``'s ``american_daily_decay``, which carries its
  own definition, source and fixtures (AM-004, AM-005). ADR 0076 decisions 6
  and 23.
Canonical implementation: this file for the sum; ``app/options/american.py``
  for each contract's decay.
Validated against: tests/options/test_decay.py (a spread and an iron condor
  summed by hand).

It is per 24 hours by the clock, not per trading session: three such days
pass over a weekend before the next session, and the figure is one of them.

No part of it is shown alone. When one leg has no figure the structure has
none: a sum that leaves a leg out reads as the whole and is not.

It is never held at zero. A put deep in the money ahead of an ex-date gains
with a day, by the interest on its strike, and a structure can gain too.

A dividend going ex inside the day is taken off the stock's price a day
later, in full (``app.options.american``). What that does to a call depends
on where its strike is. A call deep in the money falls by about the
dividend. A call at or out of the money hardly moves: its price now already
has the dividend taken off. Measured at a price of 585.11, 3.2 days from
expiry, a volatility of 0.20, a rate of 0.045 and $1.75 going ex in 23 hours
against 25, the ex-date inside the day moved the decay by -1.60 a share at a
strike of 540, by -0.74 at 575, and by two cents or less from 580 up
(tests/options/test_decay.py).

For a sold call that fall is counted here as a gain, of up to the dividend.
It is a gain the seller may not get: a call in the money can be exercised
just before the ex-date, and the sold leg is then assigned. So a sold call
that is in the money now, with cash going ex inside the day, is named, and
whoever shows the figure can say so. A sold call at or out of the money is
not named: the model counts no such fall for it. The figure is the same
either way. Only the naming turns on where the strike is.

Near an ex-date each contract's figure is rougher than elsewhere: the tree
is coarsest when the later price sits a day or so before the ex-date
(``app.options.american``, fixture AM-005).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.options.american import ONE_DAY_MS, AmericanDailyDecay, CashDividend, american_daily_decay, years_from_ms
from app.options.requirement import EntrySize
from app.options.resolver import Resolution
from app.options.structure import ResolvedStructure

# A dividend going ex within this long is near: each contract's figure is then a rough estimate.
NEAR_EX_DATE_MS = 3 * ONE_DAY_MS


class DecayNotEstimated(StrEnum):
    """Why a leg, and so its structure, has no daily decay."""

    # The pricing model's inputs are not known to be true, such as dividends that could not be looked up.
    INPUTS_NOT_KNOWN = "OPTION_DECAY_INPUTS_NOT_KNOWN"
    # No volatility explains the leg's quote, so there is none to hold for a day.
    NO_VOLATILITY = "OPTION_DECAY_NO_VOLATILITY"
    # The model has no price for the leg, now or a day later.
    NOT_PRICED = "OPTION_DECAY_NOT_PRICED"


@dataclass(frozen=True, slots=True)
class LegDecay:
    """One picked leg's daily decay, per share of its own contract, or why it has none.

    The contract's own figure, whichever side the leg takes: below zero for a
    contract that loses value with a day.
    """

    worked: AmericanDailyDecay | None
    why_not: DecayNotEstimated | None = None
    # The model's own words when it refused.
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.worked is None) == (self.why_not is None):
            raise ValueError("a leg's decay is a figure or a reason it has none, and never both")


def picks_daily_decay(resolution: Resolution) -> dict[str, LegDecay]:
    """What the next 24 hours take from each contract ``resolution`` picked, by leg id.

    Each is priced as it was when it was picked: at the volatility solved
    from its own mid quote, against the underlying's mid, with the rate, the
    dividends and the tree the resolver was given. A pick with no solved
    volatility has no figure. Nor has one the model cannot price now or a
    day later, or refuses: a refusal is never a zero.

    This prices two trees a leg. It runs where a search runs, never on an
    event loop (``app.services.option_search``).
    """
    evidence = resolution.evidence
    if evidence.model_spot is None:
        raise ValueError("a resolution with picks was priced against the underlying's mid, and this one has none")
    by_leg = {leg.leg_id: leg for leg in evidence.legs}
    dividends = tuple(
        CashDividend(years_to_ex=years_from_ms(paid.ex_ms - evidence.selection_ms), cash=paid.cash)
        for paid in evidence.model.dividends
    )
    decays: dict[str, LegDecay] = {}
    for leg_id, contract in resolution.picked.items():
        (pick,) = [candidate for candidate in by_leg[leg_id].candidates if candidate.passed_over is None]
        priced = pick.priced
        if priced is None or priced.implied_volatility is None:
            decays[leg_id] = LegDecay(
                None, DecayNotEstimated.NO_VOLATILITY, "no volatility was solved from its quote"
            )
            continue
        try:
            worked = american_daily_decay(
                spot=evidence.model_spot,
                strike=float(contract.strike),
                years_to_expiry=priced.years_to_expiry,
                rate=evidence.model.rate,
                volatility=priced.implied_volatility,
                right=contract.right,
                dividends=dividends,
                steps=evidence.model.tree_steps,
            )
        except ValueError as refused:
            # The pricer's own refusal of an input it has no meaning for, a day later.
            decays[leg_id] = LegDecay(None, DecayNotEstimated.NOT_PRICED, str(refused))
            continue
        decays[leg_id] = (
            LegDecay(worked)
            if worked is not None
            else LegDecay(None, DecayNotEstimated.NOT_PRICED, "the model has no price for it now or a day later")
        )
    return decays


@dataclass(frozen=True, slots=True)
class StructureDecay:
    """One entry's daily decay, or why it has none."""

    # Σ s_i · r_i · θ_i: per share of one structure. None when not estimated, as every figure here is.
    per_share: float | None
    # Dollars per 24 hours for one structure, and for every structure of the entry. Below zero is lost.
    per_structure: float | None
    total: float | None
    why_not: DecayNotEstimated | None = None
    # The legs with no figure, in the structure's order, when it is not estimated.
    legs_without: tuple[str, ...] = ()
    # The sold calls that are in the money now while cash goes ex inside the day. The figure counts each one's
    # fall, by up to the dividend, as a gain, and early assignment would take that gain away.
    sold_calls_in_the_money_at_an_ex_date: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.total is None) == (self.why_not is None):
            raise ValueError("a structure's decay is a figure or a reason it has none, and never both")

    @property
    def estimated(self) -> bool:
        return self.total is not None


def decay_not_estimated(structure: ResolvedStructure, why: DecayNotEstimated) -> StructureDecay:
    """A structure none of whose legs was priced, for one reason."""
    return StructureDecay(None, None, None, why_not=why, legs_without=tuple(leg.leg_id for leg in structure.legs))


def structure_daily_decay(
    structure: ResolvedStructure, size: EntrySize, leg_decays: Mapping[str, LegDecay], *, underlying_price: float
) -> StructureDecay:
    """The daily decay of ``size`` structures, from each leg's contract decay per share, by leg id.

    A bought leg counts its contract's decay as it is, a sold leg turned
    about. Every leg must have an answer handed in; one whose answer is "no
    figure" leaves the structure with none, for that leg's reason.

    ``underlying_price`` is the price each leg's decay was worked against.
    It moves no figure. It says which sold calls are in the money now, with
    a strike below it, and so are named when cash goes ex inside the day.
    """
    if not (math.isfinite(underlying_price) and underlying_price > 0):
        raise ValueError(f"the underlying's price is a number above zero; got {underlying_price!r}")
    leg_ids = {leg.leg_id for leg in structure.legs}
    if set(leg_decays) != leg_ids:
        raise ValueError(f"expected one decay for each of the legs {sorted(leg_ids)}; got {sorted(leg_decays)}")
    without = tuple(leg.leg_id for leg in structure.legs if leg_decays[leg.leg_id].worked is None)
    if without:
        return StructureDecay(None, None, None, why_not=leg_decays[without[0]].why_not, legs_without=without)
    terms = []
    in_the_money_at_an_ex_date = []
    for leg in structure.legs:
        worked = leg_decays[leg.leg_id].worked
        assert worked is not None
        if not math.isfinite(worked.decay):
            raise ValueError(f"the decay of leg {leg.leg_id!r} must be a finite number; got {worked.decay!r}")
        terms.append((1 if leg.position == "long" else -1) * leg.ratio * worked.decay)
        is_a_sold_call = leg.position == "short" and leg.contract.right == "call"
        # The strike as the pricer was handed it, against the price it was handed.
        if is_a_sold_call and worked.cash_gone_ex > 0 and float(leg.contract.strike) < underlying_price:
            in_the_money_at_an_ex_date.append(leg.leg_id)
    per_share = math.fsum(terms)
    per_structure = size.multiplier * per_share
    return StructureDecay(
        per_share=per_share,
        per_structure=per_structure,
        total=size.units * per_structure,
        sold_calls_in_the_money_at_an_ex_date=tuple(in_the_money_at_an_ex_date),
    )


def goes_ex_soon(ex_ms: Iterable[int], at_ms: int) -> bool:
    """Whether any of the dividends goes ex within the next few days of ``at_ms``: the figure is then rough."""
    return any(0 < going_ex - at_ms <= NEAR_EX_DATE_MS for going_ex in ex_ms)
