"""The words beside a preview's figures: each sentence states the figure it stands beside, and nothing it does not."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from app.options.at_expiry import AtExpiry, PriceRange, ResultAt
from app.options.deadlines import CloseKind
from app.options.decay import DecayNotEstimated, StructureDecay
from app.options.lean import ABOUT_FLAT_BELOW_DELTA, Lean, LeanDirection
from app.options.named_structures import NamedStructure
from app.options.preview import FEE_RATE_NOT_PINNED, NotAsBuilt, PickedLeg, StructureFigures, TooLateToEnter
from app.options.requirement import EntryMoney, EntrySize, StructureMoneyRefusal
from app.options.resolver import ResolverRefusal
from app.options.structure_catalogue import in_a_sentence, label, with_its_article
from app.schemas.option_preview import OptionPreviewEntryRefusal
from app.services.option_preview_views import (
    at_expiry_view,
    decay_view,
    entry_refusal_views,
    entry_sentence,
    lean_view,
    leg_view,
    money_view,
    usd,
)

# The resolver's own scenes, for a contract and a quote to stand a leg on.
from tests.options import test_resolver as scenes


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


def _line(*points: tuple[int, int]) -> tuple[ResultAt, ...]:
    return tuple(ResultAt(Decimal(price), cents) for price, cents in points)


def test_a_structure_that_gains_at_no_price_is_said_to_lose_at_every_price_and_not_to_gain_up_to_nothing() -> None:
    """The 585 call bought and the 590 sold for $5.50 a share, on a spread $5 wide: -$550.00 at 585 and below,
    -$50.00 at 590 and above. The gain is held at zero where it is defined, and the line is not."""
    above_590 = (PriceRange(Decimal(590), None),)
    paid_too_much = AtExpiry(
        gain_cents=0, gain_holds=above_590, loss_holds=(PriceRange(Decimal(0), Decimal(585)),), break_evens=(),
        sketch=_line((583, -55_000), (585, -55_000), (590, -5_000), (592, -5_000)),
    )

    seen = at_expiry_view(_figures(at_expiry=paid_too_much), "SPY")

    assert "every price" in seen.gain_sentence and "$0.00" not in seen.gain_sentence
    # The figure keeps the definition it has. Only the words say what a gain of nothing means here.
    assert (seen.gain_cents, seen.gain_usd, seen.gain_has_no_limit) == (0, "$0.00", False)
    assert "590" not in seen.gain_sentence


@pytest.mark.parametrize(
    ("break_evens", "sketch"),
    [
        pytest.param((Fraction(590),), _line((583, -50_000), (585, -50_000), (590, 0), (592, 0)), id="level at nothing from 590 up: it breaks even there"),
        pytest.param((), _line((583, 0), (585, 0), (592, 0)), id="worth the same everywhere: it loses nowhere"),
        pytest.param((Fraction(0),), _line((583, -200), (585, -300), (592, -300)), id="even only with the stock at nothing"),
    ],
)
def test_a_gain_of_nothing_that_some_price_reaches_is_still_said_as_a_gain_of_nothing(
    break_evens: tuple[Fraction, ...], sketch: tuple[ResultAt, ...]
) -> None:
    """Held at zero is not the same as never reached. Where some price brings the entry level, it does not lose
    at every price, and is not said to."""
    reached = AtExpiry(
        gain_cents=0, gain_holds=(PriceRange(Decimal(590), None),), loss_holds=(PriceRange(Decimal(0), Decimal(585)),),
        break_evens=break_evens, sketch=sketch,
    )

    seen = at_expiry_view(_figures(at_expiry=reached), "SPY")

    assert "every price" not in seen.gain_sentence and "$0.00" in seen.gain_sentence


def test_each_point_of_the_line_at_expiry_comes_in_cents_and_written_out_in_dollars() -> None:
    drawn = AtExpiry(
        gain_cents=30_600, gain_holds=(PriceRange(Decimal(590), None),), loss_holds=(PriceRange(Decimal(0), Decimal(585)),),
        break_evens=(Fraction("586.94"),), sketch=_line((583, -19_400), (585, -19_400), (587, 600), (590, 30_600), (592, 123_456_700)),
    )

    seen = at_expiry_view(_figures(at_expiry=drawn), "SPY")

    assert [(point.price, point.result_cents, point.result_usd) for point in seen.sketch] == [
        (Decimal(583), -19_400, "-$194.00"), (Decimal(585), -19_400, "-$194.00"), (Decimal(587), 600, "$6.00"),
        (Decimal(590), 30_600, "$306.00"), (Decimal(592), 123_456_700, "$1,234,567.00"),
    ]


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


def test_each_named_structure_takes_its_article_and_one_with_no_name_is_custom_in_a_list_and_in_a_sentence() -> None:
    with_an = {name for name in NamedStructure if with_its_article(name).startswith("an ")}

    assert with_an == {NamedStructure.IRON_CONDOR, NamedStructure.IRON_BUTTERFLY}, "only the iron structures take an"
    assert all(with_its_article(name).endswith(name.in_words) for name in NamedStructure), "each ends in its name in words"
    assert all(
        with_its_article(name).startswith("a ") for name in set(NamedStructure) - with_an
    ), "every other structure takes a"
    assert (label(None), label(NamedStructure.IRON_CONDOR)) == ("Custom", "Iron condor"), "a list labels no name Custom"
    assert in_a_sentence(NamedStructure.IRON_CONDOR) == "an iron condor on", "a sentence names a structure with its article"
    assert in_a_sentence(None).endswith(" on") and "custom" in in_a_sentence(None), "a sentence calls no name custom"


# ---- A leg's gap beside the owner's limit ---------------------------------------------------------


def _leg(bid: str, ask: str, *, over: bool | None) -> PickedLeg:
    quote = scenes._quote(bid, ask, received_ms=0)
    gap = (Fraction(ask) - Fraction(bid)) * 200 / (Fraction(bid) + Fraction(ask))
    return PickedLeg(
        leg_id="only", position="long", ratio=1, contract=scenes._contract("call", 585), quote=quote,
        gap_percent_of_mid=gap, priced=None, vendor_delta=None, over_gap_limit=over,
    )


def test_a_gap_is_said_inside_the_limit_over_it_or_with_no_verdict_when_no_entry_could_be_made() -> None:
    """Sixteen cents on a mid of 2.00 is 8%. Seventeen is 8.5%."""
    inside = leg_view(_leg("1.92", "2.08", over=False), 0, number=1, gap_limit_percent=Decimal(8))
    over = leg_view(_leg("1.915", "2.085", over=True), 0, number=2, gap_limit_percent=Decimal(8))
    unjudged = leg_view(_leg("1.915", "2.085", over=None), 0, number=3, gap_limit_percent=Decimal(8))

    assert (inside.number, over.number, unjudged.number) == (1, 2, 3)
    assert (inside.over_gap_limit, over.over_gap_limit, unjudged.over_gap_limit) == (False, True, None)
    assert "8.00%" in inside.gap_sentence and "skipped" not in inside.gap_sentence
    assert "8.50%" in over.gap_sentence and "skipped" in over.gap_sentence
    # Not judged: the gap is still said, and no limit is.
    assert "8.50%" in unjudged.gap_sentence and "limit" not in unjudged.gap_sentence


def test_a_gap_a_hair_over_the_limit_is_not_printed_as_the_limit_itself() -> None:
    """2.00 wide on a mid of 25.00 is exactly 8%. A hair wider rounds to 8.00% and is over."""
    a_hair_wider = _leg("23.9999", "26.0001", over=True)

    seen = leg_view(a_hair_wider, 0, number=1, gap_limit_percent=Decimal(8))

    assert seen.gap_percent_of_mid == Decimal("8.00") and seen.over_gap_limit is True
    assert "just over 8%" in seen.gap_sentence and "8.00%" not in seen.gap_sentence


# ---- What a day takes, in words ------------------------------------------------------------------------

_ORDER = ["first", "second", "third"]


def _decayed(total: float | None, **found: object) -> StructureFigures:
    size = found.pop("size", EntrySize(multiplier=100, units=1))
    decay = StructureDecay(
        per_share=None if total is None else total / (size.multiplier * size.units),
        per_structure=None if total is None else total / size.units,
        total=total,
        **found,
    )
    return _figures(size=size, decay=decay)


@pytest.mark.parametrize(
    ("dollars", "direction", "cents"),
    [
        pytest.param(-6.36, "loses", -636, id="a day costs it"),
        pytest.param(30.004, "gains", 3_000, id="a day pays it"),
        pytest.param(0.004, "neither", 0, id="under half a cent either way"),
        pytest.param(-0.004, "neither", 0, id="under half a cent the other way"),
        pytest.param(0.006, "gains", 1, id="over half a cent rounds to a cent, and is not cut off"),
        pytest.param(-12.346, "loses", -1_235, id="rounded to the nearest cent, away from nothing here"),
    ],
)
def test_a_decay_is_said_as_lost_gained_or_neither_by_its_figure_to_the_cent(dollars: float, direction: str, cents: int) -> None:
    seen = decay_view(_decayed(dollars), "SPY", _ORDER, goes_ex_soon=False)

    assert (seen.estimated, seen.direction, seen.per_24h_cents, seen.per_24h_usd) == (True, direction, cents, usd(abs(cents)))
    assert (usd(abs(cents)) in seen.sentence) is (direction != "neither")
    assert (seen.warning_sentence, seen.near_ex_date_sentence) == (None, None)


def test_a_decay_for_several_structures_says_how_many_and_for_one_does_not() -> None:
    several = decay_view(_decayed(-19.08, size=EntrySize(multiplier=100, units=3)), "SPY", _ORDER, goes_ex_soon=False)
    one = decay_view(_decayed(-6.36), "SPY", _ORDER, goes_ex_soon=False)

    assert "3 structures" in several.sentence and "structure" not in one.sentence


@pytest.mark.parametrize("why", list(DecayNotEstimated))
def test_each_reason_a_decay_is_not_estimated_has_words_of_its_own_and_names_a_leg_only_by_its_place(
    why: DecayNotEstimated,
) -> None:
    seen = decay_view(_decayed(None, why_not=why, legs_without=("second", "third")), "SPY", _ORDER, goes_ex_soon=True)
    others = {
        decay_view(_decayed(None, why_not=other, legs_without=("second",)), "SPY", _ORDER, goes_ex_soon=False).sentence
        for other in DecayNotEstimated
        if other is not why
    }

    assert (seen.estimated, seen.reason_code, seen.leg_ids) == (False, why, ["second", "third"])
    assert (seen.per_24h_cents, seen.per_24h_usd, seen.direction) == (None, None, None)
    assert seen.sentence not in others and "second" not in seen.sentence
    assert ("leg 2" in seen.sentence) is (why is not DecayNotEstimated.INPUTS_NOT_KNOWN)
    # With no figure there is nothing to call rough, and nothing to warn about.
    assert (seen.near_ex_date_sentence, seen.warning_sentence) == (None, None)


def test_sold_calls_in_the_money_at_an_ex_date_are_named_by_their_places_one_or_several() -> None:
    one = decay_view(_decayed(20.0, sold_calls_in_the_money_at_an_ex_date=("second",)), "SPY", _ORDER, goes_ex_soon=True)
    two = decay_view(
        _decayed(20.0, sold_calls_in_the_money_at_an_ex_date=("second", "third")), "SPY", _ORDER, goes_ex_soon=True
    )

    assert "Leg 2 " in one.warning_sentence and "Legs 2 and 3" in two.warning_sentence
    assert one.warning_sentence != two.warning_sentence and "second" not in two.warning_sentence
    assert one.near_ex_date_sentence and "3 days" in one.near_ex_date_sentence
    # A call at the money falls by nothing and one deep in it by the whole dividend. Neither is "about" it.
    for warning in (one.warning_sentence, two.warning_sentence):
        assert "up to the dividend" in warning and "about the dividend" not in warning


def test_a_decay_whose_legs_are_named_out_of_the_order_of_their_ids_names_them_by_their_places() -> None:
    """The request's first leg is "zulu" and its second "alpha": a place is not an order of names."""
    order = ["zulu", "alpha"]

    not_priced = decay_view(
        _decayed(None, why_not=DecayNotEstimated.NO_VOLATILITY, legs_without=("alpha",)), "SPY", order, goes_ex_soon=False
    )
    warned = decay_view(_decayed(20.0, sold_calls_in_the_money_at_an_ex_date=("zulu",)), "SPY", order, goes_ex_soon=True)

    assert "leg 2" in not_priced.sentence and "leg 1" not in not_priced.sentence
    assert "Leg 1 " in warned.warning_sentence and "Leg 2" not in warned.warning_sentence


def test_figures_with_no_structure_have_no_decay_to_show() -> None:
    assert decay_view(_figures(decay=None), "SPY", _ORDER, goes_ex_soon=False) is None
    assert decay_view(_figures(size=None, decay=None), "SPY", _ORDER, goes_ex_soon=False) is None


# ---- What an entry would be refused for, in words --------------------------------------------------------


def test_each_refusal_an_entry_would_meet_is_said_in_the_builders_words_under_its_own_code() -> None:
    wide = _leg("1.915", "2.085", over=True)
    refusals = (
        NotAsBuilt("OPTION_STRUCTURE_STRIKES_NOT_AS_NAMED", ("first", "only"), "leg 'first' sits below leg 'only'"),
        NotAsBuilt(str(StructureMoneyRefusal.LEG_GAP_OVER_LIMIT), ("only",), "the entry's own words, with (leg 'only')"),
        NotAsBuilt(str(StructureMoneyRefusal.COST_OVER_LIMIT), (), "would cost 800 cents, more than 10% of the 1400 cents"),
    )
    figures = _figures(legs=(wide,), entry_refusals=refusals)

    told = entry_refusal_views(figures, ["first", "only"], gap_limit_percent=Decimal(8), cost_sentence="The cost, said once.")

    assert [(said.reason_code, said.leg_ids) for said in told] == [
        (refusal.reason, list(refusal.leg_ids)) for refusal in refusals
    ]
    by_shape, by_gap, by_cost = (said.sentence for said in told)
    assert "Leg 1" in by_shape and "leg 2" in by_shape and "'" not in by_shape
    assert "Leg 2" in by_gap and "8.50%" in by_gap and "8%" in by_gap and "only" not in by_gap
    # The cost is said as it is said beside the money, not in the entry's cents.
    assert by_cost == "The cost, said once."
    # The limit a refusal names is the owner's own, whatever it is.
    (_, at_six, _) = entry_refusal_views(figures, ["first", "only"], gap_limit_percent=Decimal("6.25"), cost_sentence="x")
    assert "6.25%" in at_six.sentence and "8%" not in at_six.sentence


def test_money_that_cannot_be_set_aside_is_refused_in_dollars_and_never_in_cents() -> None:
    """Sold for more than the structure could ever owe: the money's own refusal counts in cents."""
    refused = NotAsBuilt(
        str(StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE), (),
        "Requirement 0 cents plus premium -3958800 cents is not above zero; the prices do not belong together.",
    )

    (told,) = entry_refusal_views(
        _figures(entry_refusals=(refused,), money=None), ["first"], gap_limit_percent=Decimal(8), cost_sentence=None
    )

    assert told.reason_code == StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE
    assert "-$39,588.00" in told.sentence and "$0.00" in told.sentence and "cents" not in told.sentence


def test_an_entry_that_is_too_late_is_told_which_close_it_is_too_late_for_and_how_long_before_the_sessions_it_comes() -> None:
    refused = NotAsBuilt(str(ResolverRefusal.PAST_ENTRY_CUTOFF), (), "the figures' own words, never shown beside a close")

    def said(late: TooLateToEnter) -> OptionPreviewEntryRefusal:
        figures = _figures(entry_refusals=(refused,), too_late_to_enter=late)
        (told,) = entry_refusal_views(figures, ["first"], gap_limit_percent=Decimal(8), cost_sentence=None)
        return told

    sold = said(TooLateToEnter(CloseKind.SOLD_LEG, close_lead_ms=10 * 60_000))
    expiring = said(TooLateToEnter(CloseKind.EXPIRY_DAY, close_lead_ms=45 * 60_000))
    at_the_close = said(TooLateToEnter(CloseKind.EXPIRY_DAY, close_lead_ms=0))

    assert (sold.reason_code, sold.leg_ids) == (ResolverRefusal.PAST_ENTRY_CUTOFF, [])
    # A sold leg's close comes every session. An expiring contract's comes on its last day.
    assert "sold leg" in sold.sentence and "10 minutes" in sold.sentence and "every session" in sold.sentence
    assert "expires today" in expiring.sentence and "45 minutes" in expiring.sentence
    assert "sold leg" not in expiring.sentence and "every session" not in expiring.sentence
    # A close set at the session's own close is not said to come any length of time before it.
    assert "before the session closes" not in at_the_close.sentence and "second" not in at_the_close.sentence
    # Each says that nothing would be opened, and none says the figures' own words.
    assert all("opened" in told.sentence and refused.detail not in told.sentence for told in (sold, expiring, at_the_close))


def test_whether_an_entry_would_be_skipped_is_said_three_ways() -> None:
    nothing_wrong = entry_sentence(())
    skipped = entry_sentence((NotAsBuilt(str(StructureMoneyRefusal.COST_OVER_LIMIT), (), "over"),))
    no_rate = entry_sentence((NotAsBuilt(FEE_RATE_NOT_PINNED, (), "no fee rate is pinned"),))

    assert len({nothing_wrong, skipped, no_rate}) == 3
    assert "1 reason" in skipped and "fee" in no_rate


# ---- A premium, in words -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("net_price", "premium_cents", "is_", "label_"),
    [
        pytest.param("1.94", 19_400, "paid", "paid", id="paid"),
        pytest.param("-3.07", -30_700, "received", "received", id="received"),
        pytest.param("0", 0, "nothing", "nothing paid or received", id="neither"),
    ],
)
def test_a_premium_is_said_paid_received_or_neither_and_written_without_its_sign(
    net_price: str, premium_cents: int, is_: str, label_: str
) -> None:
    money = EntryMoney(
        net_entry_premium_cents=premium_cents, broker_requirement_cents=50_000,
        entry_commitment_cents=50_010 + premium_cents, loss_at_expiry_cents=None,
    )

    seen = money_view(_figures(net_price=Decimal(net_price), money=money), Decimal(10))

    assert (seen.premium_is, seen.premium_is_label) == (is_, label_)
    assert (seen.premium_usd, seen.net_price_usd) == (usd(abs(premium_cents)), f"${abs(Decimal(net_price)):.2f}")
    assert "-" not in seen.premium_usd and "-" not in seen.net_price_usd
    assert (is_ in seen.premium_sentence) is (is_ != "nothing")
    assert seen.broker_requirement_usd == "$500.00"

