"""The words of an option structure preview that are not about one figure: a leg's rules, a range, a date, an age.

Every sentence a screen shows on the builder is written on the server
(#2913, #2915). The ones beside a figure are written where the figure is
turned into its view (``app.services.option_preview_views``). The ones here
say what was asked, what a number may be, and which leg a refusal is about.

**A leg is named by its place.** The builder numbers its rows "Leg 1, Leg 2".
The plan's rules, the resolver and the structure check name a leg by its id,
in quotes, because an id is what they are handed. ``legs_by_number`` turns
what they said into the builder's words. Their own words are not changed at
the source: an entry's refusal is kept with the entry as it was said.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import assert_never

from app.options.american import AmericanIvStatus
from app.options.bounds import Bounds, BoundsFault
from app.options.expiry import option_expiry_date
from app.schemas.action_plan import (
    AbsoluteExpiry,
    AbsoluteStrike,
    AtmOffsetStrike,
    AtmStrike,
    DeltaStrike,
    ExpirySelector,
    LegDistanceStrike,
    MinDteExpiry,
    NearestWeeklyExpiry,
    PercentStrike,
    SameDayExpiry,
    StrikeSelector,
)
from app.schemas.option_preview import OptionLegRules

_CENT = Decimal("0.01")
# The most of a number an owner typed that is said back. More than this is not a number anyone meant.
_MOST_SAID_BACK = 24


def sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", "?", "!")) else ".")


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def plain(number: Decimal | float | int) -> str:
    """A number written out as an owner would write it: no exponent, no trailing zeros."""
    exact = number if isinstance(number, Decimal) else Decimal(repr(number))
    if not exact:
        return "0"
    return f"{exact.normalize():,f}"


def dollars(amount: Decimal) -> str:
    """The size of a price per share, in dollars: to the cent, or as fine as it is."""
    size = abs(amount)
    return f"${size.quantize(_CENT) if size == size.quantize(_CENT) else size.normalize():,f}"


def plural(count: int, one: str) -> str:
    return f"{count:,} {one}{'' if count == 1 else 's'}"


def said_back(typed: object) -> str:
    """What an owner typed, as it is said back to them: cut short when it is too long to be a number anyone meant."""
    text = str(typed)
    return text if len(text) <= _MOST_SAID_BACK else f"{text[: _MOST_SAID_BACK - 3]}..."


def expiry_in_words(expiry: date) -> str:
    """A date as a contract's name writes it: "Oct 9 '26"."""
    return f"{expiry:%b} {expiry.day} '{expiry:%y}"


def age_in_words(age_ms: int) -> str:
    """How long ago, in whole seconds, or in whole minutes when it is a whole number of them."""
    if age_ms < 1_000:
        return "under a second"
    minutes, left_over = divmod(age_ms, 60_000)
    return plural(minutes, "minute") if minutes and not left_over else plural(age_ms // 1_000, "second")


# ---- a range ------------------------------------------------------------------------------------


def bounds_in_words(bounds: Bounds) -> str:
    """A range as a sentence takes it: "a number above 0 and below 1", "a whole number from 1 to 300"."""
    low = None if bounds.lowest is None else plain(bounds.lowest)
    high = None if bounds.highest is None else plain(bounds.highest)
    whole = bounds.step == 1
    if low is not None and high is not None:
        if bounds.lowest_included:
            span = f" from {low} to {high}" if bounds.highest_included else f" from {low} to below {high}"
        else:
            span = f" above {low} and up to {high}" if bounds.highest_included else f" above {low} and below {high}"
    elif low is not None:
        span = f" of {low} or more" if bounds.lowest_included else f" above {low}"
    elif high is not None:
        span = f" up to {high}" if bounds.highest_included else f" below {high}"
    else:
        span = ""
    finer = "" if whole or bounds.step is None else f", no finer than {plain(bounds.step)}"
    return f"{'a whole number' if whole else 'a number'}{span}{finer}"


def out_of_range_sentence(what: str, typed: object, bounds: Bounds, fault: BoundsFault, *, takes: str | None = None) -> str:
    """Why a number is refused: what it was for, what was given, and what is taken. ``what`` begins the
    sentence: "Leg 2: Delta target"."""
    if fault == "finer" and bounds.step != 1:
        return f"{what} was given {said_back(typed)}. It is taken no finer than {plain(bounds.step or Decimal(0))}."
    return f"{what} was given {said_back(typed)}. It takes {takes or bounds_in_words(bounds)}."


# ---- a leg, by its place ------------------------------------------------------------------------

_LOOP = re.compile(r"\(([a-z0-9_]{1,32}(?: -> [a-z0-9_]{1,32})+)\)")
_AFTER_LEG = re.compile(r"\b([Ll]egs?) '([a-z0-9_]{1,32})'")
_QUOTED = re.compile(r"'([a-z0-9_]{1,32})'")
_SEVERAL_LEGS = re.compile(r"\b([Ll]egs) (\d+) and leg (\d+)\b")


def legs_by_number(text: str, order: Sequence[str]) -> str:
    """``text`` with every leg id it quotes said as that leg's place in ``order``, counted from one.

    "leg 'short_call' sits below leg 'long_call'" becomes "leg 2 sits below
    leg 1". Only a quoted id is touched, and a loop of ids written between
    brackets, which is the one place an id is written bare. A quoted word
    that is no leg of ``order`` is left as it is.
    """
    place = {leg_id: number for number, leg_id in enumerate(order, start=1)}

    def numbered(found: re.Match[str]) -> str:
        return f"{found[1]} {place[found[2]]}" if found[2] in place else found[0]

    def named(found: re.Match[str]) -> str:
        return f"leg {place[found[1]]}" if found[1] in place else found[0]

    def looped(found: re.Match[str]) -> str:
        ids = found[1].split(" -> ")
        return f"({' -> '.join(f'leg {place[leg_id]}' for leg_id in ids)})" if all(i in place for i in ids) else found[0]

    def paired(found: re.Match[str]) -> str:
        # "legs 2 and leg 1" is "legs 1 and 2": two legs named together are named in their places' order.
        lower, higher = sorted((int(found[2]), int(found[3])))
        return f"{found[1]} {lower} and {higher}"

    said = _LOOP.sub(looped, text)
    said = _AFTER_LEG.sub(numbered, said)
    said = _QUOTED.sub(named, said)
    return _SEVERAL_LEGS.sub(paired, said)


def leg_names(leg_ids: Sequence[str], order: Sequence[str]) -> str:
    """Legs by their places, as a sentence begins with them: "Leg 2", "Legs 2 and 3"."""
    numbers = [str(order.index(leg_id) + 1) for leg_id in leg_ids]
    if len(numbers) == 1:
        return f"Leg {numbers[0]}"
    return f"Legs {', '.join(numbers[:-1])} and {numbers[-1]}"


# ---- a leg's rules ------------------------------------------------------------------------------


def _delta(size: float) -> str:
    """A delta to two places at least: 0.5 is "0.50", and 0.305 stays "0.305"."""
    exact = Decimal(repr(size))
    return f"{exact.quantize(_CENT) if exact == exact.quantize(_CENT) else exact.normalize():f}"


def _strike_in_words(rule: StrikeSelector, order: Sequence[str]) -> str:
    match rule:
        case AtmStrike():
            return "at the money"
        case AtmOffsetStrike(offset=offset):
            if offset == 0:
                return "at the money"
            return f"{plural(abs(offset), 'strike')} {'above' if offset > 0 else 'below'} the money"
        case AbsoluteStrike(strike=strike):
            return f"the {plain(strike)} strike"
        case DeltaStrike(target=target, tolerance_delta=within):
            return f"nearest {_delta(target)} delta, within {_delta(within)}"
        case LegDistanceStrike(leg_id=other, dollars=distance, tolerance_dollars=within):
            other_leg = f"leg {order.index(other) + 1}" if other in order else "another leg"
            where = (
                f"the same strike as {other_leg}"
                if distance == 0
                else f"${plain(abs(distance))} {'above' if distance > 0 else 'below'} {other_leg}"
            )
            return where if within == 0 else f"{where}, within ${plain(within)}"
        case PercentStrike(percent=percent, tolerance_percent=within):
            where = "at the price" if percent == 0 else f"{plain(abs(percent))}% {'above' if percent > 0 else 'below'} the price"
            return f"{where}, within {plain(within)}%"
        case _:
            assert_never(rule)


def _expiry_in_words(rule: ExpirySelector) -> str:
    match rule:
        case MinDteExpiry(days=days):
            return f"at least {plural(days, 'day')} out"
        case NearestWeeklyExpiry():
            return "nearest weekly"
        case SameDayExpiry():
            return "same day"
        case AbsoluteExpiry(expiration_ms=expiration_ms):
            return f"expiring {expiry_in_words(option_expiry_date(expiration_ms))}"
        case _:
            assert_never(rule)


def leg_asked_in_words(leg: OptionLegRules, order: Sequence[str]) -> tuple[str, str]:
    """A leg's side, and the rest of its line: ("Buy", "1 call · nearest 0.50 delta, within 0.05 · nearest weekly")."""
    what = f"{leg.qty_ratio:,} {leg.right}{'' if leg.qty_ratio == 1 else 's'}"
    line = " · ".join([what, _strike_in_words(leg.strike, order), _expiry_in_words(leg.expiry)])
    return ("Buy" if leg.position == "long" else "Sell"), line


# ---- what the model made of a leg ---------------------------------------------------------------

_DELTA_STATUS = {
    AmericanIvStatus.SOLVED: "Worked out from its mid quote.",
    AmericanIvStatus.TIME_TO_EXPIRY_BELOW_FLOOR: "Not worked out: it has under a minute left.",
    AmericanIvStatus.BELOW_EXERCISE_VALUE: "Not worked out: it is quoted under what exercising it pays.",
    AmericanIvStatus.BELOW_VOLATILITY_FLOOR: (
        "Not worked out: it is quoted at what exercising it pays, which no volatility explains."
    ),
    AmericanIvStatus.ABOVE_VOLATILITY_CEILING: "Not worked out: it is quoted above any price the model gives.",
    AmericanIvStatus.NO_CONVERGENCE: "Not worked out: the model found no answer for its quote.",
    AmericanIvStatus.INVALID_INPUT: "Not worked out: the model was given a number it has no meaning for.",
}
DELTA_INPUTS_NOT_KNOWN = "Not worked out: the dividends are not known."


def delta_status_sentence(status: AmericanIvStatus | None) -> str:
    """How a leg's delta stands, in words. ``None``: the model's inputs are not known, so it was not asked."""
    return DELTA_INPUTS_NOT_KNOWN if status is None else _DELTA_STATUS[status]
