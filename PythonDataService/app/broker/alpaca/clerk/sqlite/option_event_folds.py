"""How the books record an option position that ended with no order of the bot's (ADR 0076 decision 22).

An option expires, is exercised or is assigned, or the broker sells it
itself. On a Dry Run, which has no broker, the simulation settles it in
cash. None of these is an order the Clerk placed. Each is booked for the
bot that held the contract as one transition, ``OPTION_POSITION_ENDED``,
whose fold writes everything that changed in one SQLite transaction:

- the lot closes: an order bound to the contract, with one fill. An expiry,
  an exercise and an assignment close it at a price of zero, so the premium
  paid or received stays in the lot's cost and is realised there. A sale by
  the broker closes it at the broker's price, and a simulation's cash
  settlement at the exercise value the simulation's own record gives;
- an exercise or an assignment also moves shares: a second order, in the
  underlying's shares, with one fill at the strike. A bot assigned a call it
  holds no shares against is short the stock, and its books say so.

One transition, never two, so the option never leaves the books without the
shares and cash it became.

**The owner's ending.** A contract past its last session that nothing will
ever settle is ended at the owner's word: the lot closes at a price of zero,
so what it cost is the realised loss, and no shares and no cash move. It is
the same transition and the same fold, with its own ending and a record of
who said so, when, and why no settlement could be made. It is not the
residue write-off, which leaves the lot open and realises nothing. Only a
bought contract past its last session is ended this way: a sold one that
was assigned moved shares, and nobody's word undoes that. When the broker
did report the contract's end, in rows the canonical rule refused by name,
the record names each such row and the name it was refused by, so the money
trail says the contract was ended beside rows that could not be booked.

**A simulation's booking carries the simulation's record, or it is refused.**
That record names the price the simulation read and where it came from. The
fold works the exercise value out again from it and refuses a booking whose
close price is anything else. A price above zero is allowed only for a sale
by the broker and for a simulation's cash settlement, so nothing else can
put cash on the books that no record explains.

Formula: for ``t`` contracts of a contract with multiplier ``m`` and strike ``K``:
  option position change = -t for a bought lot, +t for a sold one
  shares = +t·m (a call exercised, a put assigned), -t·m (a put exercised, a call assigned)
  cash   = -shares · K, in whole cents
  a simulation's cash settlement moves no shares: cash = +t·m·b, with ``b`` its booked price a share
Reference: ADR 0076 decision 14; Alpaca, "Non-Trade Activities for Option Events".
  The cash settlement is a simulation's own rule (ADR 0076 decision 22).
Canonical implementation: ``app/options/event_allocation.py``. This fold
  works the shares and the cash out again by that file's ``expected_share_trade``
  and refuses a fact that states anything else. A cash settlement's price is
  ``simulated_option_settlement.exercise_value``'s, worked out again here.
Validated against: tests/broker/alpaca/clerk/sqlite/test_option_event_booking.py,
  on golden fixture OEV-001; tests/broker/alpaca/clerk/sqlite/test_option_event_fold.py.

The rows written are the ones every reader of money already reads: a command,
an effect, orders and fills. So FIFO, the budget, cash and the day's P&L
count an event as they count any fill, and no table is added. The effect's
kind is ``RECONCILE``, the word the schema kept for work that is neither an
entry nor a reduction, and it is written already ended, so no worklist of
open work lists it. The order that closes the lot has the role ``OTHER``.
The order the shares arrive on has the role ``ENTRY``: every reduction of a
bot's symbol is keyed on an entry order in it, so the owner's Flatten, an
ended run's close and the Clerk's own retry sell those shares as they sell
any others.

The fold reads only its own facts and the books as they stand, so a rebuild
from the mirror writes the same rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.broker.alpaca.clerk.simulated_option_settlement import check_settlement_note
from app.broker.alpaca.clerk.sqlite.contract_terms import bind_order_contract, contract_terms
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.options.event_allocation import OptionEventKind, OptionEventRefusal, expected_share_trade

OPTION_POSITION_ENDED = "OPTION_POSITION_ENDED"
# What the command of a booking is called, and the kind of its effect.
OPTION_EVENT_COMMAND_ACTION = "OPTION_EVENT"
OPTION_EVENT_EFFECT_KIND = "RECONCILE"
# The order that closes the option lot is neither an entry nor a reduction of the bot's own.
OPTION_EVENT_ORDER_ROLE = "OTHER"
# The order the shares arrive on stands where an entry's would: no ENTER bought them, and
# every reduction of a bot's symbol is keyed on an entry order in it. So the owner's Flatten,
# an ended run's close and the Clerk's retry can sell them as they sell any shares.
OPTION_EVENT_SHARE_ORDER_ROLE = "ENTRY"
# The execution id of the fill that closes a lot at nothing. That is no trade,
# so the fee model is never asked to price such a fill: it refuses a price of zero.
OPTION_EVENT_CLOSE_EXECUTION_PREFIX = "option-event:close:"
# The execution id of the fill a simulation's cash settlement closes a lot with. It is a
# sale at a price above zero, and it is charged fees as every simulated option sale is.
OPTION_EVENT_CASH_EXECUTION_PREFIX = "option-event:cash:"
OPTION_EVENT_SHARES_EXECUTION_PREFIX = "option-event:shares:"
# Where an owner's word lists the broker's rows that could not be booked: each with its ``ref``,
# the name the rule refused it by (``refused_as``) and the rule's own words (``detail``). Left
# out when no row came.
OWNER_WORD_ROWS_NOT_BOOKED = "rows_not_booked"
# Where an owner's word lists the broker's rows that were booked whole and still left contracts on
# the books: each with its ``ref`` and what it counted and for whom (``detail``). Left out when
# there is none.
OWNER_WORD_ROWS_BOOKED = "rows_booked"
# Where an owner's word lists the orders the Clerk did not place that sold the contract and could
# not be booked against the bot: each with its ``ref`` and what it sold (``detail``). Left out
# when there is none.
OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED = "outside_orders_not_booked"
# The names the canonical rule refuses a row by that an owner's ending may be made beside. Each
# says the books hold lots of the contract and the row cannot be matched to them, by its count
# or by its side. A row refused under any other name is no such row: one the rule was offered no
# lot for, one whose cash is not whole cents, one the broker does not report as executed.
REFUSALS_AN_ENDING_MAY_NAME = frozenset({
    OptionEventRefusal.MORE_CONTRACTS_THAN_HELD.value,
    OptionEventRefusal.WRONG_SIDE.value,
    OptionEventRefusal.HELD_ON_BOTH_SIDES.value,
})


def closed_by_an_option_event(execution_id: str) -> bool:
    """Whether a fill closed a lot for an option event, and not for an order anyone sent."""
    return execution_id.startswith((OPTION_EVENT_CLOSE_EXECUTION_PREFIX, OPTION_EVENT_CASH_EXECUTION_PREFIX))


class OptionEnding(StrEnum):
    """How an option position ended with no order of the bot's. A closed set."""

    EXPIRED = "expired"
    EXERCISED = "exercised"
    ASSIGNED = "assigned"
    # An order the Clerk did not place sold or bought back the contract.
    SOLD_OUTSIDE = "sold_outside"
    # A simulation sold it, in cash, at its exercise value at the close of its last session.
    # Only a simulation's rule ends a position this way; no broker's activity does.
    SETTLED_IN_CASH = "settled_in_cash"
    # Nothing will ever settle it, and the owner said to count it as worth nothing. Not "expired":
    # nobody knows whether it ended in the money.
    ENDED_BY_OWNER = "ended_by_owner"


OPTION_ENDING_SUMMARY_CODES: dict[OptionEnding, str] = {
    OptionEnding.EXPIRED: "OPTION_EXPIRED",
    OptionEnding.EXERCISED: "OPTION_EXERCISED",
    OptionEnding.ASSIGNED: "OPTION_ASSIGNED",
    OptionEnding.SOLD_OUTSIDE: "OPTION_SOLD_OUTSIDE",
    OptionEnding.SETTLED_IN_CASH: "OPTION_SETTLED_IN_CASH",
    OptionEnding.ENDED_BY_OWNER: "OPTION_ENDED_BY_OWNER",
}
# The endings an account activity of the broker's own kinds gives, each with the kind it is booked by.
_EVENT_KINDS: dict[OptionEnding, OptionEventKind] = {
    OptionEnding.EXPIRED: OptionEventKind.EXPIRED,
    OptionEnding.EXERCISED: OptionEventKind.EXERCISED,
    OptionEnding.ASSIGNED: OptionEventKind.ASSIGNED,
}
# What the fill that closes a lot is called, by how the position ended. A sale by an order the
# Clerk did not place is named for that order, under ``option_event_books.SALE_KEY_PREFIX``.
_CLOSE_EXECUTION_PREFIXES: dict[OptionEnding, str] = {
    OptionEnding.EXPIRED: OPTION_EVENT_CLOSE_EXECUTION_PREFIX,
    OptionEnding.EXERCISED: OPTION_EVENT_CLOSE_EXECUTION_PREFIX,
    OptionEnding.ASSIGNED: OPTION_EVENT_CLOSE_EXECUTION_PREFIX,
    OptionEnding.SETTLED_IN_CASH: OPTION_EVENT_CASH_EXECUTION_PREFIX,
    OptionEnding.ENDED_BY_OWNER: OPTION_EVENT_CLOSE_EXECUTION_PREFIX,
}


def close_execution_prefix(ending: OptionEnding) -> str | None:
    """What the id of the fill that closes a lot starts with, for an ending. None for a sale outside."""
    return _CLOSE_EXECUTION_PREFIXES.get(ending)
_BOUGHT, _SOLD = "bought", "sold"


@dataclass(frozen=True)
class OptionPositionEndedFacts:
    """What one bot's books lost and gained when an option position ended.

    ``allocation`` is the whole event's: every lot it took, of every bot. A
    run that finds this record never matches the event to lots again; it
    finishes the bots the record names and the books do not yet show.
    """

    idempotency_key: str
    payload_hash: str
    ended_by: str
    # Who reported it and under which id: an activity's, or an outside order's.
    broker: str
    source_id: str
    # The broker's own code for the activity as written, or "order" for a sale.
    source_code: str
    # The row's date, 00:00 New York time on it. None for a sale, which has a time of its own.
    event_date_ms: int | None
    # The change the broker reported to the account's position, signed as it wrote it.
    position_change: int
    contract_symbol: str
    multiplier: int
    # Contracts this bot lost: one or more.
    contracts: int
    # The side of the lots that closed: "bought" or "sold".
    held: str
    # Dollars a share the lots closed at, exact: "0" for an expiry, an exercise or an assignment.
    # Above zero for a sale outside and for a simulation's cash settlement.
    close_price: str
    # When the books count it as having happened, ``int64`` ms UTC.
    booked_as_of_ms: int
    option_order_ref: str
    close_execution_id: str
    # True when a simulation reported it: its fills are simulated executions.
    simulated: bool
    allocation: list[dict[str, Any]]
    # The shares an exercise or an assignment moved: symbol, quantity (signed),
    # price, cash_cents, order_ref, execution_id. Left out for an expiry and a sale.
    shares: dict[str, Any] | None = None
    # The broker's own share-trade row and whether it agreed. Left out when none was paired.
    share_trade: dict[str, Any] | None = None
    # A simulation's own record of its settlement: the rule, the price it read, the bar that price
    # is the close of and every figure worked out from it. Every simulated booking carries one,
    # and a real account's never does.
    simulation: dict[str, Any] | None = None
    # What the books noted while booking, each a reason code and its words: an
    # expiry signed for the other side, a lot closed before the row was read.
    notes: list[dict[str, str]] | None = None
    # For a contract ended at the owner's word: operator, reason, said_at_ms and
    # why_no_settlement. Also ``rows_not_booked`` when rows of the broker's for the contract
    # were refused by name, ``rows_booked`` when rows were booked and left contracts on the
    # books, and ``outside_orders_not_booked`` when an order the Clerk did not place sold the
    # contract and could not be booked. Left out of every other ending.
    owner_word: dict[str, Any] | None = None

    def to_facts_json(self) -> str:
        payload = asdict(self)
        for key in ("shares", "share_trade", "simulation", "notes", "owner_word"):
            if payload[key] is None:
                payload.pop(key)
        return canonicalize(payload)

    @classmethod
    def from_facts_json(cls, facts_json: str) -> OptionPositionEndedFacts:
        return cls(**json.loads(facts_json))


@dataclass(frozen=True)
class OptionEventBookWrites:
    """The row writers ``folds.py`` owns, handed in so each row's shape is defined once."""

    insert_command: Callable[..., None]
    insert_receipt: Callable[..., None]
    move_position: Callable[..., None]
    this_sequence: Callable[[sqlite3.Connection], int]


def _whole(value: object, *, at_least: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
        raise ValueError(f"{what} must be a whole number, {at_least} or more; got {value!r}")
    return value


def _exact(value: object, *, what: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{what} is written as an exact decimal; got {value!r}")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{what} is not a decimal number: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"{what} is not finite: {value!r}")
    return amount


def _require_a_simulations_record(
    facts: OptionPositionEndedFacts, contract: OptionContract, *, ended_by: OptionEnding, close_price: Decimal
) -> None:
    """Refuse a simulated booking with no record of its own, and a real one that carries a simulation's.

    A simulation's record is what says where the price of its settlement
    came from. Its figures are worked out again here, from the price it
    names and the contract's own strike.
    """
    if not facts.simulated:
        if facts.simulation is not None:
            raise ValueError("a booking that no simulation made carries no simulation's record")
        if ended_by is OptionEnding.SETTLED_IN_CASH:
            raise ValueError("only a simulation settles an option in cash at its exercise value")
        return
    if facts.simulation is None:
        raise ValueError("a simulation's booking carries the simulation's own record of what it settled, and this has none")
    if ended_by not in (OptionEnding.EXPIRED, OptionEnding.SETTLED_IN_CASH, OptionEnding.ENDED_BY_OWNER):
        raise ValueError(
            f"a simulation ends a contract by expiry, in cash or at the owner's word, never as {ended_by.value}"
        )
    if facts.held != _BOUGHT:
        raise ValueError("a simulation settles only contracts it bought")
    check_settlement_note(
        facts.simulation, contract, multiplier=facts.multiplier, outcome=ended_by.value, close_price=close_price
    )
    if facts.contracts > facts.simulation["contracts"]:
        raise ValueError(
            f"{facts.contracts} contracts are booked from a settlement of {facts.simulation['contracts']}"
        )


def _require_the_owners_word(
    facts: OptionPositionEndedFacts, contract: OptionContract, *, ended_by: OptionEnding, now_ms: int
) -> None:
    """Refuse an owner's ending that does not say who, when and why, and any other ending that claims a word.

    Only a bought contract past its last session is ended at the owner's
    word. Before then it still trades and an order can close it. A sold one
    that was assigned moved shares, which no word undoes.
    """
    word = facts.owner_word
    if ended_by is not OptionEnding.ENDED_BY_OWNER:
        if word is not None:
            raise ValueError(f"an option that was {ended_by.value} was ended at nobody's word")
        return
    if word is None:
        raise ValueError("a contract ended at the owner's word records who said so, when, and why nothing settled it")
    operator, said_at_ms, why = word.get("operator"), word.get("said_at_ms"), word.get("why_no_settlement")
    if not isinstance(operator, str) or not operator.strip():
        raise ValueError("a contract ended at the owner's word records who said so")
    if isinstance(said_at_ms, bool) or not isinstance(said_at_ms, int) or said_at_ms < 0:
        raise ValueError("a contract ended at the owner's word records when it was said")
    if not isinstance(why, str) or not why.strip():
        raise ValueError("a contract ended at the owner's word records why no settlement could be made")
    if facts.held != _BOUGHT:
        raise ValueError("only a bought contract is ended at the owner's word")
    if not past_last_session(contract.expiry_date, now_ms):
        raise ValueError(
            f"{contract.occ_symbol} still trades: only a contract past its last session is ended at the owner's word"
        )
    if OWNER_WORD_ROWS_NOT_BOOKED in word:
        _require_the_rows_an_ending_names(facts, word[OWNER_WORD_ROWS_NOT_BOOKED])
    for key, what in (
        (OWNER_WORD_ROWS_BOOKED, "rows that were booked"),
        (OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED, "outside orders that could not be booked"),
    ):
        if key in word:
            _require_what_else_an_ending_names(facts, word[key], what=what)


def _require_the_rows_an_ending_names(facts: OptionPositionEndedFacts, rows: object) -> None:
    """Refuse an owner's ending whose list of the broker's rows that could not be booked is not whole.

    Each row is named by its reference and by the name it was refused by,
    one of the three an ending may be made beside
    (``REFUSALS_AN_ENDING_MAY_NAME``). A simulation's ending names none: no
    broker reports a simulated account's option events.
    """
    if facts.simulated:
        raise ValueError("a simulation's ending names no row of a broker's: none reports a simulated account's events")
    if not isinstance(rows, list) or not rows:
        raise ValueError("an owner's ending that names rows that could not be booked names at least one")
    for row in rows:
        ref, refused_as = (row.get("ref"), row.get("refused_as")) if isinstance(row, dict) else (None, None)
        if not isinstance(ref, str) or not ref.strip() or refused_as not in REFUSALS_AN_ENDING_MAY_NAME:
            raise ValueError(
                "each row an owner's ending names carries its reference and the name the rule refused it by"
            )


def _require_what_else_an_ending_names(facts: OptionPositionEndedFacts, named: object, *, what: str) -> None:
    """Refuse an owner's ending whose list of booked rows, or of outside orders, is not whole.

    Each is named by its reference and says what is known of it. A
    simulation's ending names none: no broker reports a simulated account's
    option events or places an order in it.
    """
    if facts.simulated:
        raise ValueError(f"a simulation's ending names no {what}: a simulated account has no broker")
    if not isinstance(named, list) or not named:
        raise ValueError(f"an owner's ending that names {what} names at least one")
    for one in named:
        ref, detail = (one.get("ref"), one.get("detail")) if isinstance(one, dict) else (None, None)
        if not isinstance(ref, str) or not ref.strip() or not isinstance(detail, str) or not detail.strip():
            raise ValueError(f"each of the {what} an owner's ending names carries its reference and what is known of it")


def _require_the_figures_the_contract_gives(
    facts: OptionPositionEndedFacts, contract: OptionContract, *, now_ms: int
) -> None:
    """Refuse facts whose side, price, shares or cash are not what the contract's terms give."""
    ended_by = OptionEnding(facts.ended_by)
    _whole(facts.contracts, at_least=1, what="the contracts an event took")
    if facts.held not in (_BOUGHT, _SOLD):
        raise ValueError(f"a lot is bought or sold; got {facts.held!r}")
    close_price = _exact(facts.close_price, what="the price a lot closed at")
    # The fill's id says whether the fee model prices it. A sale filed under the name of a
    # close at nothing would be charged no fee, without a word.
    prefix = close_execution_prefix(ended_by)
    if prefix is not None and not facts.close_execution_id.startswith(prefix):
        raise ValueError(f"the fill that closes a lot that was {ended_by.value} is named {prefix}...")
    _require_a_simulations_record(facts, contract, ended_by=ended_by, close_price=close_price)
    _require_the_owners_word(facts, contract, ended_by=ended_by, now_ms=now_ms)
    if ended_by is OptionEnding.SOLD_OUTSIDE:
        if close_price <= 0:
            raise ValueError("a sale by the broker closes a lot at the broker's price, above zero")
        if facts.shares is not None:
            raise ValueError("a sale of an option moves no shares")
        return
    if ended_by is OptionEnding.SETTLED_IN_CASH:
        if close_price <= 0:
            raise ValueError("a cash settlement closes a lot at its exercise value, above zero")
        if facts.shares is not None:
            raise ValueError("a cash settlement moves no shares")
        return
    if close_price != 0:
        raise ValueError(f"an option that {ended_by.value} closes its lot at a price of zero; got {close_price}")
    if ended_by is OptionEnding.ENDED_BY_OWNER:
        if facts.shares is not None:
            raise ValueError("a contract ended at the owner's word moves no shares")
        return
    if ended_by is OptionEnding.EXERCISED and facts.held != _BOUGHT:
        raise ValueError("only a bought option is exercised")
    if ended_by is OptionEnding.ASSIGNED and facts.held != _SOLD:
        raise ValueError("only a sold option is assigned")
    expected = expected_share_trade(
        _EVENT_KINDS[ended_by], contract, contracts=facts.contracts, multiplier=facts.multiplier
    )
    if expected is None:
        if facts.shares is not None:
            raise ValueError("an option that expired moves no shares")
        return
    stated = facts.shares
    if stated is None:
        raise ValueError(f"an option that was {ended_by.value} moves shares, and the record states none")
    if (
        stated.get("quantity") != expected.shares
        or _exact(stated.get("price"), what="the price of the shares") != expected.price
        or stated.get("cash_cents") != expected.cash_cents
    ):
        raise ValueError(
            f"{facts.contracts} of {contract.occ_symbol} {ended_by.value} move {expected.shares} shares at "
            f"{expected.price} for {expected.cash_cents} cents; the record states {stated!r}"
        )


def _insert_ended_order(
    conn: sqlite3.Connection, payload: dict[str, Any], *, order_ref: str, role: str
) -> None:
    """One order of the booking, written as already filled: nothing of it is working anywhere."""
    conn.execute(
        "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
        "role, broker_state, submitted_at_ms, updated_at_ms) VALUES (?, ?, ?, NULL, ?, 'filled', NULL, ?)",
        (order_ref, payload["effect_operation_id"], order_ref, role, payload["recorded_at_ms"]),
    )


def _insert_fill(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    books: OptionEventBookWrites,
    execution_id: str,
    order_ref: str,
    quantity: int,
    price: Decimal,
    side: str,
    at_ms: int,
    simulated: bool,
) -> None:
    conn.execute(
        "INSERT INTO fills (fill_id, order_ref, qty, price, side, is_correction, execution_id, "
        "evidence_source, event_kind, superseded_execution_ref, fee, fee_fidelity, "
        "source_event_at_ms, clerk_observed_at_ms, recorded_at_ms, recorded_transition_sequence) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'fill', NULL, NULL, 'not_reported', ?, ?, ?, ?)",
        (
            execution_id,
            order_ref,
            float(quantity),
            float(price),
            side,
            execution_id,
            "simulated_execution" if simulated else "activity_recovery",
            at_ms,
            payload["clerk_observed_at_ms"],
            payload["recorded_at_ms"],
            books.this_sequence(conn),
        ),
    )


def fold_option_position_ended(
    conn: sqlite3.Connection, payload: dict[str, Any], *, books: OptionEventBookWrites
) -> None:
    """Close one bot's lots for an event and, for an exercise or an assignment, book its shares at the strike.

    Refused, with nothing written, when: the contract's terms are not the
    ones recorded; the facts state a side, a price, shares or cash the
    contract's terms do not give; the bot does not hold that many contracts
    on that side; or the shares are in another symbol than the one the bot
    is registered under.
    """
    facts = OptionPositionEndedFacts.from_facts_json(payload["facts_json"])
    sid = payload["strategy_instance_id"]
    if not sid or not payload["command_id"] or not payload["effect_operation_id"]:
        raise ValueError("an option event is booked for a bot, under a command and an effect of its own")
    if payload["order_ref"] != facts.option_order_ref:
        raise ValueError("the booking's order is the one that closes the option lot")
    if payload["source_event_at_ms"] != facts.booked_as_of_ms:
        raise ValueError("the booking's time must be the one its facts state")
    # A sale by an order the Clerk did not place names that order, and nothing else names one:
    # the money reads stop counting an outside order by this name, and by no other.
    sold_by = facts.source_id if facts.ended_by == OptionEnding.SOLD_OUTSIDE.value else None
    if payload["broker_order_id"] != sold_by:
        raise ValueError(
            f"a booking names the outside order whose fill it books and no other; this one names "
            f"{payload['broker_order_id']!r} and books {facts.ended_by} from {facts.source_id!r}"
        )
    contract = OptionContract.from_occ_symbol(facts.contract_symbol)
    recorded = contract_terms(conn, facts.contract_symbol)
    if recorded is None or recorded.multiplier != facts.multiplier:
        raise ValueError(
            f"{facts.contract_symbol} is not on the books with a multiplier of {facts.multiplier}: "
            f"the books hold {recorded!r}"
        )
    _require_the_figures_the_contract_gives(facts, contract, now_ms=payload["clerk_observed_at_ms"])
    subject_id = bot_subject_id(sid)
    held = conn.execute(
        "SELECT attributed_qty FROM positions WHERE subject_id = ? AND symbol = ?", (subject_id, facts.contract_symbol)
    ).fetchone()
    held_qty = 0.0 if held is None else float(held["attributed_qty"])
    on_side = held_qty if facts.held == _BOUGHT else -held_qty
    if on_side < facts.contracts:
        raise ValueError(
            f"{facts.contracts} {facts.held} contracts of {facts.contract_symbol} cannot be ended for {sid}: "
            f"its books hold {held_qty:g}"
        )

    books.insert_command(
        conn,
        command_id=payload["command_id"],
        authority_generation=payload["authority_generation"],
        idempotency_key=facts.idempotency_key,
        payload_hash=facts.payload_hash,
        kind="operator_lifecycle",
        subject_id=subject_id,
        strategy_instance_id=sid,
        run_id=payload["run_id"],
        action=OPTION_EVENT_COMMAND_ACTION,
        intended_end_state=None,
        state="succeeded",
        recorded_at_ms=payload["recorded_at_ms"],
    )
    conn.execute(
        "INSERT INTO effect_operations (effect_operation_id, authority_generation, "
        "idempotency_key, command_id, subject_id, strategy_instance_id, run_id, kind, state, "
        "custody_owner, created_at_ms, updated_at_ms, terminal_receipt_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', 'ACCOUNT_CLERK', ?, ?, NULL)",
        (
            payload["effect_operation_id"],
            payload["authority_generation"],
            facts.idempotency_key,
            payload["command_id"],
            subject_id,
            sid,
            payload["run_id"],
            OPTION_EVENT_EFFECT_KIND,
            payload["recorded_at_ms"],
            payload["recorded_at_ms"],
        ),
    )
    receipt_id = f"receipt:{payload['command_id']}"
    books.insert_receipt(
        conn,
        receipt_id=receipt_id,
        command_id=payload["command_id"],
        effect_operation_id=payload["effect_operation_id"],
        terminal_state="succeeded",
        payload=payload,
    )
    conn.execute(
        "UPDATE commands SET effect_operation_id = ?, receipt_id = ? WHERE command_id = ?",
        (payload["effect_operation_id"], receipt_id, payload["command_id"]),
    )
    conn.execute(
        "UPDATE effect_operations SET terminal_receipt_id = ? WHERE effect_operation_id = ?",
        (receipt_id, payload["effect_operation_id"]),
    )

    # The lot closes: the bot sells what it bought, or buys back what it sold.
    close_side = "SELL" if facts.held == _BOUGHT else "BUY"
    _insert_ended_order(conn, payload, order_ref=facts.option_order_ref, role=OPTION_EVENT_ORDER_ROLE)
    bind_order_contract(conn, order_ref=facts.option_order_ref, symbol=facts.contract_symbol)
    _insert_fill(
        conn, payload, books=books, execution_id=facts.close_execution_id, order_ref=facts.option_order_ref,
        quantity=facts.contracts, price=_exact(facts.close_price, what="the price a lot closed at"),
        side=close_side, at_ms=facts.booked_as_of_ms, simulated=facts.simulated,
    )
    books.move_position(
        conn, payload=payload, symbol=facts.contract_symbol, side=close_side, quantity=float(facts.contracts)
    )
    if facts.shares is None:
        return

    # The shares are counted under the symbol the bot is registered under, as every share fill of its is.
    registered = conn.execute(
        "SELECT symbol FROM strategy_instances WHERE strategy_instance_id = ?", (sid,)
    ).fetchone()
    share_symbol = facts.shares.get("symbol")
    if registered is None or not isinstance(share_symbol, str) or registered["symbol"].upper() != share_symbol:
        raise ValueError(
            f"the shares of this event are in {share_symbol!r}, and {sid} is registered under "
            f"{None if registered is None else registered['symbol']!r}"
        )
    share_order_ref, share_execution_id = facts.shares.get("order_ref"), facts.shares.get("execution_id")
    if not isinstance(share_order_ref, str) or not isinstance(share_execution_id, str):
        raise ValueError("the shares of an event are booked on an order and an execution of their own")
    shares = facts.shares["quantity"]
    share_side = "BUY" if shares > 0 else "SELL"
    _insert_ended_order(conn, payload, order_ref=share_order_ref, role=OPTION_EVENT_SHARE_ORDER_ROLE)
    _insert_fill(
        conn, payload, books=books, execution_id=share_execution_id, order_ref=share_order_ref,
        quantity=abs(shares), price=_exact(facts.shares["price"], what="the price of the shares"),
        side=share_side, at_ms=facts.booked_as_of_ms, simulated=facts.simulated,
    )
    books.move_position(
        conn, payload={**payload, "order_ref": share_order_ref}, symbol=share_symbol, side=share_side,
        quantity=float(abs(shares)),
    )


__all__ = [
    "OPTION_ENDING_SUMMARY_CODES",
    "OPTION_EVENT_CASH_EXECUTION_PREFIX",
    "OPTION_EVENT_CLOSE_EXECUTION_PREFIX",
    "OPTION_EVENT_COMMAND_ACTION",
    "OPTION_EVENT_EFFECT_KIND",
    "OPTION_EVENT_ORDER_ROLE",
    "OPTION_EVENT_SHARES_EXECUTION_PREFIX",
    "OPTION_EVENT_SHARE_ORDER_ROLE",
    "OPTION_POSITION_ENDED",
    "OWNER_WORD_OUTSIDE_ORDERS_NOT_BOOKED",
    "OWNER_WORD_ROWS_BOOKED",
    "OWNER_WORD_ROWS_NOT_BOOKED",
    "REFUSALS_AN_ENDING_MAY_NAME",
    "OptionEnding",
    "OptionEventBookWrites",
    "OptionPositionEndedFacts",
    "close_execution_prefix",
    "closed_by_an_option_event",
    "fold_option_position_ended",
]
