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

**A simulated account settles its own.** A Dry Run has no broker to end an
option position. Its simulation does, as one step of its trade port
(``simulated_option_settlement``), and the poll calls that step before it
reads. What the step hands back is booked by the same batch function, as a
simulated execution that carries the simulation's record. A bot that
booking leaves holding nothing is cleared of its exit's notices at once: a
Dry Run has no pass on a timer to do it, and a failed scheduled close would
otherwise keep a running bot from its next entry.

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
one lot for outside orders the books recorded in that contract. Those are
the lots on the books, and the canonical rule chooses among them.

**A position that left the books at the owner's word.** The owner wrote it
off, which closed no lot, or ended it at their word after expiry, which
closed the lot at nothing. A row of the broker's that comes later still
counts those contracts. So what the lots on the books cannot meet of a row
is set against them: the row's count is met, every lot still on the books
is booked, and nothing is booked for the contracts that had left. An
expiry moves nothing more for them and holds nobody. An exercise or an
assignment holds that bot by name: its shares are real and on no bot's
books. The lots on the books always come first, so a lot that never was at
the broker takes no row from one that is. A bot whose lots and position
disagree in any other way is offered no lot, and is held by name when a
row comes for its contract.

**An event in a contract the books never recorded holds nobody.** No bot can
hold such a contract: it is the owner's own trade in the same account. The
event is logged and the batch goes on. An exercise's shares then show as
the pass's drift, as a hand trade's always have.

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

**The acknowledgement** ends the holds that nothing else ends, and no other
(``option_event_acknowledgement``). It is given for the notice exactly as
the owner read it, and only once a reconciliation pass made for it shows
the books agree with the broker in every contract and underlying the hold
names. It books nothing, so it never stands in for a booking. An expired
contract still on the books is never acknowledged away: the owner's ending
is its exit. The hold for an order the Clerk did not place is not either:
that order's own review is.

**An order the Clerk did not place, in a contract a bot holds.** It stands
against that bot and that contract alone. While it is unsettled (working, or
filled and not yet booked) no order of the bot's own to reduce that contract
is sent: it would be a second order on the same contracts. That is asked of
the books' record of the order where every reduction is admitted
(``uncertainty.decide_capability``), not of a hold. The owner's review of
the order releases it only while it has filled nothing. A fill not yet
booked stands in the way again, and a fill that cannot be booked is held
for by name, whatever was reviewed before it. Such a fill is never booked:
its hold says so, and that the contract can be ended at the owner's word
once it has expired. That ending's record names the order.

**The owner's ending.** A contract past its last session that nothing will
ever settle is ended at the owner's word (``option_owner_ending``):
:func:`end_option_at_the_owners_word` makes one poll, now, and hands the
batch function the request with the read. What the read and the simulation
brought is booked first, so a settlement that has come takes the ending's
place. A row the canonical rule refused by name, for its count or its side
against lots the books do hold, books nothing and never will while the
books stand. Nor does a row that is booked whole and counted fewer
contracts than the books held: what it left it will never take. So neither
stands in the ending's way, and the ending's record names each as what it
is. A real account's events are then still read for
some days, back to that contract's expiry date, and a late row counts the
ended contracts as a position that left the books at the owner's word. A
row the ending's record names as refused is held for by nobody once no bot
holds the contract: the owner ended it knowing of that row.

**A row in the way, and what is offered.** Any other row that may be the
contract's end stands in the ending's way: one the Clerk cannot read, one
not reported as executed, one that changed under a booked id, a share trade
no booked event explains. The expired contract's notice then says so and
names the row, its next step names no ending, and the page does not offer
one: a row in the way of the read that wrote the notice is in the way of
the read a press would make.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import weakref
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from app.broker.alpaca.clerk.account_authority import is_simulated_account_id
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.simulated_option_settlement import (
    OptionSettlementSimulation,
    OwnersWord,
    SimulatedOptionSettlements,
    SimulatedSettlement,
    plain,
)
from app.broker.alpaca.clerk.sqlite import option_event_acknowledgement as acknowledgement
from app.broker.alpaca.clerk.sqlite import option_owner_ending as owner_ending_rule
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.account_open_work import broker_quantity_by_symbol
from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.custody_subjects import BOT_SUBJECT_PREFIX
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyResolvedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import CommandExistingConflict, TransitionInput
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, to_thread
from app.broker.alpaca.clerk.sqlite.option_event_acknowledgement import (
    ComparedWithTheBroker,
    HoldAcknowledgementReceipt,
    HoldAcknowledgementRequest,
    OptionEventHoldNotAcknowledged,
    refuse_what_an_acknowledgement_does_not_end,
    symbols_a_hold_names,
)
from app.broker.alpaca.clerk.sqlite.option_event_books import (
    EVENT_KEY_PREFIX,
    OPTION_EVENT_HOLD_ACKNOWLEDGED,
    OUTSIDE_ORDER_BROKER,
    SALE_KEY_PREFIX,
    AcknowledgedFinding,
    OutsideFillUnknown,
    UnsettledOutsideOrder,
    acknowledged_findings,
    booking_key,
    bookings,
    books_record_no_contract,
    held_option_positions,
    outside_fill,
    outside_order_hold_ref,
    outside_order_reduces_a_bots_contract,
    outside_orders_in,
    recorded_option_endings,
    unsettled_outside_order,
    written_off_contracts,
)
from app.broker.alpaca.clerk.sqlite.option_event_folds import (
    OPTION_ENDING_SUMMARY_CODES,
    OPTION_EVENT_SHARES_EXECUTION_PREFIX,
    OPTION_POSITION_ENDED,
    OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED,
    OWNER_WORD_ROWS_BOOKED,
    OWNER_WORD_ROWS_NOT_BOOKED,
    REFUSALS_AN_ENDING_MAY_NAME,
    OptionEnding,
    OptionPositionEndedFacts,
    close_execution_prefix,
    closed_by_an_option_event,
)
from app.broker.alpaca.clerk.sqlite.option_owner_ending import (
    END_EXPIRED_OPTION_LABEL,
    OWNER_ENDING_SOURCE,
    OwnerEndingOutcome,
    OwnerEndingRequest,
    expired_contract_hold_ref,
    late_row_read_since_ms,
    owner_ended,
    owner_ending_key,
    owner_ending_not_possible_ref,
    owner_ending_offered_from_ms,
    recent_owner_endings,
    rows_an_owners_ending_named,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty, resolve_flat_exit_fences
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
    allocate_option_event,
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

# Why a read was not whole, in words that are the same from one failed read to the next.
_WALK_CUT_SHORT = "The broker's walk of the account's activities stopped before the window's start."
_READ_FAILED = "The account's option activities could not be read. The broker's own error is in the Clerk's log."

# The owner of the lot that stands for outside orders in a contract. It is no bot and no custody subject.
OUTSIDE_THE_BOOKS = "outside-the-books"
# How a booking's record names a lot of a position that left a bot's books at the owner's word,
# before the bot's name. It is no bot's lot now: nothing is booked for what an event takes of it.
LEFT_THE_BOOKS_PREFIX = "left-the-books:"
_ACK_OPERATOR_REF_PREFIX = "operator:"
_BOUGHT, _SOLD = "bought", "sold"
_ENDINGS: dict[OptionEventKind, OptionEnding] = {
    OptionEventKind.EXPIRED: OptionEnding.EXPIRED,
    OptionEventKind.EXERCISED: OptionEnding.EXERCISED,
    OptionEventKind.ASSIGNED: OptionEnding.ASSIGNED,
}
# The endings that move shares at the strike, each with the kind the canonical rule works them out by.
_MOVES_SHARES: dict[OptionEnding, OptionEventKind] = {
    OptionEnding.EXERCISED: OptionEventKind.EXERCISED,
    OptionEnding.ASSIGNED: OptionEventKind.ASSIGNED,
}
# The holds a whole read's findings raise and its silence ends.
_READ_HOLDS = tuple(
    code
    for code in OPTION_EVENT_HOLD_REASON_CODES
    if code not in (OPTION_EVENTS_NOT_READ_REASON_CODE, OPTION_SHARES_HELD_REASON_CODE, OPTION_ORDER_NOT_OURS_REASON_CODE)
)

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
# What the owner does about an order the Clerk did not place, by what the books know of it. While
# it works, or does not state what it filled:
_ORDER_WORKING_NEXT_STEP = (
    "Nothing to do while the order works: once it has ended, what it filled is booked against this bot. "
    "If the order is your own, cancel it at the broker. Or mark it reviewed: the bot's own orders then go on "
    "while it has filled nothing. Anything it fills is held for again until it is booked."
)
# Once it has ended with a fill that these books cannot book against the bot:
_FILL_NEVER_BOOKED_NEXT_STEP = (
    "Nothing will book this fill, and marking the order reviewed does not release it. The account stays held "
    "for as long as its positions differ from the broker's. Once the contract has expired and the session after "
    f'its last has closed, you can end the contract at your word with "{END_EXPIRED_OPTION_LABEL}": what it cost '
    "is then booked as a loss, and what the order sold it for stays in the account's cash, on no bot's books."
)
# What the owner does about an expired contract still on a real account's books, by what its
# notice says of it. One that may be ended at their word:
_EXPIRED_NEXT_STEP = (
    "Nothing to send. The Clerk books the contract's end from the broker's record of it, when one that can be "
    "booked is read. Once the next session has closed and the broker shows none of the contract, you can end it "
    f'at your word with "{END_EXPIRED_OPTION_LABEL}": it is then counted as worth nothing, and what it cost is '
    "booked as a loss."
)
# One with a row in the way that may be its end. It names no ending: none is made while the row stands.
_EXPIRED_ROW_IN_THE_WAY_NEXT_STEP = (
    "Nothing to send, and nothing on this page ends it while the record this notice names stands as it does. "
    "The Clerk reads the account's option events again by itself: if the record comes to read so that it can be "
    "booked whole, the Clerk books it and this notice changes. Look at the account at the broker for how the "
    "contract ended."
)
# One held sold.
_EXPIRED_SOLD_NEXT_STEP = (
    "Nothing to send. The Clerk books the contract's end from the broker's record of it, when one that can be "
    "booked is read. A sold contract is not ended at the owner's word."
)
_NEXT_STEPS: dict[str, str] = {
    OPTION_SHARES_HELD_REASON_CODE: "Stop the bot and flatten it to close the shares.",
    OPTION_ORDER_NOT_OURS_REASON_CODE: _ORDER_WORKING_NEXT_STEP,
    OPTION_EVENTS_NOT_READ_REASON_CODE: "Nothing to do: the Clerk reads them once more within a minute.",
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: _EXPIRED_NEXT_STEP,
}
# The same hold on a simulated account, which has no broker to send a record.
_SIMULATED_NEXT_STEPS: dict[str, str] = {
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: (
        "Nothing to send, and no price will come by itself. You can end the contract at your word with "
        f'"{END_EXPIRED_OPTION_LABEL}": it is then counted as worth nothing, and what it cost is booked as a loss.'
    ),
}
# What a hold means for the bot, where the family's own sentence would not be true.
_OPERATOR_IMPACTS: dict[str, str] = {
    OPTION_ORDER_NOT_OURS_REASON_CODE: (
        "This bot takes no new entries, and no order of its own to close that contract is sent until the order "
        "is settled, or reviewed while it has filled nothing: it would be a second order on the same contracts. "
        "Its other reductions stay available."
    ),
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: "This bot takes no new entries. No order can close an expired contract.",
}
# What the owner does about a hold only an acknowledgement ends, for a bot's hold and for the account's.
_ACKNOWLEDGE_IT = (
    "Compare {whose} positions with the broker's. When they agree, acknowledge the notice: the Clerk then "
    "compares them once more itself, and refuses while they differ."
)
if set(_HOLD_COPY) != set(OPTION_EVENT_HOLD_REASON_CODES):
    # A hold with no words of its own could not be raised at all.
    raise RuntimeError("every option-event hold needs its own words")
if not set(acknowledgement.ENDS_ANOTHER_WAY) <= set(_NEXT_STEPS):
    # A hold an acknowledgement does not end would send the owner to acknowledge it.
    raise RuntimeError("every option-event hold that ends another way says what the owner does about it")
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
    # What came of the owner's ending this run was handed. None when it was handed none.
    owner_ending: OwnerEndingOutcome | None = None


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
    """Whether the books hold anything an option event could change, or a hold a read could end.

    Or a contract the owner ended at their word a few days ago, on a real
    account: the broker's row for it may still come, and an exercise's or an
    assignment's must not go unread. Or shares an event left a bot, which
    the next round must hold it for.
    """
    with repo.write_fence() as conn:
        if books_record_no_contract(conn):
            return False
        return (
            bool(held_option_positions(conn))
            or bool(_active_holds(conn))
            or bool(recent_owner_endings(conn, since_ms=late_row_read_since_ms(repo.clock())))
            # Shares an event left that no hold stands for yet: a stop came between the booking and its hold.
            or bool(_shares_an_event_left(conn))
        )


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

    The date of the first fill in any contract a bot still holds, the date
    of the oldest row an open hold names, or the expiry date of a contract
    the owner ended at their word a few days ago, whichever is earliest.
    With none, today. Earlier than needed is safe: a row already booked is
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
        instants += recent_owner_endings(conn, since_ms=late_row_read_since_ms(repo.clock()))
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
    # The lots that make up the position the bot holds now.
    lots: tuple[HeldOptionLot, ...]
    # The order whose fill opened each lot, by the lot's sequence.
    opened_by: Mapping[int, str]
    # Each lot an order closed: when it was opened, when it was closed, and the side it was held on.
    closed: tuple[tuple[int, int, str], ...]
    # The lots of a position the owner wrote off. They stay open in the fills and are on the
    # books no more: no lot of the bot's, and still contracts a row of the broker's may count.
    written_off: tuple[HeldOptionLot, ...] = ()


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
    matches a fill to a lot again.

    **A position the owner wrote off** left the books and closed no lot, so
    the fills hold more than the position does. When the difference is
    exactly what was written off, the oldest lots are those contracts: a
    write-off takes what was held then, and whatever the bot bought since
    is newer. They are set apart as ``written_off``, and the rest are the
    lots of what the bot holds now. Closing a written-off lot for the bot
    would take its position past nothing, so none of them is ever its lot.

    A bot whose lots and position disagree in any other way is offered no
    lot at all: which lots are its own is not known.
    """
    symbol = contract.occ_symbol
    fifo = compute_fifo_pnl(records)
    open_lots = [lot for lot in fifo.open_lots if lot.symbol == symbol]
    signed = sum(int(lot.exact_qty) * (1 if lot.side is OrderSide.BUY else -1) for lot in open_lots)
    # A lot an event closed was closed by no order: only a close by an order can come between an
    # event and the reading of its row.
    sides_at: dict[int, set[OrderSide]] = {}
    for record in records:
        if not closed_by_an_option_event(record.event_key):
            sides_at.setdefault(record.filled_at_ms, set()).add(record.side)
    closed = tuple(
        (lot.opened_at_ms, lot.closed_at_ms, _SOLD if OrderSide.BUY in sides_at[lot.closed_at_ms] else _BOUGHT)
        for lot in fifo.closed_lots
        if lot.symbol == symbol and lot.closed_at_ms in sides_at
    )
    nothing_known = _BotBook(sid=sid, lots=(), opened_by={}, closed=closed)
    held_now = int(reads.position(conn, sid, symbol))
    # What the fills hold beyond the position: the contracts of a write-off, or books that disagree.
    beyond = signed - held_now
    if beyond and (
        beyond != written_off_contracts(conn, sid, symbol)
        or any((lot.side is OrderSide.BUY) != (beyond > 0) for lot in open_lots)
        or held_now * beyond < 0
    ):
        return nothing_known
    lots: list[HeldOptionLot] = []
    written_off: list[HeldOptionLot] = []
    opened_by: dict[int, str] = {}
    in_order = iter(records)
    still_written_off = abs(beyond)
    for lot in open_lots:
        opening = next(
            (fill for fill in in_order if fill.side is lot.side and fill.filled_at_ms == lot.opened_at_ms), None
        )
        if opening is None:
            return nothing_known
        sign = 1 if lot.side is OrderSide.BUY else -1
        quantity = int(lot.exact_qty)
        gone = min(quantity, still_written_off)
        still_written_off -= gone
        for part, into in ((gone, written_off), (quantity - gone, lots)):
            if part:
                into.append(
                    HeldOptionLot(
                        owner=sid, contract=contract, quantity=sign * part, opened_at_ms=lot.opened_at_ms,
                        sequence=opening.ledger_sequence,
                    )
                )
        opened_by[opening.ledger_sequence] = opening.order_ref
    return _BotBook(sid=sid, lots=tuple(lots), opened_by=opened_by, closed=closed, written_off=tuple(written_off))


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


def _lots_that_left_the_books(
    conn: sqlite3.Connection, books: Mapping[str, _BotBook], symbol: str
) -> list[HeldOptionLot]:
    """The lots of every position in ``symbol`` that left a bot's books at the owner's word, each under its bot.

    Two ways a position leaves the books with no order and no event: the
    owner wrote it off, which closed no lot, or ended it at their word once
    it had expired, which closed the lot at nothing. Either way the broker
    knows nothing of it, and a row of the broker's that comes later still
    counts those contracts.
    """
    gone = [lot for book in books.values() for lot in book.written_off]
    for sid, ended in owner_ended(conn, symbol).items():
        gone += [
            # Only a bought contract is ended at the owner's word.
            HeldOptionLot(
                owner=sid, contract=OptionContract.from_occ_symbol(symbol), quantity=row["contracts"],
                opened_at_ms=row["opened_at_ms"], sequence=row["sequence"],
            )
            for row in ended.allocation
            if row["owner"] == sid
        ]
    return gone


def _less_what_was_taken(
    lots: Sequence[HeldOptionLot], taken: Sequence[tuple[str, int, int]]
) -> list[HeldOptionLot]:
    """``lots`` without the contracts events took of them, each named as (bot, the lot's sequence, contracts).

    Taken from the lot of that name while it is still there. A record made
    earlier may name a lot the fills have closed since: its contracts are
    then taken from that bot's other lots, in the order given. So a
    position that left the books is never counted for two rows.
    """
    counts = [abs(lot.quantity) for lot in lots]
    owed: dict[str, int] = {}
    for owner, sequence, contracts in taken:
        for index, lot in enumerate(lots):
            if (lot.owner, lot.sequence) == (owner, sequence):
                found = min(counts[index], contracts)
                counts[index] -= found
                contracts -= found
        if contracts:
            owed[owner] = owed.get(owner, 0) + contracts
    for index, lot in enumerate(lots):
        found = min(counts[index], owed.get(lot.owner, 0))
        if found:
            counts[index] -= found
            owed[lot.owner] -= found
    return [
        replace(lot, quantity=count if lot.quantity > 0 else -count)
        for lot, count in zip(lots, counts, strict=True)
        if count
    ]


def _is_a_bots(owner: str) -> bool:
    """Whether a lot in a booking's record is one a bot held on its books when the event was matched."""
    return owner != OUTSIDE_THE_BOOKS and not owner.startswith(LEFT_THE_BOOKS_PREFIX)


@dataclass(frozen=True, slots=True)
class _Matched:
    """One event matched to lots: what it takes of the lots on the books, and of positions that left them."""

    # The lots on the books it takes, a bot's or an outside holding's, as the canonical rule chose them.
    on_the_books: tuple[LotAllocation, ...]
    # What it takes of each lot that left the books at the owner's word, with the contracts taken.
    left_the_books: tuple[tuple[HeldOptionLot, int], ...] = ()


def _match(
    event: BrokerOptionEventActivity, *, multiplier: int, lots: Sequence[HeldOptionLot],
    left_the_books: Sequence[HeldOptionLot],
) -> _Matched:
    """Match one event to the lots on the books and, for what they cannot meet, to positions that left them.

    The lots on the books come first, chosen by the canonical rule
    (``app.options.event_allocation``): booking them is what leaves the
    books equal to the broker's position. Only the contracts they cannot
    meet are set against a position that left the books at the owner's
    word, on the side the event ends. So one bot's write-off never stops
    another bot's lot from being booked, and a lot that never was at the
    broker never takes a row from a lot that is.

    Raises the rule's own refusal when even those do not meet the row.
    """
    try:
        return _Matched(on_the_books=allocation_of(event, multiplier=multiplier, lots=lots).lots)
    except OptionEventRefused as refused:
        short = {
            OptionEventRefusal.CONTRACT_NOT_HELD: event.contracts,
            OptionEventRefusal.WRONG_SIDE: event.contracts,
            OptionEventRefusal.MORE_CONTRACTS_THAN_HELD: refused.unmatched_contracts,
        }.get(refused.reason)
        if short is None:
            raise
        expired = event.kind is OptionEventKind.EXPIRED
        event_date = et_date_at_ms(event.activity_date_ms)
        open_then = [lot for lot in left_the_books if et_date_at_ms(lot.opened_at_ms) <= event_date]
        ends_bought = event.position_change < 0
        on_side = [lot for lot in open_then if (lot.quantity > 0) == ends_bought]
        if expired and not on_side:
            # An expiry's sign for a sold position is an inference: held on one side only, it means that side.
            on_side = open_then
        # In the rule's own order: an exercise or an assignment the oldest first, an expiry the newest.
        on_side.sort(key=lambda lot: (lot.opened_at_ms, lot.sequence, lot.owner), reverse=expired)
        if sum(abs(lot.quantity) for lot in on_side) < short:
            raise
        on_the_books: tuple[LotAllocation, ...] = ()
        if short < event.contracts:
            met = event.contracts - short
            on_the_books = allocate_option_event(
                kind=event.kind, contract=event.contract, position_change=-met if ends_bought else met,
                event_date_ms=event.activity_date_ms, multiplier=multiplier, lots=lots,
            ).lots
        taken: list[tuple[HeldOptionLot, int]] = []
        for lot in on_side:
            if not short:
                break
            take = min(abs(lot.quantity), short)
            short -= take
            taken.append((lot, take))
        return _Matched(on_the_books=on_the_books, left_the_books=tuple(taken))


# ── Holds ────────────────────────────────────────────────────────────────────


class _Holds:
    """The holds one run found, by bot and reason. A bot of None is the account."""

    def __init__(self) -> None:
        self._found: dict[tuple[str | None, str], list[OptionEventHoldItem]] = {}
        # What the owner does about one thing found, where that depends on the state it was found in.
        self._next_steps: dict[tuple[str | None, str, str], str] = {}

    def add(
        self, reason_code: str, bots: Sequence[str], *, ref: str, symbol: str | None, date_ms: int | None, detail: str,
        next_step: str | None = None,
    ) -> None:
        """One thing found for these bots, or for the account when there are none.

        ``next_step`` is what the owner does about it in the state it was
        found in, for a hold whose next step is not one sentence for every
        state. Left out, the hold's own sentence stands.
        """
        item = OptionEventHoldItem(ref=ref, symbol=symbol, date_ms=date_ms, detail=detail)
        for sid in bots or (None,):
            items = self._found.setdefault((sid, reason_code), [])
            if item not in items:
                items.append(item)
            if next_step is not None:
                self._next_steps[(sid, reason_code, ref)] = next_step

    def next_steps(self, sid: str | None, reason_code: str, items: Sequence[OptionEventHoldItem]) -> list[str]:
        """What the owner does about these things of one hold, each said once, in the order they were found."""
        steps = [self._next_steps.get((sid, reason_code, item.ref)) for item in items]
        return list(dict.fromkeys(step for step in steps if step is not None))

    def forget(self, sid: str, refs: Collection[str]) -> None:
        """Drop what was found for one bot under these references: its contract has ended since it was found."""
        for (bot, reason_code), items in list(self._found.items()):
            if bot != sid:
                continue
            kept = [item for item in items if item.ref not in refs]
            if kept:
                self._found[(bot, reason_code)] = kept
            else:
                del self._found[(bot, reason_code)]

    def items(self) -> Mapping[tuple[str | None, str], list[OptionEventHoldItem]]:
        return self._found


def _next_step(reason_code: str, *, simulated: bool, for_the_account: bool, found: Sequence[str] = ()) -> str:
    """What the owner does about a hold.

    A simulated account has no broker, so some of it reads otherwise. A hold
    of the account's own names no bot, and its words name none. ``found``
    is what the run that found the hold's things says of the state each is
    in (``_Holds.add``): it stands in the place of the hold's own sentence.
    """
    if simulated and reason_code in _SIMULATED_NEXT_STEPS:
        return _SIMULATED_NEXT_STEPS[reason_code]
    if found:
        return " ".join(found)
    return _NEXT_STEPS.get(
        reason_code, _ACKNOWLEDGE_IT.format(whose="the account's" if for_the_account else "this bot's")
    )


def _raise_hold(
    repo: ClerkSqliteRepository, sid: str | None, reason_code: str, items: Sequence[OptionEventHoldItem],
    *, next_steps: Sequence[str] = (),
) -> None:
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
        next_step=_next_step(
            reason_code, simulated=is_simulated_account_id(repo.account_id), for_the_account=sid is None,
            found=next_steps,
        ),
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
    repo: ClerkSqliteRepository,
    *,
    request: HoldAcknowledgementRequest,
    operator: str,
    compared: ComparedWithTheBroker,
) -> HoldAcknowledgementReceipt:
    """The owner has looked at what one option-event hold names: end it, and do not raise it for those things again.

    ``compared`` is a reconciliation pass made for this request. Every check
    and the record are made inside the repository's write fence, so the hold
    that is checked is the hold that ends (``option_event_acknowledgement``
    says what is checked and why). Refused with ``OptionEventHoldNotAcknowledged``,
    and nothing written, unless all of it holds.

    The acknowledgement is the hold's own resolution, hash-chained, naming
    the owner, each thing the hold named and what was wrong with it. A later
    read that still has those rows raises nothing for them. A new row, or a
    new fault in the same row, raises the hold again.
    """
    sid, reason_code = request.strategy_instance_id, request.reason_code
    refuse_what_an_acknowledgement_does_not_end(reason_code)
    if not operator.strip():
        raise OptionEventHoldNotAcknowledged(
            acknowledgement.NO_OPERATOR, "An acknowledgement names who made it, and this one names nobody."
        )
    with repo.write_fence() as conn:
        active = reads.active_uncertainty(
            conn, scope="ACCOUNT_CLERK" if sid is None else "CUSTODY_SUBJECT", reason_code=reason_code,
            strategy_instance_id=sid,
        )
        if active is None:
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.NOT_OPEN, "No such notice is open. It may have ended since it was read."
            )
        cause = OptionEventHoldCause.from_mapping(json.loads(active["facts_json"])["cause_facts"])
        named = tuple(item.ref for item in cause.items)
        if (active["uncertainty_id"], int(active["observed_at_ms"])) != (request.uncertainty_id, request.observed_at_ms):
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.NOTICE_CHANGED,
                "The notice has changed since it was read. Read it again before acknowledging it.",
                item_refs=named,
            )
        if set(named) != set(request.item_refs):
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.NAMES_OTHER_ITEMS,
                "The notice names other things than the acknowledgement does. Read it again before acknowledging it.",
                item_refs=named,
            )
        # The pass's own fence: its comparison is of the books as they stood then, and of no later books.
        if compared.verdict == "stale" or compared.through_sequence is None:
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.ACCOUNT_NOT_COMPARED,
                "The account could not be compared with the broker just now, so nothing shows the books agree "
                "with it. Try again once the broker can be read.",
            )
        if reads.last_custody_sequence(conn) != compared.through_sequence:
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.BOOKS_MOVED,
                "The books changed after the account was compared with the broker, so the comparison is older than "
                "the books. While a bot's exit is being retried the pass itself writes a record after it compares: "
                "wait until that retry has ended, then acknowledge the notice. Otherwise try again.",
            )
        registered = None if sid is None else reads.strategy_instance(conn, sid)
        differ = sorted(
            {*compared.drifted_symbols, *compared.indeterminate_symbols}
            & set(symbols_a_hold_names(cause.items, registered_symbol=(registered or {}).get("symbol")))
        )
        if differ:
            raise OptionEventHoldNotAcknowledged(
                acknowledgement.POSITIONS_DIFFER,
                f"The broker's position differs from the books' in {', '.join(differ)}. An acknowledgement books "
                "nothing, so it would leave them apart. Bring them together first, then acknowledge the notice. "
                "If the pass has just booked an order's fill there, it compared before that booking: acknowledge "
                "again after the next pass.",
                symbols=differ,
            )
        _resolve_hold(
            repo, sid, reason_code, resolution_kind="OPERATOR_ACKNOWLEDGED",
            summary_code=OPTION_EVENT_HOLD_ACKNOWLEDGED,
            evidence_refs=(
                f"{_ACK_OPERATOR_REF_PREFIX}{operator.strip()}",
                *(AcknowledgedFinding.of(sid, reason_code, item).evidence_ref for item in cause.items),
            ),
        )
        recorded = conn.execute(
            "SELECT sequence, recorded_at_ms FROM custody_transitions ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    logger.warning(
        "an option-event hold was acknowledged",
        extra={
            "action": "option_event_hold_acknowledged", "account_id": repo.account_id, "strategy_instance_id": sid,
            "reason_code": reason_code, "operator": operator.strip(), "item_refs": list(named),
        },
    )
    return HoldAcknowledgementReceipt(
        strategy_instance_id=sid, reason_code=reason_code, item_refs=named, operator=operator.strip(),
        sequence=int(recorded["sequence"]), recorded_at_ms=int(recorded["recorded_at_ms"]),
    )


async def acknowledge_option_event_hold_after_a_pass(
    repo: ClerkSqliteRepository,
    *,
    reconcile: Callable[[], Awaitable[ComparedWithTheBroker]],
    off_loop: OffLoop,
    request: HoldAcknowledgementRequest,
    operator: str,
) -> HoldAcknowledgementReceipt:
    """Acknowledge one option-event hold on a reconciliation pass made for the request.

    ``reconcile`` runs one whole pass of the account, now. It reads the
    broker with no lock of the Clerk's held, reads and books the account's
    option events as every pass does, and compares positions. A hold that an
    acknowledgement does not end is refused before any pass is made.
    """
    refuse_what_an_acknowledgement_does_not_end(request.reason_code)
    compared = await reconcile()
    return await off_loop(
        lambda: acknowledge_option_event_hold(repo, request=request, operator=operator, compared=compared)
    )


def _sync_holds(
    repo: ClerkSqliteRepository, conn: sqlite3.Connection, holds: _Holds, *, codes: Sequence[str], may_end: bool
) -> list[tuple[str | None, str]]:
    """Raise each hold found, and end each open one of ``codes`` that was not, when this run may."""
    acknowledged = acknowledged_findings(conn)
    wanted: dict[tuple[str | None, str], list[OptionEventHoldItem]] = {}
    for (sid, reason_code), items in holds.items().items():
        if reason_code not in codes:
            continue
        # An acknowledgement covers a thing and what was wrong with it then, and nothing found since.
        standing = [
            item for item in items if AcknowledgedFinding.of(sid, reason_code, item).evidence_ref not in acknowledged
        ]
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
        _raise_hold(repo, sid, reason_code, items, next_steps=holds.next_steps(sid, reason_code, items))
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
    owner_word: dict[str, Any] | None = None,
    broker_order_id: str | None = None,
) -> OptionPositionEndedFacts | None:
    """Book one bot's part of one event, once. The record when this call booked it; None when it was booked before.

    ``broker_order_id`` is the outside order a sale books, and None for
    every other ending. The record names it, and that is what takes the
    order out of the money reads' count of outside orders: its one
    execution is then counted once, on the bot.
    """
    idempotency_key = f"{key}{sid}"
    command_id = f"cmd:{idempotency_key}"
    symbol = contract.occ_symbol
    # One spelling of a price, whatever arithmetic gave it: "0", "5.25", never "0.00" or "5E+2".
    closed_at = plain(close_price)
    payload_hash = hashlib.sha256(
        canonicalize(
            {
                "ended_by": ended_by.value, "broker": broker, "source_id": source_id, "source_code": source_code,
                "event_date_ms": event_date_ms, "position_change": position_change, "contract": symbol,
                "bot": sid, "contracts": contracts, "held": held, "close_price": closed_at,
            }
        ).encode("utf-8")
    ).hexdigest()
    shares: dict[str, Any] | None = None
    if ended_by in _MOVES_SHARES:
        moved = expected_share_trade(_MOVES_SHARES[ended_by], contract, contracts=contracts, multiplier=multiplier)
        if moved is not None:
            shares = {
                "symbol": moved.root, "quantity": moved.shares, "price": str(moved.price),
                "cash_cents": moved.cash_cents, "order_ref": _order_ref(sid, idempotency_key, "shares"),
                "execution_id": f"{OPTION_EVENT_SHARES_EXECUTION_PREFIX}{key.removeprefix(EVENT_KEY_PREFIX)}{sid}",
            }
    # The fill's name says whether the fee model prices it: a close at nothing is no trade, a
    # sale is one, a simulation's cash settlement among them.
    close_prefix = close_execution_prefix(ended_by)
    close_execution_id = (
        f"{SALE_KEY_PREFIX}fill:{key.removeprefix(SALE_KEY_PREFIX)}{sid}"
        if close_prefix is None
        else f"{close_prefix}{key.removeprefix(EVENT_KEY_PREFIX)}{sid}"
    )
    facts = OptionPositionEndedFacts(
        idempotency_key=idempotency_key, payload_hash=payload_hash, ended_by=ended_by.value, broker=broker,
        source_id=source_id, source_code=source_code, event_date_ms=event_date_ms, position_change=position_change,
        contract_symbol=symbol, multiplier=multiplier, contracts=contracts, held=held, close_price=closed_at,
        booked_as_of_ms=booked_as_of_ms, option_order_ref=_order_ref(sid, idempotency_key, "close"),
        close_execution_id=close_execution_id, simulated=simulated, allocation=allocation, shares=shares,
        share_trade=share_trade, simulation=simulation, notes=notes or None, owner_word=owner_word,
    )

    def build_transition() -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=sid,
            run_id=run_id,
            command_id=command_id,
            effect_operation_id=f"effect:{idempotency_key}",
            order_ref=facts.option_order_ref,
            broker_order_id=broker_order_id,
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


def _ending_of(event: BrokerOptionEventActivity, settlement: SimulatedSettlement | None) -> OptionEnding:
    """How the position an event ends is recorded as having ended.

    A broker's activity is recorded as what it is. A simulation's
    settlement travels as an expiry, so that the one rule that chooses lots
    chooses these too, and is recorded as what the simulation's own record
    says it was: an expiry at nothing, or a settlement in cash.
    """
    return _ENDINGS[event.kind] if settlement is None else OptionEnding(settlement.record.outcome.value)


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


def _bots_whose_lots_cannot_be_offered(
    conn: sqlite3.Connection, books: Mapping[str, _BotBook], symbol: str
) -> dict[str, tuple[int, int]]:
    """Each bot that holds the contract now and is offered no lot, with what it holds and what it wrote off.

    A bot's lots are offered while they add up to its position, or exceed
    it by exactly what the owner wrote off (``_bot_book``). When they do
    neither, which lots are the bot's own is not known: it is offered
    none, and a row for it cannot be matched to its lots.
    """
    return {
        sid: (int(held_now), written_off_contracts(conn, sid, symbol))
        for sid, book in books.items()
        if (held_now := reads.position(conn, sid, symbol)) and not book.lots
    }


@dataclass(frozen=True, slots=True)
class _SharesAsReported:
    """The broker's own share trade for one exercise or assignment, set against the contract's terms."""

    # None when no share trade was read for the event.
    trade: BrokerOptionShareTradeActivity | None = None
    # Where the row differs from the terms, in words. Empty when it agrees.
    differences: tuple[str, ...] = ()
    # Whether the shares, the cash and the symbol are the contract's and only the printed price
    # is not. The books count the strike, so nothing they hold is in doubt, and nobody is held:
    # the difference is recorded on the booking. Alpaca's own documented exercise prints such
    # a price.
    only_the_price: bool = False


_NO_SHARE_TRADE = _SharesAsReported()


def _pair_share_trades(
    events: Sequence[BrokerOptionEventActivity],
    share_trades: Sequence[BrokerOptionShareTradeActivity],
    multiplier_of: Mapping[str, int],
) -> tuple[dict[str, _SharesAsReported], list[BrokerOptionShareTradeActivity]]:
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
    paired: dict[str, _SharesAsReported] = {}
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
                paired[event.activity_id] = _SharesAsReported(
                    trade=trade, differences=differences,
                    # The same comparison, with the row's price read as the strike: nothing else differs.
                    only_the_price=bool(differences)
                    and not share_trade_disagreements(terms, trade.model_copy(update={"price": terms.price})),
                )
                left.remove(trade)
                break
    for event in events:
        if event.activity_id in expected and event.activity_id not in paired:
            paired[event.activity_id] = _NO_SHARE_TRADE
    return paired, left


@dataclass(frozen=True, slots=True)
class _RowSetAside:
    """One executed row of a recorded contract that will book nothing more while the books stand.

    An owner's ending is not kept waiting for such a row, and its record
    names it. There are two kinds.

    A row the canonical rule refused for its count or its side against lots
    the books hold, under one of the names an owner's ending may be made
    beside (``REFUSALS_AN_ENDING_MAY_NAME``). Nothing of it was booked.

    A row that is booked whole, as it reads now: it reads as it was booked,
    and every bot its record names has its record. Whatever it left on the
    books it will never take.
    """

    activity_id: str
    symbol: str
    # The refusal's own name, which is also the code of the hold raised for it. None for a row booked whole.
    refused_as: str | None
    # The rule's own words for a refused row. For a booked one, what it counted and for whom.
    detail: str
    # The broker's share trade that is the same row's other half, when one was read. For a booked
    # row, only one that agrees with it or differs in its printed price alone: a share trade that
    # states other shares may be a second exercise.
    share_trade_id: str | None = None

    @property
    def ref(self) -> str:
        return f"option-activity:{self.activity_id}"

    @property
    def booked(self) -> bool:
        return self.refused_as is None


def _how_a_row_was_booked(event: BrokerOptionEventActivity, allocation_rows: Sequence[Mapping[str, Any]]) -> str:
    """What a booked row counted and for whom, in the words an owner's ending's record and a notice carry."""
    counted: dict[str, int] = {}
    for row in allocation_rows:
        counted[row["owner"]] = counted.get(row["owner"], 0) + row["contracts"]
    for_whom = []
    for owner, contracts in sorted(counted.items()):
        if owner == OUTSIDE_THE_BOOKS:
            for_whom.append(f"{contracts} held by orders the Clerk did not place")
        elif owner.startswith(LEFT_THE_BOOKS_PREFIX):
            for_whom.append(
                f"{contracts} that had left {owner.removeprefix(LEFT_THE_BOOKS_PREFIX)}'s books at the owner's word"
            )
        else:
            for_whom.append(f"{contracts} for {owner}")
    return (
        f"{event.contracts} of {event.contract.occ_symbol} were {event.kind.value} on "
        f"{et_date_at_ms(event.activity_date_ms).isoformat()}, and that is booked: {', '.join(for_whom)}."
    )


def _book_events(
    repo: ClerkSqliteRepository,
    conn: sqlite3.Connection,
    read: OptionEventRead,
    holds: _Holds,
    *,
    simulated: Mapping[str, SimulatedSettlement],
    rows_aside: dict[str, _RowSetAside],
) -> list[tuple[str, OptionPositionEndedFacts]]:
    """Book every executed event of one read that is not booked yet, in one fixed order. Each bot with its new record.

    ``simulated`` is what a simulation's settlement step handed back, by
    activity id. Each is booked beside the read's own rows, by the same
    rule, as a simulated execution that carries the simulation's record.

    ``rows_aside`` is filled here, by activity id, with each of the read's
    own rows that will book nothing more while the books stand
    (``_RowSetAside``): one the canonical rule refused this run under a
    name an owner's ending may be made beside, and one that is booked
    whole as it reads now. That is decided here, where the row is set
    against its record, and nowhere else. A row that changed under a
    booked id is no such row, nor is one a bot its record names could not
    be booked for.
    """
    account_id = repo.account_id
    executed = [
        *(event for event in read.events if event.status is OptionActivityStatus.EXECUTED),
        *(settlement.event for settlement in simulated.values()),
    ]
    multiplier_of = {}
    for event in executed:
        terms = contract_terms(conn, event.contract.occ_symbol)
        if terms is not None:
            multiplier_of[event.contract.occ_symbol] = terms.multiplier
    paired, unpaired = _pair_share_trades(in_allocation_order(executed), read.share_trades, multiplier_of)
    booked: list[tuple[str, OptionPositionEndedFacts]] = []
    # What outside orders hold of each contract, less what this run's events have taken of it.
    outside_left: dict[str, int] = {}
    # The lots of each contract that left the books at the owner's word, less what this run's events took of them.
    left_the_books: dict[str, list[HeldOptionLot]] = {}

    def still_left(contract: OptionContract, books: Mapping[str, _BotBook] | None = None) -> list[HeldOptionLot]:
        if contract.occ_symbol not in left_the_books:
            left_the_books[contract.occ_symbol] = _lots_that_left_the_books(
                conn, _bot_books(conn, account_id, contract) if books is None else books, contract.occ_symbol
            )
        return left_the_books[contract.occ_symbol]

    def take_of_what_left(contract: OptionContract, taken: Sequence[tuple[str, int, int]]) -> None:
        left_the_books[contract.occ_symbol] = _less_what_was_taken(still_left(contract), taken)

    for event in in_allocation_order(executed):
        contract, symbol = event.contract, event.contract.occ_symbol
        ref = f"option-activity:{event.activity_id}"
        key = booking_key(EVENT_KEY_PREFIX, event.broker, event.activity_id)

        def hold(reason_code: str, bots: Sequence[str], detail: str, *, _ref: str = ref, _event: BrokerOptionEventActivity = event) -> None:
            holds.add(reason_code, bots, ref=_ref, symbol=_event.contract.occ_symbol, date_ms=_event.activity_date_ms, detail=detail)

        settlement = simulated.get(event.activity_id)
        ending = _ending_of(event, settlement)
        recorded = bookings(conn, key)
        if recorded:
            first = next(iter(recorded.values()))
            owners = sorted({row["owner"] for row in first.allocation if _is_a_bots(row["owner"])})
            if (first.ended_by, first.contract_symbol, first.position_change, first.event_date_ms) != (
                ending.value, symbol, event.position_change, event.activity_date_ms
            ):
                hold(
                    OPTION_EVENT_ROW_CHANGED_REASON_CODE, owners,
                    f"Activity {event.activity_id} was booked as {first.position_change:+d} of {first.contract_symbol} "
                    f"{first.ended_by}; it now reads {event.position_change:+d} of {symbol} {ending.value}.",
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
            # So is what it took of a position that had left the books at the owner's word.
            taken_of_what_left = [
                (row["owner"].removeprefix(LEFT_THE_BOOKS_PREFIX), row["sequence"], row["contracts"])
                for row in allocation_rows
                if row["owner"].startswith(LEFT_THE_BOOKS_PREFIX)
            ]
            if taken_of_what_left:
                take_of_what_left(contract, taken_of_what_left)
        else:
            terms = contract_terms(conn, symbol)
            try:
                outside = _outside_lot(conn, contract)
            except OutsideFillUnknown as unknown:
                # Whose lot the event took is not known while an outside order in the contract does not
                # state what it filled. The bots holding it are held for that order by name, whatever
                # the owner reviewed (``_book_outside_orders``).
                logger.warning(
                    "an option event is not booked while an outside order in its contract does not state what it filled",
                    extra={
                        "action": "option_event_waits_for_an_outside_fill", "account_id": account_id,
                        "symbol": symbol, "activity_id": event.activity_id, "broker_order_id": str(unknown),
                    },
                )
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
                matched = _match(
                    event, multiplier=100 if terms is None else terms.multiplier, lots=lots,
                    left_the_books=[] if terms is None else still_left(contract, books),
                )
            except OptionEventRefused as refused:
                if terms is None:
                    # No bot can hold a contract the books never recorded: this is the owner's own
                    # trade in the same account. It concerns no bot's books, so nobody is held. The
                    # shares of an exercise show as the pass's drift, as a hand trade's always have.
                    logger.info(
                        "an option event in a contract the books never recorded is no bot's; nothing is booked",
                        extra={
                            "action": "option_event_in_a_contract_never_recorded", "account_id": account_id,
                            "symbol": symbol, "activity_id": event.activity_id, "kind": event.kind.value,
                            "position_change": event.position_change, "refused": refused.reason.value,
                        },
                    )
                    continue
                if refused.reason.value in REFUSALS_AN_ENDING_MAY_NAME:
                    # Refused for its count or its side against lots the books do hold: nothing of
                    # it is booked, now or while the books stand.
                    its_shares = paired.get(event.activity_id, _NO_SHARE_TRADE).trade
                    rows_aside[event.activity_id] = _RowSetAside(
                        activity_id=event.activity_id, symbol=symbol, refused_as=refused.reason.value,
                        detail=str(refused), share_trade_id=None if its_shares is None else its_shares.activity_id,
                    )
                unmatched = (
                    _bots_whose_lots_cannot_be_offered(conn, books, symbol)
                    if refused.reason is OptionEventRefusal.CONTRACT_NOT_HELD
                    else {}
                )
                # The rule was offered no lot, and a bot holds the contract: its lots and its
                # position disagree. Each such bot is held by name, with what is true of it.
                for sid, (held_now, written_off) in unmatched.items():
                    hold(
                        OPTION_EVENT_CONTRACT_NOT_HELD_REASON_CODE, [sid],
                        f"{sid} holds {held_now:+d} of {symbol} and wrote off {written_off:+d} of it earlier, and its "
                        f"lots add up to neither, so this row, {event.kind.value} {event.position_change:+d}, "
                        "cannot be matched to them. Nothing was booked.",
                    )
                holders = _holders(conn, symbol)
                if not unmatched and not holders and ref in rows_an_owners_ending_named(conn, symbol):
                    # The owner ended the contract knowing of this row: the ending's record names
                    # it. No bot holds the contract now, so the row is held for by nobody.
                    logger.info(
                        "an option event an owner's ending named is still refused; nobody is held for it",
                        extra={
                            "action": "option_event_named_by_an_owners_ending", "account_id": account_id,
                            "symbol": symbol, "activity_id": event.activity_id, "refused": refused.reason.value,
                        },
                    )
                elif not unmatched:
                    hold(refused.reason.value, holders, str(refused))
                continue
            taken_by_bot = [lot for lot in matched.on_the_books if lot.owner != OUTSIDE_THE_BOOKS]
            owners = sorted({lot.owner for lot in taken_by_bot})
            if outside is not None:
                outside_left[symbol] += sum(
                    lot.option_quantity_change for lot in matched.on_the_books if lot.owner == OUTSIDE_THE_BOOKS
                )
            if matched.left_the_books:
                take_of_what_left(
                    contract, [(lot.owner, lot.sequence, contracts) for lot, contracts in matched.left_the_books]
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
            held = _BOUGHT if any(lot.option_quantity_change < 0 for lot in matched.on_the_books) else _SOLD
            if matched.on_the_books and event.kind is OptionEventKind.EXPIRED and held != _signed_side(event.position_change):
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
            reported = paired.get(event.activity_id, _NO_SHARE_TRADE)
            share_trade_note = None
            if event.kind is not OptionEventKind.EXPIRED:
                share_trade_note = {
                    "activity_id": None if reported.trade is None else reported.trade.activity_id,
                    "agrees": reported.trade is not None and not reported.differences,
                    "differences": list(reported.differences),
                }
                if reported.only_the_price:
                    share_trade_note["only_the_price_differs"] = True
            allocation_rows = _allocation_rows(matched.on_the_books)
            booked_as_of_ms = max(
                [last_session_close_ms(event_date) - 1, *(lot.opened_at_ms for lot in taken_by_bot)]
            )
            opened_by = {sequence: order_ref for book in books.values() for sequence, order_ref in book.opened_by.items()}
            for row in allocation_rows:
                row["opened_by"] = opened_by.get(row["sequence"]) if row["owner"] != OUTSIDE_THE_BOOKS else None
            # What the row counts of a position that left the books at the owner's word closes no
            # lot: the write-off took it off the books, or the owner's ending closed it at nothing.
            gone_by_bot: dict[str, int] = {}
            for lot, contracts in matched.left_the_books:
                gone_by_bot[lot.owner] = gone_by_bot.get(lot.owner, 0) + contracts
                allocation_rows.append({
                    "owner": f"{LEFT_THE_BOOKS_PREFIX}{lot.owner}", "sequence": lot.sequence,
                    "opened_at_ms": lot.opened_at_ms, "contracts": contracts, "option_quantity_change": 0,
                    "share_quantity_change": 0, "cash_change_cents": 0, "opened_by": None,
                })
            if gone_by_bot and event.kind is OptionEventKind.EXPIRED:
                # An expiry moves nothing more for them: nothing is booked and nobody is held.
                logger.info(
                    "an option expiry counts a position that left the books at the owner's word; nothing is booked for it",
                    extra={
                        "action": "option_event_already_written_off", "account_id": account_id,
                        "symbol": symbol, "activity_id": event.activity_id, "bots": sorted(gone_by_bot),
                    },
                )
            elif gone_by_bot:
                # An exercise or an assignment moved shares and cash, and they are real.
                notes += [
                    {
                        "code": OPTION_EVENT_CONTRACT_NOT_HELD_REASON_CODE, "bot": sid,
                        "detail": (
                            f"{contracts} of {symbol} left {sid}'s books at the owner's word before this row was "
                            f"read. The row says they were {event.kind.value}: the shares and the cash that "
                            "moved are on no bot's books."
                        ),
                    }
                    for sid, contracts in sorted(gone_by_bot.items())
                ]

        # Book each bot the record names and the books do not yet show. The record, never a second matching.
        multiplier = multiplier_of.get(symbol)
        # Whether the broker's row is booked whole as it reads now: every bot the record names has its record.
        booked_whole = bool(owners) and settlement is None
        for sid in owners:
            if sid in recorded:
                continue
            if multiplier is None:
                booked_whole = False
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
                booked_whole = False
                continue
            try:
                record = _append_booking(
                    repo, sid=sid, run_id=_run_of(conn, rows[0].get("opened_by")), key=key,
                    ended_by=ending, broker=event.broker, source_id=event.activity_id,
                    source_code=event.activity_type, event_date_ms=event.activity_date_ms,
                    position_change=event.position_change, contract=contract, multiplier=multiplier,
                    contracts=contracts, held=held,
                    # An event closes a lot at nothing. A simulation's cash settlement closes it at
                    # the exercise value the simulation's own record gives, and nothing else does.
                    close_price=Decimal(0) if settlement is None else settlement.record.booked_price,
                    booked_as_of_ms=booked_as_of_ms, simulated=settlement is not None,
                    allocation=allocation_rows, share_trade=share_trade_note,
                    simulation=None if settlement is None else settlement.record.note(),
                    notes=notes, owner_word=None if settlement is None else settlement.record.owner_word(),
                )
            except _RowChanged:
                hold(
                    OPTION_EVENT_ROW_CHANGED_REASON_CODE, [sid],
                    f"Activity {event.activity_id} was booked for {sid} before with other figures.",
                )
                booked_whole = False
                continue
            if record is not None:
                booked.append((sid, record))

        # What the record noted is held for as long as the row is read and the owner has not acknowledged it.
        for note in notes:
            hold(note["code"], [note["bot"]] if "bot" in note else owners, note["detail"])
        reported = paired.get(event.activity_id, _NO_SHARE_TRADE)
        if booked_whole:
            # It will book nothing more. Its share trade is the same row's other half when it
            # agrees with it, or differs in its printed price alone.
            agrees = reported.trade is not None and (not reported.differences or reported.only_the_price)
            rows_aside[event.activity_id] = _RowSetAside(
                activity_id=event.activity_id, symbol=symbol, refused_as=None,
                detail=_how_a_row_was_booked(event, allocation_rows),
                share_trade_id=reported.trade.activity_id if agrees and reported.trade is not None else None,
            )
        if event.kind is not OptionEventKind.EXPIRED and owners:
            if reported.trade is None:
                hold(
                    OPTION_SHARE_TRADE_MISSING_REASON_CODE, owners,
                    f"No share trade was read for the {event.kind.value} {symbol} of "
                    f"{et_date_at_ms(event.activity_date_ms).isoformat()}.",
                )
            elif reported.differences and not reported.only_the_price:
                # Shares, cash or symbol that are not the contract's: what the broker holds may
                # not be what the books booked. A printed price alone puts nothing in doubt.
                holds.add(
                    OPTION_SHARE_TRADE_DISAGREES_REASON_CODE, owners, ref=f"share-trade:{reported.trade.activity_id}",
                    symbol=reported.trade.underlying_symbol, date_ms=reported.trade.activity_date_ms,
                    detail=f"For {symbol}: {'; '.join(reported.differences)}.",
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


def _rows_that_may_end(
    read: OptionEventRead, contract: OptionContract, rows_aside: Mapping[str, _RowSetAside]
) -> tuple[list[str], list[_RowSetAside]]:
    """The rows of one read that may be how ``contract`` ended: the ids of those in an owner's ending's way, and those not.

    A row may be the contract's end when it is an event for the contract, a
    row the Clerk cannot read that names the contract or its underlying, or
    a share trade in its underlying dated on or after its expiry date.

    Two kinds do not stand in the way (``rows_aside``), each with the share
    trade that is its other half. An event the canonical rule refused this
    run for its count or its side against lots the books hold: nothing of
    it was booked. And an event that is booked whole as it reads now: what
    it left on the books it will never take. Waiting for either settles
    nothing. Every other row does stand in the way: one the rule refused
    under another name, one it never judged, one that changed under a
    booked id, and one that a bot its record names could not be booked
    for.
    """
    symbol, root = contract.occ_symbol, contract.root
    for_the_contract = [event.activity_id for event in read.events if event.contract.occ_symbol == symbol]
    set_aside = [rows_aside[activity_id] for activity_id in for_the_contract if activity_id in rows_aside]
    their_share_trades = {row.share_trade_id for row in set_aside}
    in_the_way = [activity_id for activity_id in for_the_contract if activity_id not in rows_aside]
    in_the_way += [row.activity_id for row in read.unreadable if (row.symbol or "").upper() in (symbol, root)]
    expired_on_ms = et_midnight_ms(contract.expiry_date)
    in_the_way += [
        trade.activity_id for trade in read.share_trades
        if trade.underlying_symbol == root and trade.activity_date_ms >= expired_on_ms
        and trade.activity_id not in their_share_trades
    ]
    return sorted(in_the_way), sorted(set_aside, key=lambda row: row.activity_id)


# What is said of a row that changed under a booked id, wherever an expired contract's notice names one.
_A_BOOKED_ROW_CHANGED = (
    "The broker's own record moved after it was booked, and no rule here can say which reading is the true one."
)


def _what_came_and_ends_nothing(set_aside: Sequence[_RowSetAside]) -> str:
    """What the broker's records of a contract's end came to, when none of them will end what is still held."""
    said = []
    for booked, lead in (
        (True, "A record of its end that came is booked, and it counted fewer contracts than these books held."),
        (False, "The broker's record of its end will not be booked."),
    ):
        rows = [row for row in set_aside if row.booked is booked]
        if rows:
            said += [lead, *(f"Activity {row.activity_id}: {row.detail}" for row in rows)]
    return " ".join(said)


def _why_rows_stand_in_the_way(symbol: str, in_the_way: Sequence[str], holds: _Holds) -> str:
    """Why an expired contract is not ended at the owner's word while these rows stand, in its notice's words.

    Each row is named, with what this run found wrong with it under the
    hold it raised for it. It promises no ending: none is made while a row
    that may be the contract's end is not booked whole as it reads.
    """
    refs = {f"{kind}:{activity_id}" for activity_id in in_the_way for kind in ("option-activity", "share-trade")}
    found: dict[str, list[str]] = {}
    for (_sid, reason_code), items in holds.items().items():
        for item in items:
            if item.ref in refs and item.detail not in found.get(reason_code, ()):
                found.setdefault(reason_code, []).append(item.detail)
    said = [" ".join([_HOLD_COPY[reason_code][1], *details]) for reason_code, details in found.items()]
    if OPTION_EVENT_ROW_CHANGED_REASON_CODE in found:
        said.append(_A_BOOKED_ROW_CHANGED)
    return " ".join([
        f"A record of the broker's that may be {symbol}'s end ({', '.join(in_the_way)}) is not booked whole as it "
        "reads, and the contract is not ended at the owner's word while that stands.",
        *said,
    ])


def _hold_what_expired_and_is_still_held(
    conn: sqlite3.Connection, read: OptionEventRead, holds: _Holds, *, now_ms: int, not_settled: Mapping[str, str],
    rows_aside: Mapping[str, _RowSetAside],
) -> None:
    """After a contract's last session, set any lot still open on it against the broker's position.

    A contract held on both sides nets to nothing at the broker and may never
    get a row. So may one whose row is yet to come. Either way the books
    still hold what the broker does not, and the bot is held by name. So
    each holder is held when the broker shows none of the contract, or
    another net than the books': nothing at the broker is no agreement
    while a lot is still open here. While the broker still shows what the
    books hold there is nothing to say yet.
    ``not_settled`` is a simulation's own answer: each contract it holds
    past its last session and could not settle, with why.

    ``rows_aside`` are the rows this run found will book nothing more
    (``_RowSetAside``). A bought contract the broker holds none of, whose
    every row is such a row, is one the owner can end at their word, and
    its words say what came and why it ends nothing.

    **What is said decides what is offered.** A bought contract with a row
    in the way that may be its end is named a second time, under
    ``owner_ending_not_possible_ref``, with what is known of the row. The
    page reads that and does not offer the ending (``recovery_policy``),
    and the next step names no ending. A row in the way of this read is in
    the way of the read the press makes, so a press could only be refused.
    What may differ at the press keeps the offer: a record still to come,
    and a broker that still shows some of the contract.
    """
    totals: dict[str, int] = {}
    sides: dict[str, set[bool]] = {}
    positions = held_option_positions(conn)
    for _sid, symbol, quantity in positions:
        totals[symbol] = totals.get(symbol, 0) + quantity
        sides.setdefault(symbol, set()).add(quantity > 0)
    for sid, symbol, quantity in positions:
        contract = OptionContract.from_occ_symbol(symbol)
        if not past_last_session(contract.expiry_date, now_ms):
            continue
        ref = expired_contract_hold_ref(symbol)
        expired_on = contract.expiry_date.isoformat()
        if symbol in not_settled:
            holds.add(
                OPTION_EXPIRED_NOT_SETTLED_REASON_CODE, [sid], ref=ref, symbol=symbol, date_ms=None,
                detail=f"{symbol} expired on {expired_on}. {not_settled[symbol]}",
            )
            continue
        if read.broker_positions is None:
            continue
        at_broker = read.broker_positions.get(symbol, 0.0)
        if at_broker != 0 and at_broker == totals[symbol]:
            continue
        both_ways = len(sides[symbol]) > 1
        on_the_books = (
            f"these books hold it bought by one bot and sold by another, {totals[symbol]:+d} in all "
            f"({quantity:+d} for this bot)"
            if both_ways
            else f"these books hold {totals[symbol]:+d} ({quantity:+d} for this bot)"
        )
        in_the_way, set_aside = _rows_that_may_end(read, contract, rows_aside)
        what_now = f", and no activity read {'ends either side' if both_ways else 'explains the difference'}."
        next_step = _EXPIRED_NEXT_STEP
        if quantity < 0:
            # Only a bought contract is ended at the owner's word (``option_owner_ending``).
            what_now += " This bot holds it sold, and a sold contract is not ended at the owner's word."
            next_step = _EXPIRED_SOLD_NEXT_STEP
        elif in_the_way:
            what_now = "."
            next_step = _EXPIRED_ROW_IN_THE_WAY_NEXT_STEP
        elif set_aside and at_broker == 0:
            # The state the owner's ending goes ahead in though a row came: say so, and why the row is no end.
            what_now = f". {_what_came_and_ends_nothing(set_aside)}"
        holds.add(
            OPTION_EXPIRED_NOT_SETTLED_REASON_CODE, [sid], ref=ref, symbol=symbol, date_ms=None,
            detail=f"{symbol} expired on {expired_on}. The broker shows {at_broker:g} of it, {on_the_books}{what_now}",
            next_step=next_step,
        )
        if in_the_way and quantity > 0:
            # Said a second time under a reference of its own, which the page reads: no ending is offered.
            holds.add(
                OPTION_EXPIRED_NOT_SETTLED_REASON_CODE, [sid], ref=owner_ending_not_possible_ref(symbol),
                symbol=symbol, date_ms=None, detail=_why_rows_stand_in_the_way(symbol, in_the_way, holds),
                next_step=next_step,
            )


def _shares_an_event_left(conn: sqlite3.Connection) -> dict[str, OptionPositionEndedFacts]:
    """Each bot that holds shares now and was left shares by an option event, with the newest such booking.

    Read from the books' own records of what ended, never from what one run
    booked. A bot that trades options holds shares only because an exercise
    or an assignment left them.
    """
    newest: dict[str, OptionPositionEndedFacts] = {}
    for ending in recorded_option_endings(conn):
        if ending.facts.shares is not None:
            newest[ending.strategy_instance_id] = ending.facts
    return {
        sid: facts
        for sid, facts in newest.items()
        if facts.shares is not None and reads.position(conn, sid, facts.shares["symbol"])
    }


def _sync_shares_held(repo: ClerkSqliteRepository, conn: sqlite3.Connection) -> None:
    """Hold each bot for the shares an option event left it, and end that hold once the bot holds none.

    Worked out from the books each time. So a stop between a booking and
    its holds loses nothing: the next round finds the shares and holds for
    them, though it books nothing itself.
    """
    left = _shares_an_event_left(conn)
    for sid, facts in left.items():
        shares = facts.shares["quantity"] if facts.shares is not None else 0
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
        if hold["reason_code"] == OPTION_SHARES_HELD_REASON_CODE and sid is not None and sid not in left:
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
            detail: str, *, next_step: str = _FILL_NEVER_BOOKED_NEXT_STEP,
            _unsettled: UnsettledOutsideOrder = unsettled, _ref: str = ref, _symbol: str = symbol,
        ) -> None:
            # The owner's review releases an order that has filled nothing, and nothing else: a
            # fill that is not booked is held for by name whatever was reviewed before it.
            # ``next_step`` is what the owner does about it. A fill that will not be booked is not
            # waited for: nothing books it, and the order's review does not release it.
            if not _unsettled.released_by_review:
                holds.add(
                    OPTION_ORDER_NOT_OURS_REASON_CODE, _unsettled.against, ref=_ref, symbol=_symbol, date_ms=None,
                    detail=detail, next_step=next_step,
                )

        if unsettled.filled is None or not unsettled.ended:
            hold(unsettled.sentence, next_step=_ORDER_WORKING_NEXT_STEP)
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
                broker_order_id=order.broker_order_id,
            )
            if record is not None:
                booked.append((sid, record))
    return booked


# ── The owner's ending of a contract nothing will settle ─────────────────────


# What the owner is told of a contract that was settled after all, in the place of their ending:
# how it ended, said of the whole position or of the part a record took.
_HOW_IT_ENDED: dict[OptionEnding, str] = {
    OptionEnding.EXPIRED: "it expired worth nothing",
    OptionEnding.EXERCISED: "it was exercised",
    OptionEnding.ASSIGNED: "it was assigned",
    OptionEnding.SOLD_OUTSIDE: "it was sold by an order the Clerk did not place",
    OptionEnding.SETTLED_IN_CASH: "the simulation settled it in cash by its own rule",
    OptionEnding.ENDED_BY_OWNER: "it was ended at the owner's word",
}


def _owner_ending_refused(reason_code: str, message: str) -> OwnerEndingOutcome:
    return OwnerEndingOutcome(result="refused", reason_code=reason_code, message=f"{message} Nothing was ended.")


def _nothing_held(request: OwnerEndingRequest) -> OwnerEndingOutcome:
    return _owner_ending_refused(
        owner_ending_rule.NOTHING_HELD,
        f"{request.strategy_instance_id} holds no {request.symbol.upper()} on these books.",
    )


def _booked_ending(facts: OptionPositionEndedFacts, *, still_held: int = 0) -> OwnerEndingOutcome:
    """What the owner is told of a contract whose end is on the books: ended at their word, or settled after all.

    ``still_held`` is what the bot holds of the contract after a record
    booked this round took a part of its position. The owner is then told
    it was a part, and that the rest was not ended.
    """
    command_id = f"cmd:{facts.idempotency_key}"
    if facts.ended_by == OptionEnding.ENDED_BY_OWNER.value:
        return OwnerEndingOutcome(
            result="ended", ended_by=facts.ended_by, command_id=command_id,
            message=f"{facts.contract_symbol} was ended at your word and counted as worth nothing.",
        )
    how = _HOW_IT_ENDED[OptionEnding(facts.ended_by)]
    if still_held:
        return OwnerEndingOutcome(
            result="settled", ended_by=facts.ended_by, command_id=command_id,
            message=(
                f"{facts.contract_symbol} was not ended at your word. A record of its end had come after all, for "
                f"{facts.contracts} of the {facts.contracts + still_held} contracts this bot held, and that part "
                f"was booked first: {how}. The other {still_held} "
                f"{'is' if still_held == 1 else 'are'} still on these books and nothing ended "
                f"{'it' if still_held == 1 else 'them'}. History shows the booking, and the contract's notice "
                "says what can be done about the rest."
            ),
        )
    return OwnerEndingOutcome(
        result="settled", ended_by=facts.ended_by, command_id=command_id,
        message=(
            f"{facts.contract_symbol} was not ended at your word: a settlement had come after all, and it was "
            f"booked in its place. {how[0].upper()}{how[1:]}. History shows it."
        ),
    )


def _last_ending(conn: sqlite3.Connection, sid: str, symbol: str) -> OptionPositionEndedFacts | None:
    """The newest record of one bot's position in one contract having ended, or None. One bot's own records."""
    row = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE strategy_instance_id = ? AND transition_kind = ? "
        "AND json_extract(facts_json, '$.contract_symbol') = ? ORDER BY sequence DESC LIMIT 1",
        (sid, OPTION_POSITION_ENDED, symbol),
    ).fetchone()
    return None if row is None else OptionPositionEndedFacts.from_facts_json(row["facts_json"])


def _why_a_simulation_did_not_let_go(settled: SimulatedOptionSettlements | None, symbol: str) -> OwnerEndingOutcome:
    """Why a simulated account still holds a contract after its simulation was handed the owner's word."""
    if settled is not None and symbol in settled.waiting:
        return _owner_ending_refused(
            owner_ending_rule.WAITING_FOR_THE_CLOSING_PRICE,
            f"The simulation is still waiting for the closing price of {symbol}'s last session.",
        )
    if settled is not None and symbol in settled.not_settled:
        return _owner_ending_refused(owner_ending_rule.NOT_SETTLED_BY_THE_SIMULATION, settled.not_settled[symbol])
    return _owner_ending_refused(
        owner_ending_rule.NOT_SETTLED_BY_THE_SIMULATION,
        f"The simulation's end of {symbol} could not be booked. This bot's notices say why.",
    )


def _why_a_real_account_cannot_end(
    read: OptionEventRead, request: OwnerEndingRequest, contract: OptionContract,
    *, held_now: float, now_ms: int, rows_in_the_way: Sequence[str],
) -> OwnerEndingOutcome | None:
    """Why this read does not prove a real account's contract has no settlement, or None when it does.

    Proof is all of: the contract is past its last session and the wait for
    the broker's record is over; the read is whole; no row that could be
    this contract's stands in the way (``_rows_that_may_end``: a row the
    rule refused by name does not, nor does one that is booked whole); the
    broker's position was read and holds none of it; and this bot is the
    one the books hold it for, bought.
    """
    sid, symbol = request.strategy_instance_id, contract.occ_symbol
    if not past_last_session(contract.expiry_date, now_ms):
        return _owner_ending_refused(
            owner_ending_rule.CONTRACT_STILL_TRADES, f"{symbol} has not reached the end of its last session."
        )
    if held_now < 0:
        return _owner_ending_refused(
            owner_ending_rule.SOLD_CONTRACT,
            f"{sid} holds {symbol} sold. A sold contract that was assigned moved shares, so it is not ended at a word.",
        )
    if now_ms < owner_ending_offered_from_ms(contract.expiry_date):
        return _owner_ending_refused(
            owner_ending_rule.RECORD_MAY_STILL_COME,
            f"The broker's record of how {symbol} ended may still come: it is waited for until the close of the "
            "session after the contract's last.",
        )
    if not read.whole:
        return _owner_ending_refused(
            owner_ending_rule.READ_NOT_WHOLE,
            f"The account's option events could not be read whole, so nothing proves {symbol} has no record. "
            f"{read.failure or ''}".strip(),
        )
    if rows_in_the_way:
        return _owner_ending_refused(
            owner_ending_rule.ROW_NOT_BOOKED,
            f"The broker reports activity that may be {symbol}'s end ({', '.join(rows_in_the_way)}), and it is not "
            f"booked whole as it reads now. The notice that {symbol} is not settled says what is known of it.",
        )
    if read.broker_positions is None:
        return _owner_ending_refused(
            owner_ending_rule.BROKER_POSITION_UNREAD,
            f"The broker's positions could not be read, so nothing proves it holds no {symbol}.",
        )
    at_broker = read.broker_positions.get(symbol, 0.0)
    if at_broker:
        return _owner_ending_refused(
            owner_ending_rule.BROKER_STILL_HOLDS,
            f"The broker still shows {at_broker:g} of {symbol}. A position the broker holds is not ended at a word.",
        )
    return None


# Why no settlement could be made of a real account's contract, as the ending's record says it.
_NO_RECORD_CAME = (
    "A whole read of the account's option events, made after the close of the session that follows the contract's "
    "last, has no record of how the contract ended, and the broker holds none of it."
)


def _outside_orders_not_booked(
    conn: sqlite3.Connection, holds: _Holds, sid: str, symbol: str
) -> list[dict[str, str]]:
    """Each order the Clerk did not place that stands unbooked against one bot in one contract, as an ending names it.

    Its reference and what is known of it: the order, what it filled, why
    that was not booked, and the price it states when it states one. The
    words are the ones this run's hold for the order carries. An order the
    books have settled is not among them: one booked against a bot as a
    sale, and one that ended having filled nothing.
    """
    found = {
        item.ref: item.detail
        for (bot, reason_code), items in holds.items().items()
        if bot == sid and reason_code == OPTION_ORDER_NOT_OURS_REASON_CODE
        for item in items
    }
    named = []
    for order in outside_orders_in(conn, symbol):
        unsettled = unsettled_outside_order(conn, order)
        if unsettled is None or sid not in unsettled.against:
            continue
        ref = outside_order_hold_ref(order.broker_order_id)
        detail = found.get(ref, unsettled.sentence)
        if unsettled.filled and order.filled_avg_price is not None and order.filled_avg_price > 0:
            detail += f" The order states a price of {plain(Decimal(str(order.filled_avg_price)))} a share."
        named.append({"ref": ref, "detail": detail})
    return named


def _why_no_settlement(set_aside: Sequence[_RowSetAside], outside: Sequence[Mapping[str, str]] = ()) -> str:
    """Why no settlement could be made, in the ending's own record.

    No row came. Or the rows that came end nothing of what was still held:
    one was refused, or one was booked and counted fewer contracts than
    the books held. Each is said of the rows it is true of. ``outside`` is
    each order the Clerk did not place that stands unbooked in the
    contract: the record names it, and says where what it sold for is.
    """
    if not set_aside and not outside:
        return _NO_RECORD_CAME
    said = []
    for booked, lead in (
        (True, "The broker's record of the contract's end was booked and counted fewer contracts than these books held."),
        (False, "The broker's record of how the contract ended could not be booked."),
    ):
        rows = [row for row in set_aside if row.booked is booked]
        if rows:
            said += [lead, *(f"Activity {row.activity_id}: {row.detail}" for row in rows)]
    if outside:
        said += [
            "An order the Clerk did not place stands in this contract on these books, and was not booked against "
            "this bot.",
            *(one["detail"] for one in outside),
            "Whatever such an order sold the contract for is in the account's cash and on no bot's books: what "
            "this bot paid for the contract is booked as its loss in full.",
        ]
    return (
        f"{' '.join(said)} A whole read of the account's option events, made after the close of the session that "
        "follows the contract's last, has no other record of it, and the broker holds none of it."
    )


def _rows_an_ending_names(set_aside: Sequence[_RowSetAside], *, booked: bool) -> list[dict[str, str]]:
    """The rows an ending was made beside, as its record names them: those refused by name, or those booked whole.

    Each with its reference and what is known of it. A refused row carries
    the name it was refused by.
    """
    return [
        {
            "ref": row.ref, **({} if row.refused_as is None else {"refused_as": row.refused_as}), "detail": row.detail,
            **({} if row.share_trade_id is None else {"share_trade_ref": f"share-trade:{row.share_trade_id}"}),
        }
        for row in set_aside
        if row.booked is booked
    ]


def _end_at_the_owners_word(
    repo: ClerkSqliteRepository,
    conn: sqlite3.Connection,
    read: OptionEventRead,
    settled: SimulatedOptionSettlements | None,
    request: OwnerEndingRequest,
    *,
    booked: Sequence[tuple[str, OptionPositionEndedFacts]],
    rows_aside: Mapping[str, _RowSetAside],
    holds: _Holds,
) -> tuple[OwnerEndingOutcome, tuple[str, OptionPositionEndedFacts] | None]:
    """End one bot's expired contract at the owner's word, if this read left it on the books with nothing to book it from.

    Called after the read's rows and the simulation's settlements are
    booked. A contract one of those took off the books is not ended at a
    word: the owner is told how it ended. On a simulated account the
    simulation itself lets go first, in its own ledger, and its record is
    what was booked. On a real account the booking is made here, once the
    read proves there is nothing else to book.

    ``rows_aside`` are the rows this run found will book nothing more
    (``_RowSetAside``): refused by name, or booked whole. Such a row of the
    contract's settles nothing of what is still held and never will, so
    the ending goes ahead beside it and its record names it.

    **An order the Clerk did not place** that sold the contract and that
    the books recorded and could not book does not stand in the way
    either: it filled more than the bots hold, or states no price, and
    nothing will ever book it. After expiry the ending is the only exit
    that state has. The amount is unchanged: what the contract cost is
    booked as the loss. The record names the order with what this run's
    hold says of it (``holds``), and says that what it sold for is in the
    account's cash and on no bot's books.

    The outcome, and the new record when this call booked one.
    """
    sid, symbol = request.strategy_instance_id, request.symbol.upper()
    this_round = next((facts for bot, facts in booked if bot == sid and facts.contract_symbol == symbol), None)
    held_now = reads.position(conn, sid, symbol)
    if this_round is not None:
        # A record that came took this bot's position, or a part of it. Either way nothing is
        # ended at a word in the same round: the owner is told what was booked, and of what.
        return _booked_ending(this_round, still_held=abs(int(held_now))), None
    if not held_now:
        earlier = _last_ending(conn, sid, symbol)
        return (_nothing_held(request) if earlier is None else _booked_ending(earlier)), None
    if is_simulated_account_id(repo.account_id):
        return _why_a_simulation_did_not_let_go(settled, symbol), None
    contract = OptionContract.from_occ_symbol(symbol)
    now_ms = repo.clock()
    in_the_way, set_aside = _rows_that_may_end(read, contract, rows_aside)
    refused = _why_a_real_account_cannot_end(
        read, request, contract, held_now=held_now, now_ms=now_ms, rows_in_the_way=in_the_way
    )
    if refused is not None:
        return refused, None
    terms = contract_terms(conn, symbol)
    book = _bot_books(conn, repo.account_id, contract).get(sid)
    contracts = int(held_now)
    if terms is None or book is None or sum(lot.quantity for lot in book.lots) != contracts:
        return _owner_ending_refused(
            owner_ending_rule.LOTS_DISAGREE,
            f"{sid}'s lots of {symbol} do not add up to the {held_now:g} it holds, so they cannot be closed.",
        ), None
    allocation = [
        {
            "owner": sid, "sequence": lot.sequence, "opened_at_ms": lot.opened_at_ms, "contracts": lot.quantity,
            "option_quantity_change": -lot.quantity, "share_quantity_change": 0, "cash_change_cents": 0,
            "opened_by": book.opened_by.get(lot.sequence),
        }
        for lot in book.lots
    ]
    outside = _outside_orders_not_booked(conn, holds, sid, symbol)
    try:
        record = _append_booking(
            repo, sid=sid, run_id=_run_of(conn, allocation[0]["opened_by"]), key=owner_ending_key(symbol),
            ended_by=OptionEnding.ENDED_BY_OWNER, broker=OWNER_ENDING_SOURCE, source_id=f"owner-ending:{symbol}",
            source_code="OWNER_ENDING", event_date_ms=et_midnight_ms(contract.expiry_date),
            position_change=-contracts, contract=contract, multiplier=terms.multiplier, contracts=contracts,
            held=_BOUGHT, close_price=Decimal(0),
            # Counted as having happened when the contract stopped trading, as an expiry is.
            booked_as_of_ms=max([last_session_close_ms(contract.expiry_date) - 1, *(lot.opened_at_ms for lot in book.lots)]),
            simulated=False, allocation=allocation, share_trade=None, simulation=None, notes=None,
            owner_word={
                "operator": request.operator.strip(), "reason": (request.reason or "").strip() or None,
                "said_at_ms": now_ms,
                "why_no_settlement": _why_no_settlement(set_aside, outside),
                # Each left out when it names none: the record of an ending no row came for is what it always was.
                **{
                    key: named
                    for key, named in (
                        (OWNER_WORD_ROWS_NOT_BOOKED, _rows_an_ending_names(set_aside, booked=False)),
                        (OWNER_WORD_ROWS_BOOKED, _rows_an_ending_names(set_aside, booked=True)),
                        (OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED, outside),
                    )
                    if named
                },
            },
        )
    except _RowChanged:
        return _owner_ending_refused(
            owner_ending_rule.LOTS_DISAGREE,
            f"An ending of {symbol} at the owner's word is on the books for {sid} with other figures.",
        ), None
    if record is None:
        # Booked before under the same key and the same figures, and the position is still held:
        # the books do not say what this call would.
        return _owner_ending_refused(
            owner_ending_rule.LOTS_DISAGREE,
            f"An ending of {symbol} at the owner's word is on the books for {sid}, and it still holds the contract.",
        ), None
    logger.warning(
        "ended an expired option contract at the owner's word",
        extra={
            "action": "option_ended_at_the_owners_word", "account_id": repo.account_id, "strategy_instance_id": sid,
            "symbol": symbol, "contracts": contracts, "operator": request.operator,
        },
    )
    return _booked_ending(record), (sid, record)


# ── The batch function ───────────────────────────────────────────────────────


def book_option_events(
    repo: ClerkSqliteRepository,
    read: OptionEventRead | None,
    *,
    settled: SimulatedOptionSettlements | None = None,
    owner_ending: OwnerEndingRequest | None = None,
) -> OptionEventRound:
    """Book what one read of the account's option events found, and hold by name for what could not be booked.

    ``read`` is None when the broker was not asked this time. The books' own
    record of outside orders is still acted on: an order in a contract a bot
    holds is held against that bot, and its fill is booked once it has ended.

    ``settled`` is what a simulation's settlement step handed back, on a
    simulated account. Its settlements are booked with the read's rows, and
    what it could not settle is held for by name. None on a real account.

    ``owner_ending`` is the owner's word to end one bot's expired contract
    that nothing will settle (``option_owner_ending``). What the read and
    the simulation brought is booked first. The contract is ended at the
    owner's word only when that left it on the books, and this same read
    proves there is still nothing to book it from. The round says which.

    Everything from the first read of the books to the last append happens
    inside the repository's write fence. On books that never recorded an
    option contract one small table is read and nothing is written.
    """
    with repo.write_fence() as conn:
        if books_record_no_contract(conn):
            return OptionEventRound(owner_ending=None if owner_ending is None else _nothing_held(owner_ending))
        holds = _Holds()
        booked = _book_outside_orders(repo, conn, holds)
        whole = read is not None and read.whole
        owner_outcome: OwnerEndingOutcome | None = None
        if read is None and owner_ending is not None:
            owner_outcome = _owner_ending_refused(
                owner_ending_rule.READ_NOT_WHOLE,
                "The account's option events were not read, so nothing proves the contract has no settlement.",
            )
        if read is not None:
            _hold_what_could_not_be_read(conn, read, holds)
            simulated = {} if settled is None else {item.event.activity_id: item for item in settled.settled}
            rows_aside: dict[str, _RowSetAside] = {}
            booked += _book_events(repo, conn, read, holds, simulated=simulated, rows_aside=rows_aside)
            if owner_ending is not None:
                owner_outcome, ended = _end_at_the_owners_word(
                    repo, conn, read, settled, owner_ending, booked=booked, rows_aside=rows_aside, holds=holds
                )
                if ended is not None:
                    booked.append(ended)
                    # The ended bot holds the contract no more: what this run found for it about
                    # the rows and the outside orders its ending names was found before the ending,
                    # and is its no longer.
                    word = ended[1].owner_word or {}
                    holds.forget(
                        ended[0],
                        [
                            named["ref"]
                            for key in (OWNER_WORD_ROWS_NOT_BOOKED, OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED)
                            for named in word.get(key, ())
                        ],
                    )
            if not whole:
                # After the bookings: a bot this round has just emptied is waiting for nothing more.
                holders = sorted({sid for sid, _symbol, _quantity in held_option_positions(conn)})
                if holders:
                    holds.add(
                        OPTION_EVENTS_NOT_READ_REASON_CODE, holders, ref="read", symbol=None, date_ms=None,
                        detail=read.failure or _WALK_CUT_SHORT,
                    )
            if whole:
                _hold_what_expired_and_is_still_held(
                    conn, read, holds, now_ms=repo.clock(), not_settled={} if settled is None else settled.not_settled,
                    rows_aside=rows_aside,
                )
        # What the books' own record of outside orders holds for ends when that record no longer has it.
        held = _sync_holds(repo, conn, holds, codes=(OPTION_ORDER_NOT_OURS_REASON_CODE,), may_end=True)
        if read is not None:
            # A read that was not whole ends no hold: a row it did not reach is not a row that is gone.
            held += _sync_holds(repo, conn, holds, codes=(OPTION_EVENTS_NOT_READ_REASON_CODE, *_READ_HOLDS), may_end=whole)
        _sync_shares_held(repo, conn)
        if is_simulated_account_id(repo.account_id):
            # A simulated account has no pass on a timer to end an exit's notices once its bot
            # holds nothing (ADR 0076 decision 20). Its simulation is its own broker, and let go
            # of the contract in the step whose record was just booked. So a bot this run's
            # bookings left flat in every symbol is cleared of them here, as the owner's
            # write-off clears its own, and a failed scheduled close does not keep a running bot
            # from its next entry. Not in the booking's transaction: a stop in between leaves the
            # notices to the next pass, which ends them on the same proof.
            for sid in sorted({sid for sid, _facts in booked}):
                resolve_flat_exit_fences(
                    repo, strategy_instance_id=sid, evidence_refs=("simulated_option_position_ended", "attributed_flat")
                )
        return OptionEventRound(
            booked=tuple(f"cmd:{facts.idempotency_key}" for _sid, facts in booked), held=tuple(held), read_whole=whole,
            owner_ending=owner_outcome,
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
                    failure = _WALK_CUT_SHORT
                break
        else:
            failure = f"The walk of the account's activities is longer than {MAX_OPTION_EVENT_PAGES} reads."
    except (BrokerError, ValueError) as exc:
        # The broker's own words go to the log and never into the hold: they change from one
        # failed read to the next, and a hold whose words change is written again each time.
        failure = _READ_FAILED
        logger.warning(
            "the account's option events could not be read",
            extra={"action": "option_events_read_failed", "account_id": repo.account_id, "error": str(exc)},
        )
    return OptionEventRead(
        after_ms=after_ms,
        events=tuple(events.values()),
        share_trades=tuple(share_trades.values()),
        unreadable=tuple(unreadable.values()),
        history_complete=complete,
        failure=failure,
    )


async def poll_option_events(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    simulation: OptionSettlementSimulation | None = None,
    off_loop: OffLoop = to_thread,
    asked_for: bool = False,
    broker_positions: Sequence[BrokerPosition] | None = None,
    act_on_the_books_between_reads: bool = False,
    owner_ending: OwnerEndingRequest | None = None,
) -> OptionEventPoll:
    """Read the account's option events when a read is due, and book what was read.

    ``simulation`` is the settlement step of a simulated account's trade
    port. A simulation has no broker to end its positions, so before the
    read it is asked to settle what it holds past expiry, and what it hands
    back is booked with the read. A real account names none.

    ``owner_ending`` is the owner's word to end one expired contract that
    nothing will settle. The read is then made now, whatever the cadence,
    and after any read already under way: this is the one look more that
    the ending rests on. The simulation is handed the word with its step,
    and the batch function is handed the request with the read.

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
    forced = owner_ending is not None
    # A read the owner's ending rests on waits its turn behind one under way. No other read does.
    may_read = forced or not state.lock.locked()
    due = may_read and (forced or _due(state, now_ms, asked_for=asked_for))
    if not due and broker_positions is None and not act_on_the_books_between_reads:
        return OptionEventPoll(read=False)
    if not forced and not await off_loop(lambda: option_work_on_the_books(repo)):
        if due:
            # Nothing an event could change: look again when the next read would be due.
            state.read_at_ms, state.nothing_pending = now_ms, True
        return OptionEventPoll(read=False)
    differs = False
    if not due and may_read and broker_positions is not None:
        # A pass's own snapshot shows a recorded contract's position differing from the books: that
        # asks for a read, as ``asked_for`` does, and so at most one a minute.
        differs = await off_loop(lambda: option_position_differs(repo, broker_positions))
        due = differs
    # From here to the lock nothing is awaited, and the cadence is asked once more. While the books
    # were being asked another caller may have begun this same read, or finished it: then it is
    # theirs, and this one neither waits behind their walk nor reads again after it.
    if due and not forced and (state.lock.locked() or not _due(state, repo.clock(), asked_for=asked_for or differs)):
        due = False
    if not due:
        if not act_on_the_books_between_reads:
            return OptionEventPoll(read=False)
        return OptionEventPoll(read=False, round=await off_loop(lambda: book_option_events(repo, None)))
    async with state.lock:
        # Counted from the attempt: a read or a booking that raises is tried again a minute
        # later, never on the keeper's every round.
        state.read_at_ms, state.nothing_pending = now_ms, False
        after_ms = await off_loop(lambda: option_event_window_start_ms(repo))
        # A write to the simulation's own ledger, made with no lock of the Clerk's held, as the read is.
        settled = None
        if simulation is not None and owner_ending is None:
            settled = simulation.settle_expired_options(after_ms=after_ms)
        elif simulation is not None and owner_ending is not None:
            settled = simulation.settle_expired_options(
                after_ms=after_ms,
                at_the_owners_word={
                    owner_ending.symbol.upper(): OwnersWord(
                        operator=owner_ending.operator.strip(),
                        reason=(owner_ending.reason or "").strip() or None,
                        said_at_ms=now_ms,
                    )
                },
            )
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
        done = await off_loop(
            lambda: book_option_events(repo, events, settled=settled)
            if owner_ending is None
            else book_option_events(repo, events, settled=settled, owner_ending=owner_ending)
        )
        state.read_at_ms = repo.clock()
        # While a hold stands something is still to come: a row, a share trade, a price. So it is
        # while a simulation waits for a contract's closing bar. Look again sooner.
        awaited = settled is not None and bool(settled.waiting)
        state.nothing_pending = done.read_whole and not done.held and not awaited
        return OptionEventPoll(read=True, round=done)


async def poll_option_events_in_a_pass(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    simulation: OptionSettlementSimulation | None,
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
            repo, read=read, simulation=simulation, off_loop=off_loop, broker_positions=broker_positions,
            act_on_the_books_between_reads=True,
        )
    except ClerkSqliteError:
        raise
    except Exception:
        logger.exception(
            "reading and booking option events failed; the pass goes on to its verdict",
            extra={"action": "option_events_step_failed", "account_id": repo.account_id},
        )


class OwnerEndingRefused(Exception):
    """An expired contract was not ended at the owner's word. Nothing was booked at that word."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


# The contract's settlement had come after all and was booked in place of the owner's ending.
OWNER_ENDING_SETTLED_INSTEAD = "OPTION_OWNER_ENDING_SETTLED_INSTEAD"


@dataclass(frozen=True, slots=True)
class OwnerEndingReceipt:
    """The books' record of one contract ended at the owner's word."""

    sequence: int
    recorded_at_ms: int
    symbol: str
    command_id: str


async def end_option_at_the_owners_word(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    simulation: OptionSettlementSimulation | None,
    off_loop: OffLoop,
    request: OwnerEndingRequest,
) -> OwnerEndingReceipt:
    """End one bot's expired contract at the owner's word, after one look more (``option_owner_ending``).

    The look is the poll itself, made now: the same read, the same
    simulation step and the same batch function as every other end of an
    option position. A settlement that has come is booked in the ending's
    place, and the owner is told that it was: ``OwnerEndingRefused`` under
    ``OWNER_ENDING_SETTLED_INSTEAD``, with how the contract ended. Any other
    refusal names what does not prove the contract has no settlement.
    """
    if not request.operator.strip():
        raise OwnerEndingRefused(
            owner_ending_rule.NOTHING_HELD, "An ended contract records who ended it, and this request names nobody."
        )
    polled = await poll_option_events(repo, read=read, simulation=simulation, off_loop=off_loop, owner_ending=request)
    outcome = None if polled.round is None else polled.round.owner_ending
    if outcome is None:
        raise OwnerEndingRefused(
            owner_ending_rule.READ_NOT_WHOLE, "The account's option events were not read. Nothing was ended."
        )
    if outcome.result == "refused":
        raise OwnerEndingRefused(outcome.reason_code or owner_ending_rule.NOTHING_HELD, outcome.message)
    if outcome.result == "settled":
        raise OwnerEndingRefused(OWNER_ENDING_SETTLED_INSTEAD, outcome.message)
    assert outcome.command_id is not None
    command_id = outcome.command_id

    def _recorded() -> tuple[int, int]:
        with repo.write_fence() as conn:
            row = conn.execute(
                "SELECT sequence, recorded_at_ms FROM custody_transitions WHERE effect_operation_id = ? "
                "AND transition_kind = ? ORDER BY sequence LIMIT 1",
                (f"effect:{command_id.removeprefix('cmd:')}", OPTION_POSITION_ENDED),
            ).fetchone()
        return int(row["sequence"]), int(row["recorded_at_ms"])

    sequence, recorded_at_ms = await off_loop(_recorded)
    return OwnerEndingReceipt(
        sequence=sequence, recorded_at_ms=recorded_at_ms, symbol=request.symbol.upper(), command_id=command_id
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
    "OWNER_ENDING_SETTLED_INSTEAD",
    "OptionEventPoll",
    "OptionEventRead",
    "OptionEventRound",
    "OwnerEndingReceipt",
    "OwnerEndingRefused",
    "acknowledge_option_event_hold",
    "acknowledge_option_event_hold_after_a_pass",
    "book_option_events",
    "book_outside_option_orders_in_a_pass",
    "books_record_no_contract",
    "end_option_at_the_owners_word",
    "held_option_positions",
    "option_event_window_start_ms",
    "option_position_differs",
    "option_work_on_the_books",
    "outside_order_reduces_a_bots_contract",
    "poll_option_events",
    "poll_option_events_in_a_pass",
]
