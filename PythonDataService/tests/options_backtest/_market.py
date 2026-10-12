"""One session's market data built from plain dictionaries, and the setups a scene needs.

``TableMarket`` is a real ``SessionMarket``, not a mock. Times are minutes after
the open: minute ``k`` is the bar closing at ``at(k)``. A price list is
``{minute: (open, close)}`` or ``{minute: (open, high, low, close)}``; a minute
that is not listed did not trade, and nothing is filled in.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from app.options.contract import OptionContract, OptionRight
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.replay import FeeFor, replay_session
from app.options_backtest.results import EventKind, Fill, LegTrade, SessionOutcome
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup, OptionsBacktestSetup

# 2026-10-09, a full session: 09:30 to 16:00 New York time.
OPEN_MS = 1_791_552_600_000
FULL_CLOSE_MS = OPEN_MS + 390 * MS_PER_MINUTE
# A half day closes at 13:00.
HALF_CLOSE_MS = OPEN_MS + 210 * MS_PER_MINUTE

D = Decimal

Prices = Mapping[int, tuple[str, ...]]


def at(minute: int) -> int:
    """The instant of the bar closing ``minute`` minutes after the open."""
    return OPEN_MS + minute * MS_PER_MINUTE


def minute_of(instant_ms: int) -> int:
    return (instant_ms - OPEN_MS) // MS_PER_MINUTE


def contract(right: OptionRight, strike: str) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=date(2026, 10, 9), right=right, strike=D(strike))


def flat(price: str, first: int, last: int) -> dict[int, tuple[str, ...]]:
    """A contract that traded at one price, open and close, in every minute from ``first`` to ``last``."""
    return {minute: (price, price) for minute in range(first, last + 1)}


def _bar(minute: int, prices: tuple[str, ...]) -> MinuteBar:
    if len(prices) == 2:
        opened, closed = D(prices[0]), D(prices[1])
        return MinuteBar(
            close_ms=at(minute), open=opened, high=max(opened, closed), low=min(opened, closed), close=closed
        )
    opened, high, low, closed = (D(price) for price in prices)
    return MinuteBar(close_ms=at(minute), open=opened, high=high, low=low, close=closed)


class TableMarket:
    """A session's observations as the replay reads them, from tables."""

    def __init__(
        self,
        *,
        underlying: Mapping[int, str],
        picks: Mapping[tuple[str, str], Picked | PickRefused],
        options: Mapping[OptionContract, Prices],
        session_close_ms: int = FULL_CLOSE_MS,
    ) -> None:
        self.session_open_ms = OPEN_MS
        self.session_close_ms = session_close_ms
        self._underlying = {at(minute): D(price) for minute, price in underlying.items()}
        # A pick is looked up by the leg and the underlying price the replay hands in, as a real picker's
        # strike would follow the price: a replay that hands in the wrong price gets the wrong contract.
        self._picks = {(leg_id, D(price)): result for (leg_id, price), result in picks.items()}
        self._bars = {
            option: {at(minute): _bar(minute, prices) for minute, prices in series.items()}
            for option, series in options.items()
        }

    def underlying_close(self, close_ms: int) -> Decimal | None:
        return self._underlying.get(close_ms)

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        try:
            return self._picks[(leg.leg_id, underlying_price)]
        except KeyError:
            raise LookupError(
                f"the replay asked for a pick of leg {leg.leg_id!r} with the underlying at {underlying_price} "
                f"(minute {minute_of(at_ms)}), which this scene did not expect"
            ) from None

    def option_bar(self, option: OptionContract, close_ms: int) -> MinuteBar | None:
        return self._bars.get(option, {}).get(close_ms)


def steady(price: str, first: int = 1, last: int = 390) -> dict[int, str]:
    """An underlying that closed at one price in every minute from ``first`` to ``last``."""
    return {minute: price for minute in range(first, last + 1)}


def picked(*contracts: tuple[str, str, OptionContract]) -> dict[tuple[str, str], Picked | PickRefused]:
    """``(leg id, underlying price, contract)`` rows as a pick table."""
    return {(leg_id, price): Picked(option) for leg_id, price, option in contracts}


def leg(
    leg_id: str = "call",
    right: OptionRight = "call",
    *,
    contracts: int = 1,
    stop: tuple[str, str] | None = None,
    momentum: tuple[str, str] | None = None,
    reentry: tuple[str, int] | None = None,
) -> dict[str, Any]:
    """A leg as a setup is written: ``stop=("percent", "25")``, ``momentum=("percent_up", "25")``, ``reentry=("at_once", 1)``."""
    written: dict[str, Any] = {
        "leg_id": leg_id,
        "right": right,
        "contracts": contracts,
        "expiry": {"kind": "same_day"},
        "strike": {"kind": "atm"},
    }
    if stop is not None:
        written["stop_loss"] = {"kind": stop[0], "value": stop[1]}
    if momentum is not None:
        written["momentum"] = {"kind": momentum[0], "value": momentum[1]}
    if reentry is not None:
        written["reentry_on_stop"] = {"kind": reentry[0], "count": reentry[1]}
    return written


def after_open(minutes: int) -> dict[str, Any]:
    return {"anchor": "open", "offset_ms": minutes * MS_PER_MINUTE}


def before_close(minutes: int) -> dict[str, Any]:
    return {"anchor": "close", "offset_ms": minutes * MS_PER_MINUTE}


def setup(
    *legs: dict[str, Any],
    square_off: str = "partial",
    entry: dict[str, Any] | None = None,
    exit: dict[str, Any] | None = None,
    no_reentry_after: dict[str, Any] | None = None,
    slippage: str = "0",
    step: str = "0.01",
) -> OptionsBacktestSetup:
    """A setup that enters at minute 5 and leaves at minute 60 unless a scene says otherwise, with no slippage."""
    written: dict[str, Any] = {
        "underlying": "SPY",
        "entry": entry or after_open(5),
        "exit": exit or after_open(60),
        "square_off": square_off,
        "legs": list(legs),
        "slippage_percent": slippage,
        "price_step_usd": step,
        "fees": {"kind": "not_charged"},
        "starting_cash_usd": "10000",
    }
    if no_reentry_after is not None:
        written["no_reentry_after"] = no_reentry_after
    return OptionsBacktestSetup.model_validate(written)


def no_fee(_fill: object) -> Decimal:
    return D(0)


def replay(
    written: OptionsBacktestSetup, market: TableMarket, *, cash: str = "10000", fee_for: FeeFor = no_fee
) -> SessionOutcome:
    return replay_session(written, market, cash_usd=D(cash), fee_for=fee_for)


def fee_per_contract(dollars: str) -> FeeFor:
    """A fee of ``dollars`` for each contract of each fill."""
    return lambda fill: D(dollars) * fill.contracts


def row(trade: LegTrade) -> tuple[Any, ...]:
    """A trade as an owner reads it, with every time as minutes after the open:

    ``(leg, entry number, entry minute, price seen, price filled, exit reason, exit minute, price seen, price filled)``.
    A leg marked, not sold, shows the minute of its mark and the mark as both prices.
    """
    leaving = trade.exit
    exit_seen = leaving.seen if isinstance(leaving, Fill) else leaving.price
    return (
        trade.leg_id,
        trade.entry_number,
        minute_of(trade.entry.bar_close_ms),
        trade.entry.seen,
        trade.entry.filled,
        trade.exit_reason.value,
        minute_of(leaving.bar_close_ms),
        exit_seen,
        trade.exit_price,
    )


def kinds(outcome: SessionOutcome, leg_id: str | None = None) -> list[EventKind]:
    """What happened, in order, to one leg or to every leg."""
    return [event.kind for event in outcome.events if leg_id is None or event.leg_id == leg_id]


def minutes(outcome: SessionOutcome, kind: EventKind, leg_id: str | None = None) -> list[int]:
    """The minutes after the open at which one kind of thing happened, to one leg or to every leg."""
    return [
        minute_of(event.at_ms)
        for event in outcome.events
        if event.kind is kind and (leg_id is None or event.leg_id == leg_id)
    ]
