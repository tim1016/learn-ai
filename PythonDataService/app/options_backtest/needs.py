"""Exactly which option contracts one session's replay could read (#3030).

A run reads one session at a time, and a session's option bars are a read of their own for every contract. The
replay can ask the market for the bars of a contract only after a leg has picked it, and a pick depends on the
underlying's price alone: never on what an option traded at, on a fill, or on how much slippage moved a stop. So the
set of contracts a session could read follows from the underlying's bars and the listing, before one option bar is
read. This module works that set out. It is a superset the replay never leaves, and it is not a guess: every
contract in it is one a pick can come to.

A leg picks at the entry time. A leg that can come back after a stop picks afresh at the minute its re-entry
starts, which is any minute after the entry time and before the exit time at which the underlying traded (the replay
starts a re-entry only on a minute the underlying has a close, and enters nothing at or after the exit time). A leg
can come back if it has a re-entry rule and something can close it: its own stop, or, under a complete square-off,
a stop on any leg.

A session the replay skips needs nothing. The reasons are the replay's own, in its order: the setup's window does not
fit the session (the setup's ``window_fits`` is the one rule), and then the underlying has no bar closing at the
entry time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.picking import Listing, prepare_leg
from app.options_backtest.results import SkipReason
from app.options_backtest.setup import LegSetup, OptionsBacktestSetup


@dataclass(frozen=True)
class SessionNeeds:
    """The contracts whose bars a session's replay could read, or why the session is skipped and reads none."""

    skip_reason: SkipReason | None
    contracts: frozenset[OptionContract]


def _can_come_back(setup: OptionsBacktestSetup, leg: LegSetup) -> bool:
    """Whether ``leg`` can start a re-entry: it has a rule for one and something can close it."""
    if leg.reentry_on_stop is None:
        return False
    if leg.stop_loss is not None:
        return True
    # A complete square-off closes every open leg when any leg is stopped, and a closed leg earns its re-entry.
    return setup.square_off == "complete" and any(other.stop_loss is not None for other in setup.legs)


def session_needs(
    setup: OptionsBacktestSetup,
    *,
    trading_date: date,
    session_open_ms: int,
    session_close_ms: int,
    underlying: Sequence[MinuteBar],
    listing: Listing,
) -> SessionNeeds:
    """The contracts a replay of ``setup`` over the session on ``trading_date`` could ask the market for.

    ``underlying`` is the session's raw minute bars, named by their close, in time order and without repeats
    (the market a run builds refuses any others); ``listing`` is the listing of ``trading_date``.
    """
    if not setup.window_fits(session_open_ms=session_open_ms, session_close_ms=session_close_ms):
        return SessionNeeds(SkipReason.WINDOW_DOES_NOT_FIT, frozenset())
    entry_ms = setup.entry.at(session_open_ms=session_open_ms, session_close_ms=session_close_ms)
    exit_ms = setup.exit.at(session_open_ms=session_open_ms, session_close_ms=session_close_ms)
    closes = {bar.close_ms: bar.close for bar in underlying}
    entry_price = closes.get(entry_ms)
    if entry_price is None:
        return SessionNeeds(SkipReason.UNDERLYING_DID_NOT_TRADE, frozenset())
    # A re-entry can start at any minute strictly between the entry time and the exit time that has a close.
    later_prices = [price for close_ms, price in closes.items() if entry_ms < close_ms < exit_ms]
    contracts: set[OptionContract] = set()
    for leg in setup.legs:
        prepared = prepare_leg(leg, listing=listing, trading_date=trading_date)
        if isinstance(prepared, PickRefused):
            continue
        prices: set[Decimal] = {entry_price}
        if _can_come_back(setup, leg):
            prices.update(later_prices)
        for price in prices:
            picked = prepared.pick(price)
            if isinstance(picked, Picked):
                contracts.add(picked.contract)
    return SessionNeeds(None, frozenset(contracts))
