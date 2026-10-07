"""The parts of an option structure preview's answer: figures turned into what a screen shows, and the words beside them.

Every function here is a pure view of something already worked out
(``app.options.preview``'s figures, a quote as it was held, the pricing
inputs as they were read). Beyond rounding a figure for the eye, three
things are worked out here, each a restatement in another unit: the premium
for one structure, which is the whole premium over the structures asked;
the shares a structure must move like to be called anything but flat, which
is the lean's own threshold times the multiplier; and the daily decay to the
nearest cent. No limit is ever judged on a rounded number.

The sentences are the server's, and so is every amount of money as a screen
shows it: beside each figure in cents is the same figure written out in
dollars, so no screen does arithmetic or composes prose about money (#2913,
#2915). A leg is named by its place in the request, counted from one.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from app.options.american import ONE_DAY_MS
from app.options.at_expiry import PriceRange
from app.options.contract import OptionContract
from app.options.decay import NEAR_EX_DATE_MS, DecayNotEstimated
from app.options.lean import ABOUT_FLAT_BELOW_DELTA, LeanDirection
from app.options.preview import FEE_RATE_NOT_PINNED, NotAsBuilt, PickedLeg, StructureFigures
from app.options.quotes import HeldQuote
from app.options.requirement import StructureMoneyRefusal
from app.schemas.option_preview import (
    OptionContractView,
    OptionPreviewAtExpiry,
    OptionPreviewDecay,
    OptionPreviewDividend,
    OptionPreviewEntryRefusal,
    OptionPreviewLean,
    OptionPreviewLeg,
    OptionPreviewLegAsked,
    OptionPreviewMoney,
    OptionPreviewPricingInputs,
    OptionPreviewQuote,
    OptionPreviewSketchPoint,
    OptionStructurePreviewRequest,
    PriceRangeView,
)
from app.services.option_model_inputs import ModelInputsRead
from app.services.option_preview_words import (
    delta_status_sentence,
    dollars,
    in_the_builders_words,
    leg_asked_in_words,
    leg_names,
    plural,
    sentence,
    usd,
)

_CENT = Decimal("0.01")

__all__ = [
    "at_expiry_view",
    "contract_view",
    "decay_view",
    "entry_refusal_views",
    "lean_view",
    "leg_view",
    "legs_asked_view",
    "money_view",
    "pricing_inputs_view",
    "quote_view",
    "rounded",
    "sentence",
    "usd",
    "where_in_words",
]


def rounded(value: Fraction, places: Decimal = _CENT) -> Decimal:
    """An exact fraction as a decimal for the eye, half to even. Never used to judge a limit."""
    with localcontext() as exact:
        exact.prec = 60
        return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(places, rounding=ROUND_HALF_EVEN)


def _against_the_limit(percent: Decimal, limit: Decimal, *, over: bool) -> str:
    """A percent beside the limit it was judged by. The verdict is the exact comparison: rounded for the eye,
    a percent a hair past its limit would print as the limit itself, and two equal numbers be called over."""
    if over and percent <= limit:
        return f"just over {limit}%"
    if not over and percent > limit:
        return f"just under {limit}%"
    return f"{percent}%"


def contract_view(contract: OptionContract) -> OptionContractView:
    return OptionContractView(
        occ_symbol=contract.occ_symbol,
        underlying=contract.root,
        expiry_ms=contract.expiry_ms,
        right=contract.right,
        strike=contract.strike,
    )


def quote_view(quote: HeldQuote, now: int) -> OptionPreviewQuote:
    assert quote.bid is not None and quote.ask is not None
    return OptionPreviewQuote(
        bid=quote.bid.price,
        bid_age_ms=now - quote.bid.received_ms,
        ask=quote.ask.price,
        ask_age_ms=now - quote.ask.received_ms,
    )


def _gap_sentence(leg: PickedLeg, percent: Decimal, limit: Decimal) -> str:
    if leg.over_gap_limit is None:
        return f"The gap between its bid and ask is {percent}% of its mid."
    apart = _against_the_limit(percent, limit, over=leg.over_gap_limit)
    if leg.over_gap_limit:
        return (
            f"The gap between its bid and ask is {apart} of its mid, over your {limit}% limit. "
            "An entry would be skipped right now."
        )
    return f"The gap between its bid and ask is {apart} of its mid, inside your {limit}% limit."


def leg_view(leg: PickedLeg, now: int, *, number: int, gap_limit_percent: Decimal) -> OptionPreviewLeg:
    """``number`` is the leg's place in the request, counted from one."""
    gap = rounded(leg.gap_percent_of_mid)
    return OptionPreviewLeg(
        leg_id=leg.leg_id,
        number=number,
        position=leg.position,
        ratio=leg.ratio,
        contract=contract_view(leg.contract),
        quote=quote_view(leg.quote, now),
        gap_percent_of_mid=gap,
        over_gap_limit=leg.over_gap_limit,
        gap_sentence=_gap_sentence(leg, gap, gap_limit_percent),
        delta=None if leg.priced is None else leg.priced.delta,
        implied_volatility=None if leg.priced is None else leg.priced.implied_volatility,
        delta_status="inputs_not_known" if leg.priced is None else str(leg.priced.status),
        delta_status_sentence=delta_status_sentence(None if leg.priced is None else leg.priced.status),
        ibkr_delta=leg.vendor_delta,
    )


def legs_asked_view(request: OptionStructurePreviewRequest) -> list[OptionPreviewLegAsked]:
    """Each leg as it was asked for, in the request's order, with its rules in words."""
    order = [leg.leg_id for leg in request.legs]
    asked = []
    for number, leg in enumerate(request.legs, start=1):
        side, line = leg_asked_in_words(leg, order)
        asked.append(OptionPreviewLegAsked(leg_id=leg.leg_id, number=number, side_label=side, sentence=line))
    return asked


def money_view(figures: StructureFigures, cost_limit_percent: Decimal) -> OptionPreviewMoney | None:
    money, size = figures.money, figures.size
    if money is None or size is None:
        return None
    assert figures.net_price is not None and figures.fee_provision_cents is not None
    assert figures.round_trip_cost_cents is not None and figures.round_trip_cost_percent is not None
    assert figures.over_cost_limit is not None
    percent = rounded(figures.round_trip_cost_percent)
    which_is = _against_the_limit(percent, cost_limit_percent, over=figures.over_cost_limit)
    cost = (
        f"Opening and closing at once would cost {usd(figures.round_trip_cost_cents)}, which is {which_is} of the "
        f"{usd(money.entry_commitment_cents)} set aside. Your limit is {cost_limit_percent}%."
    )
    premium_is = "paid" if figures.net_price > 0 else "received" if figures.net_price < 0 else "nothing"
    per_structure_cents = money.net_entry_premium_cents // size.units
    premium, a_share = usd(abs(money.net_entry_premium_cents)), dollars(figures.net_price)
    if premium_is == "nothing":
        premium_sentence = "No premium is paid or received."
    elif size.units == 1:
        premium_sentence = f"Premium {premium_is}: {premium}, which is {a_share} a share."
    else:
        premium_sentence = (
            f"Premium {premium_is}: {premium} for {size.units:,} structures, which is "
            f"{usd(abs(per_structure_cents))} a structure and {a_share} a share."
        )
    return OptionPreviewMoney(
        structures=size.units,
        multiplier=size.multiplier,
        net_price=figures.net_price,
        premium_is=premium_is,
        net_entry_premium_cents=money.net_entry_premium_cents,
        net_entry_premium_per_structure_cents=per_structure_cents,
        net_price_usd=a_share,
        premium_usd=premium,
        premium_per_structure_usd=usd(abs(per_structure_cents)),
        premium_is_label={"paid": "paid", "received": "received", "nothing": "nothing paid or received"}[premium_is],
        premium_sentence=premium_sentence,
        broker_requirement_usd=usd(money.broker_requirement_cents),
        fee_estimate_usd=usd(figures.fee_provision_cents),
        entry_commitment_usd=usd(money.entry_commitment_cents),
        round_trip_cost_usd=usd(figures.round_trip_cost_cents),
        broker_requirement_cents=money.broker_requirement_cents,
        fee_estimate_cents=figures.fee_provision_cents,
        entry_commitment_cents=money.entry_commitment_cents,
        round_trip_cost_cents=figures.round_trip_cost_cents,
        round_trip_cost_percent=percent,
        cost_limit_percent=cost_limit_percent,
        over_cost_limit=figures.over_cost_limit,
        cost_sentence=cost + (" An entry would be skipped right now." if figures.over_cost_limit else ""),
        fees_sentence="Fees are an estimate until Alpaca reports them.",
    )


def where_in_words(holds: Sequence[PriceRange]) -> str:
    """Stretches of underlying price, in words: "at 585 or below", "from 575 to 595", "at 600 or above"."""
    said = []
    for held in holds:
        if held.high is None and held.low == 0:
            # A payoff that is level everywhere: every price there is, not "zero or above".
            said.append("at any price")
        elif held.high is None:
            said.append(f"at {held.low:f} or above")
        elif held.high == 0:
            said.append("at zero")
        elif held.low == 0:
            said.append(f"at {held.high:f} or below")
        elif held.low == held.high:
            said.append(f"at {held.low:f}")
        else:
            said.append(f"from {held.low:f} to {held.high:f}")
    return ", or ".join(said)


def at_expiry_view(figures: StructureFigures, underlying: str) -> OptionPreviewAtExpiry | None:
    if figures.money is None:
        return None
    warning = "A market exit before expiry can do worse than this."
    seen = figures.at_expiry
    if seen is None:
        return OptionPreviewAtExpiry(
            estimated=False,
            loss_cents=None,
            loss_holds=[],
            gain_cents=None,
            gain_has_no_limit=False,
            gain_holds=[],
            break_even_prices=[],
            loss_sentence=None,
            gain_sentence=None,
            break_even_sentence=None,
            assumptions_sentence="Not estimated: the legs do not share an expiry.",
            warning_sentence=warning,
            loss_usd=None,
            gain_usd=None,
            sketch=[],
        )
    loss_cents = figures.money.loss_at_expiry_cents
    assert loss_cents is not None
    even_at = [rounded(price) for price in seen.break_evens]
    # The gain is held at zero where it is defined, so "up to $0.00" is said of a structure that never gains
    # and of one that only loses. It only loses when no price brings it level: no break-even, and every point
    # of its line below zero. The highest result is at a strike, which is on the line, or at a price of zero
    # alone, which would be a break-even.
    loses_at_every_price = bool(
        seen.gain_cents == 0
        and not seen.break_evens
        and seen.sketch
        and all(point.result_cents < 0 for point in seen.sketch)
    )
    if seen.gain_cents is None:
        gain_sentence = f"The gain has no limit as {underlying} rises."
    elif loses_at_every_price:
        gain_sentence = f"No gain at any price of {underlying}: at expiry it loses at every price."
    else:
        gain_sentence = f"Gain up to {usd(seen.gain_cents)}, with {underlying} {where_in_words(seen.gain_holds)}."
    return OptionPreviewAtExpiry(
        estimated=True,
        loss_cents=loss_cents,
        loss_holds=[PriceRangeView(from_price=held.low, to_price=held.high) for held in seen.loss_holds],
        gain_cents=seen.gain_cents,
        gain_has_no_limit=seen.gain_cents is None,
        gain_holds=[PriceRangeView(from_price=held.low, to_price=held.high) for held in seen.gain_holds],
        break_even_prices=even_at,
        loss_sentence=f"Lose up to {usd(loss_cents)}, with {underlying} {where_in_words(seen.loss_holds)}.",
        gain_sentence=gain_sentence,
        break_even_sentence=(
            f"Even with {underlying} at {' and at '.join(str(price) for price in even_at)}."
            if even_at
            else "It is even at no price."
        ),
        assumptions_sentence=(
            "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never "
            "exercised or assigned early."
        ),
        warning_sentence=warning,
        loss_usd=usd(loss_cents),
        gain_usd=None if seen.gain_cents is None else usd(seen.gain_cents),
        sketch=[
            OptionPreviewSketchPoint(price=point.price, result_cents=point.result_cents, result_usd=usd(point.result_cents))
            for point in seen.sketch
        ],
    )


def decay_view(
    figures: StructureFigures, underlying: str, order: Sequence[str], *, goes_ex_soon: bool
) -> OptionPreviewDecay | None:
    """What a day takes from the entry, or why that is not estimated. ``goes_ex_soon``: the underlying goes
    ex-dividend within the next few days, where the figure is rougher."""
    seen, size = figures.decay, figures.size
    if seen is None or size is None:
        return None
    assumptions = (
        "An estimate by this system's own model: each leg's price 24 hours from now less its price now, with "
        f"{underlying}'s price, each leg's volatility and the rate held. It is per 24 hours by the clock: over a "
        "weekend three of them pass before the next session."
    )
    if seen.total is None:
        # A decay with no figure says why: ``StructureDecay`` is never made without one or the other.
        assert seen.why_not is not None
        first = order.index(seen.legs_without[0]) + 1
        why = {
            DecayNotEstimated.INPUTS_NOT_KNOWN: f"the dividends for {underlying} are not known, so nothing is priced.",
            DecayNotEstimated.NO_VOLATILITY: f"no volatility could be worked out for leg {first} from its quote.",
            DecayNotEstimated.NOT_PRICED: (
                f"the model has no price for leg {first} a day from now. That is usual on a contract's last full day."
            ),
        }[seen.why_not]
        return OptionPreviewDecay(
            estimated=False,
            reason_code=str(seen.why_not),
            leg_ids=list(seen.legs_without),
            per_24h_cents=None,
            per_24h_usd=None,
            direction=None,
            sentence=f"Not estimated: {why}",
            assumptions_sentence=assumptions,
            warning_sentence=None,
            near_ex_date_sentence=None,
        )
    # A model's figure, shown to the cent. Nothing is judged by it.
    cents = int((Decimal(seen.total) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    for_how_many = "" if size.units == 1 else f", for {size.units:,} structures"
    if cents < 0:
        direction, said = "loses", f"Loses about {usd(-cents)} per 24 hours{for_how_many}."
    elif cents > 0:
        direction, said = "gains", f"Gains about {usd(cents)} per 24 hours{for_how_many}."
    else:
        direction, said = "neither", "Neither gains nor loses with a day, to the cent."
    warning = None
    if seen.sold_calls_in_the_money_at_an_ex_date:
        sold = seen.sold_calls_in_the_money_at_an_ex_date
        one = len(sold) == 1
        warning = (
            f"{leg_names(sold, order)} {'is a sold call' if one else 'are sold calls'} in the money, and {underlying} "
            f"goes ex-dividend inside the day. This figure counts {'that call' if one else 'each such call'}'s fall, "
            "by up to the dividend, as a gain. A call like that may be assigned early, which takes the gain away."
        )
    return OptionPreviewDecay(
        estimated=True,
        reason_code=None,
        leg_ids=[],
        per_24h_cents=cents,
        per_24h_usd=usd(abs(cents)),
        direction=direction,
        sentence=said,
        assumptions_sentence=assumptions,
        warning_sentence=warning,
        near_ex_date_sentence=(
            f"{underlying} goes ex-dividend within {plural(NEAR_EX_DATE_MS // ONE_DAY_MS, 'day')}. Near an ex-date "
            "this figure is a rough estimate."
            if goes_ex_soon
            else None
        ),
    )


def entry_refusal_views(
    figures: StructureFigures, order: Sequence[str], *, gap_limit_percent: Decimal, cost_sentence: str | None
) -> list[OptionPreviewEntryRefusal]:
    """Every reason an entry on these picks and quotes would be refused, each in the builder's own words."""
    gaps = {leg.leg_id: rounded(leg.gap_percent_of_mid) for leg in figures.legs}
    told = []
    for refusal in figures.entry_refusals:
        if refusal.reason == StructureMoneyRefusal.LEG_GAP_OVER_LIMIT:
            (leg_id,) = refusal.leg_ids
            apart = _against_the_limit(gaps[leg_id], gap_limit_percent, over=True)
            said = (
                f"{leg_names([leg_id], order)}'s bid and ask are {apart} of its mid apart, over your "
                f"{gap_limit_percent}% limit."
            )
        elif refusal.reason == StructureMoneyRefusal.COST_OVER_LIMIT and cost_sentence is not None:
            said = cost_sentence
        else:
            said = sentence(in_the_builders_words(refusal.detail, order))
        told.append(OptionPreviewEntryRefusal(reason_code=refusal.reason, leg_ids=list(refusal.leg_ids), sentence=said))
    return told


def entry_sentence(refusals: Sequence[NotAsBuilt]) -> str:
    """Whether an entry on these quotes would be skipped, in a sentence."""
    if not refusals:
        return (
            "Nothing in these picks or their prices would stop an entry right now. An entry is also held to the "
            "budget, to the account and to the quotes at its own moment."
        )
    if all(refusal.reason == FEE_RATE_NOT_PINNED for refusal in refusals):
        return "An entry would be refused today: no fee rate is pinned, so nothing can be set aside."
    return f"An entry would be skipped right now, for {plural(len(refusals), 'reason')}."


def lean_view(figures: StructureFigures, underlying: str, listens_to: str | None) -> OptionPreviewLean | None:
    leaning, size = figures.lean, figures.size
    if leaning is None or size is None:
        return None
    about = f"{abs(leaning.shares):,.0f} {underlying} shares"
    flat_below = ABOUT_FLAT_BELOW_DELTA * size.multiplier
    if leaning.direction is LeanDirection.UP:
        sentence = f"Leans up. It moves like about {about}."
    elif leaning.direction is LeanDirection.DOWN:
        sentence = f"Leans down. It moves like about {about} sold short."
    else:
        sentence = f"About flat. It moves like fewer than {flat_below * size.units:,.0f} {underlying} shares either way."
    falls = leaning.direction is LeanDirection.DOWN
    warning = None
    if falls:
        warning = f"This structure gains when {underlying} falls."
        if listens_to is not None:
            warning += f" The strategy's signal is a buy signal on {listens_to}."
    return OptionPreviewLean(
        net_delta=leaning.net_delta,
        shares_per_structure=leaning.shares_per_structure,
        shares=leaning.shares,
        direction=leaning.direction.value,
        about_flat_below_shares_per_structure=flat_below,
        sentence=sentence,
        gains_when_underlying_falls=falls,
        warning_sentence=warning,
    )


def pricing_inputs_view(inputs: ModelInputsRead) -> OptionPreviewPricingInputs:
    return OptionPreviewPricingInputs(
        rate=inputs.rate,
        rate_source=inputs.rate_source,
        rate_is_default=inputs.rate_is_default,
        dividends_known=inputs.dividends is not None,
        dividends=[OptionPreviewDividend(ex_ms=paid.ex_ms, cash_per_share=paid.cash) for paid in inputs.dividends or ()],
        dividends_source=inputs.dividends_source,
        dividends_read_ms=inputs.dividends_read_ms,
    )
