"""A setup in plain sentences, for the owner to read before pressing Start (#3030).

One sentence for the window, one per leg, one for square-off and one for
costs. A rule that is switched off is not mentioned. Each time is said as a
distance from the open or the close and as the New York clock time on a full
session, so a setup that holds its distance on a half day is read for the
usual day. The words are written here and the page shows them as they are.
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
    StopLoss,
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
    sentence = f"Trades {setup.underlying}. Starts {_time(setup.entry, clock)} and is flat {_time(setup.exit, clock)}."
    if setup.no_reentry_after is None:
        return sentence
    return f"{sentence} A stop hit later than {_time(setup.no_reentry_after, clock)} earns no re-entry."


def _notices(setup: OptionsBacktestSetup, clock: SessionClock) -> tuple[str, ...]:
    if _minute_of_day(setup.exit, clock) <= _minute_of_day(setup.entry, clock):
        return ("The exit is no later than the entry on a full day, so this setup would not trade.",)
    return ()


def _time(time: SessionTime, clock: SessionClock) -> str:
    minutes = time.offset_ms // MS_PER_MINUTE
    if time.anchor == "open":
        distance = "at the open" if minutes == 0 else f"{_minutes(minutes)} after the open"
    else:
        distance = "at the close" if minutes == 0 else f"{_minutes(minutes)} before the close"
    return f"{distance} ({_on_a_full_day(_minute_of_day(time, clock), clock)})"


def _minute_of_day(time: SessionTime, clock: SessionClock) -> int:
    minutes = time.offset_ms // MS_PER_MINUTE
    return clock.open_minute_et + minutes if time.anchor == "open" else clock.close_minute_et - minutes


def _on_a_full_day(minute: int, clock: SessionClock) -> str:
    # Outside the session there is no clock time worth printing: it would read as a time that trades.
    if minute < clock.open_minute_et:
        return "before the open on a full day"
    if minute > clock.close_minute_et:
        return "after the close on a full day"
    return f"{minute // 60:02d}:{minute % 60:02d} on a full day"


def _leg(leg: LegSetup) -> str:
    entry = f"Leg {leg.leg_id}: buys {_what_is_bought(leg)} {_expiry(leg.expiry)}, {_when_it_buys(leg.momentum)}."
    sentences = [entry]
    if leg.stop_loss is not None:
        sentences.append(f"Sells if it falls {_stop_distance(leg.stop_loss)} below what was paid.")
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
        return "expiring the same day"
    if rule.kind == "min_days":
        if rule.days == 0:
            return "expiring on the nearest listed day, today included"
        return f"expiring at least {_count(rule.days, 'day')} after the trading day"
    return "expiring this week"


def _when_it_buys(momentum: Momentum | None) -> str:
    if momentum is None:
        return "straight away"
    amount, way = _momentum_amount(momentum)
    return f"once its premium is {amount} {way} where it stood at the start"


def _momentum_amount(momentum: Momentum) -> tuple[str, str]:
    unit, _, direction = momentum.kind.rpartition("_")
    return _amount(unit, momentum.value), "above" if direction == "up" else "below"


def _stop_distance(stop: StopLoss) -> str:
    return _amount(stop.kind, stop.value)


def _amount(unit: str, value: Decimal) -> str:
    """``unit`` is ``percent`` or ``points``, the two a rule is set in."""
    if unit == "percent":
        return f"{_plain(value)}%"
    return f"{_plain(value)} {'point' if value == 1 else 'points'}"


def _reentry(rule: ReentryOnStop) -> str:
    times = f"up to {_count(rule.count, 'time')}"
    if rule.kind == "at_once":
        return f"After a stop, buys again at once, {times}."
    return f"After a stop, picks a fresh contract and waits for momentum again, {times}."


def _square_off(setup: OptionsBacktestSetup) -> str:
    if setup.square_off == "partial":
        return "Square off is partial: a stop closes only the leg that was stopped."
    return "Square off is complete: a stop closes every open leg and cancels legs still waiting for momentum."


def _costs(setup: OptionsBacktestSetup) -> str:
    step = _dollars(setup.price_step_usd)
    if setup.slippage_percent == 0:
        fills = f"No slippage is applied; fills are still rounded against you to {step}."
    else:
        fills = f"Each fill is moved {_plain(setup.slippage_percent)}% against you, then rounded against you to {step}."
    return f"{fills} {_fees(setup.fees)} Starting cash is {_dollars(setup.starting_cash_usd)}."


def _fees(fees: FeePolicy) -> str:
    # The policy names a per-contract fee for the per-contract kind and for no other.
    if fees.per_contract_usd is not None:
        return f"Fees are {_dollars(fees.per_contract_usd)} per contract traded."
    if fees.kind == "estimated":
        return "Fees are estimated from published option fee rates."
    return "Fees are not charged, so results are before fees."


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
