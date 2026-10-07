"""The words beside a preview's figures: each sentence states the figure it stands beside, and nothing it does not."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from app.options.at_expiry import AtExpiry, PriceRange
from app.options.lean import ABOUT_FLAT_BELOW_DELTA, Lean, LeanDirection
from app.options.named_structures import NamedStructure
from app.options.preview import StructureFigures
from app.options.requirement import EntryMoney, EntrySize
from app.options.structure_catalogue import with_its_article
from app.services.option_preview_views import at_expiry_view, lean_view, money_view, usd


def _money(*, commitment_cents: int = 19_410) -> EntryMoney:
    return EntryMoney(
        net_entry_premium_cents=19_400,
        broker_requirement_cents=0,
        entry_commitment_cents=commitment_cents,
        loss_at_expiry_cents=19_400,
    )


def _figures(**found: object) -> StructureFigures:
    shown = {
        "legs": (),
        "size": EntrySize(multiplier=100, units=1),
        "net_price": Decimal("1.94"),
        "fee_provision_cents": 10,
        "money": _money(),
        "round_trip_cost_cents": 800,
        "round_trip_cost_percent": Fraction(80_000, 19_410),
        "over_cost_limit": False,
    }
    return StructureFigures(**(shown | found))


def test_a_structure_that_is_about_flat_says_how_few_shares_for_every_structure_asked() -> None:
    """Under five shares a structure either way is about flat. Three structures: fewer than fifteen."""
    flat = Lean(net_delta=0.01, shares_per_structure=1.0, shares=3.0, direction=LeanDirection.ABOUT_FLAT)

    seen = lean_view(_figures(lean=flat, size=EntrySize(multiplier=100, units=3)), "SPY", "QQQ")

    assert seen.about_flat_below_shares_per_structure == 100 * ABOUT_FLAT_BELOW_DELTA == 5.0
    assert (seen.direction, seen.gains_when_underlying_falls, seen.warning_sentence) == ("about_flat", False, None)
    assert "15 SPY shares" in seen.sentence and "flat" in seen.sentence.lower()


def test_a_structure_that_leans_down_warns_of_it_with_or_without_a_signal_to_name() -> None:
    down = Lean(net_delta=-0.40, shares_per_structure=-40.0, shares=-40.0, direction=LeanDirection.DOWN)

    told = lean_view(_figures(lean=down), "SPY", "QQQ")
    untold = lean_view(_figures(lean=down), "SPY", None)

    assert told.gains_when_underlying_falls and untold.gains_when_underlying_falls
    assert "SPY" in untold.warning_sentence and "QQQ" not in untold.warning_sentence
    assert "QQQ" in told.warning_sentence


def test_a_payoff_that_is_level_everywhere_is_said_to_hold_at_any_price() -> None:
    """A box: worth the same wherever the stock ends. Its loss and its gain hold at every price, not "at 0 or above"."""
    everywhere = (PriceRange(Decimal(0), None),)
    level = AtExpiry(gain_cents=0, gain_holds=everywhere, loss_holds=everywhere, break_evens=())

    seen = at_expiry_view(_figures(at_expiry=level), "SPY")

    assert [(held.from_price, held.to_price) for held in seen.loss_holds] == [(Decimal(0), None)]
    for said in (seen.loss_sentence, seen.gain_sentence):
        assert "any price" in said and "or above" not in said and "at 0" not in said


@pytest.mark.parametrize(
    ("percent", "limit", "over", "never_printed_as"),
    [
        pytest.param(Fraction(10_004, 1_000), "10", True, "10.00%", id="a hair over a limit of 10"),
        pytest.param(Fraction(9_998, 1_000), "9.999", False, "10.00%", id="a hair under a limit of 9.999"),
    ],
)
def test_a_cost_a_hair_from_the_limit_is_not_printed_as_a_number_that_says_the_opposite(
    percent: Fraction, limit: str, over: bool, never_printed_as: str
) -> None:
    """The verdict is the exact comparison. Rounded for the eye the cost would read as the limit, or past it."""
    seen = money_view(_figures(round_trip_cost_percent=percent, over_cost_limit=over), Decimal(limit))

    assert (seen.over_cost_limit, seen.round_trip_cost_percent) == (over, Decimal("10.00"))
    assert never_printed_as not in seen.cost_sentence
    assert f"{limit}%" in seen.cost_sentence
    assert ("skipped" in seen.cost_sentence) is over


def test_a_cost_well_clear_of_the_limit_is_printed_as_the_percent_it_is() -> None:
    seen = money_view(_figures(), Decimal("7.5"))

    assert all(said in seen.cost_sentence for said in (usd(800), "4.12%", usd(19_410), "7.5%"))
    assert "skipped" not in seen.cost_sentence


def test_each_named_structure_takes_the_article_its_name_calls_for() -> None:
    with_an = {name for name in NamedStructure if with_its_article(name).startswith("an ")}

    assert with_an == {NamedStructure.IRON_CONDOR, NamedStructure.IRON_BUTTERFLY}
    assert all(with_its_article(name).endswith(name.in_words) for name in NamedStructure)
    assert all(with_its_article(name).startswith("a ") for name in set(NamedStructure) - with_an)
