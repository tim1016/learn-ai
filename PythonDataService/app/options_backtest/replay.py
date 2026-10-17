"""Replay one trading session of an options backtest setup (#3030, and #3106 and #3107 for sold legs).

Formula: cash change = Σ cash effect of every fill, each net of its fee: a buy takes its value and fee out, a sale
    brings its value less its fee in, whichever of a leg's two orders it is. A leg left open at the session's
    close is marked, not closed: a bought leg has paid out and not been paid back, and a sold leg has been paid
    and not bought back, so it is owed.
Reference: the owner's rules in #3030 (the sixteen behaviours) and in #3095 (sold legs, covered, held with their
  collateral out of what the run may spend, stopped and closed with their protection). The arithmetic is
  ``fills.py``'s, and every question about cover, collateral, cash and which sold legs go with a leaving bought leg
  for a setup that sells is ``cover_book.py``'s; ``app/options_bots/session.py`` decides what happens when.
Canonical implementation: this file for simulated fills and historical accounting, with ``fills.py`` for the prices and the money and ``cover_book.py`` for what is
  held, what covers it, what it pledges, which fills may go together and which sold legs leave with a bought one.
  Session decisions and execution-driven leg transitions are shared with deployed Options bots in
  ``app/options_bots/session.py``; only this adapter creates historical simulated fills.
Validated against: tests/options_backtest/test_replay_*.py, whose expectations are worked
  by hand from the rules and not taken from this code's output.

Setup and one session's market data in; every decision, fill and trade out. It reads no
network, clock, database or randomness, and the same inputs give an equal outcome.

A minute bar is named by its close: the bar closing at ``t`` holds the trades in
``[t - 60 s, t)``. A rule is read on a bar's close, and an order it decides at ``t`` fills at
the open of the bar closing at ``t + 60 s``. Inside one minute close ``t`` the order is always:

1. fills that are due (entries, then closes);
2. the exit time, or else the stops and their square-off;
3. entries, unless an exit was decided at ``t``.

A leg is in exactly one phase at a time: done for the day, waiting on momentum, ordered (its
entry fills in the next minute), held (a position, possibly with a close pending), or earned
(its position is closed and it has earned a re-entry it has not yet started).

Sold legs. A leg may open by a sale and close by a buy-back. A **fill group** is the fills of one kind in one
minute: the opening group, then the closing group. Rules, for a setup that sells:

* The opening group is judged together by the cover book: other side, then second expiry, then cover, then cash,
  and each refused leg is done for the day with an event that says why. Orders whose contract has no bar are
  dropped before it, and a sale to open that would fill at nothing is not taken.
* The closing group fills buy-backs first. A bought leg's sale then fills only if the cover book says the leg may
  leave what is then held, counting a sold leg whose buy-back found no bar; otherwise it waits and is tried every
  minute. No cash is read there, and a close is never refused.
* A sold leg never bought back is marked at its last traded close as an amount owed, and a bought leg still
  waiting for it is marked, not sold. The session is incomplete.
* A sold leg's stop level is what it was sold for moved up by the rule, and a minute that closes at or above it hits
  the stop. Under a partial square-off a stop closes only the leg that carries it, so a stop on a sold leg buys back
  that leg and leaves the bought legs held.
* **Protection.** When a bought leg's close is decided by a stop, the cover book is asked for the leaving plan over
  what is held and not already being closed: the sold legs that must go with it, so that what remains is covered and
  pledges no more than it did. Each gets a close order in the same minute and exit group, with the exit reason
  ``closed_with_its_protection``, in the book's order: last listed first, the same kind and expiry before any other.
  That is a policy by listed order and is not minimal: it can close more sold legs than another policy would. The
  book is asked again of what would then be held until it has nothing to add, so that the sale is allowed in the
  minute the buy-backs fill. A bought leg that protects nothing sold is sold freely. No cash is read when it is
  decided, so nothing is re-planned when cash changes; the closing group makes the bought leg's sale wait until its
  sold legs have been bought back.
* A sold leg closed by its own stop, by a complete square-off or with its protection earns a re-entry on a bought
  leg's terms: a rule, a count left, and a close decided no later than the cutoff. A re-entry joins an opening group
  like any first entry and is refused like one, so a sold leg whose cover did not come back is refused for cover.

Minute bars cannot show which of two contracts printed first inside a minute, so judging an opening group as one
simulated atomic group, bought legs standing beside the sold legs they cover, is a modelling choice and not
observed coverage. A setup with no sold leg never consults the cover book and produces the outcome it produced
before sold legs existed, figure for figure. A loss with no limit in what is held (``CoverInvariantBroken``) is a
fault in the replay or the book and is never caught here: it is not a result.

One record of money is kept for each fill group that filled anything (``MoneyStep``): the figures that a judgment of
whether the cash could have carried a run is later made from. They are facts, not judgments.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, getcontext, localcontext
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.cover_book import (
    Candidate,
    OpeningRefusal,
    admit_opening,
    assignment_obligation,
    collateral,
    covers,
    may_leave,
)
from app.options_backtest.market import MinuteBar, Picked, PickRefused, SessionMarket
from app.options_backtest.observation import HeldMark, MinuteObservation
from app.options_backtest.results import (
    ENTRY_REFUSAL_EVENTS,
    EntryRefusal,
    EventKind,
    ExitReason,
    Fill,
    FillGroup,
    LegTrade,
    Mark,
    MoneyStep,
    SessionOutcome,
    SessionStatus,
    SkipReason,
)
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup, OptionsBacktestSetup
from app.options_bots.session import (
    CloseOrder as _CloseOrder,
)
from app.options_bots.session import (
    ExecutionOutcome,
    OptionsSession,
    _attempt_of,
    _position_of,
)
from app.options_bots.session import (
    Leg as _Leg,
)
from app.options_bots.session import (
    Phase as _Phase,
)
from app.options_bots.session import (
    Position as _Position,
)

# The fee, in dollars, for one fill. The caller owns the policy.
FeeFor = Callable[[Fill], Decimal]

# What a setup that sells nothing holds against its positions: a bought option has no requirement, only its premium.
_NOTHING_PLEDGED = Decimal(0)


def entry_refusal(refusal: OpeningRefusal, *, cover_did_not_trade: bool = False) -> EntryRefusal:
    """The word an entry's refusal is told in: the book's refusal by value, and its one for want of cover as two.

    A sold leg left without cover is told as ``COVER_DID_NOT_TRADE`` when a bought leg of its kind was dropped in the
    same minute for want of a bar, and as ``COVER_NOT_YET_HELD`` when none was held or filling. Any other refusal
    is looked up by value, so a refusal the book gains that this module has no word for raises ``ValueError``.
    """
    if refusal is OpeningRefusal.NO_COVER:
        return EntryRefusal.COVER_DID_NOT_TRADE if cover_did_not_trade else EntryRefusal.COVER_NOT_YET_HELD
    return EntryRefusal(refusal.value)


@dataclass(frozen=True)
class _Taken:
    """An entry order that found a bar this minute: the fill it would be, and its fee."""

    bar: MinuteBar
    fill: Fill
    fee: Decimal

    @property
    def cash_effect(self) -> Decimal:
        """The cash the fill would move: a buy takes its cost out, a sale to open brings its proceeds in."""
        return fills.cash_effect(
            self.fill.side,
            self.fill.filled,
            multiplier=self.fill.multiplier,
            contracts=self.fill.contracts,
            fee=self.fee,
        )


def replay_session(
    setup: OptionsBacktestSetup, market: SessionMarket, *, cash_usd: Decimal, fee_for: FeeFor,
    observer: Callable[[MinuteObservation], None] | None = None,
) -> SessionOutcome:
    """Replay ``setup`` over one session's ``market``, starting with ``cash_usd``.

    ``cash_usd`` is the cash at this session's start; the setup's own starting cash belongs to
    the run, which carries it from one session to the next. ``fee_for`` gives the fee in dollars
    for each fill, entries and exits alike; it is also asked for an entry that is then refused for
    want of cash, since the fee is part of what the entry would cost. Raises ``ValueError`` for a
    session whose open or close is not on a whole minute: no bar could close at its exit time.

    The replay's own arithmetic runs in an exact decimal context: a hundred digits, with ``Inexact``
    trapped, so a figure too long to hold (a bar price of more than about a hundred digits) raises
    ``decimal.Inexact`` and is never rounded. ``fee_for`` and the market's methods (``underlying_close``,
    ``pick`` and ``option_bar``) are the caller's code and run in the decimal context the caller had
    when it called, so a fee that rounds to the cent or a picker that rounds to a strike is ordinary use.

    An optional ``observer`` receives immutable session-open and minute-close facts, after that
    close's fills and decisions. It reads no additional bars and cannot supply decisions or marks.
    A missing contemporaneous held mark stays missing; ordinary replay settlement is unchanged.
    """
    if market.session_open_ms % MS_PER_MINUTE or market.session_close_ms % MS_PER_MINUTE:
        raise ValueError("a session opens and closes on a whole minute: bars are named by the minute they close")
    caller_context = getcontext().copy()
    with localcontext(fills.EXACT):
        return _Session(setup, market, fee_for, cash_usd, caller_context, observer).run()


def _skipped(reason: SkipReason) -> SessionOutcome:
    return SessionOutcome(SessionStatus.SKIPPED, reason, (), (), Decimal(0))


class _Session(OptionsSession):
    """The state of one session's replay. Built inside ``replay_session`` and gone when it returns."""

    def __init__(
        self,
        setup: OptionsBacktestSetup,
        market: SessionMarket,
        fee_for: FeeFor,
        cash_usd: Decimal,
        caller_context: Context,
        observer: Callable[[MinuteObservation], None] | None,
    ) -> None:
        super().__init__(setup, market)
        self.fee_for = fee_for
        self.caller_context = caller_context
        self.start_cash = self.cash = cash_usd
        self.trades: list[LegTrade] = []
        self.money_steps: list[MoneyStep] = []
        self.observer = observer

    def run(self) -> SessionOutcome:
        if not self.setup.window_fits(session_open_ms=self.open_ms, session_close_ms=self.close_ms):
            self._observe_flat_session()
            return _skipped(SkipReason.WINDOW_DOES_NOT_FIT)
        self._start(self.entry_ms)
        if self.state.skipped is not None:
            self._observe_flat_session()
            return _skipped(self.state.skipped)
        if self.observer is not None:
            for t in range(self.open_ms, self.entry_ms + MS_PER_MINUTE, MS_PER_MINUTE):
                self._observe_minute(t, {})
        for t in range(self.entry_ms + MS_PER_MINUTE, self.close_ms + MS_PER_MINUTE, MS_PER_MINUTE):
            self._minute(t)
        self._mark_what_is_held()
        return self._outcome()

    # --- one minute -------------------------------------------------------------------------------

    def _minute(self, t: int) -> None:
        self._fill_entries(t)
        bars = self._read_held_bars(t)
        self._fill_closes(t, bars)
        self._advance_rules(t, bars)
        self._observe_minute(t, bars)

    def _observe_flat_session(self) -> None:
        if self.observer is not None:
            for t in range(self.open_ms, self.close_ms + MS_PER_MINUTE, MS_PER_MINUTE):
                self._observe_minute(t, {})

    def _observe_minute(self, t: int, bars: dict[str, MinuteBar | None]) -> None:
        """Observe post-fill/post-decision holdings using bars already read by the replay."""
        if self.observer is None:
            return
        marks: list[HeldMark] = []
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                bar = bars.get(leg.setup.leg_id)
                marks.append(HeldMark(
                    leg.setup.leg_id, position.entry.contract,
                    position.entry.contracts * (-1 if position.entry.side == "sell" else 1),
                    position.entry.multiplier, None if bar is None else bar.close,
                    position.close is not None,
                ))
        observation = MinuteObservation(t, self.cash, self._money()[1], tuple(marks))
        with localcontext(self.caller_context):
            self.observer(observation)

    # --- fills that are due -----------------------------------------------------------------------

    def _fill_entries(self, t: int) -> None:
        """Fill the entries decided a minute ago as one opening group, or drop those whose contract did not trade.

        An order whose contract has no bar is dropped, and a sale to open that would fill at nothing is not taken.
        The rest are one simulated atomic group: ``_judge_opening`` says which are refused and why, each refused
        leg is done for the day, and the others fill together. A fee is asked for each fill, refused ones
        included, to know what it would cost; only a taken fill is charged.
        """
        ordered = [leg for leg in self.legs if leg.phase is _Phase.ORDERED]
        if not ordered:
            return
        found: dict[str, _Taken] = {}
        for leg in ordered:
            attempt = _attempt_of(leg)
            bar = self._option_bar(attempt.contract, t)
            if bar is not None:
                fill = self._fill(leg, attempt.contract, attempt.multiplier, leg.setup.side, t, bar)
                # A sale to open at nothing is not taken, so it is not a fill a fee is asked of.
                fee = Decimal(0) if _is_a_sale_to_open_at_nothing(fill) else self._fee(fill)
                found[leg.setup.leg_id] = _Taken(bar, fill, fee)
        refusals = self._judge_opening(ordered, found)
        before = self._money()
        filled = False
        for leg in ordered:
            entry = found.get(leg.setup.leg_id)
            if entry is None:
                self._drop_entry(leg, t)
            elif (why := refusals.get(leg.setup.leg_id)) is not None:
                self._event(ENTRY_REFUSAL_EVENTS[why], t, leg, price=entry.fill.filled, reason=why)
                leg.phase = _Phase.DONE
            else:
                self._open(leg, t, entry)
                filled = True
        if filled:
            self._record_step(t, FillGroup.OPENING, before)

    def _judge_opening(self, ordered: list[_Leg], found: dict[str, _Taken]) -> dict[str, EntryRefusal]:
        """Why each entry that found a bar is refused, by leg: a leg that is not in the answer is taken.

        A setup that sells has the cover book judge the group. A setup that does not has nothing to cover and
        nothing pledged, so the book's answer for it is the cash alone, all of the group or none: refused when what
        the group costs is more than the cash on hand. That is asked without the book so that a bought option of
        any multiplier replays as it always did.
        """
        refusals: dict[str, EntryRefusal] = {}
        standing: list[_Leg] = []
        for leg in ordered:
            entry = found.get(leg.setup.leg_id)
            if entry is None:
                continue
            if _is_a_sale_to_open_at_nothing(entry.fill):
                refusals[leg.setup.leg_id] = EntryRefusal.SALE_AT_ZERO
            else:
                standing.append(leg)
        if not self.sells:
            after = self.cash + sum((found[leg.setup.leg_id].cash_effect for leg in standing), start=Decimal(0))
            if after < 0:
                refusals.update(dict.fromkeys((leg.setup.leg_id for leg in standing), EntryRefusal.CASH_SHORT))
            return refusals
        candidates = [
            Candidate(
                leg.setup.leg_id,
                found[leg.setup.leg_id].fill.contract,
                leg.setup.side,
                found[leg.setup.leg_id].fill.contracts,
                found[leg.setup.leg_id].fill.multiplier,
                found[leg.setup.leg_id].cash_effect,
            )
            for leg in standing
        ]
        admission = admit_opening(self._held(), candidates, cash=self.cash, closing=self._closing_legs())
        # A bought leg of this group that found no bar is a cover that did not trade, whatever it was to do next.
        dropped = [
            _attempt_of(leg).contract for leg in ordered if leg.setup.leg_id not in found and leg.setup.side == "buy"
        ]
        for refused in admission.refused:
            did_not_trade = any(covers(bought, refused.candidate.contract) for bought in dropped)
            refusals[refused.candidate.leg_id] = entry_refusal(refused.reason, cover_did_not_trade=did_not_trade)
        return refusals


    def _open(self, leg: _Leg, t: int, taken: _Taken) -> None:
        self._observe_execution(ExecutionOutcome(
            outcome_id=f"entry:{leg.setup.leg_id}:{leg.order_decided_at_ms}",
            leg_id=leg.setup.leg_id, entry_number=leg.entries_filled, kind="entry",
            fill=taken.fill, terminal=True, fee_usd=taken.fee, mark=Mark(t, taken.bar.close),
        ), t)
        self.cash += taken.cash_effect

    def _fill_closes(self, t: int, bars: dict[str, MinuteBar | None]) -> None:
        """Fill the closes that are due as one closing group: buy-backs first, then each bought leg's sale.

        A bought leg's sale fills only if the cover book says the leg may leave what is then held, which still
        counts a sold leg whose buy-back found no bar this minute and a bought leg whose sale is yet to be tried.
        Otherwise the sale waits, and is tried again every minute. No cash is read: a close is never refused.
        """
        due: list[tuple[_Leg, MinuteBar]] = []
        for leg in self.legs:
            bar = bars.get(leg.setup.leg_id)
            if leg.phase is _Phase.HELD and _position_of(leg).close is not None and bar is not None:
                due.append((leg, bar))
        if not due:
            return
        before = self._money()
        buy_backs = [(leg, bar) for leg, bar in due if leg.setup.side == "sell"]
        sales = [(leg, bar) for leg, bar in due if leg.setup.side == "buy"]
        bought_back = [_position_of(leg).attempt.contract for leg, _ in buy_backs]
        for leg, bar in buy_backs:
            self._close(leg, t, bar)
        held = self._held() if self.sells else None
        closed = bool(buy_backs)
        for leg, bar in sales:
            if held is not None and not may_leave(held, leg.setup.leg_id):
                continue
            self._close(leg, t, bar)
            closed = True
            if held is not None:
                held = held.without([leg.setup.leg_id])
        if closed:
            self._record_step(t, FillGroup.CLOSING, before, bought_back=bought_back)

    def _close(self, leg: _Leg, t: int, bar: MinuteBar) -> None:
        """Fill the close that is due: a sale for a bought leg and a buy-back for a sold one."""
        position = _position_of(leg)
        close = position.close
        assert close is not None
        side: Literal["buy", "sell"] = "buy" if position.entry.side == "sell" else "sell"
        fill = self._fill(leg, position.attempt.contract, position.attempt.multiplier, side, t, bar)
        fee = self._fee(fill)
        self.cash += fills.cash_effect(side, fill.filled, multiplier=fill.multiplier, contracts=fill.contracts, fee=fee)
        self._record(position, close, close.reason, fill, fee)
        self._observe_execution(ExecutionOutcome(
            outcome_id=f"close:{leg.setup.leg_id}:{close.decided_at_ms}",
            leg_id=leg.setup.leg_id, entry_number=position.entry_number, kind="close",
            fill=fill, terminal=True, fee_usd=fee,
        ), t)

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


    # --- the exit time and the stops --------------------------------------------------------------


    # --- entries ----------------------------------------------------------------------------------


    # --- the end ----------------------------------------------------------------------------------

    def _mark_what_is_held(self) -> None:
        """Mark every leg still held at its last traded close: a bought leg is not sold, a sold leg is owed."""
        for leg in self.legs:
            if leg.phase is _Phase.HELD:
                position = _position_of(leg)
                # The exit time decided a close for every leg still held, so there is one to name.
                assert position.close is not None
                owed = position.entry.side == "sell"
                self._record(
                    position,
                    position.close,
                    ExitReason.MARKED_NOT_BOUGHT_BACK if owed else ExitReason.MARKED_NOT_SOLD,
                    position.last_seen,
                    Decimal(0),
                )
                self._event(
                    EventKind.MARKED_NOT_BOUGHT_BACK if owed else EventKind.MARKED_NOT_SOLD,
                    self.close_ms,
                    leg,
                    position=position,
                    contract=position.attempt.contract,
                    price=position.last_seen.price,
                )

    def _outcome(self) -> SessionOutcome:
        order = {leg.setup.leg_id: index for index, leg in enumerate(self.legs)}
        trades = tuple(sorted(self.trades, key=lambda trade: (trade.entry.bar_close_ms, order[trade.leg_id])))
        if any(trade.marked for trade in trades):
            status = SessionStatus.INCOMPLETE
        else:
            status = SessionStatus.TRADED if trades else SessionStatus.NO_TRADE
        return SessionOutcome(
            status, None, trades, tuple(self.events), self.cash - self.start_cash, tuple(self.money_steps)
        )

    # --- what is held, and what it pledges --------------------------------------------------------


    def _money(self) -> tuple[Decimal, Decimal]:
        """The cash on hand and the collateral of what is held. A setup that sells nothing pledges nothing."""
        return self.cash, (collateral(self._held()) if self.sells else _NOTHING_PLEDGED)

    def _record_step(
        self,
        t: int,
        group: FillGroup,
        before: tuple[Decimal, Decimal],
        *,
        bought_back: Sequence[OptionContract] = (),
    ) -> None:
        """Record what a fill group that filled something did to the money.

        ``before`` is ``_money()`` as the group began, and ``bought_back`` the contracts of the sold legs it bought
        back. A buy-back made under cover is one whose kind has a bought contract still held after the group.
        """
        cash_before, collateral_before = before
        collateral_after = obligation = _NOTHING_PLEDGED
        under_cover = False
        if self.sells:
            held = self._held()
            collateral_after, obligation = collateral(held), assignment_obligation(held)
            under_cover = any(
                covers(holding.contract, back)
                for back in bought_back
                for holding in held.holdings
                if holding.side == "buy"
            )
        self.money_steps.append(
            MoneyStep(
                at_ms=t,
                group=group,
                cash_before_usd=cash_before,
                cash_after_usd=self.cash,
                collateral_before_usd=collateral_before,
                collateral_after_usd=collateral_after,
                obligation_after_usd=obligation,
                bought_back_with_cover_held=under_cover,
            )
        )

    # --- the caller's code ------------------------------------------------------------------------

    def _underlying_close(self, close_ms: int) -> Decimal | None:
        with localcontext(self.caller_context):
            return self.market.underlying_close(close_ms)

    def _pick(self, leg: LegSetup, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        with localcontext(self.caller_context):
            return self.market.pick(leg, at_ms=at_ms, underlying_price=underlying_price)

    def _option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        with localcontext(self.caller_context):
            return self.market.option_bar(contract, close_ms)

    def _fee(self, fill: Fill) -> Decimal:
        with localcontext(self.caller_context):
            return self.fee_for(fill)

    # --- records ----------------------------------------------------------------------------------


    def _record(
        self, position: _Position, close: _CloseOrder, reason: ExitReason, leaving: Fill | Mark, exit_fee: Decimal
    ) -> None:
        entry, attempt = position.entry, position.attempt
        left_at = leaving.filled if isinstance(leaving, Fill) else leaving.price
        # A sold leg was sold at its entry and is bought back, or marked, at its exit.
        bought, sold = (left_at, entry.filled) if entry.side == "sell" else (entry.filled, left_at)
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
                    bought=bought,
                    sold=sold,
                    multiplier=entry.multiplier,
                    contracts=entry.contracts,
                    entry_fee=position.entry_fee,
                    exit_fee=exit_fee,
                ),
            )
        )


def _is_a_sale_to_open_at_nothing(fill: Fill) -> bool:
    """A sale to open that would fill at nothing is not taken. A closing sale or a buy-back at nothing is."""
    return fill.side == "sell" and fill.filled == 0
