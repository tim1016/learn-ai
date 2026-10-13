"""The small phrases a setup's rules are put into words with, in one place (#3030).

Two readers of a setup say its rules: ``describe.py`` (a setup in sentences, before a run is started) and
``comparison.py`` (two saved runs' settings side by side). A rule reads the same in both because both take its words
from here; a sentence is built around a phrase by whichever of them needs it, and the phrase is never written twice.
Nothing here knows which reader is asking.

Formula: none. Every phrase is a rule's own numbers written out; nothing is calculated, and a number is written without a
  trailing zero or an exponent (25.0000 reads 25) and never rounded.
Reference: ``app/options_backtest/setup.py`` (the rules these words describe).
Canonical implementation: this file.
Validated against: tests/options_backtest/test_describe.py (the sentences made from these phrases) and
  tests/options_backtest/test_phrases.py (the phrases alone).
"""

from __future__ import annotations

from decimal import Decimal

from app.options_backtest.setup import (
    MS_PER_MINUTE,
    ExpiryRule,
    LegSide,
    Momentum,
    ReentryOnStop,
    SessionTime,
    StopLoss,
)

# What each momentum kind measures in, and which side of the starting premium it waits for.
_MOMENTUM_WAYS: dict[str, tuple[str, str]] = {
    "percent_up": ("percent", "above"),
    "percent_down": ("percent", "below"),
    "points_up": ("points", "above"),
    "points_down": ("points", "below"),
}


# How a leg's side is said of a leg that is part of a setup: it was bought, or it was sold.
_SIDE_WORDS: dict[LegSide, str] = {"buy": "bought", "sell": "sold"}


def count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def plain(value: Decimal) -> str:
    """``value`` without a trailing zero or an exponent: 25.0000 reads 25."""
    return format(value.normalize(), "f")


def dollars(value: Decimal) -> str:
    """Dollars with at least cents and thousands grouped, every digit kept: $10,000.00, $0.5 reads $0.50, $0.0125 stays."""
    whole, _, cents = plain(value).partition(".")
    return f"${int(whole):,}.{cents.ljust(2, '0')}"


def amount(unit: str, value: Decimal) -> str:
    """``unit`` is ``percent`` or ``points``, the two a rule is set in."""
    if unit == "percent":
        return f"{plain(value)}%"
    return f"{plain(value)} {'point' if value == 1 else 'points'}"


def distance(time: SessionTime, *, at: str = "at ") -> str:
    """A time as its distance from the open or the close. ``at`` leads a time that is the open or the close itself."""
    minutes = time.offset_ms // MS_PER_MINUTE
    if time.anchor == "open":
        return f"{at}the open" if minutes == 0 else f"{count(minutes, 'minute')} after the open"
    return f"{at}the close" if minutes == 0 else f"{count(minutes, 'minute')} before the close"


def expiry(rule: ExpiryRule) -> str:
    if rule.kind == "same_day":
        return "expiring on the trading date itself"
    if rule.kind == "this_week":
        return "expiring on the nearest listed expiry, today included, that falls on the last trading day of its week"
    if rule.days == 0:
        return "expiring on the nearest listed expiry, today included"
    return f"expiring on the nearest listed expiry at least {count(rule.days, 'calendar day')} after the trading day"


def strikes_from_the_money(steps: int) -> str:
    """How far a strike is from the money, for a rule that is not at it: 2 strikes above the money."""
    return f"{count(abs(steps), 'strike')} {'above' if steps > 0 else 'below'} the money"


def bought_or_sold(side: LegSide) -> str:
    return _SIDE_WORDS[side]


def stop_loss(rule: StopLoss, side: LegSide = "buy") -> str:
    """What a minute must close at to stop a leg out: a fall from what a bought leg cost, a rise from what a sold
    one fetched."""
    if side == "sell":
        return f"at or above its stop level, {amount(rule.kind, rule.value)} above what it was sold for"
    return f"at least {amount(rule.kind, rule.value)} below what was paid"


def momentum(rule: Momentum) -> str:
    unit, way = _MOMENTUM_WAYS[rule.kind]
    return f"at least {amount(unit, rule.value)} {way} where its premium stood at the entry time"


def reentry(rule: ReentryOnStop, side: LegSide = "buy") -> str:
    """What a leg does after a stop, as a sentence.

    A sold leg may have no stop of its own and still re-enter: a stop on the bought leg that protects it closes it
    with that leg (#3107), so its sentence says the stop that closes it is its own or one that takes it along.
    """
    times = f"up to {count(rule.count, 'time')} in a session"
    fresh = (
        "After a stop closes it (its own, or one that takes it along), picks a fresh contract by the same rules"
        if side == "sell"
        else "After a stop, picks a fresh contract by the same rules"
    )
    if rule.kind == "at_once":
        return f"{fresh} and {'sells' if side == 'sell' else 'buys'} it at once, {times}."
    return f"{fresh}, takes a new starting premium from it and waits for momentum again, {times}."


def square_off(mode: str, *, sells: bool = False) -> str:
    """``mode`` is ``partial`` or ``complete``, the two a setup's square-off is set to.

    ``sells`` is whether the setup has a sold leg. A partial square-off is then not only the leg that was stopped: a
    bought leg that is stopped takes with it the sold legs it protects (#3107). They are closed in its minute and exit
    group, last listed first, which can close more legs than another rule would, and the sentence says so.
    """
    if mode == "partial":
        if sells:
            return (
                "Square off is partial: a stop closes only the leg that was stopped, and a stopped bought leg takes "
                "with it the sold legs it protects, closed in the same minute and exit group, last listed first, "
                "which can close more legs than strictly needed."
            )
        return "Square off is partial: a stop closes only the leg that was stopped."
    return (
        "Square off is complete: a stop closes every open leg and cancels legs still waiting for momentum, "
        "which stay cancelled for the day."
    )
