"""The parts of an option structure preview's answer: figures turned into what a screen shows, and the words beside them.

Every function here is a pure view of something already worked out
(``app.options.preview``'s figures, a quote as it was held, the pricing
inputs as they were read). Beyond rounding a figure for the eye, two things
are worked out here, each a restatement in another unit: the premium for one
structure, which is the whole premium over the structures asked, and the
shares a structure must move like to be called anything but flat, which is
the lean's own threshold times the multiplier. No limit is ever judged on a
rounded number. The sentences are the server's: no screen composes prose
about money (#2913, #2915).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from app.options.at_expiry import PriceRange
from app.options.contract import OptionContract
from app.options.lean import ABOUT_FLAT_BELOW_DELTA, LeanDirection
from app.options.preview import PickedLeg, StructureFigures
from app.options.quotes import HeldQuote
from app.schemas.option_preview import (
    OptionContractView,
    OptionPreviewAtExpiry,
    OptionPreviewDividend,
    OptionPreviewLean,
    OptionPreviewLeg,
    OptionPreviewMoney,
    OptionPreviewPricingInputs,
    OptionPreviewQuote,
    PriceRangeView,
)
from app.services.option_model_inputs import ModelInputsRead

_CENT = Decimal("0.01")


def sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", "?", "!")) else ".")


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def rounded(value: Fraction, places: Decimal = _CENT) -> Decimal:
    """An exact fraction as a decimal for the eye, half to even. Never used to judge a limit."""
    with localcontext() as exact:
        exact.prec = 60
        return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(places, rounding=ROUND_HALF_EVEN)


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


def leg_view(leg: PickedLeg, now: int) -> OptionPreviewLeg:
    return OptionPreviewLeg(
        leg_id=leg.leg_id,
        position=leg.position,
        ratio=leg.ratio,
        contract=contract_view(leg.contract),
        quote=quote_view(leg.quote, now),
        gap_percent_of_mid=rounded(leg.gap_percent_of_mid),
        delta=None if leg.priced is None else leg.priced.delta,
        implied_volatility=None if leg.priced is None else leg.priced.implied_volatility,
        delta_status="inputs_not_known" if leg.priced is None else str(leg.priced.status),
        ibkr_delta=leg.vendor_delta,
    )


def money_view(figures: StructureFigures, cost_limit_percent: Decimal) -> OptionPreviewMoney | None:
    money, size = figures.money, figures.size
    if money is None or size is None:
        return None
    assert figures.net_price is not None and figures.fee_provision_cents is not None
    assert figures.round_trip_cost_cents is not None and figures.round_trip_cost_percent is not None
    assert figures.over_cost_limit is not None
    percent = rounded(figures.round_trip_cost_percent)
    # The verdict is the exact comparison. Rounded for the eye, a cost a hair past the limit would print as
    # the limit itself, and the sentence would call two equal numbers over.
    if figures.over_cost_limit and percent <= cost_limit_percent:
        which_is = f"just over {cost_limit_percent}%"
    elif not figures.over_cost_limit and percent > cost_limit_percent:
        which_is = f"just under {cost_limit_percent}%"
    else:
        which_is = f"{percent}%"
    cost = (
        f"Opening and closing at once would cost {usd(figures.round_trip_cost_cents)}, which is {which_is} of the "
        f"{usd(money.entry_commitment_cents)} set aside. Your limit is {cost_limit_percent}%."
    )
    return OptionPreviewMoney(
        structures=size.units,
        multiplier=size.multiplier,
        net_price=figures.net_price,
        premium_is="paid" if figures.net_price > 0 else "received" if figures.net_price < 0 else "nothing",
        net_entry_premium_cents=money.net_entry_premium_cents,
        net_entry_premium_per_structure_cents=money.net_entry_premium_cents // size.units,
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
        )
    loss_cents = figures.money.loss_at_expiry_cents
    assert loss_cents is not None
    even_at = [rounded(price) for price in seen.break_evens]
    return OptionPreviewAtExpiry(
        estimated=True,
        loss_cents=loss_cents,
        loss_holds=[PriceRangeView(from_price=held.low, to_price=held.high) for held in seen.loss_holds],
        gain_cents=seen.gain_cents,
        gain_has_no_limit=seen.gain_cents is None,
        gain_holds=[PriceRangeView(from_price=held.low, to_price=held.high) for held in seen.gain_holds],
        break_even_prices=even_at,
        loss_sentence=f"Lose up to {usd(loss_cents)}, with {underlying} {where_in_words(seen.loss_holds)}.",
        gain_sentence=(
            f"The gain has no limit as {underlying} rises."
            if seen.gain_cents is None
            else f"Gain up to {usd(seen.gain_cents)}, with {underlying} {where_in_words(seen.gain_holds)}."
        ),
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
    )


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
