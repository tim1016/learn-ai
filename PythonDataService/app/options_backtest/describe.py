"""A setup in plain sentences, for the owner to read before pressing Start (#3030).

One block of sentences for the window, one per leg, one for square-off and
one for costs. A rule that is switched off is not mentioned. Each time is said
as a distance from the open or the close and as the New York clock time on a
full session, so a setup that holds its distance on a half day is read for the
usual day. The entry and exit times are when a rule is read: an order fills at
the next minute's opening trade, and the window says so. A rule that triggers
on a premium is reached only by a minute's close. Whether the times fit a full
day is the setup's own rule (``window_fits``), the one the replay applies to
skip a session. The words are written here and the page shows them as they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.options_backtest.session_clock import SessionClock
from app.options_backtest.setup import (
    MS_PER_MINUTE,
    ExpiryRule,
    FeePolicy,
    LegSetup,
    Momentum,
    OptionsBacktestSetup,
    ReentryOnStop,
    SessionTime,
)


@dataclass(frozen=True)
class LegDescription:
    """One leg, in a sentence or two."""

    leg_id: str
    text: str


@dataclass(frozen=True)
class SetupDescription:
    """A setup in sentences. ``notices`` holds what the owner should read first, and is empty for a setup that looks sound."""

    window: str
    legs: tuple[LegDescription, ...]
    square_off: str
    costs: str
    notices: tuple[str, ...]


def describe_setup(setup: OptionsBacktestSetup, *, full_session: SessionClock) -> SetupDescription:
    """``setup`` in sentences, with its times read against the clock of a full session."""
    return SetupDescription(
        window=_window(setup, full_session),
        legs=tuple(LegDescription(leg_id=leg.leg_id, text=_leg(leg)) for leg in setup.legs),
        square_off=_square_off(setup),
        costs=_costs(setup),
        notices=_notices(setup, full_session),
    )


def _window(setup: OptionsBacktestSetup, clock: SessionClock) -> str:
    sentences = [
        f"Trades {setup.underlying}.",
        f"The entry is read {_time(setup.entry, clock)}.",
        f"The exit is read {_time(setup.exit, clock)}.",
        "On a day that closes early both times keep their distance from the open and the close, "
        "and a day they no longer fit is skipped.",
        "An order fills at the next minute's opening trade, never at the minute or the price that set it off.",
        "A buy is dropped if its contract does not trade in that minute; a sale is tried each minute until it does.",
        "At the exit, whatever is open is sold, legs still waiting are cancelled and nothing more is bought.",
    ]
    if setup.no_reentry_after is not None:
        sentences.append(
            f"A stop hit later than {_time(setup.no_reentry_after, clock, at='')} earns no re-entry. "
            "First entries are not affected."
        )
    return " ".join(sentences)


def _notices(setup: OptionsBacktestSetup, clock: SessionClock) -> tuple[str, ...]:
    open_ms, close_ms = _clock_ms(clock)
    if setup.window_fits(session_open_ms=open_ms, session_close_ms=close_ms):
        return ()
    entry_ms = setup.entry.at(session_open_ms=open_ms, session_close_ms=close_ms)
    exit_ms = setup.exit.at(session_open_ms=open_ms, session_close_ms=close_ms)
    notices = ["On a full day this setup would not trade, and the run skips any session its times do not fit."]
    if entry_ms < open_ms + MS_PER_MINUTE:
        notices.append("The entry is too close to the open, or before it. It must be at least a minute after the open.")
    if exit_ms > close_ms - MS_PER_MINUTE:
        notices.append(
            "The exit is too close to the close, or after it. It must be at least a minute before the close."
        )
    if exit_ms <= entry_ms:
        notices.append("The exit is not after the entry.")
    return tuple(notices)


def _time(time: SessionTime, clock: SessionClock, *, at: str = "at ") -> str:
    """``time`` as a distance and a clock time. ``at`` leads a time that is the open or the close itself."""
    minutes = time.offset_ms // MS_PER_MINUTE
    if time.anchor == "open":
        distance = f"{at}the open" if minutes == 0 else f"{_minutes(minutes)} after the open"
    else:
        distance = f"{at}the close" if minutes == 0 else f"{_minutes(minutes)} before the close"
    return f"{distance} ({_on_a_full_day(_minute_of_day(time, clock), clock)})"


def _clock_ms(clock: SessionClock) -> tuple[int, int]:
    """The full session's open and close counted from New York midnight; the setup's rules only compare them."""
    return clock.open_minute_et * MS_PER_MINUTE, clock.close_minute_et * MS_PER_MINUTE


def _minute_of_day(time: SessionTime, clock: SessionClock) -> int:
    open_ms, close_ms = _clock_ms(clock)
    return time.at(session_open_ms=open_ms, session_close_ms=close_ms) // MS_PER_MINUTE


def _on_a_full_day(minute: int, clock: SessionClock) -> str:
    # Outside the session there is no clock time worth printing: it would read as a time that trades.
    if minute < clock.open_minute_et:
        return "before the open on a full day"
    if minute > clock.close_minute_et:
        return "after the close on a full day"
    return f"{minute // 60:02d}:{minute % 60:02d} on a full day"


def _leg(leg: LegSetup) -> str:
    sentences = [f"Leg {leg.leg_id}: buys {_what_is_bought(leg)} {_expiry(leg.expiry)}.", _when_it_buys(leg.momentum)]
    if leg.stop_loss is not None:
        sentences.append(
            f"Sells once a minute closes at least {_amount(leg.stop_loss.kind, leg.stop_loss.value)} below what was paid."
        )
    if leg.reentry_on_stop is not None:
        sentences.append(_reentry(leg.reentry_on_stop))
    return " ".join(sentences)


def _what_is_bought(leg: LegSetup) -> str:
    right = f"{leg.right}{'' if leg.contracts == 1 else 's'}"
    if leg.strike.kind == "atm":
        return f"{leg.contracts} at-the-money {right}"
    where = "above" if leg.strike.steps > 0 else "below"
    return f"{leg.contracts} {right} {_count(abs(leg.strike.steps), 'strike')} {where} the money"


def _expiry(rule: ExpiryRule) -> str:
    if rule.kind == "same_day":
        return "expiring on the trading date itself"
    if rule.kind == "this_week":
        return "expiring on the nearest listed expiry, today included, that falls on the last trading day of its week"
    if rule.days == 0:
        return "expiring on the nearest listed expiry, today included"
    return f"expiring on the nearest listed expiry at least {_count(rule.days, 'calendar day')} after the trading day"


# What each momentum kind measures in, and which side of the starting premium it waits for.
_MOMENTUM_WAYS: dict[str, tuple[str, str]] = {
    "percent_up": ("percent", "above"),
    "percent_down": ("percent", "below"),
    "points_up": ("points", "above"),
    "points_down": ("points", "below"),
}


def _when_it_buys(momentum: Momentum | None) -> str:
    if momentum is None:
        return "The buy comes as soon as the entry is read."
    unit, way = _MOMENTUM_WAYS[momentum.kind]
    return (
        f"The buy waits until a minute closes at least {_amount(unit, momentum.value)} {way} "
        "where its premium stood at the entry time."
    )


def _amount(unit: str, value: Decimal) -> str:
    """``unit`` is ``percent`` or ``points``, the two a rule is set in."""
    if unit == "percent":
        return f"{_plain(value)}%"
    return f"{_plain(value)} {'point' if value == 1 else 'points'}"


def _reentry(rule: ReentryOnStop) -> str:
    times = f"up to {_count(rule.count, 'time')} in a session"
    fresh = "After a stop, picks a fresh contract by the same rules"
    if rule.kind == "at_once":
        return f"{fresh} and buys it at once, {times}."
    return f"{fresh}, takes a new starting premium from it and waits for momentum again, {times}."


def _square_off(setup: OptionsBacktestSetup) -> str:
    if setup.square_off == "partial":
        return "Square off is partial: a stop closes only the leg that was stopped."
    return (
        "Square off is complete: a stop closes every open leg and cancels legs still waiting for momentum, "
        "which stay cancelled for the day."
    )


def _costs(setup: OptionsBacktestSetup) -> str:
    step = _dollars(setup.price_step_usd)
    if setup.slippage_percent == 0:
        fills = f"No slippage is applied; fills are still rounded against you to {step}."
    else:
        fills = f"Each fill is moved {_plain(setup.slippage_percent)}% against you, then rounded against you to {step}."
    return f"{fills} {_fees(setup.fees)} Starting cash is {_dollars(setup.starting_cash_usd)}."


def fee_policy_sentence(fees: FeePolicy) -> str:
    """The fee policy in one sentence, for the setup's costs and for the headline of a run."""
    # The policy names a per-contract fee for the per-contract kind and for no other.
    if fees.per_contract_usd is not None:
        return f"Fees are {_dollars(fees.per_contract_usd)} per contract traded."
    if fees.kind == "estimated":
        return "Fees are estimated from published option fee rates."
    return "Fees are not charged, so results are before fees."


def _fees(fees: FeePolicy) -> str:
    if fees.kind == "estimated":
        return f"{fee_policy_sentence(fees)} A session from before those rates exist cannot be run with this choice."
    return fee_policy_sentence(fees)


def _minutes(minutes: int) -> str:
    return _count(minutes, "minute")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _plain(value: Decimal) -> str:
    """``value`` without a trailing zero or an exponent: 25.0000 reads 25."""
    return format(value.normalize(), "f")


def _dollars(value: Decimal) -> str:
    whole, _, cents = _plain(value).partition(".")
    return f"${int(whole):,}.{cents.ljust(2, '0')}"
