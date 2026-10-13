"""A setup in plain sentences, for the owner to read before pressing Start (#3030).

One list of sentences for the window, one for each leg, one for square-off and
one for costs, each built a sentence at a time (never one string cut up), and the
page shows every sentence as it is. A rule that is switched off is not mentioned. Each time is said
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

from app.options_backtest import phrases
from app.options_backtest.session_clock import SessionClock
from app.options_backtest.setup import MS_PER_MINUTE, FeePolicy, LegSetup, OptionsBacktestSetup, SessionTime


@dataclass(frozen=True)
class LegDescription:
    """One leg, in a sentence or two."""

    leg_id: str
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class SetupDescription:
    """A setup in sentences. ``notices`` holds what the owner should read first, and is empty for a setup that looks sound."""

    window: tuple[str, ...]
    legs: tuple[LegDescription, ...]
    square_off: tuple[str, ...]
    costs: tuple[str, ...]
    notices: tuple[str, ...]


def describe_setup(setup: OptionsBacktestSetup, *, full_session: SessionClock) -> SetupDescription:
    """``setup`` in sentences, with its times read against the clock of a full session."""
    return SetupDescription(
        window=_window(setup, full_session),
        legs=tuple(LegDescription(leg_id=leg.leg_id, sentences=leg_sentences(leg)) for leg in setup.legs),
        square_off=_square_off(setup),
        costs=_costs(setup),
        notices=_notices(setup, full_session),
    )


def _window(setup: OptionsBacktestSetup, clock: SessionClock) -> tuple[str, ...]:
    sentences = [
        f"Trades {setup.underlying}.",
        f"The entry is read {_time(setup.entry, clock)}.",
        f"The exit is read {_time(setup.exit, clock)}.",
        "On a day that closes early both times keep their distance from the open and the close, "
        "and a day they no longer fit is skipped.",
        "An order fills at the next minute's opening trade, never at the minute or the price that set it off.",
        *_what_happens_to_orders(setup),
    ]
    if setup.no_reentry_after is not None:
        sentences.append(f"A stop hit later than {_time(setup.no_reentry_after, clock, at='')} earns no re-entry.")
        sentences.append("First entries are not affected.")
    return tuple(sentences)


def _what_happens_to_orders(setup: OptionsBacktestSetup) -> tuple[str, str]:
    """Which orders are dropped and which are tried again, and what the exit does.

    A setup that sells has orders to open and to close of both sides, so it says them by which is which. A setup that
    only buys is said as it always was.
    """
    if setup.sells:
        return (
            "An order to open is dropped if its contract does not trade in that minute; "
            "an order to close is tried each minute until it does.",
            "At the exit, a bought leg still open is sold, a sold leg still open is bought back, "
            "legs still waiting are cancelled and nothing more is opened.",
        )
    return (
        "A buy is dropped if its contract does not trade in that minute; a sale is tried each minute until it does.",
        "At the exit, whatever is open is sold, legs still waiting are cancelled and nothing more is bought.",
    )


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
    return f"{phrases.distance(time, at=at)} ({_on_a_full_day(_minute_of_day(time, clock), clock)})"


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


def leg_sentences(leg: LegSetup) -> tuple[str, ...]:
    """One leg in a sentence or two. A comparison of two runs says a leg only one of them has with these.

    A bought leg buys to open and sells to close. A sold leg sells to open and is bought back: its stop is a rise in
    its premium and its momentum rule gates the sale. The sale is refused, and the leg done for the day, unless a bought
    contract covers it in the minute it fills. A setup cannot know that one is open by then (the bought leg may wait
    on a momentum rule the sold leg does not have), so the replay settles it and this says what the replay does.
    """
    contract = f"{_what_is_traded(leg)} {phrases.expiry(leg.expiry)}"
    if leg.side == "sell":
        sentences = [f"Leg {leg.leg_id}: sells to open {contract}, and is bought back to close."]
    else:
        sentences = [f"Leg {leg.leg_id}: buys {contract}."]
    sentences.append(_when_it_opens(leg))
    if leg.side == "sell":
        sentences.append(
            f"The sale needs one bought {leg.right} of the same expiry for each contract sold, held already or "
            "filling in the same minute; with too few, it is refused and the leg is done for the day."
        )
    if leg.stop_loss is not None:
        closes = "Bought back" if leg.side == "sell" else "Sells"
        sentences.append(f"{closes} once a minute closes {phrases.stop_loss(leg.stop_loss, leg.side)}.")
    if leg.reentry_on_stop is not None:
        sentences.append(phrases.reentry(leg.reentry_on_stop, leg.side))
    return tuple(sentences)


def _what_is_traded(leg: LegSetup) -> str:
    right = f"{leg.right}{'' if leg.contracts == 1 else 's'}"
    if leg.strike.kind == "atm":
        return f"{leg.contracts} at-the-money {right}"
    return f"{leg.contracts} {right} {phrases.strikes_from_the_money(leg.strike.steps)}"


def _when_it_opens(leg: LegSetup) -> str:
    opens = "sale" if leg.side == "sell" else "buy"
    if leg.momentum is None:
        return f"The {opens} comes as soon as the entry is read."
    return f"The {opens} waits until a minute closes {phrases.momentum(leg.momentum)}."


def _square_off(setup: OptionsBacktestSetup) -> tuple[str, ...]:
    return (phrases.square_off(setup.square_off, sells=setup.sells),)


def _costs(setup: OptionsBacktestSetup) -> tuple[str, ...]:
    step = phrases.dollars(setup.price_step_usd)
    if setup.slippage_percent == 0:
        fills = f"No slippage is applied; fills are still rounded against you to {step}."
    else:
        fills = f"Each fill is moved {phrases.plain(setup.slippage_percent)}% against you, then rounded against you to {step}."
    return (fills, *_fees(setup.fees), f"Starting cash is {phrases.dollars(setup.starting_cash_usd)}.")


def fee_policy_sentence(fees: FeePolicy) -> str:
    """The fee policy in one sentence, for the setup's costs and for the headline of a run."""
    # The policy names a per-contract fee for the per-contract kind and for no other.
    if fees.per_contract_usd is not None:
        return f"Fees are {phrases.dollars(fees.per_contract_usd)} per contract traded."
    if fees.kind == "estimated":
        return "Fees are estimated from published option fee rates."
    return "Fees are not charged, so results are before fees."


def result_statements(setup: OptionsBacktestSetup) -> tuple[str, ...]:
    """What the result of a run of ``setup`` must not be taken for, in three sentences; none for a setup that only buys.

    A setup that sells is replayed under three choices the owner could mistake for facts: the legs that open in one
    minute are judged as one group, because minute bars cannot show which contract printed first; the requirement held
    against what is sold is Alpaca's published rule for covered positions, worked out here; and nothing in the replay
    asks a broker, so the result says nothing of whether one would have taken these orders (#3109, PRD #3095).
    """
    if not setup.sells:
        return ()
    return (
        "Legs opened in the same minute are treated as one group, and minute bars cannot show which of them printed "
        "first. A sold leg's cover is judged as if it were held beside it, which is a modelling choice and not "
        "observed coverage.",
        "The requirement held against what is sold is Alpaca's published rule for covered positions as of today, "
        "worked out by this system.",
        "This result claims nothing about whether a broker would have accepted these orders.",
    )


def _fees(fees: FeePolicy) -> tuple[str, ...]:
    if fees.kind == "estimated":
        return (fee_policy_sentence(fees), "A session from before those rates exist cannot be run with this choice.")
    return (fee_policy_sentence(fees),)
