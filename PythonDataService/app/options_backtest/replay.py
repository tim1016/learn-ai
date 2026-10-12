"""Replay one trading session of an options backtest setup (#3030).

Formula: cash change = Σ exit proceeds − Σ entry costs, each net of its fee. A leg left
    open at the session's close is marked, not sold: it has paid out and not been paid back.
Reference: the owner's rules in #3030 (the sixteen behaviours). The arithmetic is
  ``fills.py``'s; this file decides what happens when.
Canonical implementation: this file, with ``fills.py`` for the prices and the money.
Validated against: tests/options_backtest/test_replay_*.py, whose expectations are worked
  by hand from the rules and not taken from this code's output.

Setup and one session's market data in; every decision, fill and trade out. It reads no
network, clock, database or randomness, and the same inputs give an equal outcome.

A minute bar is named by its close. A rule is read on a bar's close, and an order it decides
at ``t`` fills at the open of the bar closing at ``t + 60 s``. Inside one minute close ``t``
the order is always:

1. fills that are due (entries, then closes);
2. the exit time, or else the stops and their square-off;
3. entries, unless an exit was decided at ``t``.

A leg is in exactly one phase at a time: done for the day, waiting on momentum, ordered (its
entry fills in the next minute), held (a position, possibly with a close pending), or earned
(its position is closed and it has earned a re-entry it has not yet started).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum, auto
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.market import MinuteBar, PickRefused, SessionMarket
from app.options_backtest.results import (
    CASH_SHORT,
    CancelReason,
    Event,
    EventKind,
    ExitReason,
    Fill,
    LegTrade,
    Mark,
    SessionOutcome,
    SessionStatus,
    SkipReason,
)
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup, OptionsBacktestSetup

# The fee, in dollars, for one fill. The caller owns the policy.
FeeFor = Callable[[Fill], Decimal]


class _Phase(Enum):
    DONE = auto()
    WAITING = auto()
    ORDERED = auto()
    HELD = auto()
    EARNED = auto()


@dataclass(frozen=True)
class _Attempt:
    """One contract a leg is trying to buy."""

    contract: OptionContract
    multiplier: int
    starting_price: Decimal | None
    momentum_level: Decimal | None
    # Decided with no momentum wait. A dropped one ends the attempt; a momentum one goes back to waiting.
    at_once: bool


@dataclass(frozen=True)
class _CloseOrder:
    reason: ExitReason
    group: int
    decided_at_ms: int
    earns_reentry: bool


@dataclass
class _Position:
    attempt: _Attempt
    entry_number: int
    entry: Fill
    entry_fee: Decimal
    entry_decided_at_ms: int
    stop_level: Decimal | None
    # The close of the last bar of this contract seen while held; a mark if the leg is never sold.
    last_seen: Mark
    close: _CloseOrder | None = None


@dataclass(frozen=True)
class _Taken:
    """An entry order that found a bar this minute: the fill it would be, and its fee."""

    bar: MinuteBar
    fill: Fill
    fee: Decimal

    @property
    def cost(self) -> Decimal:
        return fills.entry_cost(
            self.fill.filled, multiplier=self.fill.multiplier, contracts=self.fill.contracts, fee=self.fee
        )


@dataclass
class _Leg:
    setup: LegSetup
    reentries_left: int
    phase: _Phase = _Phase.DONE
    attempt: _Attempt | None = None
    order_decided_at_ms: int = 0
    position: _Position | None = None
    entries_filled: int = 0


def replay_session(
    setup: OptionsBacktestSetup, market: SessionMarket, *, cash_usd: Decimal, fee_for: FeeFor
) -> SessionOutcome:
    """Replay ``setup`` over one session's ``market``, starting with ``cash_usd``.

    ``fee_for`` gives the fee in dollars for each fill, entries and exits alike. Raises
    ``ValueError`` for a session whose open or close is not on a whole minute: no bar could
    close at its exit time.
    """
    if market.session_open_ms % MS_PER_MINUTE or market.session_close_ms % MS_PER_MINUTE:
        raise ValueError("a session opens and closes on a whole minute: bars are named by the minute they close")
    with localcontext(fills.EXACT):
        return _Session(setup, market, fee_for, cash_usd).run()


def _skipped(reason: SkipReason) -> SessionOutcome:
    return SessionOutcome(SessionStatus.SKIPPED, reason, (), (), Decimal(0))


class _Session:
    """The state of one session's replay. Built inside ``replay_session`` and gone when it returns."""

    def __init__(self, setup: OptionsBacktestSetup, market: SessionMarket, fee_for: FeeFor, cash_usd: Decimal) -> None:
        self.setup = setup
        self.market = market
        self.fee_for = fee_for
        self.start_cash = self.cash = cash_usd
        self.open_ms, self.close_ms = market.session_open_ms, market.session_close_ms
        self.entry_ms = setup.entry.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        self.exit_ms = setup.exit.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        self.cutoff_ms = (
            None
            if setup.no_reentry_after is None
            else setup.no_reentry_after.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        )
        self.legs = [
            _Leg(leg, leg.reentry_on_stop.count if leg.reentry_on_stop is not None else 0) for leg in setup.legs
        ]
        self.events: list[Event] = []
        self.trades: list[LegTrade] = []
        self.groups = 0

    def run(self) -> SessionOutcome:
        if not self.open_ms + MS_PER_MINUTE <= self.entry_ms < self.exit_ms <= self.close_ms - MS_PER_MINUTE:
            return _skipped(SkipReason.WINDOW_DOES_NOT_FIT)
        underlying = self.market.underlying_close(self.entry_ms)
        if underlying is None:
            return _skipped(SkipReason.UNDERLYING_DID_NOT_TRADE)
        for leg in self.legs:
            self._begin(leg, self.entry_ms, underlying, wait=leg.setup.momentum is not None)
        for t in range(self.entry_ms + MS_PER_MINUTE, self.close_ms + MS_PER_MINUTE, MS_PER_MINUTE):
            self._minute(t)
        self._mark_what_is_held()
        return self._outcome()

    # --- one minute -------------------------------------------------------------------------------

    def _minute(self, t: int) -> None:
        self._fill_entries(t)
        self._fill_closes(t)
        bars = self._read_held_bars(t)
        if t == self.exit_ms:
            self._exit_time(t)
        elif t < self.exit_ms and not self._stops(t, bars):
            self._enter(t)

    # --- fills that are due -----------------------------------------------------------------------

    def _fill_entries(self, t: int) -> None:
        """Fill the entries decided a minute ago, or drop those whose contract did not trade.

        The cost of every entry that found a bar is added up and held to the cash on hand: if it is more,
        all of them are refused, not only the last. A fee is asked for each fill, refused ones included,
        to know what it would cost; only a taken fill is charged.
        """
        ordered = [leg for leg in self.legs if leg.phase is _Phase.ORDERED]
        taken: dict[str, _Taken] = {}
        for leg in ordered:
            attempt = _attempt_of(leg)
            bar = self.market.option_bar(attempt.contract, t)
            if bar is not None:
                fill = self._fill(leg, attempt.contract, attempt.multiplier, "buy", t, bar)
                taken[leg.setup.leg_id] = _Taken(bar, fill, self.fee_for(fill))
        refused = sum((entry.cost for entry in taken.values()), start=Decimal(0)) > self.cash
        for leg in ordered:
            entry = taken.get(leg.setup.leg_id)
            if entry is None:
                self._drop_entry(leg, t)
            elif refused:
                self._event(EventKind.ENTRY_REFUSED_FOR_CASH, t, leg, price=entry.fill.filled, reason=CASH_SHORT)
                leg.phase = _Phase.DONE
            else:
                self._open(leg, t, entry)

    def _drop_entry(self, leg: _Leg, t: int) -> None:
        self._event(EventKind.ENTRY_DROPPED, t, leg)
        if _attempt_of(leg).at_once:
            leg.phase = _Phase.DONE
            self._lose_reentry(leg, t)
        else:
            leg.phase = _Phase.WAITING

    def _open(self, leg: _Leg, t: int, taken: _Taken) -> None:
        attempt = _attempt_of(leg)
        fill, fee = taken.fill, taken.fee
        rule = leg.setup.stop_loss
        leg.position = _Position(
            attempt=attempt,
            entry_number=leg.entries_filled,
            entry=fill,
            entry_fee=fee,
            entry_decided_at_ms=leg.order_decided_at_ms,
            stop_level=None if rule is None else fills.stop_level(rule, bought=fill.filled),
            last_seen=Mark(t, taken.bar.close),
        )
        if leg.entries_filled > 0:
            leg.reentries_left -= 1
        leg.entries_filled += 1
        leg.phase = _Phase.HELD
        self.cash -= taken.cost
        self._event(EventKind.ENTRY_FILLED, t, leg, contract=fill.contract, price=fill.filled)

    def _fill_closes(self, t: int) -> None:
        for leg in self.legs:
            if leg.phase is not _Phase.HELD:
                continue
            position = _position_of(leg)
            bar = self.market.option_bar(position.attempt.contract, t)
            if position.close is None or bar is None:
                continue
            fill = self._fill(leg, position.attempt.contract, position.attempt.multiplier, "sell", t, bar)
            fee = self.fee_for(fill)
            self.cash += fills.exit_proceeds(fill.filled, multiplier=fill.multiplier, contracts=fill.contracts, fee=fee)
            self._record(position, position.close, position.close.reason, fill, fee)
            self._event(EventKind.CLOSE_FILLED, t, leg, contract=fill.contract, price=fill.filled)
            leg.phase = _Phase.EARNED if position.close.earns_reentry else _Phase.DONE
            leg.position = None

    def _fill(
        self, leg: _Leg, contract: OptionContract, multiplier: int, side: Literal["buy", "sell"], t: int, bar: MinuteBar
    ) -> Fill:
        price = fills.buy_fill_price if side == "buy" else fills.sell_fill_price
        return Fill(
            leg_id=leg.setup.leg_id,
            contract=contract,
            side=side,
            contracts=leg.setup.contracts,
            multiplier=multiplier,
            bar_close_ms=t,
            seen=bar.open,
            filled=price(
                bar.open, slippage_percent=self.setup.slippage_percent, price_step_usd=self.setup.price_step_usd
            ),
        )

    def _read_held_bars(self, t: int) -> dict[str, MinuteBar | None]:
        """Every held leg's bar closing at ``t``, read once: it is the last close seen and the stop's price."""
        bars: dict[str, MinuteBar | None] = {}
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                bar = self.market.option_bar(position.attempt.contract, t)
                bars[leg.setup.leg_id] = bar
                if bar is not None:
                    position.last_seen = Mark(t, bar.close)
        return bars

    # --- the exit time and the stops --------------------------------------------------------------

    def _exit_time(self, t: int) -> None:
        group: int | None = None
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                if position.close is None:
                    group = self._next_group() if group is None else group
                    position.close = _CloseOrder(ExitReason.EXIT_TIME, group, t, earns_reentry=False)
                    self._event(EventKind.EXIT_TIME, t, leg, contract=position.attempt.contract)
                elif position.close.earns_reentry:
                    # Its sale is still to fill; the re-entry it earned never starts.
                    self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.EXIT_TIME)
            elif leg.phase in (_Phase.WAITING, _Phase.EARNED):
                leg.phase = _Phase.DONE
                self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.EXIT_TIME)

    def _stops(self, t: int, bars: dict[str, MinuteBar | None]) -> bool:
        """Decide the closes a stop calls for at ``t``. True when an exit was decided.

        Every leg whose close at ``t`` is at or below its stop is one exit group. Under ``partial`` only
        those legs are closed; under ``complete`` every open leg is, and every leg still waiting is cancelled.
        """
        hit = self._legs_at_their_stop(bars)
        if not hit:
            return False
        group = self._next_group()
        complete = self.setup.square_off == "complete"
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                if position.close is not None:
                    continue
                if (bar := hit.get(leg.setup.leg_id)) is not None:
                    reason = ExitReason.STOP
                    self._event(EventKind.STOP_HIT, t, leg, contract=position.attempt.contract, price=bar.close)
                elif complete:
                    reason = ExitReason.SQUARE_OFF
                    self._event(EventKind.SQUARED_OFF, t, leg, contract=position.attempt.contract)
                else:
                    continue
                position.close = _CloseOrder(reason, group, t, earns_reentry=self._earns_reentry(leg, t))
            elif complete and leg.phase is _Phase.WAITING:
                leg.phase = _Phase.DONE
                self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.SQUARE_OFF)
        return True

    def _legs_at_their_stop(self, bars: dict[str, MinuteBar | None]) -> dict[str, MinuteBar]:
        """The bar of each open leg with a stop and no close pending whose close is at or below the stop."""
        hit: dict[str, MinuteBar] = {}
        for leg in self.legs:
            if leg.phase is not _Phase.HELD:
                continue
            position = _position_of(leg)
            bar = bars.get(leg.setup.leg_id)
            if (
                position.close is None
                and position.stop_level is not None
                and bar is not None
                and fills.stop_hit(position.stop_level, close=bar.close)
            ):
                hit[leg.setup.leg_id] = bar
        return hit

    def _earns_reentry(self, leg: _Leg, t: int) -> bool:
        """Whether a leg closed at ``t`` earns a re-entry. A stop after the cutoff earns none."""
        if leg.setup.reentry_on_stop is None or leg.reentries_left <= 0:
            return False
        if self.cutoff_ms is not None and t > self.cutoff_ms:
            self._event(EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, t, leg)
            return False
        self._event(EventKind.RE_ENTRY_EARNED, t, leg)
        return True

    # --- entries ----------------------------------------------------------------------------------

    def _enter(self, t: int) -> None:
        if self.setup.square_off == "complete" and self._a_close_is_pending():
            return
        for leg in self.legs:
            if leg.phase is _Phase.EARNED:
                self._reenter(leg, t)
            elif leg.phase is _Phase.WAITING:
                self._check_momentum(leg, t)

    def _reenter(self, leg: _Leg, t: int) -> None:
        """Start a re-entry the leg has earned, once its close has filled. With no underlying price it waits a minute."""
        underlying = self.market.underlying_close(t)
        rule = leg.setup.reentry_on_stop
        assert rule is not None
        if underlying is not None:
            self._begin(leg, t, underlying, wait=rule.kind == "on_momentum")

    def _a_close_is_pending(self) -> bool:
        return any(leg.phase is _Phase.HELD and _position_of(leg).close is not None for leg in self.legs)

    def _begin(self, leg: _Leg, t: int, underlying: Decimal, *, wait: bool) -> None:
        """Pick a contract for the leg's next attempt at ``t``; then buy at once or start waiting for momentum."""
        picked = self.market.pick(leg.setup, at_ms=t, underlying_price=underlying)
        if isinstance(picked, PickRefused):
            self._event(EventKind.PICK_REFUSED, t, leg, reason=picked.reason.value, detail=picked.detail)
            leg.phase = _Phase.DONE
            self._lose_reentry(leg, t)
            return
        self._event(EventKind.PICKED, t, leg, contract=picked.contract)
        if not wait:
            leg.attempt = _Attempt(picked.contract, picked.multiplier, None, None, at_once=True)
            leg.phase, leg.order_decided_at_ms = _Phase.ORDERED, t
            return
        bar = self.market.option_bar(picked.contract, t)
        if bar is None:
            self._event(EventKind.NO_STARTING_PRICE, t, leg, contract=picked.contract)
            leg.phase = _Phase.DONE
            self._lose_reentry(leg, t)
            return
        momentum = leg.setup.momentum
        assert momentum is not None
        level = fills.momentum_level(momentum, starting_price=bar.close)
        leg.attempt = _Attempt(picked.contract, picked.multiplier, bar.close, level, at_once=False)
        leg.phase = _Phase.WAITING
        self._event(EventKind.STARTING_PRICE_TAKEN, t, leg, contract=picked.contract, price=bar.close)

    def _check_momentum(self, leg: _Leg, t: int) -> None:
        attempt = _attempt_of(leg)
        momentum = leg.setup.momentum
        assert momentum is not None and attempt.momentum_level is not None
        bar = self.market.option_bar(attempt.contract, t)
        if bar is not None and fills.momentum_reached(momentum, level=attempt.momentum_level, close=bar.close):
            self._event(EventKind.MOMENTUM_HIT, t, leg, contract=attempt.contract, price=bar.close)
            leg.phase, leg.order_decided_at_ms = _Phase.ORDERED, t

    def _lose_reentry(self, leg: _Leg, t: int) -> None:
        """A re-entry that ended without entering, for want of market data. A first entry is not one."""
        if leg.entries_filled > 0:
            self._event(EventKind.RE_ENTRY_LOST, t, leg)

    # --- the end ----------------------------------------------------------------------------------

    def _mark_what_is_held(self) -> None:
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                # The exit time decided a close for every leg still held, so there is one to name.
                assert position.close is not None
                self._record(position, position.close, ExitReason.MARKED_NOT_SOLD, position.last_seen, Decimal(0))
                self._event(
                    EventKind.MARKED_NOT_SOLD,
                    self.close_ms,
                    leg,
                    contract=position.attempt.contract,
                    price=position.last_seen.price,
                )

    def _outcome(self) -> SessionOutcome:
        order = {leg.setup.leg_id: index for index, leg in enumerate(self.legs)}
        trades = tuple(sorted(self.trades, key=lambda trade: (trade.entry.bar_close_ms, order[trade.leg_id])))
        if any(trade.exit_reason is ExitReason.MARKED_NOT_SOLD for trade in trades):
            status = SessionStatus.INCOMPLETE
        else:
            status = SessionStatus.TRADED if trades else SessionStatus.NO_TRADE
        return SessionOutcome(status, None, trades, tuple(self.events), self.cash - self.start_cash)

    # --- records ----------------------------------------------------------------------------------

    def _next_group(self) -> int:
        self.groups += 1
        return self.groups

    def _event(
        self,
        kind: EventKind,
        at_ms: int,
        leg: _Leg,
        *,
        contract: OptionContract | None = None,
        price: Decimal | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        number = leg.position.entry_number if leg.position is not None else leg.entries_filled
        self.events.append(Event(kind, at_ms, leg.setup.leg_id, number, contract, price, reason, detail))

    def _record(
        self, position: _Position, close: _CloseOrder, reason: ExitReason, leaving: Fill | Mark, exit_fee: Decimal
    ) -> None:
        entry, attempt = position.entry, position.attempt
        sold = leaving.filled if isinstance(leaving, Fill) else leaving.price
        self.trades.append(
            LegTrade(
                leg_id=entry.leg_id,
                entry_number=position.entry_number,
                contract=entry.contract,
                contracts=entry.contracts,
                multiplier=entry.multiplier,
                starting_price=attempt.starting_price,
                momentum_level=attempt.momentum_level,
                stop_level=position.stop_level,
                entry_decided_at_ms=position.entry_decided_at_ms,
                entry=entry,
                exit_reason=reason,
                exit_group=close.group,
                exit_decided_at_ms=close.decided_at_ms,
                exit=leaving,
                entry_fee_usd=position.entry_fee,
                exit_fee_usd=exit_fee,
                pnl_usd=fills.trade_pnl(
                    bought=entry.filled,
                    sold=sold,
                    multiplier=entry.multiplier,
                    contracts=entry.contracts,
                    entry_fee=position.entry_fee,
                    exit_fee=exit_fee,
                ),
            )
        )


def _attempt_of(leg: _Leg) -> _Attempt:
    assert leg.attempt is not None
    return leg.attempt


def _position_of(leg: _Leg) -> _Position:
    assert leg.position is not None
    return leg.position
