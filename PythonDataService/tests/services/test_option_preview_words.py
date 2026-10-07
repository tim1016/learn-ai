"""A preview's words that are not about one figure: a leg named by its place, a leg's rules, a range.

What is asserted is what each sentence must carry: the leg's number and never its id, the rule's own numbers,
the range's own bounds, and which of two ways a sentence goes. No sentence is held to its wording.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.options.american import AmericanIvStatus
from app.options.bounds import Bounds
from app.options.expiry import option_expiry_anchor_ms
from app.schemas.option_preview import OptionLegRules
from app.services.option_preview_words import (
    age_in_words,
    bounds_in_words,
    delta_status_sentence,
    dollars,
    fixed_dates_taken_in_words,
    in_the_builders_words,
    leg_asked_in_words,
    leg_names,
    legs_by_number,
    out_of_range_sentence,
    places_in_words,
    plain,
    said_back,
)

# The resolver's own scenes, for the Friday its contracts expire on.
from tests.options import test_resolver as scenes

# The second leg in the request is written first in the alphabet: a place is not an order of names.
_ORDER = ["zulu", "alpha", "leg_3"]


# ---- A leg, by its place ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_its_source_says_it", "as_the_builder_reads_it"),
    [
        pytest.param("leg 'alpha' asks for a strike", "leg 2 asks for a strike", id="after the word leg"),
        pytest.param(
            "in a bull call spread leg 'zulu' sits below leg 'alpha'; they came to 590 and 585",
            "in a bull call spread leg 1 sits below leg 2; they came to 590 and 585",
            id="two legs in one sentence",
        ),
        pytest.param("legs 'zulu' and 'alpha' both came to SPY261009C00585000", "legs 1 and 2 both came to SPY261009C00585000", id="a pair"),
        pytest.param("legs 'alpha' and 'zulu' both came to SPY261009C00585000", "legs 1 and 2 both came to SPY261009C00585000", id="a pair named the other way round"),
        pytest.param("sold leg 'alpha' expires 2026-10-09", "sold leg 2 expires 2026-10-09", id="a sold leg"),
        pytest.param("'leg_3' is closed 2 times", "leg 3 is closed 2 times", id="an id with no word before it"),
        pytest.param(
            "the underlying's quote and leg 'alpha''s quote were received 2401 ms apart",
            "the underlying's quote and leg 2's quote were received 2401 ms apart",
            id="a leg's own quote",
        ),
        pytest.param(
            "leg 'alpha' measures its strike from 'zulu', which is not right",
            "leg 2 measures its strike from leg 1, which is not right",
            id="one after the word and one bare",
        ),
        pytest.param(
            "legs measure their strikes from each other in a loop (alpha -> zulu -> alpha); one must change",
            "legs measure their strikes from each other in a loop (leg 2 -> leg 1 -> leg 2); one must change",
            id="a loop, where ids are written bare",
        ),
        pytest.param("Leg 'zulu' has no whole quote", "Leg 1 has no whole quote", id="at the start of a sentence"),
    ],
)
def test_a_leg_named_by_its_id_is_said_by_its_place_in_the_request(
    as_its_source_says_it: str, as_the_builder_reads_it: str
) -> None:
    assert legs_by_number(as_its_source_says_it, _ORDER) == as_the_builder_reads_it


@pytest.mark.parametrize(
    "left_alone",
    [
        "a bull call spread is 1 bought call and 1 sold call; these legs are not",
        "leg 'someone_else' is no leg of this request",
        "the quote feed is not healthy: reconnecting (attempt 2 -> 3)",
        "an option is a call or a put; got 'straddle'",
        "the alpha of zulu is leg_3",
    ],
    ids=["no leg named", "an id that is no leg here", "an arrow that is no loop of legs", "a quoted word that is no leg", "ids never quoted"],
)
def test_words_that_name_no_leg_of_the_request_are_left_as_they_were(left_alone: str) -> None:
    assert legs_by_number(left_alone, _ORDER) == left_alone


def test_legs_are_listed_by_their_places_one_or_several() -> None:
    assert leg_names(["alpha"], _ORDER) == "Leg 2"
    assert leg_names(["leg_3", "zulu"], _ORDER) == "Legs 3 and 1"
    assert leg_names(["zulu", "alpha", "leg_3"], _ORDER) == "Legs 1, 2 and 3"
    assert places_in_words([2, 4]) == "Legs 2 and 4"


# ---- Money in dollars, and a number nobody meant by its length ---------------------------------------------


def test_an_amount_a_refusal_gives_in_cents_is_said_in_dollars_with_its_sign() -> None:
    """The money's own refusal, as an entry keeps it. An owner reads dollars."""
    as_the_money_says_it = "Requirement 0 cents plus premium -3958800 cents is not above zero; the prices do not belong together."

    said = in_the_builders_words(as_the_money_says_it, _ORDER)

    assert "$0.00" in said and "-$39,588.00" in said
    assert "cents" not in said and "3958800" not in said


def test_one_sentence_gets_all_three_translations_and_nothing_else_in_it_moves() -> None:
    said = in_the_builders_words("leg 'alpha' would set aside 19410 cents for SPY261009C00585000 at 1791302400000", _ORDER)

    assert said == "leg 2 would set aside $194.10 for SPY261009C00585000 at 1791302400000"


@pytest.mark.parametrize(
    ("as_its_source_says_it", "carries", "never"),
    [
        pytest.param(
            f"leg 'zulu' asks for the strike +{10**400} from the money; the chain lists no such call",
            ["leg 1", "401 digits"], ["0000000000000000"], id="steps beyond counting",
        ),
        pytest.param(
            f"leg 'zulu' asks for the strike -{10**20} from the money", ["negative", "21 digits"], ["0000000000000000"],
            id="steps below, beyond counting",
        ),
        pytest.param(
            f"leg 'alpha' asks for an expiry {10**30} or more days after 2026-10-06", ["leg 2", "31 digits", "2026-10-06"],
            ["0000000000000000"], id="days beyond counting",
        ),
    ],
)
def test_a_number_longer_than_anyone_meant_is_said_by_its_length_wherever_a_sentence_carries_it(
    as_its_source_says_it: str, carries: list[str], never: list[str]
) -> None:
    said = in_the_builders_words(as_its_source_says_it, _ORDER)

    assert all(part in said for part in carries), said
    assert not any(part in said for part in never), said
    assert len(said) < 120


@pytest.mark.parametrize(
    "left_alone",
    [
        "leg asks for a delta of 0.30000000000000004; the nearest is 0.2999999999999999889",
        "SPY261009C00585000 was received at 1791302400000 and holds 123456789012345 contracts",
        "the quote is 1234567890123456.25 wide",
    ],
    ids=["the digits after a decimal point", "a contract code, a time and a number of fifteen digits", "a long number with a fraction"],
)
def test_a_number_that_is_not_a_long_whole_number_is_left_as_it_was(left_alone: str) -> None:
    assert in_the_builders_words(left_alone, _ORDER) == left_alone


def test_the_dates_a_fixed_expiry_may_be_are_said_first_to_last_with_their_years_in_full() -> None:
    """"Jan 1 '00 to Dec 31 '99" reads as backwards. A year in full does not."""
    said = fixed_dates_taken_in_words()

    assert said.index("2000") < said.index("2099")
    assert "'00" not in said and "'99" not in said


# ---- A leg's rules --------------------------------------------------------------------------------------


def _leg(strike: dict[str, Any], expiry: dict[str, Any] | None = None, **leg: Any) -> OptionLegRules:
    asked = {"leg_id": "alpha", "position": "long", "qty_ratio": 1, "right": "call", "strike": strike}
    return OptionLegRules.model_validate(asked | {"expiry": expiry or {"selector": "nearest_weekly"}} | leg)


@pytest.mark.parametrize(
    ("strike", "carries", "never"),
    [
        pytest.param({"selector": "delta", "target": 0.5}, ["0.50 delta", "0.05"], ["above", "below"], id="a delta, written to two places"),
        pytest.param({"selector": "delta", "target": 0.305, "tolerance_delta": 0.1}, ["0.305", "0.10"], [], id="a delta finer than two places"),
        pytest.param({"selector": "atm_offset", "offset": 2}, ["2 strikes", "above"], ["below"], id="steps above the money"),
        pytest.param({"selector": "atm_offset", "offset": -1}, ["1 strike ", "below"], ["above", "strikes"], id="one step below"),
        pytest.param({"selector": "atm_offset", "offset": 0}, ["at the money"], ["strike"], id="no steps at all"),
        pytest.param({"selector": "leg_distance", "leg_id": "zulu", "dollars": 5.0}, ["$5", "above", "leg 1"], ["below", "within", "zulu"], id="a distance above another leg"),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "leg_3", "dollars": -2.5, "tolerance_dollars": 0.5},
            ["$2.5", "below", "leg 3", "within $0.5"], ["above", "leg_3"], id="a distance below, with room",
        ),
        pytest.param({"selector": "leg_distance", "leg_id": "zulu", "dollars": 0.0}, ["same strike", "leg 1"], ["above", "below", "$"], id="no distance"),
        pytest.param({"selector": "percent", "percent": 1.5}, ["1.5%", "above", "0.5%"], ["below"], id="a percent above the price"),
        pytest.param({"selector": "percent", "percent": -3.0, "tolerance_percent": 1.0}, ["3%", "below", "within 1%"], ["above"], id="a percent below"),
        pytest.param({"selector": "atm"}, ["at the money"], [], id="at the money"),
        pytest.param({"selector": "absolute", "strike": 452.5}, ["452.5"], [], id="one named strike"),
        pytest.param({"selector": "atm_offset", "offset": 10**400}, ["401 digits", "above"], ["0000000000000000"], id="steps beyond counting"),
        pytest.param({"selector": "atm_offset", "offset": -(10**400)}, ["401 digits", "below"], ["0000000000000000"], id="steps below, beyond counting"),
        pytest.param({"selector": "atm_offset", "offset": 10**14}, ["100,000,000,000,000 strikes"], ["digits"], id="fifteen digits are still said whole"),
    ],
)
def test_a_legs_strike_rule_is_said_with_its_own_numbers_and_which_way_it_goes(
    strike: dict[str, Any], carries: list[str], never: list[str]
) -> None:
    _side, line = leg_asked_in_words(_leg(strike), _ORDER)

    assert all(said in line for said in carries), line
    assert not any(said in line for said in never), line


@pytest.mark.parametrize(
    ("expiry", "carries"),
    [
        pytest.param({"selector": "min_dte", "days": 7}, "7 days", id="at least a week out"),
        pytest.param({"selector": "min_dte", "days": 10**400}, "401 digits", id="days beyond counting, said by their length"),
        pytest.param({"selector": "min_dte", "days": 1}, "1 day ", id="at least a day out"),
        pytest.param({"selector": "nearest_weekly"}, "weekly", id="the nearest weekly"),
        pytest.param({"selector": "same_day"}, "same day", id="the same day"),
        pytest.param(
            {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(scenes._FRIDAY)}, "Oct 9 '26", id="a fixed date, with its year"
        ),
    ],
)
def test_a_legs_expiry_rule_is_said_with_its_own_number_or_its_own_date(expiry: dict[str, Any], carries: str) -> None:
    _side, line = leg_asked_in_words(_leg({"selector": "atm"}, expiry), _ORDER)

    assert carries in line


def test_a_legs_line_says_its_side_how_many_and_what_and_a_leg_it_cannot_find_is_another_leg() -> None:
    bought = leg_asked_in_words(_leg({"selector": "atm"}, qty_ratio=1, right="put"), _ORDER)
    sold = leg_asked_in_words(_leg({"selector": "atm"}, position="short", qty_ratio=2), _ORDER)
    from_nowhere = leg_asked_in_words(_leg({"selector": "leg_distance", "leg_id": "gone", "dollars": 5.0}), _ORDER)

    assert (bought[0], sold[0]) == ("Buy", "Sell")
    assert bought[1].startswith("1 put ") and sold[1].startswith("2 calls ")
    assert "another leg" in from_nowhere[1] and "gone" not in from_nowhere[1]


# ---- A range ----------------------------------------------------------------------------------------------


def _range(low: str | None, low_in: bool, high: str | None, high_in: bool, step: str | None = None) -> Bounds:
    return Bounds(
        None if low is None else Decimal(low), low_in, None if high is None else Decimal(high), high_in,
        None if step is None else Decimal(step),
    )


@pytest.mark.parametrize(
    ("bounds", "carries", "never"),
    [
        pytest.param(_range("0", False, "1", False), ["above 0", "below 1"], ["from", "whole"], id="open at both ends"),
        pytest.param(_range("0", True, "100", True, "0.000001"), ["from 0 to 100", "0.000001"], ["above", "whole"], id="closed, with a finest step"),
        pytest.param(_range("0", False, "100", True), ["above 0", "up to 100"], ["below"], id="open below, closed above"),
        pytest.param(_range("0", True, "99999.999", False, "0.001"), ["from 0", "below 99,999.999"], ["up to"], id="closed below, open above"),
        pytest.param(_range("1", True, "300", True, "1"), ["whole number", "from 1 to 300"], ["finer"], id="whole numbers"),
        pytest.param(_range("1", True, None, True, "1"), ["whole number", "1 or more"], ["to"], id="no top"),
        pytest.param(_range(None, True, None, True, "1"), ["whole number"], ["from", "above", "more"], id="no bound at all"),
        pytest.param(_range(None, True, "5", False), ["below 5"], ["from", "above"], id="no bottom"),
    ],
)
def test_a_range_is_said_with_its_own_ends_and_whether_each_end_is_taken(bounds: Bounds, carries: list[str], never: list[str]) -> None:
    said = bounds_in_words(bounds)

    assert all(part in said for part in carries), said
    assert not any(part in said for part in never), said


def test_a_number_refused_is_said_back_with_what_it_was_for_and_what_is_taken() -> None:
    percent = _range("0", True, "100", True, "0.000001")

    above = out_of_range_sentence("Whole-structure cost", "150", percent, "above")
    finer = out_of_range_sentence("Whole-structure cost", "3.3333333", percent, "finer")
    not_whole = out_of_range_sentence("Entry wait", 2.5, _range("1", True, "300", True, "1"), "finer")

    assert above.startswith("Whole-structure cost") and "150" in above and "from 0 to 100" in above
    # Inside the range and too fine: what is said is the step, not the ends.
    assert "3.3333333" in finer and "0.000001" in finer and "from 0 to 100" not in finer
    assert "2.5" in not_whole and "whole number" in not_whole


def test_a_number_of_fifteen_characters_or_fewer_is_said_back_as_it_was_typed() -> None:
    assert [said_back(typed) for typed in ("1e999999999", "-1e999999999", 2.5, 150, "123456789012345", "-0.000001")] == [
        "1e999999999", "-1e999999999", "2.5", "150", "123456789012345", "-0.000001",
    ]


@pytest.mark.parametrize(
    ("typed", "length"),
    [
        pytest.param("9" * 5_000, "5,000 digits", id="five thousand nines"),
        pytest.param(10**400, "401 digits", id="a whole number of 401 digits"),
        pytest.param("1234567890123456", "16 digits", id="one digit too long"),
        pytest.param(-(10**20), "21 digits", id="as long below nothing"),
        pytest.param("3.33333333333333333", "19 characters", id="a long fraction, by its characters"),
        pytest.param("1e" + "9" * 40, "42 characters", id="a long exponent, by its characters"),
    ],
)
def test_a_number_too_long_to_have_been_meant_is_said_by_its_length_and_never_whole(typed: object, length: str) -> None:
    said = said_back(typed)

    assert length in said and len(said) < 40
    assert str(typed)[:16] not in said
    assert ("negative" in said) is str(typed).startswith("-")


# ---- Numbers, as an owner writes them ----------------------------------------------------------------------


def test_a_number_is_written_out_with_no_exponent_and_no_trailing_zeros() -> None:
    assert [plain(number) for number in (5.0, 0.05, Decimal("1E+2"), Decimal("99999.999"), -2.50, 0.0, 1e-05)] == [
        "5", "0.05", "100", "99,999.999", "-2.5", "0", "0.00001",
    ]


def test_a_price_a_share_is_written_to_the_cent_or_as_fine_as_it_is_and_without_its_sign() -> None:
    assert [dollars(Decimal(price)) for price in ("1.94", "-3.07", "2", "0.005", "1234.5")] == [
        "$1.94", "$3.07", "$2.00", "$0.005", "$1,234.50",
    ]


def test_an_age_is_said_in_seconds_and_a_whole_minute_as_a_minute() -> None:
    assert [age_in_words(age) for age in (0, 999, 1_000, 2_400, 60_000, 61_000, 120_000)] == [
        "under a second", "under a second", "1 second", "2 seconds", "1 minute", "61 seconds", "2 minutes",
    ]


def test_every_answer_the_model_can_give_for_a_delta_has_its_own_words_and_only_one_says_it_was_worked_out() -> None:
    said = {status: delta_status_sentence(status) for status in AmericanIvStatus}

    assert len(set(said.values())) == len(AmericanIvStatus)
    assert [status for status, words in said.items() if not words.startswith("Not worked out")] == [AmericanIvStatus.SOLVED]
    # Inputs that are not known are not one of the model's answers: it was never asked.
    assert delta_status_sentence(None) not in said.values()
