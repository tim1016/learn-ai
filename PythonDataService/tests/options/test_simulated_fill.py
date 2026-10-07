"""How a simulation fills an option order, and what each fill says of the quote it used."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import (
    SimulatedFillModel,
    SimulatedLegFill,
    simulated_entry_fills,
    simulated_exit_fills,
)
from app.options.structure import ResolvedLeg, ResolvedStructure

# The resolver's own scenes, for their contracts and quotes.
from tests.options import test_resolver as scenes

_NOW = 1_791_000_000_000
_GENERATION = scenes._GENERATION
_BOUGHT, _SOLD = scenes._contract("call", 585), scenes._contract("call", 590)
# One bought 585 call and two sold 590 calls a structure: the ratio is not one.
_STRUCTURE = ResolvedStructure(
    legs=(
        ResolvedLeg(leg_id="bought", contract=_BOUGHT, position="long", ratio=1),
        ResolvedLeg(leg_id="sold", contract=_SOLD, position="short", ratio=2),
    )
)


def _quote(bid: str | None, ask: str | None, *, bid_age: int = 400, ask_age: int = 900, **changes: object) -> HeldQuote:
    held = HeldQuote(
        bid=None if bid is None else QuoteSide(Decimal(bid), _NOW - bid_age),
        ask=None if ask is None else QuoteSide(Decimal(ask), _NOW - ask_age),
        data_type=QuoteDataType.LIVE,
        generation=_GENERATION,
    )
    return replace(held, **changes)


def _exit(quotes: dict[str, HeldQuote | None], units: int = 1) -> dict[str, SimulatedLegFill]:
    fills = simulated_exit_fills(
        _STRUCTURE, quotes, units=units, at_ms=_NOW, generation=_GENERATION, max_age_ms=5_000
    )
    return {fill.leg_id: fill for fill in fills}


def test_an_entry_fills_every_leg_in_full_a_bought_leg_at_its_ask_and_a_sold_leg_at_its_bid() -> None:
    quotes = {"bought": _quote("4.08", "4.12"), "sold": _quote("2.18", "2.22")}

    bought, sold = simulated_entry_fills(_STRUCTURE, quotes, units=3, at_ms=_NOW)

    assert bought == SimulatedLegFill("bought", _BOUGHT, "buy", 3, Decimal("4.12"), SimulatedFillModel.NATURAL_PRICE, 900)
    # Two a structure, three structures; priced from the bid, which was received 400 ms before.
    assert sold == SimulatedLegFill("sold", _SOLD, "sell", 6, Decimal("2.18"), SimulatedFillModel.NATURAL_PRICE, 400)


def test_an_entry_with_no_quote_kept_for_a_leg_is_not_given_an_invented_fill() -> None:
    with pytest.raises(ValueError, match="sold"):
        simulated_entry_fills(_STRUCTURE, {"bought": _quote("4.08", "4.12")}, units=1, at_ms=_NOW)
    with pytest.raises(ValueError, match="bought"):
        simulated_entry_fills(
            _STRUCTURE, {"bought": _quote("4.08", None), "sold": _quote("2.18", "2.22")}, units=1, at_ms=_NOW
        )
    with pytest.raises(ValueError, match="structures"):
        simulated_entry_fills(
            _STRUCTURE, {"bought": _quote("4.08", "4.12"), "sold": _quote("2.18", "2.22")}, units=0, at_ms=_NOW
        )


def test_an_exit_reverses_each_leg_selling_what_was_bought_at_its_bid_and_buying_back_what_was_sold_at_its_ask() -> None:
    fills = _exit({"bought": _quote("5.10", "5.14"), "sold": _quote("2.90", "2.94")}, units=2)

    assert fills["bought"] == SimulatedLegFill("bought", _BOUGHT, "sell", 2, Decimal("5.10"), SimulatedFillModel.NATURAL_PRICE, 400)
    assert fills["sold"] == SimulatedLegFill("sold", _SOLD, "buy", 4, Decimal("2.94"), SimulatedFillModel.NATURAL_PRICE, 900)


@pytest.mark.parametrize(
    ("quote", "age_ms"),
    [
        pytest.param(_quote("5.10", "5.14", bid_age=65_000, ask_age=65_000), 65_000, id="a minute old"),
        pytest.param(_quote("5.10", "5.14", generation=_GENERATION - 1), 400, id="from before a reconnect"),
        pytest.param(_quote("5.10", "5.14", data_type=QuoteDataType.DELAYED), 400, id="delayed data"),
        pytest.param(_quote("5.10", "5.04"), 400, id="crossed"),
        pytest.param(_quote("5.10", None), 400, id="its bid only: the side a sale needs"),
    ],
)
def test_an_exit_with_no_quote_a_decision_could_use_is_priced_from_the_newest_held_and_says_so(
    quote: HeldQuote, age_ms: int
) -> None:
    """The bought call is sold at the bid of the newest quote held for it, whatever is wrong with that quote,
    and the fill carries the model's name and the bid's age."""
    fill = _exit({"bought": quote, "sold": _quote("2.90", "2.94")})["bought"]

    assert (fill.price, fill.model, fill.quote_age_ms) == (
        Decimal("5.10"),
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE,
        age_ms,
    )


@pytest.mark.parametrize(
    ("leg_id", "quote"),
    [
        pytest.param("bought", None, id="no quote held at all"),
        pytest.param("bought", _quote(None, "5.14"), id="selling, and only an ask is held"),
        pytest.param("bought", _quote("NaN", "5.14"), id="selling, and the bid is no number"),
        pytest.param("sold", _quote("2.90", None), id="buying back, and only a bid is held"),
        pytest.param("sold", _quote("2.90", "0"), id="buying back, and nothing is offered"),
    ],
)
def test_an_exit_with_nothing_to_price_a_leg_from_leaves_it_unpriced(leg_id: str, quote: HeldQuote | None) -> None:
    quotes = {"bought": _quote("5.10", "5.14"), "sold": _quote("2.90", "2.94")} | {leg_id: quote}

    fills = _exit(quotes)

    assert (fills[leg_id].price, fills[leg_id].model, fills[leg_id].quote_age_ms) == (None, SimulatedFillModel.UNPRICED, None)
    # The other leg is priced as it would have been: one leg's missing quote is not the structure's.
    other = "sold" if leg_id == "bought" else "bought"
    assert fills[other].model is SimulatedFillModel.NATURAL_PRICE


def test_a_bought_leg_with_nothing_bid_is_sold_for_nothing() -> None:
    """A call gone worthless has no bid. Its natural price to sell at is zero, and that is what the books get."""
    fill = _exit({"bought": _quote("0", "0.02"), "sold": _quote("2.90", "2.94")})["bought"]

    assert (fill.price, fill.model) == (Decimal("0"), SimulatedFillModel.NATURAL_PRICE)
