"""Canonical clock, premium, stop and re-entry rules for Options bots and replay.

Formula: stop and momentum levels are ``options_backtest.fills``'s; re-entry counts
    decrease on the first confirmed positive fill of an attempt, never its order.
Reference: the owner's option-leg rules in #3030 and #3095, including #3107's
    protection policy and cutoff measured at the stop decision.
Canonical implementation: this file for session decisions and execution-driven
    leg transitions; ``app/options_backtest/fills.py`` for thresholds and
    ``app/options_backtest/cover_book.py`` for protection and cover.
Validated against: tests/options_bots/test_session.py and the existing hand-worked
    tests/options_backtest/test_replay_*.py and vendor golden replay fixtures.

No order is filled here. Historical execution supplies modeled outcomes; a
running bot supplies only Clerk-observed cumulative outcomes. A read-only market
projection contains completed option TRADES bars, not bid/ask midpoints. All
instants are int64 ms UTC. Session reset is owned by the runner after settled
custody, never by this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.cover_book import Held, Holding, leaving_plan
from app.options_backtest.market import MinuteBar, Picked, PickRefused, SessionMarket
from app.options_backtest.results import (
    ENTRY_REFUSAL_EVENTS,
    CancelReason,
    EntryRefusal,
    Event,
    EventKind,
    ExitReason,
    Fill,
    Mark,
    SkipReason,
)
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup, OptionsBacktestSetup, StopLoss


class Phase(StrEnum):
    DONE = "done"
    WAITING = "waiting"
    ORDERED = "ordered"
    HELD = "held"
    EARNED = "earned"


class CustodyReason(StrEnum):
    CLERK_REDUCTION = "clerk_reduction"


@dataclass(frozen=True)
class Attempt:
    contract: OptionContract
    multiplier: int
    starting_price: Decimal | None
    momentum_level: Decimal | None
    at_once: bool


@dataclass(frozen=True)
class CloseOrder:
    reason: ExitReason
    group: int
    decided_at_ms: int
    earns_reentry: bool


@dataclass
class Position:
    attempt: Attempt
    entry_number: int
    entry: Fill
    entry_fee: Decimal
    entry_decided_at_ms: int
    stop_level: Decimal | None
    last_seen: Mark
    close: CloseOrder | None = None
    entry_terminal: bool = True
    closed_contracts: int = 0
    externally_reduced: bool = False

    @property
    def contracts_held(self) -> int:
        return self.entry.contracts - self.closed_contracts


@dataclass
class Leg:
    setup: LegSetup
    reentries_left: int
    phase: Phase = Phase.DONE
    attempt: Attempt | None = None
    order_decided_at_ms: int = 0
    position: Position | None = None
    entries_filled: int = 0
    entry_cancel_requested: bool = False
    close_on_fill: CloseOrder | None = None


class SessionState(BaseModel):
    """Durable session state; restore the exact setup/session, never a fresh day's counters."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    setup: OptionsBacktestSetup
    session_open_ms: int
    session_close_ms: int
    legs: list[Leg]
    groups: int = 0
    started: bool = False
    exit_requested: bool = False
    skipped: SkipReason | None = None
    last_minute_ms: int | None = None
    observed_outcomes: dict[str, str] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)

    @model_validator(mode="after")
    def _state_describes_its_setup(self) -> SessionState:
        if self.session_close_ms <= self.session_open_ms or any(
            instant % MS_PER_MINUTE for instant in (self.session_open_ms, self.session_close_ms)
        ):
            raise ValueError("restored session needs its ordered whole-minute calendar instants")
        if [leg.setup for leg in self.legs] != list(self.setup.legs):
            raise ValueError("restored legs must match the exact setup in listed order")
        if self.groups < 0 or (
            self.last_minute_ms is not None
            and (
                self.last_minute_ms % MS_PER_MINUTE
                or not self.session_open_ms < self.last_minute_ms <= self.session_close_ms
            )
        ):
            raise ValueError("restored session progress is outside its calendar session")
        for leg in self.legs:
            limit = leg.setup.reentry_on_stop.count if leg.setup.reentry_on_stop is not None else 0
            if not 0 <= leg.entries_filled <= limit + 1 or leg.reentries_left != limit - max(0, leg.entries_filled - 1):
                raise ValueError("restored re-entry count must follow confirmed opening attempts")
            if (leg.phase is Phase.HELD) != (leg.position is not None):
                raise ValueError("a held phase needs its actual position, and only a held phase has one")
            if leg.phase in (Phase.HELD, Phase.WAITING, Phase.ORDERED) and leg.attempt is None:
                raise ValueError("a waiting, ordered or held leg needs its selected contract")
            position = leg.position
            if position is not None and (
                position.entry_number != leg.entries_filled - 1
                or position.attempt != leg.attempt
                or position.entry.leg_id != leg.setup.leg_id
                or position.entry.contract != position.attempt.contract
                or position.entry.side != leg.setup.side
                or not 0 <= position.closed_contracts <= position.entry.contracts <= leg.setup.contracts
                or position.entry.contracts == 0
            ):
                raise ValueError("restored position must describe its selected attempt and confirmed quantity")
        return self


@dataclass(frozen=True)
class OpenLeg:
    leg_id: str
    entry_number: int
    contract: OptionContract
    contracts: int
    multiplier: int
    decided_at_ms: int
    side: Literal["buy", "sell"]


@dataclass(frozen=True)
class CloseLeg:
    leg_id: str
    entry_number: int
    contract: OptionContract
    contracts: int
    multiplier: int
    decided_at_ms: int
    reason: ExitReason
    group: int


@dataclass(frozen=True)
class CancelEntry:
    leg_id: str
    entry_number: int
    decided_at_ms: int
    entry_decided_at_ms: int


OptionsDecision = OpenLeg | CloseLeg | CancelEntry
DECISION_ADAPTERS = {"open": TypeAdapter(OpenLeg), "close": TypeAdapter(CloseLeg), "cancel": TypeAdapter(CancelEntry)}


@dataclass(frozen=True)
class ExecutionOutcome:
    """An observed attempt update, cumulative within its entry or close order.

    ``fill`` contains the cumulative quantity and observed average price, not a
    delta. ``terminal`` proves the opening order cannot fill again. A close
    outcome's cumulative contracts count only this attempt's closing executions.
    A nonterminal outcome with no fill means unknown/resting, never flat.
    ``decided_at_ms`` identifies the originating entry decision, including a
    later momentum retry that has the same unconsumed entry number.
    """

    outcome_id: str
    leg_id: str
    entry_number: int
    kind: Literal["entry", "close"]
    fill: Fill | None = None
    terminal: bool = False
    fee_usd: Decimal = Decimal(0)
    mark: Mark | None = None
    refusal: EntryRefusal | None = None
    decided_at_ms: int | None = None


_OUTCOME = TypeAdapter(ExecutionOutcome)


@dataclass(frozen=True)
class OptionsStep:
    decisions: tuple[OptionsDecision, ...]
    events: tuple[Event, ...]


class OptionsSession:
    """One session's shared rules over an admitted, synchronous market projection.

    ``advance`` consumes observations before reading that minute's stops, and
    never sends an order. Persist ``state`` and its returned decisions together
    before delivering them. Exact outcome redelivery is harmless; a reused id
    with different facts and an out-of-order minute fail explicitly.
    """

    def __init__(
        self, setup: OptionsBacktestSetup, market: SessionMarket, *, state: SessionState | None = None
    ) -> None:
        self.setup, self.market = setup, market
        self.open_ms, self.close_ms = market.session_open_ms, market.session_close_ms
        if self.open_ms % MS_PER_MINUTE or self.close_ms % MS_PER_MINUTE:
            raise ValueError("a session opens and closes on a whole minute")
        if state is not None and (
            state.setup != setup
            or state.session_open_ms != self.open_ms
            or state.session_close_ms != self.close_ms
            or [leg.setup for leg in state.legs] != list(setup.legs)
        ):
            raise ValueError("restored options session must name the exact setup, legs and calendar session")
        self.state = state or SessionState(
            setup=setup,
            session_open_ms=self.open_ms,
            session_close_ms=self.close_ms,
            legs=[Leg(leg, leg.reentry_on_stop.count if leg.reentry_on_stop is not None else 0) for leg in setup.legs],
        )
        self.legs, self.events = self.state.legs, self.state.events
        self.entry_ms = setup.entry.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        self.exit_ms = setup.exit.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        self.cutoff_ms = (
            None
            if setup.no_reentry_after is None
            else setup.no_reentry_after.at(session_open_ms=self.open_ms, session_close_ms=self.close_ms)
        )
        self.sells = setup.sells
        if self.state.exit_requested:
            for leg in self.legs:
                if leg.phase is Phase.ORDERED and leg.close_on_fill is None:
                    # Repair a pre-intent snapshot from the already-recorded
                    # exit duty; an identical final minute will not run again.
                    decided = self.state.last_minute_ms or self.exit_ms
                    leg.close_on_fill = CloseOrder(ExitReason.EXIT_TIME, self._next_group(), decided, False)

    def advance(
        self,
        at_ms: int,
        outcomes: Sequence[ExecutionOutcome] = (),
        *,
        allow_entries: bool = True,
        observed_at_ms: int | None = None,
    ) -> OptionsStep:
        """Observe confirmed execution, then advance one completed calendar minute.

        Identical minutes may carry newer execution evidence, but their market
        decisions are never replayed. A fresh session first advanced after its
        entry minute does not catch up an entry; the runner schedules the next
        session. Resumed sessions retain ``started`` and their selected legs.
        """
        if at_ms % MS_PER_MINUTE or not self.open_ms < at_ms <= self.close_ms:
            raise ValueError("options rules advance on completed minutes of their calendar session")
        if self.state.last_minute_ms is not None and at_ms < self.state.last_minute_ms:
            raise ValueError("options session minutes must not move backwards")
        event_start = len(self.events)
        before = self._pending_decisions()
        for outcome in outcomes:
            self._observe_execution(outcome, at_ms if observed_at_ms is None else observed_at_ms)
        if at_ms == self.state.last_minute_ms:
            return OptionsStep(
                self._new_decisions(before, at_ms, allow_entries=allow_entries),
                tuple(self.events[event_start:]),
            )
        with localcontext(fills.EXACT):
            if not self.state.started:
                if at_ms < self.entry_ms:
                    self.state.last_minute_ms = at_ms
                    return OptionsStep((), tuple(self.events[event_start:]))
                self._start(at_ms, allow_entries=allow_entries)
            elif self.state.skipped is None:
                bars = {} if at_ms >= self.exit_ms else self._read_held_bars(at_ms)
                self._advance_rules(at_ms, bars, allow_entries=allow_entries)
        self.state.last_minute_ms = at_ms
        return OptionsStep(
            self._new_decisions(before, at_ms, allow_entries=allow_entries), tuple(self.events[event_start:])
        )

    def _start(self, at_ms: int, *, allow_entries: bool = True) -> None:
        self.state.started = True
        if not self.setup.window_fits(session_open_ms=self.open_ms, session_close_ms=self.close_ms):
            self.state.skipped = SkipReason.WINDOW_DOES_NOT_FIT
            return
        if at_ms != self.entry_ms or not allow_entries:
            # A missed initial entry belongs to the next session, not a late order.
            return
        underlying = self._underlying_close(at_ms)
        if underlying is None:
            self.state.skipped = SkipReason.UNDERLYING_DID_NOT_TRADE
            return
        for leg in self.legs:
            self._begin(leg, at_ms, underlying, wait=leg.setup.momentum is not None)

    def _advance_rules(self, t: int, bars: dict[str, MinuteBar | None], *, allow_entries: bool = True) -> None:
        if t >= self.exit_ms:
            self._exit_time(t)
            self.state.exit_requested = True
        elif not self._stops(t, bars) and allow_entries:
            self._enter(t)

    def _pending_decisions(self) -> set[tuple[str, str, int, int]]:
        result: set[tuple[str, str, int, int]] = set()
        for leg in self.legs:
            if leg.phase is Phase.ORDERED:
                result.add(("entry", leg.setup.leg_id, leg.entries_filled, leg.order_decided_at_ms))
            if leg.position is not None and leg.position.close is not None:
                close = leg.position.close
                result.add(("close", leg.setup.leg_id, leg.position.entry_number, close.decided_at_ms))
        return result

    def _new_decisions(
        self, before: set[tuple[str, str, int, int]], at_ms: int, *, allow_entries: bool = True
    ) -> tuple[OptionsDecision, ...]:
        decisions: list[OptionsDecision] = []
        for leg in self.legs:
            if (
                leg.phase is Phase.ORDERED
                and ("entry", leg.setup.leg_id, leg.entries_filled, leg.order_decided_at_ms) not in before
            ):
                attempt = _attempt_of(leg)
                decisions.append(
                    OpenLeg(
                        leg.setup.leg_id,
                        leg.entries_filled,
                        attempt.contract,
                        leg.setup.contracts,
                        attempt.multiplier,
                        leg.order_decided_at_ms,
                        leg.setup.side,
                    )
                )
            if leg.position is not None and leg.position.close is not None:
                position, close = leg.position, leg.position.close
                if ("close", leg.setup.leg_id, position.entry_number, close.decided_at_ms) not in before:
                    decisions.append(
                        CloseLeg(
                            leg.setup.leg_id,
                            position.entry_number,
                            position.attempt.contract,
                            position.contracts_held,
                            position.entry.multiplier,
                            close.decided_at_ms,
                            close.reason,
                            close.group,
                        )
                    )
            pending = leg.phase is Phase.ORDERED or (leg.position is not None and not leg.position.entry_terminal)
            if (
                pending
                and not leg.entry_cancel_requested
                and (
                    at_ms >= self.exit_ms
                    or leg.position is not None
                    or not allow_entries
                    or leg.close_on_fill is not None
                )
            ):
                number = leg.entries_filled if leg.position is None else leg.position.entry_number
                decisions.insert(0, CancelEntry(leg.setup.leg_id, number, at_ms, leg.order_decided_at_ms))
                leg.entry_cancel_requested = True
        sides = {leg.setup.leg_id: leg.setup.side for leg in self.legs}
        return tuple(
            sorted(
                decisions,
                key=lambda decision: (
                    0
                    if isinstance(decision, CancelEntry)
                    else 1
                    if isinstance(decision, CloseLeg) and sides[decision.leg_id] == "sell"
                    else 2
                    if isinstance(decision, CloseLeg)
                    else 3
                    if decision.side == "buy"
                    else 4
                ),
            )
        )

    def _observe_execution(self, outcome: ExecutionOutcome, at_ms: int) -> None:
        digest = sha256(_OUTCOME.dump_json(outcome)).hexdigest()
        previous = self.state.observed_outcomes.get(outcome.outcome_id)
        if previous is not None:
            if previous != digest:
                raise ValueError("an execution outcome id was reused with different facts")
            return
        leg = next((leg for leg in self.legs if leg.setup.leg_id == outcome.leg_id), None)
        if leg is None:
            raise ValueError("execution outcome names no setup leg")
        position = leg.position
        expected_number = leg.entries_filled if position is None else position.entry_number
        if outcome.entry_number != expected_number:
            raise ValueError("execution outcome names another leg attempt")
        if outcome.decided_at_ms is not None and outcome.decided_at_ms != leg.order_decided_at_ms:
            raise ValueError("execution outcome names another entry decision")
        fill = outcome.fill
        if fill is not None and (
            fill.leg_id != leg.setup.leg_id
            or fill.contract != _attempt_of(leg).contract
            or fill.multiplier != _attempt_of(leg).multiplier
            or not 0 < fill.contracts <= leg.setup.contracts
            or fill.filled < 0
            or fill.bar_close_ms > at_ms
        ):
            raise ValueError("execution fill does not match the selected leg attempt")
        if outcome.kind == "entry":
            self._observe_entry(leg, outcome, at_ms)
        else:
            self._observe_close(leg, outcome, at_ms)
        self.state.observed_outcomes[outcome.outcome_id] = digest

    def _observe_entry(self, leg: Leg, outcome: ExecutionOutcome, at_ms: int) -> None:
        fill, position = outcome.fill, leg.position
        if fill is None:
            if not outcome.terminal:
                return
            if position is not None:
                position.entry_terminal = True
                self._finish_closed(leg)
            elif outcome.refusal is not None:
                self._event(ENTRY_REFUSAL_EVENTS[outcome.refusal], at_ms, leg, reason=outcome.refusal)
                leg.phase = Phase.DONE
            else:
                self._drop_entry(leg, at_ms)
            return
        if fill.side != leg.setup.side:
            raise ValueError("entry fill side must match the setup leg")
        if position is None:
            if leg.phase is not Phase.ORDERED:
                raise ValueError("an entry fill needs its pending entry decision")
            rule = leg.setup.stop_loss
            position = Position(
                attempt=_attempt_of(leg),
                entry_number=leg.entries_filled,
                entry=fill,
                entry_fee=outcome.fee_usd,
                entry_decided_at_ms=leg.order_decided_at_ms,
                stop_level=None if rule is None else _stop_level_of(rule, fill),
                last_seen=outcome.mark or Mark(at_ms, fill.filled),
                entry_terminal=outcome.terminal,
                close=leg.close_on_fill,
            )
            if leg.entries_filled > 0:
                leg.reentries_left -= 1
            leg.entries_filled += 1
            leg.position, leg.phase = position, Phase.HELD
            self._event(
                EventKind.ENTRY_FILLED, at_ms, leg, position=position, contract=fill.contract, price=fill.filled
            )
            if position.close is not None:
                kind = EventKind.EXIT_TIME if position.close.reason is ExitReason.EXIT_TIME else EventKind.SQUARED_OFF
                self._event(kind, at_ms, leg, position=position, contract=fill.contract)
        else:
            if fill.contracts < position.entry.contracts:
                raise ValueError("cumulative opening fills cannot decrease")
            if position.entry_terminal and fill != position.entry:
                raise ValueError("a terminal opening outcome cannot gain another fill")
            position.entry, position.entry_fee = fill, outcome.fee_usd
            position.entry_terminal = outcome.terminal or position.entry_terminal
            if leg.setup.stop_loss is not None:
                position.stop_level = _stop_level_of(leg.setup.stop_loss, fill)
            if outcome.mark is not None:
                position.last_seen = outcome.mark
            self._finish_closed(leg)

    def _observe_close(self, leg: Leg, outcome: ExecutionOutcome, at_ms: int) -> None:
        if outcome.fill is None:
            return
        position = _position_of(leg)
        fill = outcome.fill
        if fill.side == leg.setup.side or not position.closed_contracts <= fill.contracts <= position.entry.contracts:
            raise ValueError("cumulative closing fill must reduce this attempt's confirmed quantity")
        added = fill.contracts - position.closed_contracts
        position.closed_contracts = fill.contracts
        if position.close is None:
            # The Clerk's mandatory closes are custody facts even when the
            # setup has not asked to exit (ADR 0076). They earn no re-entry.
            position.externally_reduced = True
            if added:
                self._event(
                    EventKind.CLOSE_FILLED,
                    at_ms,
                    leg,
                    position=position,
                    contract=fill.contract,
                    price=fill.filled,
                    reason=CustodyReason.CLERK_REDUCTION,
                    detail="The Clerk reduced this leg outside its session decisions.",
                )
        elif added and fill.contracts == position.entry.contracts:
            self._event(
                EventKind.CLOSE_FILLED, at_ms, leg, position=position, contract=fill.contract, price=fill.filled
            )
        self._finish_closed(leg)

    def _finish_closed(self, leg: Leg) -> None:
        position = _position_of(leg)
        if position.entry_terminal and position.closed_contracts == position.entry.contracts:
            earns = (
                position.close is not None
                and position.close.earns_reentry
                and not position.externally_reduced
                and not self.state.exit_requested
            )
            leg.phase = Phase.EARNED if earns else Phase.DONE
            leg.position = None

    def _drop_entry(self, leg: Leg, t: int) -> None:
        self._event(EventKind.ENTRY_DROPPED, t, leg)
        if _attempt_of(leg).at_once:
            leg.phase = Phase.DONE
            self._lose_reentry(leg, t)
        else:
            leg.phase = Phase.WAITING

    def _read_held_bars(self, t: int) -> dict[str, MinuteBar | None]:
        """Every held leg's bar closing at ``t``, read once: it prices a close that is due, is the last close
        seen, and is the bar a stop is read on."""
        bars: dict[str, MinuteBar | None] = {}
        for leg in self.legs:
            if leg.phase is Phase.HELD:
                position = _position_of(leg)
                if t < position.entry.bar_close_ms:
                    continue
                bar = self._option_bar(position.attempt.contract, t)
                bars[leg.setup.leg_id] = bar
                if bar is not None:
                    position.last_seen = Mark(t, bar.close)
        return bars

    def _exit_time(self, t: int) -> None:
        group: int | None = None
        for leg in self.legs:
            if leg.phase is Phase.HELD:
                position = _position_of(leg)
                if position.close is None:
                    group = self._next_group() if group is None else group
                    position.close = CloseOrder(ExitReason.EXIT_TIME, group, t, earns_reentry=False)
                    self._event(EventKind.EXIT_TIME, t, leg, position=position, contract=position.attempt.contract)
                elif position.close.earns_reentry and not self.state.exit_requested:
                    # Its sale is still to fill; the re-entry it earned never starts.
                    self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.EXIT_TIME)
            elif leg.phase in (Phase.WAITING, Phase.EARNED):
                leg.phase = Phase.DONE
                self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.EXIT_TIME)
            elif leg.phase is Phase.ORDERED and leg.close_on_fill is None:
                group = self._next_group() if group is None else group
                leg.close_on_fill = CloseOrder(ExitReason.EXIT_TIME, group, t, earns_reentry=False)

    def _stops(self, t: int, bars: dict[str, MinuteBar | None]) -> bool:
        """Decide the closes a stop calls for at ``t``. True when an exit was decided.

        Every leg whose close at ``t`` has reached its stop is one exit group: a bought leg at or below its stop
        level, a sold leg at or above its own. Under ``partial`` only those legs are closed, and the sold legs that
        must go with a bought leg that is leaving (see ``_close_with_protection``); under ``complete`` every open leg
        is, and every leg still waiting is cancelled.
        """
        hit = self._legs_at_their_stop(bars)
        if not hit:
            return False
        group = self._next_group()
        complete = self.setup.square_off == "complete"
        # What is held and not already being closed, before these stops are decided: the book protection is asked of.
        # A complete square-off closes everything held, so no sold leg is left for it to protect.
        standing = self._held(including_closing=False) if self.sells and not complete else None
        leaving: list[str] = []
        stopped_sold: list[str] = []
        for leg in self.legs:
            if leg.phase is Phase.HELD:
                position = _position_of(leg)
                if position.close is not None:
                    continue
                if (bar := hit.get(leg.setup.leg_id)) is not None:
                    reason = ExitReason.STOP
                    self._event(
                        EventKind.STOP_HIT,
                        t,
                        leg,
                        position=position,
                        contract=position.attempt.contract,
                        price=bar.close,
                    )
                elif complete:
                    reason = ExitReason.SQUARE_OFF
                    self._event(EventKind.SQUARED_OFF, t, leg, position=position, contract=position.attempt.contract)
                else:
                    continue
                position.close = CloseOrder(reason, group, t, earns_reentry=self._earns_reentry(leg, t))
                (stopped_sold if leg.setup.side == "sell" else leaving).append(leg.setup.leg_id)
            elif complete and leg.phase is Phase.WAITING:
                leg.phase = Phase.DONE
                self._event(EventKind.LEG_CANCELLED, t, leg, reason=CancelReason.SQUARE_OFF)
            elif complete and leg.phase is Phase.ORDERED:
                # Cancellation may race a live fill; its quantity still belongs to this square-off.
                leg.close_on_fill = CloseOrder(ExitReason.SQUARE_OFF, group, t, earns_reentry=False)
        if standing is not None and leaving:
            # A sold leg stopped in this same minute is already leaving: it is no longer what protection is asked of.
            self._close_with_protection(t, group, standing.without(stopped_sold), leaving)
        return True

    def _close_with_protection(self, t: int, group: int, standing: Held, leaving: Sequence[str]) -> None:
        """Give each sold leg that must go with the bought legs ``leaving`` a close order in their exit group.

        ``standing`` is what is held and not already being closed, the leaving legs among it. Which sold legs go, and
        in what order, is the cover book's (``leaving_plan``); the replay does not work it out. A leg already being
        closed is not in ``standing``, and neither is any sold leg stopped in this minute: removing a closing bought
        leg and leaving the sold legs that depend on it would hand the book a loss with no limit.

        The book measures its plan against what is pledged before any of the plan's sold legs is bought back, and
        the closing group measures a sale against what is pledged after the buy-backs fill. A sold leg that went can
        have been what held the requirement up, so a leaving leg may still not be allowed to leave once the plan's
        legs are gone, and its sale would then wait for the exit time. The book is therefore asked again of what
        would then be held, until it has nothing to add: the sale fills in the minute the buy-backs do.
        """
        by_id = {leg.setup.leg_id: leg for leg in self.legs}
        plan: list[str] = []
        while more := leaving_plan(standing.without(plan), leaving):
            plan.extend(more)
        for leg_id in plan:
            leg = by_id[leg_id]
            position = _position_of(leg)
            self._event(
                EventKind.CLOSED_WITH_ITS_PROTECTION, t, leg, position=position, contract=position.attempt.contract
            )
            position.close = CloseOrder(
                ExitReason.CLOSED_WITH_ITS_PROTECTION, group, t, earns_reentry=self._earns_reentry(leg, t)
            )

    def _legs_at_their_stop(self, bars: dict[str, MinuteBar | None]) -> dict[str, MinuteBar]:
        """The bar of each open leg with a stop and no close pending whose close has reached the stop."""
        hit: dict[str, MinuteBar] = {}
        for leg in self.legs:
            if leg.phase is not Phase.HELD:
                continue
            position = _position_of(leg)
            bar = bars.get(leg.setup.leg_id)
            # A bought leg whose sale the cover book holds back (``may_leave``, in ``_fill_closes``) stays HELD with its
            # close pending and a bar every minute, so ``close is None`` does not always hold here. Without it
            # ``_stops`` would report an exit, and take an exit-group number, in every minute the sale waits; that
            # skips ``_enter``, and a waiting momentum leg would be entered later (no second stop is recorded: ``_stops``
            # skips a leg that has a close pending).
            if (
                position.close is None
                and position.stop_level is not None
                and bar is not None
                and _stop_reached(position, bar.close)
            ):
                hit[leg.setup.leg_id] = bar
        return hit

    def _earns_reentry(self, leg: Leg, t: int) -> bool:
        """Whether a leg closed at ``t`` earns a re-entry. A stop after the cutoff earns none."""
        if leg.setup.reentry_on_stop is None or leg.reentries_left <= 0 or _position_of(leg).externally_reduced:
            return False
        if self.cutoff_ms is not None and t > self.cutoff_ms:
            self._event(EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF, t, leg)
            return False
        self._event(EventKind.RE_ENTRY_EARNED, t, leg)
        return True

    def _enter(self, t: int) -> None:
        if self.setup.square_off == "complete" and self._a_close_is_pending():
            return
        for leg in self.legs:
            if leg.phase is Phase.EARNED:
                self._reenter(leg, t)
            elif leg.phase is Phase.WAITING:
                self._check_momentum(leg, t)

    def _reenter(self, leg: Leg, t: int) -> None:
        """Start a re-entry the leg has earned, once its close has filled. With no underlying price it waits a minute."""
        underlying = self._underlying_close(t)
        rule = leg.setup.reentry_on_stop
        assert rule is not None
        if underlying is not None:
            self._begin(leg, t, underlying, wait=rule.kind == "on_momentum")

    def _a_close_is_pending(self) -> bool:
        return any(leg.phase is Phase.HELD and _position_of(leg).close is not None for leg in self.legs)

    def _begin(self, leg: Leg, t: int, underlying: Decimal, *, wait: bool) -> None:
        """Pick a contract for the leg's next attempt at ``t``; then buy at once or start waiting for momentum."""
        leg.entry_cancel_requested = False
        leg.close_on_fill = None
        picked = self._pick(leg.setup, t, underlying)
        if isinstance(picked, PickRefused):
            self._event(EventKind.PICK_REFUSED, t, leg, reason=picked.reason, detail=picked.detail)
            leg.phase = Phase.DONE
            self._lose_reentry(leg, t)
            return
        self._event(EventKind.PICKED, t, leg, contract=picked.contract)
        if not wait:
            leg.attempt = Attempt(picked.contract, picked.multiplier, None, None, at_once=True)
            leg.phase, leg.order_decided_at_ms = Phase.ORDERED, t
            return
        bar = self._option_bar(picked.contract, t)
        if bar is None:
            self._event(EventKind.NO_STARTING_PRICE, t, leg, contract=picked.contract)
            leg.phase = Phase.DONE
            self._lose_reentry(leg, t)
            return
        momentum = leg.setup.momentum
        assert momentum is not None
        level = fills.momentum_level(momentum, starting_price=bar.close)
        leg.attempt = Attempt(picked.contract, picked.multiplier, bar.close, level, at_once=False)
        leg.phase = Phase.WAITING
        self._event(EventKind.STARTING_PRICE_TAKEN, t, leg, contract=picked.contract, price=bar.close)

    def _check_momentum(self, leg: Leg, t: int) -> None:
        attempt = _attempt_of(leg)
        momentum = leg.setup.momentum
        assert momentum is not None and attempt.momentum_level is not None
        bar = self._option_bar(attempt.contract, t)
        if bar is not None and fills.momentum_reached(momentum, level=attempt.momentum_level, close=bar.close):
            self._event(EventKind.MOMENTUM_HIT, t, leg, contract=attempt.contract, price=bar.close)
            leg.phase, leg.order_decided_at_ms = Phase.ORDERED, t

    def _lose_reentry(self, leg: Leg, t: int) -> None:
        """A re-entry that ended without entering, for want of market data. A first entry is not one."""
        if leg.entries_filled > 0:
            self._event(EventKind.RE_ENTRY_LOST, t, leg)

    def _held(self, *, including_closing: bool = True) -> Held:
        """What the legs hold now, as the cover book reads it. Asked only of a setup that sells.

        A leg whose close is decided is still held and still pledged, so it is in the answer unless
        ``including_closing`` is false, which asks for what is held and not already being closed.
        """
        holdings: list[Holding] = []
        for leg in self.legs:
            if (
                leg.phase is Phase.HELD
                and _position_of(leg).contracts_held > 0
                and (including_closing or _position_of(leg).close is None)
            ):
                entry = _position_of(leg).entry
                holdings.append(
                    Holding(
                        leg.setup.leg_id, entry.contract, entry.side, _position_of(leg).contracts_held, entry.multiplier
                    )
                )
        return Held(tuple(leg.setup.leg_id for leg in self.legs), tuple(holdings))

    def _closing_legs(self) -> list[str]:
        """The legs whose close is decided and not yet filled: still held, still pledged."""
        return [
            leg.setup.leg_id for leg in self.legs if leg.phase is Phase.HELD and _position_of(leg).close is not None
        ]

    def _event(
        self,
        kind: EventKind,
        at_ms: int,
        leg: Leg,
        *,
        position: Position | None = None,
        contract: OptionContract | None = None,
        price: Decimal | None = None,
        reason: StrEnum | None = None,
        detail: str | None = None,
    ) -> None:
        """Record an event. One about a position names it. One about an attempt to enter does not, and carries
        the number that entry has or would have: the entries the leg has filled so far."""
        number = leg.entries_filled if position is None else position.entry_number
        why = None if reason is None else reason.value
        self.events.append(Event(kind, at_ms, leg.setup.leg_id, number, contract, price, why, detail))

    def _underlying_close(self, close_ms: int) -> Decimal | None:
        return self.market.underlying_close(close_ms)

    def _pick(self, leg: LegSetup, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        return self.market.pick(leg, at_ms=at_ms, underlying_price=underlying_price)

    def _option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        return self.market.option_bar(contract, close_ms)

    def _next_group(self) -> int:
        self.state.groups += 1
        return self.state.groups


def _stop_level_of(rule: StopLoss, fill: Fill) -> Decimal:
    if fill.side == "sell":
        return fills.sold_stop_level(rule, sold=fill.filled)
    return fills.stop_level(rule, bought=fill.filled)


def _stop_reached(position: Position, close: Decimal) -> bool:
    assert position.stop_level is not None
    if position.entry.side == "sell":
        return fills.sold_stop_hit(position.stop_level, close=close)
    return fills.stop_hit(position.stop_level, close=close)


def _attempt_of(leg: Leg) -> Attempt:
    assert leg.attempt is not None
    return leg.attempt


def _position_of(leg: Leg) -> Position:
    assert leg.position is not None
    return leg.position
