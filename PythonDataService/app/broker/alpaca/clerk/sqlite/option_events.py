"""The Clerk reads, books and holds on what ends an option position without an order of its own (ADR 0076 decision 22).

A sold option is assigned, a bought option is exercised, an option expires,
or the broker sells an expiring position itself. The broker reports the
first three as account activities, with a date and no lot, and the fourth as
an order the Clerk did not place. This module is what acts on them.

**Two functions.** :func:`poll_option_events` reads the broker, outside every
lock, and follows the read to its end. :func:`book_option_events` is the one
batch function: it takes what was read and the books as they stand, and
books each event for the bot that held the contract or holds that bot by
name. It reads no network. It runs inside the repository's write fence from
its first read to its last append, so no fill can land between a decision
and its record.

**Who calls the poll.** The deadline keeper, as one more step of its round,
and the reconciliation pass, before its final look at the broker. The poll
starts no pass and takes no account-wide gate. How often it reads is kept in
memory per store: every five minutes, every minute while a read was not
whole or a hold of this family stands, at once after a restart, and at once
(at most once a minute) when a pass sees a recorded contract's position
differ from the books.

**A stock-only account is never read.** The worklist starts at
``option_contracts``. On books that never recorded a contract that table is
empty: no other row is read, the broker is not asked, nothing is written.

**An event is taken once, by its id.** A booking's command is keyed by a
hash of the broker and the activity's id, with the bot. The same id with
other figures is a conflict and a hold. A share trade's id is never a key in
that space. When an event took lots of two bots, each bot's record carries
the whole allocation; a run that finds one record finishes the other bot
from it and never matches the event to lots a second time.

**Which lots are offered.** Every bot's open lots of the contract, read from
its own FIFO, each with the ledger sequence of the fill that opened it, and
one lot for outside orders the books recorded in that contract. A bot whose
lots and whose attributed position disagree (the owner wrote the position
off) is offered none.

**When the books count an event as having happened.** One millisecond before
the close of the last session on or before the row's date, and never before
the newest lot it takes. So an expiry falls inside its own session's P&L,
and FIFO closes the lots an event took before any fill of a later day.

**Holds.** One family of reason codes (``uncertainty_causes``), each raised
for the bot it can be tied to and account-wide otherwise. Each blocks
entries and admits reductions and the Flatten. A hold stands while a whole
read still has its cause. It ends when a whole read no longer has it, or
when the owner acknowledges it (:func:`acknowledge_option_event_hold`). A
read that failed or was not whole ends no hold and is never read as "nothing
happened".

**An order the Clerk did not place, in a contract a bot holds.** It stands
against that bot and that contract alone. While it is unsettled (working, or
filled and not yet booked) no order of the bot's own to reduce that contract
is sent: it would be a second order on the same contracts. That is asked of
the books' record of the order where every reduction is admitted
(``uncertainty.decide_capability``), not of a hold. The owner's review of
the order releases it.

**A row read after the owner wrote its position off** closes nothing: the
write-off closed no lot and the bot is offered none. An expiry's row then
confirms the write-off and holds nobody. An exercise's or an assignment's
row holds that bot by name for the shares on no bot's books.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.account_open_work import broker_quantity_by_symbol
from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.custody_subjects import BOT_SUBJECT_PREFIX
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyResolvedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import (
    CommandExistingConflict,
    ExternalOrderResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, to_thread
from app.broker.alpaca.clerk.sqlite.option_event_books import (
    EVENT_KEY_PREFIX,
    OPTION_EVENT_HOLD_ACKNOWLEDGED,
    OUTSIDE_ORDER_BROKER,
    SALE_KEY_PREFIX,
    OutsideFillUnknown,
    acknowledged_hold_items,
    booking_key,
    bookings,
    books_record_no_contract,
    held_option_positions,
    hold_item_ack_ref,
    outside_fill,
    outside_order_hold_ref,
    outside_order_reduces_a_bots_contract,
    outside_orders_in,
    unsettled_outside_order,
    written_off_contracts,
)
from app.broker.alpaca.clerk.sqlite.option_event_folds import (
    OPTION_ENDING_SUMMARY_CODES,
    OPTION_EVENT_CLOSE_EXECUTION_PREFIX,
    OPTION_EVENT_SHARES_EXECUTION_PREFIX,
    OPTION_POSITION_ENDED,
    OptionEnding,
    OptionPositionEndedFacts,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OPTION_EVENT_AFTER_A_LOT_CLOSED_REASON_CODE,
    OPTION_EVENT_CASH_NOT_WHOLE_CENTS_REASON_CODE,
    OPTION_EVENT_CONTRACT_NOT_HELD_REASON_CODE,
    OPTION_EVENT_HELD_ON_BOTH_SIDES_REASON_CODE,
    OPTION_EVENT_HOLD_REASON_CODES,
    OPTION_EVENT_MORE_CONTRACTS_THAN_HELD_REASON_CODE,
    OPTION_EVENT_NOT_EXECUTED_REASON_CODE,
    OPTION_EVENT_ROW_CHANGED_REASON_CODE,
    OPTION_EVENT_SHARES_IN_ANOTHER_SYMBOL_REASON_CODE,
    OPTION_EVENT_UNREADABLE_REASON_CODE,
    OPTION_EVENT_WRONG_SIDE_REASON_CODE,
    OPTION_EVENTS_NOT_READ_REASON_CODE,
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE,
    OPTION_EXPIRY_SIGNED_FOR_OTHER_SIDE_REASON_CODE,
    OPTION_ORDER_NOT_OURS_REASON_CODE,
    OPTION_SHARE_TRADE_DISAGREES_REASON_CODE,
    OPTION_SHARE_TRADE_MISSING_REASON_CODE,
    OPTION_SHARE_TRADE_UNPAIRED_REASON_CODE,
    OPTION_SHARES_HELD_REASON_CODE,
    OptionEventHoldCause,
    OptionEventHoldItem,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerPosition, OrderSide
from app.broker.contract.option_events import (
    BrokerOptionActivityUnreadable,
    BrokerOptionEventActivity,
    BrokerOptionEventResume,
    BrokerOptionShareTradeActivity,
    OptionActivityStatus,
    allocation_of,
    in_allocation_order,
    share_trade_disagreements,
)
from app.broker.contract.ports import BrokerReadPort
from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref
from app.options.contract import OptionContract
from app.options.deadlines import last_session_close_ms, past_last_session
from app.options.event_allocation import (
    HeldOptionLot,
    LotAllocation,
    OptionEventKind,
    OptionEventRefusal,
    OptionEventRefused,
    expected_share_trade,
)
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)

# How often the account's option events are read while nothing is wrong.
OPTION_EVENT_POLL_INTERVAL_MS = 5 * 60_000
# How soon they are read again after a read that was not whole or that left a hold standing, and
# the least time between two reads a pass asks for because a contract's position differs from the books.
OPTION_EVENT_POLL_RETRY_MS = 60_000
# The most pages one poll follows. A walk that is longer is not whole, and says so.
MAX_OPTION_EVENT_PAGES = 50

# The owner of the lot that stands for outside orders in a contract. It is no bot and no custody subject.
OUTSIDE_THE_BOOKS = "outside-the-books"
_ACK_OPERATOR_REF_PREFIX = "operator:"
_BOUGHT, _SOLD = "bought", "sold"
_ENDINGS: dict[OptionEventKind, OptionEnding] = {
    OptionEventKind.EXPIRED: OptionEnding.EXPIRED,
    OptionEventKind.EXERCISED: OptionEnding.EXERCISED,
    OptionEventKind.ASSIGNED: OptionEnding.ASSIGNED,
}
# The holds a whole read's findings raise and its silence ends.
_READ_HOLDS = tuple(
    code
    for code in OPTION_EVENT_HOLD_REASON_CODES
    if code not in (OPTION_EVENTS_NOT_READ_REASON_CODE, OPTION_SHARES_HELD_REASON_CODE, OPTION_ORDER_NOT_OURS_REASON_CODE)
)
# What the owner may acknowledge. The other holds end only by proof: a whole read, or a flat position.
_ACKNOWLEDGEABLE = frozenset(_READ_HOLDS) | {OPTION_ORDER_NOT_OURS_REASON_CODE}

# What each hold says. The reason code names it; these are the owner's words for it.
_HOLD_COPY: dict[str, tuple[str, str]] = {
    OPTION_EVENTS_NOT_READ_REASON_CODE: (
        "Option events could not all be read",
        "The account's assignments, exercises and expiries could not be read whole, so the books may be missing one.",
    ),
    OPTION_EVENT_UNREADABLE_REASON_CODE: (
        "An option activity could not be read",
        "The broker reports an option activity these books cannot read whole. Nothing of it was booked.",
    ),
    OPTION_EVENT_NOT_EXECUTED_REASON_CODE: (
        "An option activity is not reported as executed",
        "The broker marks an option activity as corrected or cancelled and does not say what that replaces. "
        "Nothing of it was booked.",
    ),
    OPTION_EVENT_CONTRACT_NOT_HELD_REASON_CODE: (
        "An option event names a contract no bot holds",
        "The broker reports an option event in a contract these books hold no lot of. Nothing was booked.",
    ),
    OPTION_EVENT_WRONG_SIDE_REASON_CODE: (
        "An option event is for the side no bot holds",
        "The broker reports an option event that ends the other side of the contract than the books hold. "
        "Nothing was booked.",
    ),
    OPTION_EVENT_HELD_ON_BOTH_SIDES_REASON_CODE: (
        "An option expired while held on both sides",
        "The contract is held bought by one bot and sold by another. The broker reports only the account's net, "
        "so no count of it can be matched to a bot. Nothing was booked.",
    ),
    OPTION_EVENT_MORE_CONTRACTS_THAN_HELD_REASON_CODE: (
        "An option event is for more contracts than are held",
        "The broker reports an option event for more contracts than the lots it could take hold. Nothing was booked.",
    ),
    OPTION_EVENT_CASH_NOT_WHOLE_CENTS_REASON_CODE: (
        "An option event's cash is not whole cents",
        "The shares at the strike do not come to whole cents. The amount is never rounded, so nothing was booked.",
    ),
    OPTION_EVENT_ROW_CHANGED_REASON_CODE: (
        "An option activity changed after it was booked",
        "The broker now reports other figures under the id of an activity these books already booked. "
        "The booking stands as it was made.",
    ),
    OPTION_EVENT_SHARES_IN_ANOTHER_SYMBOL_REASON_CODE: (
        "An option event's shares are in another symbol",
        "The contract's shares are written under another symbol than the one this bot is registered under, "
        "so the books cannot count them. Nothing was booked.",
    ),
    OPTION_SHARE_TRADE_MISSING_REASON_CODE: (
        "The share trade of an option event was not reported",
        "The exercise or assignment was booked from the contract's own terms. The broker's own row for the "
        "share trade was not among the activities read.",
    ),
    OPTION_SHARE_TRADE_UNPAIRED_REASON_CODE: (
        "A share trade belongs to no option event read",
        "The broker reports a share trade from an option event, and no exercise or assignment read matches it.",
    ),
    OPTION_SHARE_TRADE_DISAGREES_REASON_CODE: (
        "The broker's share trade differs from the contract's terms",
        "The exercise or assignment was booked from the contract's own terms. The broker's row for the share "
        "trade states something else.",
    ),
    OPTION_EXPIRY_SIGNED_FOR_OTHER_SIDE_REASON_CODE: (
        "An expiry was booked against the sign of its row",
        "The broker's row is signed for the other side than the lots it closed. The books held the contract on "
        "one side only, so the lots were closed.",
    ),
    OPTION_EVENT_AFTER_A_LOT_CLOSED_REASON_CODE: (
        "An option event was booked after an older lot was closed",
        "The event was booked on the lots open when its row was read. Another bot closed a lot of the same "
        "contract after the event's date, and the event may have been that lot's: the broker names no lot.",
    ),
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: (
        "An expired contract is still on the books",
        "The contract's last session is over and its position is not settled on these books.",
    ),
    OPTION_SHARES_HELD_REASON_CODE: (
        "Shares from an option event are held",
        "An exercise or an assignment left this bot holding shares. The bot does not trade them.",
    ),
    OPTION_ORDER_NOT_OURS_REASON_CODE: (
        "An order the Clerk did not place is in a contract this bot holds",
        "The broker shows an order these books did not send, in a contract this bot holds, on the side that "
        "reduces it.",
    ),
}
_NEXT_STEPS: dict[str, str] = {
    OPTION_SHARES_HELD_REASON_CODE: "Stop the bot and flatten it to close the shares.",
    OPTION_ORDER_NOT_OURS_REASON_CODE: (
        "Nothing to do while the order works: once it has ended, what it filled is booked against this bot. "
        "If the order is your own, cancel it at the broker, or mark it reviewed and the bot's own orders go on."
    ),
    OPTION_EVENTS_NOT_READ_REASON_CODE: "Nothing to do: the Clerk reads them once more within a minute.",
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: (
        "Nothing to send. It is settled when the broker's activity for it is read, or, in a simulation, when a "
        "price of the underlying from the contract's last session is kept."
    ),
}
# What a hold means for the bot, where the family's own sentence would not be true.
_OPERATOR_IMPACTS: dict[str, str] = {
    OPTION_ORDER_NOT_OURS_REASON_CODE: (
        "This bot takes no new entries, and no order of its own to close that contract is sent until the order "
        "is settled or reviewed: it would be a second order on the same contracts. Its other reductions stay "
        "available."
    ),
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: "This bot takes no new entries. No order can close an expired contract.",
}
_DEFAULT_NEXT_STEP = "Compare this bot's positions with the broker's, then acknowledge the notice."
if set(_HOLD_COPY) != set(OPTION_EVENT_HOLD_REASON_CODES):
    # A hold with no words of its own could not be raised at all.
    raise RuntimeError("every option-event hold needs its own words")
if not {refusal.value for refusal in OptionEventRefusal} <= set(OPTION_EVENT_HOLD_REASON_CODES):
    # A refusal with no hold of its own would be found and then dropped without a word.
    raise RuntimeError("every refusal of the allocation rule needs a hold under its own code")


@dataclass(frozen=True, slots=True)
class OptionEventRead:
    """Everything one poll read of the account's option events, with what it could not read."""

    # The window's start: any instant on its first New York date.
    after_ms: int
    events: tuple[BrokerOptionEventActivity, ...] = ()
    share_trades: tuple[BrokerOptionShareTradeActivity, ...] = ()
    unreadable: tuple[BrokerOptionActivityUnreadable, ...] = ()
    # Whether the walk reached the window's start. False is never "nothing happened".
    history_complete: bool = False
    # Why the walk did not, in plain words. None when it did.
    failure: str | None = None
    # The broker's signed position by symbol, read after the events. None when it was not read.
    broker_positions: Mapping[str, float] | None = None
    # True when a simulation reported the events: its bookings are simulated executions.
    simulated: bool = False
    # A simulation's own record of each settlement, by activity id.
    settlement_notes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # The contracts a simulation holds past their last session and could not settle, with why.
    not_settled: Mapping[str, str] = field(default_factory=dict)

    @property
    def whole(self) -> bool:
        """Whether the walk reached the window's start. Rows it could not read are held for one by one."""
        return self.history_complete and self.failure is None


@dataclass(frozen=True, slots=True)
class OptionEventRound:
    """What one run of the batch function did."""

    # The command id of every booking it appended.
    booked: tuple[str, ...] = ()
    # Each hold standing when it ended, as (bot or None, reason code).
    held: tuple[tuple[str | None, str], ...] = ()
    read_whole: bool = False


@dataclass(frozen=True, slots=True)
class OptionEventPoll:
    """What one call of the poll did: whether it read the broker, and what the batch function then did."""

    read: bool
    round: OptionEventRound | None = None


# ── Reads of the books ───────────────────────────────────────────────────────


def _active_holds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    marks = ", ".join("?" for _ in OPTION_EVENT_HOLD_REASON_CODES)
    rows = conn.execute(
        "SELECT uncertainty_id, scope, strategy_instance_id, reason_code, facts_json FROM uncertainties "
        f"WHERE reason_code IN ({marks}) AND resolved_at_ms IS NULL",
        OPTION_EVENT_HOLD_REASON_CODES,
    ).fetchall()
    return [dict(row) for row in rows]


def option_work_on_the_books(repo: ClerkSqliteRepository) -> bool:
    """Whether the books hold anything an option event could change, or a hold a read could end."""
    with repo.write_fence() as conn:
        if books_record_no_contract(conn):
            return False
        return bool(held_option_positions(conn)) or bool(_active_holds(conn))


def option_position_differs(repo: ClerkSqliteRepository, broker_positions: Sequence[BrokerPosition]) -> bool:
    """Whether the broker's position in a recorded contract a bot holds differs from the books' total."""
    with repo.write_fence() as conn:
        if books_record_no_contract(conn):
            return False
        held: dict[str, int] = {}
        for _sid, symbol, quantity in held_option_positions(conn):
            held[symbol] = held.get(symbol, 0) + quantity
    at_broker = broker_quantity_by_symbol(list(broker_positions))
    return any(at_broker.get(symbol, 0.0) != quantity for symbol, quantity in held.items())


def option_event_window_start_ms(repo: ClerkSqliteRepository) -> int:
    """Where a read of option events starts: 00:00 New York time on the first date that can still matter.

    The date of the first fill in any contract a bot still holds, or the
    date of the oldest row an open hold names, whichever is earlier. With
    neither, today. Earlier than needed is safe: a row already booked is
    skipped by its id.
    """
    with repo.write_fence() as conn:
        row = conn.execute(
            "SELECT MIN(COALESCE(f.source_event_at_ms, f.recorded_at_ms)) AS first_ms "
            "FROM option_contracts c CROSS JOIN positions p ON p.symbol = c.symbol "
            "JOIN order_contracts bound ON bound.symbol = c.symbol "
            "JOIN fills f ON f.order_ref = bound.order_ref WHERE p.attributed_qty != 0"
        ).fetchone()
        instants = [] if row is None or row["first_ms"] is None else [int(row["first_ms"])]
        for hold in _active_holds(conn):
            cause = json.loads(hold["facts_json"]).get("cause_facts") or {}
            instants += [item["date_ms"] for item in cause.get("items", []) if item.get("date_ms") is not None]
    return et_midnight_ms(et_date_at_ms(min([*instants, repo.clock()])))


# ── Keys and references ──────────────────────────────────────────────────────


def _order_ref(sid: str, idempotency_key: str, part: str) -> str:
    digest = hashlib.sha256(f"{idempotency_key}\n{part}".encode()).hexdigest()[:20]
    return build_order_ref(build_bot_order_namespace(sid), f"o{part[0]}{digest}")


# ── A contract's lots, across bots and outside the books ─────────────────────


@dataclass(frozen=True, slots=True)
class _BotBook:
    """One bot's books in one contract."""

    sid: str
    lots: tuple[HeldOptionLot, ...]
    # The order whose fill opened each lot, by the lot's sequence.
    opened_by: Mapping[int, str]
    # Each lot an order closed: when it was opened, when it was closed, and the side it was held on.
    closed: tuple[tuple[int, int, str], ...]


def _bots_that_traded(conn: sqlite3.Connection, symbol: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT e.strategy_instance_id AS sid FROM order_contracts bound "
        "JOIN orders o ON o.order_ref = bound.order_ref "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE bound.symbol = ? AND e.strategy_instance_id IS NOT NULL ORDER BY sid",
        (symbol,),
    ).fetchall()
    return [row["sid"] for row in rows]


def _bot_book(conn: sqlite3.Connection, sid: str, contract: OptionContract, records: Sequence[FillRecord]) -> _BotBook:
    """One bot's open lots of ``contract``, each named by the ledger sequence of the fill that opened it.

    The lots are FIFO's own (``fifo_pnl.compute_fifo_pnl``); nothing here
    matches a fill to a lot again. A bot whose lots do not add up to its
    attributed position is offered none: its position was written off, and
    closing a lot for it would take the position past nothing.
    """
    symbol = contract.occ_symbol
    fifo = compute_fifo_pnl(records)
    open_lots = [lot for lot in fifo.open_lots if lot.symbol == symbol]
    signed = sum(int(lot.exact_qty) * (1 if lot.side is OrderSide.BUY else -1) for lot in open_lots)
    # A lot an event closed was closed by no order: only a close by an order can come between an
    # event and the reading of its row.
    sides_at: dict[int, set[OrderSide]] = {}
    for record in records:
        if not record.event_key.startswith(OPTION_EVENT_CLOSE_EXECUTION_PREFIX):
            sides_at.setdefault(record.filled_at_ms, set()).add(record.side)
    closed = tuple(
        (lot.opened_at_ms, lot.closed_at_ms, _SOLD if OrderSide.BUY in sides_at[lot.closed_at_ms] else _BOUGHT)
        for lot in fifo.closed_lots
        if lot.symbol == symbol and lot.closed_at_ms in sides_at
    )
    if signed != reads.position(conn, sid, symbol):
        return _BotBook(sid=sid, lots=(), opened_by={}, closed=closed)
    lots: list[HeldOptionLot] = []
    opened_by: dict[int, str] = {}
    in_order = iter(records)
    for lot in open_lots:
        opening = next(
            (fill for fill in in_order if fill.side is lot.side and fill.filled_at_ms == lot.opened_at_ms), None
        )
        if opening is None:
            return _BotBook(sid=sid, lots=(), opened_by={}, closed=closed)
        quantity = int(lot.exact_qty)
        lots.append(
            HeldOptionLot(
                owner=sid, contract=contract, quantity=quantity if lot.side is OrderSide.BUY else -quantity,
                opened_at_ms=lot.opened_at_ms, sequence=opening.ledger_sequence,
            )
        )
        opened_by[opening.ledger_sequence] = opening.order_ref
    return _BotBook(sid=sid, lots=tuple(lots), opened_by=opened_by, closed=closed)


def _bot_books(conn: sqlite3.Connection, account_id: str, contract: OptionContract) -> dict[str, _BotBook]:
    """Every bot that ever traded ``contract``, with its open lots now."""
    symbol = contract.occ_symbol
    sids = _bots_that_traded(conn, symbol)
    if not sids:
        return {}
    by_bot: dict[str, list[FillRecord]] = {sid: [] for sid in sids}
    for record in effective_fill_records(conn, account_id=account_id, strategy_instance_ids=sids):
        if record.symbol == symbol:
            by_bot[record.sid.removeprefix(BOT_SUBJECT_PREFIX)].append(record)
    return {sid: _bot_book(conn, sid, contract, records) for sid, records in by_bot.items()}


def _outside_lot(
    conn: sqlite3.Connection, contract: OptionContract, *, leaving_out: frozenset[str] = frozenset()
) -> HeldOptionLot | None:
    """One lot for what outside orders hold of ``contract``, or None when they hold nothing.

    Orders already booked against a bot as a sale are left out: they closed a
    bot's lot, not an outside one. Raises ``OutsideFillUnknown`` when an
    order does not state what it filled.
    """
    held, opened_at_ms, sequence = 0, None, None
    for order in outside_orders_in(conn, contract.occ_symbol):
        if order.broker_order_id in leaving_out or bookings(conn, booking_key(SALE_KEY_PREFIX, OUTSIDE_ORDER_BROKER, order.broker_order_id)):
            continue
        signed = outside_fill(order)
        if signed and opened_at_ms is None:
            opened_at_ms = order.observation_recorded_at_ms or order.observed_at_ms
            sequence = order.observation_sequence or 0
        held += signed
    if not held or opened_at_ms is None or sequence is None:
        return None
    return HeldOptionLot(
        owner=OUTSIDE_THE_BOOKS, contract=contract, quantity=held, opened_at_ms=opened_at_ms, sequence=sequence
    )


# ── Holds ────────────────────────────────────────────────────────────────────


class _Holds:
    """The holds one run found, by bot and reason. A bot of None is the account."""

    def __init__(self) -> None:
        self._found: dict[tuple[str | None, str], list[OptionEventHoldItem]] = {}

    def add(
        self, reason_code: str, bots: Sequence[str], *, ref: str, symbol: str | None, date_ms: int | None, detail: str
    ) -> None:
        item = OptionEventHoldItem(ref=ref, symbol=symbol, date_ms=date_ms, detail=detail)
        for sid in bots or (None,):
            items = self._found.setdefault((sid, reason_code), [])
            if item not in items:
                items.append(item)

    def items(self) -> Mapping[tuple[str | None, str], list[OptionEventHoldItem]]:
        return self._found


def _raise_hold(repo: ClerkSqliteRepository, sid: str | None, reason_code: str, items: Sequence[OptionEventHoldItem]) -> None:
    headline, what = _HOLD_COPY[reason_code]
    raise_uncertainty(
        repo,
        strategy_instance_id=sid,
        reason_code=reason_code,
        headline=headline,
        explanation=" ".join([what, *(item.detail for item in items)]),
        operator_impact=_OPERATOR_IMPACTS.get(
            reason_code,
            "This bot takes no new entries. Reductions and the Flatten stay available."
            if sid is not None
            else "No bot of this account takes a new entry. Reductions and the Flatten stay available.",
        ),
        next_step=_NEXT_STEPS.get(reason_code, _DEFAULT_NEXT_STEP),
        evidence_refs=tuple(item.ref for item in items),
        cause_facts=OptionEventHoldCause(items=tuple(items)).to_mapping(),
    )


def _resolve_hold(
    repo: ClerkSqliteRepository, sid: str | None, reason_code: str, *, resolution_kind: str, summary_code: str,
    evidence_refs: Sequence[str],
) -> bool:
    def build_transition(uncertainty_id: str) -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=sid,
            transition_kind="UNCERTAINTY_RESOLVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=summary_code,
            facts_json=UncertaintyResolvedFacts(
                uncertainty_id=uncertainty_id, resolution_kind=resolution_kind, evidence_refs=list(evidence_refs)
            ).to_facts_json(),
        )

    return repo.resolve_uncertainty_if_active(
        scope="ACCOUNT_CLERK" if sid is None else "CUSTODY_SUBJECT",
        reason_code=reason_code,
        strategy_instance_id=sid,
        build_transition=build_transition,
    )


def acknowledge_option_event_hold(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str | None, reason_code: str, operator: str
) -> bool:
    """The owner has looked at what one option-event hold names: end it, and do not raise it for those things again.

    The acknowledgement is the hold's own resolution, hash-chained, naming
    the owner and each thing the hold named. A later read that still has
    those rows raises nothing for them. A new row raises the hold again.
    Whether a hold was open to acknowledge is the answer.
    """
    if reason_code not in _ACKNOWLEDGEABLE:
        raise ValueError(f"{reason_code!r} ends by proof, not by acknowledgement")
    if not operator.strip():
        raise ValueError("an acknowledgement names who made it")
    with repo.write_fence() as conn:
        active = reads.active_uncertainty(
            conn, scope="ACCOUNT_CLERK" if strategy_instance_id is None else "CUSTODY_SUBJECT",
            reason_code=reason_code, strategy_instance_id=strategy_instance_id,
        )
        if active is None:
            return False
        cause = OptionEventHoldCause.from_mapping(json.loads(active["facts_json"])["cause_facts"])
        return _resolve_hold(
            repo, strategy_instance_id, reason_code, resolution_kind="OPERATOR_ACKNOWLEDGED",
            summary_code=OPTION_EVENT_HOLD_ACKNOWLEDGED,
            evidence_refs=(
                f"{_ACK_OPERATOR_REF_PREFIX}{operator.strip()}",
                *(hold_item_ack_ref(strategy_instance_id, reason_code, item.ref) for item in cause.items),
            ),
        )


def _sync_holds(
    repo: ClerkSqliteRepository, conn: sqlite3.Connection, holds: _Holds, *, codes: Sequence[str], may_end: bool
) -> list[tuple[str | None, str]]:
    """Raise each hold found, and end each open one of ``codes`` that was not, when this run may."""
    acknowledged = acknowledged_hold_items(conn)
    wanted: dict[tuple[str | None, str], list[OptionEventHoldItem]] = {}
    for (sid, reason_code), items in holds.items().items():
        if reason_code not in codes:
            continue
        standing = [item for item in items if hold_item_ack_ref(sid, reason_code, item.ref) not in acknowledged]
        if standing:
            wanted[(sid, reason_code)] = standing
    for hold in _active_holds(conn):
        key = (hold["strategy_instance_id"], hold["reason_code"])
        if hold["reason_code"] in codes and key not in wanted and may_end:
            _resolve_hold(
                repo, key[0], key[1], resolution_kind="CAUSE_CLEARED", summary_code="OPTION_EVENT_HOLD_CAUSE_GONE",
                evidence_refs=("option_events_read",),
            )
    for (sid, reason_code), items in wanted.items():
        _raise_hold(repo, sid, reason_code, items)
    return list(wanted)


# ── Booking ──────────────────────────────────────────────────────────────────


def _allocation_rows(lots: Sequence[LotAllocation]) -> list[dict[str, Any]]:
    return [
        {
            "owner": lot.owner, "sequence": lot.sequence, "opened_at_ms": lot.opened_at_ms,
            "contracts": lot.contracts, "option_quantity_change": lot.option_quantity_change,
            "share_quantity_change": lot.share_quantity_change, "cash_change_cents": lot.cash_change_cents,
        }
        for lot in lots
    ]


def _run_of(conn: sqlite3.Connection, order_ref: str | None) -> str | None:
    """The run whose entry opened a lot: the run a booking for that lot belongs to."""
    if order_ref is None:
        return None
    row = conn.execute(
        "SELECT e.run_id FROM orders o JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE o.order_ref = ?",
        (order_ref,),
    ).fetchone()
    return None if row is None else row["run_id"]


def _append_booking(
    repo: ClerkSqliteRepository,
    *,
    sid: str,
    run_id: str | None,
    key: str,
    ended_by: OptionEnding,
    broker: str,
    source_id: str,
    source_code: str,
    event_date_ms: int | None,
    position_change: int,
    contract: OptionContract,
    multiplier: int,
    contracts: int,
    held: str,
    close_price: Decimal,
    booked_as_of_ms: int,
    simulated: bool,
    allocation: list[dict[str, Any]],
    share_trade: dict[str, Any] | None,
    simulation: dict[str, Any] | None,
    notes: list[dict[str, str]] | None,
) -> OptionPositionEndedFacts | None:
    """Book one bot's part of one event, once. The record when this call booked it; None when it was booked before."""
    idempotency_key = f"{key}{sid}"
    command_id = f"cmd:{idempotency_key}"
    symbol = contract.occ_symbol
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "ended_by": ended_by.value, "broker": broker, "source_id": source_id, "source_code": source_code,
                "event_date_ms": event_date_ms, "position_change": position_change, "contract": symbol,
                "bot": sid, "contracts": contracts, "held": held, "close_price": str(close_price),
            }
        ).encode("utf-8")
    ).hexdigest()
    shares: dict[str, Any] | None = None
    if ended_by is not OptionEnding.SOLD_OUTSIDE:
        moved = expected_share_trade(OptionEventKind(ended_by.value), contract, contracts=contracts, multiplier=multiplier)
        if moved is not None:
            shares = {
                "symbol": moved.root, "quantity": moved.shares, "price": str(moved.price),
                "cash_cents": moved.cash_cents, "order_ref": _order_ref(sid, idempotency_key, "shares"),
                "execution_id": f"{OPTION_EVENT_SHARES_EXECUTION_PREFIX}{key.removeprefix(EVENT_KEY_PREFIX)}{sid}",
            }
    close_execution_id = (
        f"{SALE_KEY_PREFIX}fill:{key.removeprefix(SALE_KEY_PREFIX)}{sid}"
        if ended_by is OptionEnding.SOLD_OUTSIDE
        else f"{OPTION_EVENT_CLOSE_EXECUTION_PREFIX}{key.removeprefix(EVENT_KEY_PREFIX)}{sid}"
    )
    facts = OptionPositionEndedFacts(
        idempotency_key=idempotency_key, payload_hash=payload_hash, ended_by=ended_by.value, broker=broker,
        source_id=source_id, source_code=source_code, event_date_ms=event_date_ms, position_change=position_change,
        contract_symbol=symbol, multiplier=multiplier, contracts=contracts, held=held, close_price=str(close_price),
        booked_as_of_ms=booked_as_of_ms, option_order_ref=_order_ref(sid, idempotency_key, "close"),
        close_execution_id=close_execution_id, simulated=simulated, allocation=allocation, shares=shares,
        share_trade=share_trade, simulation=simulation, notes=notes or None,
    )

    def build_transition() -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=sid,
            run_id=run_id,
            command_id=command_id,
            effect_operation_id=f"effect:{idempotency_key}",
            order_ref=facts.option_order_ref,
            transition_kind=OPTION_POSITION_ENDED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            proof_reference=source_id,
            source_event_at_ms=booked_as_of_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code=OPTION_ENDING_SUMMARY_CODES[ended_by],
            facts_json=facts.to_facts_json(),
        )

    outcome = repo.commit_first_transition(
        command_id=command_id, idempotency_key=idempotency_key, payload_hash=payload_hash,
        build_transition=build_transition,
    )
    if isinstance(outcome, CommandExistingConflict):
        raise _RowChanged(sid)
    created = getattr(outcome, "committed", None) is not None
    if created:
        logger.info(
            "booked the end of an option position",
            extra={
                "action": "option_position_ended_booked", "account_id": repo.account_id,
                "strategy_instance_id": sid, "symbol": symbol, "ended_by": ended_by.value, "contracts": contracts,
                "source_id": source_id,
            },
        )
    return facts if created else None


class _RowChanged(Exception):
    """The same id was booked before with other figures."""


def _signed_side(position_change: int) -> str:
    return _BOUGHT if position_change < 0 else _SOLD


def _holders(conn: sqlite3.Connection, symbol: str) -> list[str]:
    return [sid for sid, held_symbol, _qty in held_option_positions(conn) if held_symbol == symbol]


def _holders_under(conn: sqlite3.Connection, underlying: str) -> list[str]:
    """The bots holding any contract on one underlying, by the contract's root as written."""
    return sorted(
        {
            sid
            for sid, symbol, _qty in held_option_positions(conn)
            if OptionContract.from_occ_symbol(symbol).root == underlying
        }
    )


def _bots_a_row_names(conn: sqlite3.Connection, symbol: str | None) -> list[str]:
    """The bots a row can be tied to by its symbol: holders of that contract, or of any contract on that underlying."""
    if symbol is None:
        return []
    return _holders(conn, symbol.upper()) or _holders_under(conn, symbol.upper())


def _bots_that_wrote_it_off(
    conn: sqlite3.Connection, books: Mapping[str, _BotBook], event: BrokerOptionEventActivity
) -> tuple[str, ...]:
    """The bots whose written-off position in the event's contract accounts for the whole event, or none.

    An exercise ends a bought position and an assignment a sold one. An
    expiry ends either. A write-off took the position off the books and
    closed no lot, so the event has no lot to take: booking it would count
    the same contracts a second time.
    """
    sides = {OptionEventKind.EXERCISED: (_BOUGHT,), OptionEventKind.ASSIGNED: (_SOLD,)}.get(event.kind, (_BOUGHT, _SOLD))
    wrote_off = {
        sid: abs(contracts)
        for sid in books
        if (contracts := written_off_contracts(conn, sid, event.contract.occ_symbol))
        and (_BOUGHT if contracts > 0 else _SOLD) in sides
    }
    return tuple(sorted(wrote_off)) if sum(wrote_off.values()) >= abs(event.position_change) else ()


def _pair_share_trades(
    events: Sequence[BrokerOptionEventActivity],
    share_trades: Sequence[BrokerOptionShareTradeActivity],
    multiplier_of: Mapping[str, int],
) -> tuple[dict[str, tuple[BrokerOptionShareTradeActivity | None, tuple[str, ...]]], list[BrokerOptionShareTradeActivity]]:
    """Each exercise or assignment with the broker's share trade for it, and the share trades that belong to none.

    A share trade names the underlying and no event. It is paired with an
    event of its date and underlying: first with one whose terms it meets
    exactly, then, of what is left, with one it differs from. An event with
    none left has no share trade. The contract's terms, never the row, are
    what the books count.
    """
    left = sorted(
        (trade for trade in share_trades if trade.status is OptionActivityStatus.EXECUTED),
        key=lambda trade: trade.activity_id,
    )
    paired: dict[str, tuple[BrokerOptionShareTradeActivity | None, tuple[str, ...]]] = {}
    expected = {}
    for event in events:
        if event.kind is OptionEventKind.EXPIRED:
            continue
        try:
            expected[event.activity_id] = expected_share_trade(
                event.kind, event.contract, contracts=event.contracts,
                multiplier=multiplier_of.get(event.contract.occ_symbol, 100),
            )
        except OptionEventRefused:
            continue
    for exactly in (True, False):
        for event in events:
            terms = expected.get(event.activity_id)
            if terms is None or event.activity_id in paired:
                continue
            for trade in left:
                if trade.activity_date_ms != event.activity_date_ms or trade.underlying_symbol != terms.root:
                    continue
                if event.group_id is not None and trade.group_id is not None and trade.group_id != event.group_id:
                    continue
                differences = share_trade_disagreements(terms, trade)
                if exactly and differences:
                    continue
                paired[event.activity_id] = (trade, differences)
                left.remove(trade)
                break
    for event in events:
        if event.activity_id in expected and event.activity_id not in paired:
            paired[event.activity_id] = (None, ())
    return paired, left


def _book_events(
    repo: ClerkSqliteRepository, conn: sqlite3.Connection, read: OptionEventRead, holds: _Holds
) -> list[tuple[str, OptionPositionEndedFacts]]:
    """Book every executed event of one read that is not booked yet, in one fixed order. Each bot with its new record."""
    account_id = repo.account_id
    executed = [event for event in read.events if event.status is OptionActivityStatus.EXECUTED]
    multiplier_of = {}
    for event in executed:
        terms = contract_terms(conn, event.contract.occ_symbol)
        if terms is not None:
            multiplier_of[event.contract.occ_symbol] = terms.multiplier
    paired, unpaired = _pair_share_trades(in_allocation_order(executed), read.share_trades, multiplier_of)
    booked: list[tuple[str, OptionPositionEndedFacts]] = []
    # What outside orders hold of each contract, less what this run's events have taken of it.
    outside_left: dict[str, int] = {}
    for event in in_allocation_order(executed):
        contract, symbol = event.contract, event.contract.occ_symbol
        ref = f"option-activity:{event.activity_id}"
        key = booking_key(EVENT_KEY_PREFIX, event.broker, event.activity_id)

        def hold(reason_code: str, bots: Sequence[str], detail: str, *, _ref: str = ref, _event: BrokerOptionEventActivity = event) -> None:
            holds.add(reason_code, bots, ref=_ref, symbol=_event.contract.occ_symbol, date_ms=_event.activity_date_ms, detail=detail)

        recorded = bookings(conn, key)
        if recorded:
            first = next(iter(recorded.values()))
            owners = sorted({row["owner"] for row in first.allocation if row["owner"] != OUTSIDE_THE_BOOKS})
            if (first.ended_by, first.contract_symbol, first.position_change, first.event_date_ms) != (
                event.kind.value, symbol, event.position_change, event.activity_date_ms
            ):
                hold(
                    OPTION_EVENT_ROW_CHANGED_REASON_CODE, owners,
                    f"Activity {event.activity_id} was booked as {first.position_change:+d} of {first.contract_symbol} "
                    f"{first.ended_by}; it now reads {event.position_change:+d} of {symbol} {event.kind.value}.",
                )
                continue
            allocation_rows = first.allocation
            notes = first.notes or []
            share_trade_note = first.share_trade
            booked_as_of_ms = first.booked_as_of_ms
            # What the record says this event took from outside the books is gone for the events after it.
            taken_outside = sum(
                row["option_quantity_change"] for row in allocation_rows if row["owner"] == OUTSIDE_THE_BOOKS
            )
            if taken_outside:
                try:
                    whole = _outside_lot(conn, contract)
                except OutsideFillUnknown:
                    whole = None
                if whole is not None:
                    outside_left[symbol] = outside_left.get(symbol, whole.quantity) + taken_outside
        else:
            terms = contract_terms(conn, symbol)
            try:
                outside = _outside_lot(conn, contract)
            except OutsideFillUnknown:
                # Whose lot the event took is not known while an outside order in the contract does not
                # state what it filled. The bots holding it are held for that order by name.
                continue
            if outside is not None:
                left = outside_left.setdefault(symbol, outside.quantity)
                outside = None if not left else HeldOptionLot(
                    owner=OUTSIDE_THE_BOOKS, contract=contract, quantity=left, opened_at_ms=outside.opened_at_ms,
                    sequence=outside.sequence,
                )
            books = _bot_books(conn, account_id, contract) if terms is not None else {}
            lots = [lot for book in books.values() for lot in book.lots]
            if outside is not None:
                lots.append(outside)
            try:
                allocation = allocation_of(event, multiplier=100 if terms is None else terms.multiplier, lots=lots)
            except OptionEventRefused as refused:
                wrote_off = (
                    _bots_that_wrote_it_off(conn, books, event)
                    if refused.reason is OptionEventRefusal.CONTRACT_NOT_HELD
                    else ()
                )
                if wrote_off and event.kind is OptionEventKind.EXPIRED:
                    # The owner took the position off the books before the row came. The row moves
                    # nothing more, so nothing is booked and nobody is held.
                    logger.info(
                        "an option expiry was read for a position already written off; nothing is booked",
                        extra={
                            "action": "option_event_already_written_off", "account_id": account_id,
                            "symbol": symbol, "activity_id": event.activity_id, "bots": list(wrote_off),
                        },
                    )
                elif wrote_off:
                    hold(
                        OPTION_EVENT_CONTRACT_NOT_HELD_REASON_CODE, wrote_off,
                        f"The position in {symbol} was written off before this row was read. The row says it was "
                        f"{event.kind.value}: the shares and the cash that moved are on no bot's books.",
                    )
                else:
                    hold(refused.reason.value, _holders(conn, symbol), str(refused))
                continue
            taken_by_bot = [lot for lot in allocation.lots if lot.owner != OUTSIDE_THE_BOOKS]
            owners = sorted({lot.owner for lot in taken_by_bot})
            if outside is not None:
                outside_left[symbol] += sum(
                    lot.option_quantity_change for lot in allocation.lots if lot.owner == OUTSIDE_THE_BOOKS
                )
            if event.kind is not OptionEventKind.EXPIRED:
                elsewhere = [
                    sid for sid in owners if (reads.strategy_instance(conn, sid) or {}).get("symbol", "").upper() != contract.root
                ]
                if elsewhere:
                    hold(
                        OPTION_EVENT_SHARES_IN_ANOTHER_SYMBOL_REASON_CODE, elsewhere,
                        f"{symbol} moves shares written as {contract.root}.",
                    )
                    continue
            notes = []
            held = _BOUGHT if any(lot.option_quantity_change < 0 for lot in allocation.lots) else _SOLD
            if event.kind is OptionEventKind.EXPIRED and held != _signed_side(event.position_change):
                notes.append({
                    "code": OPTION_EXPIRY_SIGNED_FOR_OTHER_SIDE_REASON_CODE,
                    "detail": f"The expiry of {symbol} reads {event.position_change:+d}, and the lots it closed were {held}.",
                })
            event_date = et_date_at_ms(event.activity_date_ms)
            others = sorted(
                book.sid
                for book in books.values()
                if any(
                    side == held and et_date_at_ms(opened_at_ms) <= event_date < et_date_at_ms(closed_at_ms)
                    for opened_at_ms, closed_at_ms, side in book.closed
                )
            )
            for sid in owners:
                closers = [other for other in others if other != sid]
                if closers:
                    notes.append({
                        "code": OPTION_EVENT_AFTER_A_LOT_CLOSED_REASON_CODE, "bot": sid,
                        "detail": (
                            f"{symbol} was {event.kind.value} on {event_date.isoformat()}. {', '.join(closers)} closed "
                            f"a lot of it after that date and before the row was read."
                        ),
                    })
            trade, differences = paired.get(event.activity_id, (None, ()))
            share_trade_note = None
            if event.kind is not OptionEventKind.EXPIRED:
                share_trade_note = {
                    "activity_id": None if trade is None else trade.activity_id,
                    "agrees": trade is not None and not differences,
                    "differences": list(differences),
                }
            allocation_rows = _allocation_rows(allocation.lots)
            booked_as_of_ms = max(
                [last_session_close_ms(event_date) - 1, *(lot.opened_at_ms for lot in taken_by_bot)]
            )
            opened_by = {sequence: order_ref for book in books.values() for sequence, order_ref in book.opened_by.items()}
            for row in allocation_rows:
                row["opened_by"] = opened_by.get(row["sequence"]) if row["owner"] != OUTSIDE_THE_BOOKS else None

        # Book each bot the record names and the books do not yet show. The record, never a second matching.
        multiplier = multiplier_of.get(symbol)
        for sid in owners:
            if sid in recorded or multiplier is None:
                continue
            rows = [row for row in allocation_rows if row["owner"] == sid]
            contracts = sum(row["contracts"] for row in rows)
            held = _BOUGHT if rows[0]["option_quantity_change"] < 0 else _SOLD
            on_side = reads.position(conn, sid, symbol) * (1 if held == _BOUGHT else -1)
            if on_side < contracts:
                hold(
                    OPTION_EVENT_MORE_CONTRACTS_THAN_HELD_REASON_CODE, [sid],
                    f"{contracts} of {symbol} {event.kind.value} were matched to {sid}, whose books now hold "
                    f"{reads.position(conn, sid, symbol):g}.",
                )
                continue
            try:
                record = _append_booking(
                    repo, sid=sid, run_id=_run_of(conn, rows[0].get("opened_by")), key=key,
                    ended_by=_ENDINGS[event.kind], broker=event.broker, source_id=event.activity_id,
                    source_code=event.activity_type, event_date_ms=event.activity_date_ms,
                    position_change=event.position_change, contract=contract, multiplier=multiplier,
                    contracts=contracts, held=held, close_price=Decimal(0), booked_as_of_ms=booked_as_of_ms,
                    simulated=read.simulated, allocation=allocation_rows, share_trade=share_trade_note,
                    simulation=dict(read.settlement_notes[event.activity_id])
                    if event.activity_id in read.settlement_notes else None,
                    notes=notes,
                )
            except _RowChanged:
                hold(
                    OPTION_EVENT_ROW_CHANGED_REASON_CODE, [sid],
                    f"Activity {event.activity_id} was booked for {sid} before with other figures.",
                )
                continue
            if record is not None:
                booked.append((sid, record))

        # What the record noted is held for as long as the row is read and the owner has not acknowledged it.
        for note in notes:
            hold(note["code"], [note["bot"]] if "bot" in note else owners, note["detail"])
        trade, differences = paired.get(event.activity_id, (None, ()))
        if event.kind is not OptionEventKind.EXPIRED and owners:
            if trade is None:
                hold(
                    OPTION_SHARE_TRADE_MISSING_REASON_CODE, owners,
                    f"No share trade was read for the {event.kind.value} {symbol} of "
                    f"{et_date_at_ms(event.activity_date_ms).isoformat()}.",
                )
            elif differences:
                holds.add(
                    OPTION_SHARE_TRADE_DISAGREES_REASON_CODE, owners, ref=f"share-trade:{trade.activity_id}",
                    symbol=trade.underlying_symbol, date_ms=trade.activity_date_ms,
                    detail=f"For {symbol}: {'; '.join(differences)}.",
                )
    for trade in unpaired:
        holds.add(
            OPTION_SHARE_TRADE_UNPAIRED_REASON_CODE, _holders_under(conn, trade.underlying_symbol),
            ref=f"share-trade:{trade.activity_id}", symbol=trade.underlying_symbol, date_ms=trade.activity_date_ms,
            detail=f"{trade.shares:+d} {trade.underlying_symbol} at {trade.price}.",
        )
    return booked


def _hold_what_could_not_be_read(conn: sqlite3.Connection, read: OptionEventRead, holds: _Holds) -> None:
    for row in read.unreadable:
        holds.add(
            OPTION_EVENT_UNREADABLE_REASON_CODE, _bots_a_row_names(conn, row.symbol),
            ref=f"option-activity:{row.activity_id}", symbol=row.symbol, date_ms=row.activity_date_ms,
            detail=f"Activity {row.activity_id} ({row.activity_type}, {row.symbol or 'no symbol'}): {row.reason}.",
        )
    for event in read.events:
        if event.status is not OptionActivityStatus.EXECUTED:
            holds.add(
                OPTION_EVENT_NOT_EXECUTED_REASON_CODE, _holders(conn, event.contract.occ_symbol),
                ref=f"option-activity:{event.activity_id}", symbol=event.contract.occ_symbol,
                date_ms=event.activity_date_ms,
                detail=f"Activity {event.activity_id} ({event.activity_type}) is reported as {event.status.value}.",
            )
    for trade in read.share_trades:
        if trade.status is not OptionActivityStatus.EXECUTED:
            holds.add(
                OPTION_EVENT_NOT_EXECUTED_REASON_CODE, _holders_under(conn, trade.underlying_symbol),
                ref=f"share-trade:{trade.activity_id}", symbol=trade.underlying_symbol, date_ms=trade.activity_date_ms,
                detail=f"Share trade {trade.activity_id} is reported as {trade.status.value}.",
            )


def _hold_what_expired_and_is_still_held(
    conn: sqlite3.Connection, read: OptionEventRead, holds: _Holds, *, now_ms: int
) -> None:
    """After a contract's last session, set any lot still open on it against the broker's position.

    A contract held on both sides nets to nothing at the broker and may never
    get a row. So may one whose row is yet to come. Either way the books
    still hold what the broker does not, and the bot is held by name.
    """
    totals: dict[str, int] = {}
    positions = held_option_positions(conn)
    for _sid, symbol, quantity in positions:
        totals[symbol] = totals.get(symbol, 0) + quantity
    for sid, symbol, quantity in positions:
        contract = OptionContract.from_occ_symbol(symbol)
        if not past_last_session(contract.expiry_date, now_ms):
            continue
        ref = f"contract:{symbol}"
        expired_on = contract.expiry_date.isoformat()
        if symbol in read.not_settled:
            holds.add(
                OPTION_EXPIRED_NOT_SETTLED_REASON_CODE, [sid], ref=ref, symbol=symbol, date_ms=None,
                detail=f"{symbol} expired on {expired_on}. {read.not_settled[symbol]}",
            )
        elif read.broker_positions is not None and read.broker_positions.get(symbol, 0.0) != totals[symbol]:
            holds.add(
                OPTION_EXPIRED_NOT_SETTLED_REASON_CODE, [sid], ref=ref, symbol=symbol, date_ms=None,
                detail=(
                    f"{symbol} expired on {expired_on}. The broker shows {read.broker_positions.get(symbol, 0.0):g} "
                    f"of it, these books hold {totals[symbol]:+d} ({quantity:+d} for this bot), and no activity "
                    "read explains the difference."
                ),
            )


def _sync_shares_held(
    repo: ClerkSqliteRepository, conn: sqlite3.Connection, booked: Sequence[tuple[str, OptionPositionEndedFacts]]
) -> None:
    """Hold a bot for the shares an event left it, and end that hold once the bot holds none."""
    for sid, facts in booked:
        if facts.shares is None:
            continue
        shares = facts.shares["quantity"]
        _raise_hold(
            repo, sid, OPTION_SHARES_HELD_REASON_CODE,
            [OptionEventHoldItem(
                ref=f"option-activity:{facts.source_id}", symbol=facts.shares["symbol"], date_ms=None,
                detail=(
                    f"{facts.contracts} of {facts.contract_symbol} {facts.ended_by} "
                    f"{'bought' if shares > 0 else 'sold'} {abs(shares)} {facts.shares['symbol']} at "
                    f"{facts.shares['price']}."
                ),
            )],
        )
    for hold in _active_holds(conn):
        sid = hold["strategy_instance_id"]
        if hold["reason_code"] != OPTION_SHARES_HELD_REASON_CODE or sid is None:
            continue
        registered = reads.strategy_instance(conn, sid)
        if registered is not None and not reads.position(conn, sid, registered["symbol"]):
            _resolve_hold(
                repo, sid, OPTION_SHARES_HELD_REASON_CODE, resolution_kind="ATTRIBUTED_FLAT_PROVEN",
                summary_code="OPTION_SHARES_HELD_RESOLVED", evidence_refs=("attributed_flat",),
            )


# ── A sale the broker makes itself ───────────────────────────────────────────


def _book_outside_orders(
    repo: ClerkSqliteRepository, conn: sqlite3.Connection, holds: _Holds
) -> list[tuple[str, OptionPositionEndedFacts]]:
    """Hold each bot beside an outside order in a contract it holds, and book the order's fill against the oldest lots.

    Read from the books alone: the outside orders they recorded in a
    recorded contract. An order on the side that reduces what a bot holds is
    the broker's own sale, or the owner's by hand; nothing on it says which.
    While it works the bot is held. Once it has ended with a fill, the fill
    is booked against the oldest lots at the broker's average price. It is
    not booked while another outside order may hold the same contract, when
    it filled more than the bots hold, or when it states no fill or no
    price: the hold then says which. Which orders these are is
    ``unsettled_outside_order``'s to say, and every check of a bot's own
    order reads the same answer (``outside_order_in_the_way``).
    """
    booked: list[tuple[str, OptionPositionEndedFacts]] = []
    rows = conn.execute(
        "SELECT eo.broker_order_id AS broker_order_id FROM option_contracts c "
        "CROSS JOIN external_orders eo ON UPPER(eo.symbol) = c.symbol ORDER BY eo.observed_at_ms, eo.broker_order_id"
    ).fetchall()
    for row in rows:
        order = reads.external_order_by_broker_order_id(conn, row["broker_order_id"])
        # An order that adds to what is held outside the books asks nothing here: it is offered
        # to an event as an outside lot.
        unsettled = None if order is None else unsettled_outside_order(conn, order)
        if order is None or unsettled is None:
            continue
        symbol = order.symbol.upper()
        key = booking_key(SALE_KEY_PREFIX, OUTSIDE_ORDER_BROKER, order.broker_order_id)
        ref = outside_order_hold_ref(order.broker_order_id)
        named = f"Order {order.broker_order_id} to {order.side.lower()} {order.qty:g} of {symbol}"
        reduces = _BOUGHT if order.side == "SELL" else _SOLD

        def hold(
            detail: str, *, _order: ExternalOrderResource = order, _ref: str = ref, _symbol: str = symbol,
            _bots: Sequence[str] = unsettled.against,
        ) -> None:
            # An order the owner has reviewed is held for no more. What it filled is still booked.
            if _order.acknowledged_at_ms is None:
                holds.add(OPTION_ORDER_NOT_OURS_REASON_CODE, _bots, ref=_ref, symbol=_symbol, date_ms=None, detail=detail)

        if unsettled.filled is None or not unsettled.ended:
            hold(unsettled.sentence)
            continue
        filled = unsettled.filled
        if order.filled_avg_price is None or order.filled_avg_price <= 0:
            hold(f"{named} filled {filled} and states no price for it.")
            continue
        contract = OptionContract.from_occ_symbol(symbol)
        try:
            outside = _outside_lot(conn, contract, leaving_out=frozenset({order.broker_order_id}))
        except OutsideFillUnknown:
            # The order that does not state its fill is held for by name, above.
            continue
        if outside is not None and (outside.quantity > 0) == (reduces == _BOUGHT):
            hold(
                f"{named} filled {filled}. Another outside order holds that contract too, so whose lot it closed "
                "is not known. It was not booked."
            )
            continue
        terms = contract_terms(conn, symbol)
        books = _bot_books(conn, repo.account_id, contract)
        lots = sorted(
            (lot for book in books.values() for lot in book.lots if (lot.quantity > 0) == (reduces == _BOUGHT)),
            key=lambda lot: (lot.opened_at_ms, lot.sequence, lot.owner),
        )
        if terms is None or sum(abs(lot.quantity) for lot in lots) < filled:
            hold(f"{named} filled {filled}, more than the bots' lots hold. It was not booked.")
            continue
        left, taken = filled, []
        for lot in lots:
            if not left:
                break
            take = min(abs(lot.quantity), left)
            left -= take
            taken.append(
                {
                    "owner": lot.owner, "sequence": lot.sequence, "opened_at_ms": lot.opened_at_ms, "contracts": take,
                    "option_quantity_change": -take if reduces == _BOUGHT else take, "share_quantity_change": 0,
                    "cash_change_cents": 0, "opened_by": books[lot.owner].opened_by.get(lot.sequence),
                }
            )
        booked_as_of_ms = max([order.observed_at_ms, *(row["opened_at_ms"] for row in taken)])
        for sid in sorted({row["owner"] for row in taken}):
            mine = [row for row in taken if row["owner"] == sid]
            record = _append_booking(
                repo, sid=sid, run_id=_run_of(conn, mine[0]["opened_by"]), key=key, ended_by=OptionEnding.SOLD_OUTSIDE,
                broker=OUTSIDE_ORDER_BROKER, source_id=order.broker_order_id, source_code="order", event_date_ms=None,
                position_change=-filled if reduces == _BOUGHT else filled, contract=contract,
                multiplier=terms.multiplier, contracts=sum(row["contracts"] for row in mine), held=reduces,
                close_price=Decimal(str(order.filled_avg_price)), booked_as_of_ms=booked_as_of_ms, simulated=False,
                allocation=taken, share_trade=None, simulation=None, notes=None,
            )
            if record is not None:
                booked.append((sid, record))
    return booked


# ── The batch function ───────────────────────────────────────────────────────


def book_option_events(repo: ClerkSqliteRepository, read: OptionEventRead | None) -> OptionEventRound:
    """Book what one read of the account's option events found, and hold by name for what could not be booked.

    ``read`` is None when the broker was not asked this time. The books' own
    record of outside orders is still acted on: an order in a contract a bot
    holds is held against that bot, and its fill is booked once it has ended.

    Everything from the first read of the books to the last append happens
    inside the repository's write fence. On books that never recorded an
    option contract one small table is read and nothing is written.
    """
    with repo.write_fence() as conn:
        if books_record_no_contract(conn):
            return OptionEventRound()
        holds = _Holds()
        booked = _book_outside_orders(repo, conn, holds)
        whole = read is not None and read.whole
        if read is not None:
            if not whole:
                holders = sorted({sid for sid, _symbol, _quantity in held_option_positions(conn)})
                if holders:
                    holds.add(
                        OPTION_EVENTS_NOT_READ_REASON_CODE, holders, ref="read", symbol=None, date_ms=None,
                        detail=read.failure or "The walk of the account's activities did not reach the window's start.",
                    )
            _hold_what_could_not_be_read(conn, read, holds)
            booked += _book_events(repo, conn, read, holds)
            if whole:
                _hold_what_expired_and_is_still_held(conn, read, holds, now_ms=repo.clock())
        # What the books' own record of outside orders holds for ends when that record no longer has it.
        held = _sync_holds(repo, conn, holds, codes=(OPTION_ORDER_NOT_OURS_REASON_CODE,), may_end=True)
        if read is not None:
            # A read that was not whole ends no hold: a row it did not reach is not a row that is gone.
            held += _sync_holds(repo, conn, holds, codes=(OPTION_EVENTS_NOT_READ_REASON_CODE, *_READ_HOLDS), may_end=whole)
        _sync_shares_held(repo, conn, booked)
        return OptionEventRound(
            booked=tuple(f"cmd:{facts.idempotency_key}" for _sid, facts in booked), held=tuple(held), read_whole=whole
        )


# ── The poll ─────────────────────────────────────────────────────────────────


@dataclass
class _PollState:
    """When one store's option events were last read. In memory only: a restart reads at once."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    read_at_ms: int | None = None
    # Whether that read was whole and left no hold standing: nothing is waited for.
    nothing_pending: bool = False


_POLL_STATES: weakref.WeakKeyDictionary[ClerkSqliteRepository, _PollState] = weakref.WeakKeyDictionary()
_POLL_STATES_GUARD = threading.Lock()


def _poll_state(repo: ClerkSqliteRepository) -> _PollState:
    with _POLL_STATES_GUARD:
        state = _POLL_STATES.get(repo)
        if state is None:
            state = _PollState()
            _POLL_STATES[repo] = state
        return state


def _due(state: _PollState, now_ms: int, *, asked_for: bool) -> bool:
    if state.read_at_ms is None or now_ms < state.read_at_ms:
        # Never read since this process opened the books, or the clock was set back: read.
        return True
    wait_ms = OPTION_EVENT_POLL_INTERVAL_MS if state.nothing_pending and not asked_for else OPTION_EVENT_POLL_RETRY_MS
    return now_ms - state.read_at_ms >= wait_ms


async def _read_events(repo: ClerkSqliteRepository, read: BrokerReadPort, *, after_ms: int) -> OptionEventRead:
    """Walk the account's option events from ``after_ms`` to the walk's end. Holds no lock."""
    events: dict[str, BrokerOptionEventActivity] = {}
    share_trades: dict[str, BrokerOptionShareTradeActivity] = {}
    unreadable: dict[str, BrokerOptionActivityUnreadable] = {}
    resume: BrokerOptionEventResume | None = None
    complete, failure = False, None
    try:
        for _page in range(MAX_OPTION_EVENT_PAGES):
            evidence = await read.read_option_event_activities(after_ms=after_ms, resume=resume)
            # An event and its share trade are each kept once, by id among their own kind.
            events.update({event.activity_id: event for event in evidence.events})
            share_trades.update({trade.activity_id: trade for trade in evidence.share_trades})
            unreadable.update({row.activity_id: row for row in evidence.unreadable})
            resume = evidence.resume
            if resume is None:
                complete = evidence.history_complete
                if not complete:
                    failure = "The broker's walk of the account's activities stopped before the window's start."
                break
        else:
            failure = f"The walk of the account's activities is longer than {MAX_OPTION_EVENT_PAGES} reads."
    except (BrokerError, ValueError) as exc:
        failure = f"The account's option activities could not be read: {exc}"
        logger.warning(
            "the account's option events could not be read",
            extra={"action": "option_events_read_failed", "account_id": repo.account_id, "error": str(exc)},
        )
    report = getattr(read, "simulated_option_settlements", None)
    settlements = report() if callable(report) else None
    return OptionEventRead(
        after_ms=after_ms,
        events=tuple(events.values()),
        share_trades=tuple(share_trades.values()),
        unreadable=tuple(unreadable.values()),
        history_complete=complete,
        failure=failure,
        simulated=settlements is not None,
        settlement_notes={} if settlements is None else settlements.notes,
        not_settled={} if settlements is None else settlements.not_settled,
    )


async def poll_option_events(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    off_loop: OffLoop = to_thread,
    asked_for: bool = False,
    broker_positions: Sequence[BrokerPosition] | None = None,
    act_on_the_books_between_reads: bool = False,
) -> OptionEventPoll:
    """Read the account's option events when a read is due, and book what was read.

    ``broker_positions`` is a pass's own reading of the account. When it
    shows a recorded contract's position differing from the books, the
    events are read at once, and at most once a minute; ``asked_for`` says
    the same without a reading. Given no reading, the positions are read
    here, after the events, and only when a held contract is past its last
    session.
    ``act_on_the_books_between_reads`` is the pass's too: when no read is
    due it still acts on the books' record of outside orders, which the pass
    has just brought up to date.

    The broker is read with no lock held. The books are read and written by
    :func:`book_option_events`, through ``off_loop``, which a caller that
    holds the Clerk's intake domain gives as its own hop. Nothing is read
    from the broker on books that hold no option position and no open
    option-event hold.
    """
    state = _poll_state(repo)
    now_ms = repo.clock()
    may_read = not state.lock.locked()
    due = may_read and _due(state, now_ms, asked_for=asked_for)
    if not due and broker_positions is None and not act_on_the_books_between_reads:
        return OptionEventPoll(read=False)
    if not await off_loop(lambda: option_work_on_the_books(repo)):
        if due:
            # Nothing an event could change: look again when the next read would be due.
            state.read_at_ms, state.nothing_pending = now_ms, True
        return OptionEventPoll(read=False)
    if not due and may_read and broker_positions is not None:
        # A pass's own snapshot shows a recorded contract's position differing from the books.
        differs = await off_loop(lambda: option_position_differs(repo, broker_positions))
        due = differs and _due(state, now_ms, asked_for=True)
    if not due:
        if not act_on_the_books_between_reads:
            return OptionEventPoll(read=False)
        return OptionEventPoll(read=False, round=await off_loop(lambda: book_option_events(repo, None)))
    async with state.lock:
        # Counted from the attempt: a read or a booking that raises is tried again a minute
        # later, never on the keeper's every round.
        state.read_at_ms, state.nothing_pending = now_ms, False
        after_ms = await off_loop(lambda: option_event_window_start_ms(repo))
        events = await _read_events(repo, read, after_ms=after_ms)
        positions = broker_positions
        expired_held = (
            positions is None
            and events.failure is None
            and await off_loop(lambda: _a_held_contract_is_past_its_last_session(repo))
        )
        if expired_held:
            try:
                positions = await read.list_positions()
            except BrokerError as exc:
                logger.warning(
                    "the account's positions could not be read beside its option events",
                    extra={"action": "option_event_positions_unread", "account_id": repo.account_id, "error": str(exc)},
                )
        if positions is not None:
            events = replace(events, broker_positions=broker_quantity_by_symbol(list(positions)))
        done = await off_loop(lambda: book_option_events(repo, events))
        state.read_at_ms = repo.clock()
        # While a hold stands something is still to come: a row, a share trade, a price. Look again sooner.
        state.nothing_pending = done.read_whole and not done.held
        return OptionEventPoll(read=True, round=done)


async def poll_option_events_in_a_pass(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    off_loop: OffLoop,
    broker_positions: Sequence[BrokerPosition],
) -> None:
    """The reconciliation pass's step: read and book option events, and never fail the pass doing it.

    The pass is what every bot's custody rests on, a stock bot's included.
    A fault in this step is logged with its stack and the pass goes on to
    its verdict: a position the step did not book still shows as drift
    there, and holds new entries. An error of the store itself is the
    pass's own to meet and is raised.
    """
    try:
        await poll_option_events(
            repo, read=read, off_loop=off_loop, broker_positions=broker_positions,
            act_on_the_books_between_reads=True,
        )
    except ClerkSqliteError:
        raise
    except Exception:
        logger.exception(
            "reading and booking option events failed; the pass goes on to its verdict",
            extra={"action": "option_events_step_failed", "account_id": repo.account_id},
        )


def book_outside_option_orders_in_a_pass(repo: ClerkSqliteRepository) -> None:
    """Act on the books' record of outside orders inside a pass's verdict, and never fail the verdict doing it."""
    try:
        book_option_events(repo, None)
    except ClerkSqliteError:
        raise
    except Exception:
        logger.exception(
            "acting on outside orders in option contracts failed; the pass goes on to its verdict",
            extra={"action": "option_outside_orders_step_failed", "account_id": repo.account_id},
        )


def _a_held_contract_is_past_its_last_session(repo: ClerkSqliteRepository) -> bool:
    now_ms = repo.clock()
    with repo.write_fence() as conn:
        return any(
            past_last_session(OptionContract.from_occ_symbol(symbol).expiry_date, now_ms)
            for _sid, symbol, _quantity in held_option_positions(conn)
        )


__all__ = [
    "MAX_OPTION_EVENT_PAGES",
    "OPTION_EVENT_HOLD_ACKNOWLEDGED",
    "OPTION_EVENT_POLL_INTERVAL_MS",
    "OPTION_EVENT_POLL_RETRY_MS",
    "OUTSIDE_THE_BOOKS",
    "OptionEventPoll",
    "OptionEventRead",
    "OptionEventRound",
    "acknowledge_option_event_hold",
    "book_option_events",
    "book_outside_option_orders_in_a_pass",
    "books_record_no_contract",
    "held_option_positions",
    "option_event_window_start_ms",
    "option_position_differs",
    "option_work_on_the_books",
    "outside_order_reduces_a_bots_contract",
    "poll_option_events",
    "poll_option_events_in_a_pass",
]
