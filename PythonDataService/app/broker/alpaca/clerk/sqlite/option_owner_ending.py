"""The owner's ending of an expired option contract that nothing will ever settle (ADR 0076 decision 22).

A contract past its last session is taken off the books by its settlement:
the broker's own activity on a real account, the simulation's rule on a Dry
Run. Sometimes none will ever come. A Dry Run that kept no price of the
underlying at the close cannot settle. A real account's broker may hold none
of the contract and report nothing of how it ended. Then the position would
stay on the books for ever, and its bot with it.

So the owner can end it at their word. The lot closes at a price of zero:
what the contract cost is the realised loss, in History and in the budget.
No fee, no shares, no order. The record says who, when, and why no
settlement could be made. It is booked through the same read and the same
batch function as every other end of an option position
(``option_events.book_option_events``), as an ``OPTION_POSITION_ENDED``.

**It is not the residue write-off.** A write-off leaves the lot open and
realises nothing, so the premium would sit in the budget as a position for
good. Once a contract has expired the write-off is never offered for it.

**When it is offered.** The contract is past its last session, and the
bot's ``OPTION_EXPIRED_NOT_SETTLED`` hold is open and names it: a whole read
made after the expiry found no settlement. On a real account one more thing:
the close of the first session after the contract's last
(``OWNER_ENDING_WAIT_SESSIONS``), so the broker's record has had a full
business day to come.

**At the press** the Clerk looks once more. A Dry Run's simulation is asked
for its price again, and a price that has come settles the contract by the
simulation's rule. A real account's option events are read whole, and a row
that has come is booked in its place. Only when there is still nothing, and
on a real account the broker holds none of the contract, is it ended at the
owner's word.

**A row that comes afterwards** on a real account is still read for
``OWNER_ENDING_LATE_ROW_DAYS`` days. An expiry's row books nothing: the lot
is closed. An exercise's or an assignment's row holds that bot by name: the
shares are real and on no bot's books.

This module is a leaf: the rule's constants, the request and its outcome,
and the plain reads of the books' own record of such endings.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.broker.alpaca.clerk.sqlite.option_event_books import booking_key, bookings
from app.broker.alpaca.clerk.sqlite.option_event_folds import OPTION_POSITION_ENDED, OptionPositionEndedFacts
from app.lean_sidecar.trading_calendar import next_trading_day, session_close_ms_utc
from app.options.deadlines import last_session_close_ms
from app.utils.session_anchors import et_date_at_ms

# What the action is called: its label where it is offered, and what a notice that sends the owner to it quotes.
END_EXPIRED_OPTION_LABEL = "End the expired contract"

# How many whole sessions after a contract's last one the broker's record of its end is waited for,
# before the owner may end the contract at their word on a real account. Until the timing of
# Alpaca's row has been seen on an account, one full business day.
OWNER_ENDING_WAIT_SESSIONS = 1
# How long after an owner's ending on a real account the account's option events are still read
# back to that contract's expiry date, for a row that comes late.
OWNER_ENDING_LATE_ROW_DAYS = 7
_DAY_MS = 86_400_000

# The idempotency key of an owner's ending starts with this; one key for each contract, then its bot.
OWNER_ENDING_KEY_PREFIX = "option-owner-ending:"
# Who an owner's ending on a real account is recorded as reported by, where an event names its broker.
OWNER_ENDING_SOURCE = "owner"

# Why an owner's ending was refused, each under its own code.
NOTHING_HELD = "OPTION_OWNER_ENDING_NOTHING_HELD"
CONTRACT_STILL_TRADES = "OPTION_OWNER_ENDING_CONTRACT_STILL_TRADES"
SOLD_CONTRACT = "OPTION_OWNER_ENDING_SOLD_CONTRACT"
READ_NOT_WHOLE = "OPTION_OWNER_ENDING_READ_NOT_WHOLE"
ROW_NOT_BOOKED = "OPTION_OWNER_ENDING_ROW_NOT_BOOKED"
BROKER_POSITION_UNREAD = "OPTION_OWNER_ENDING_BROKER_POSITION_UNREAD"
BROKER_STILL_HOLDS = "OPTION_OWNER_ENDING_BROKER_STILL_HOLDS"
RECORD_MAY_STILL_COME = "OPTION_OWNER_ENDING_RECORD_MAY_STILL_COME"
NOT_HELD_FOR_AS_UNSETTLED = "OPTION_OWNER_ENDING_NOT_HELD_FOR"
LOTS_DISAGREE = "OPTION_OWNER_ENDING_LOTS_DISAGREE"
WAITING_FOR_THE_CLOSING_PRICE = "OPTION_OWNER_ENDING_WAITING_FOR_THE_CLOSING_PRICE"
NOT_SETTLED_BY_THE_SIMULATION = "OPTION_OWNER_ENDING_SIMULATION_CANNOT_LET_GO"


@dataclass(frozen=True, slots=True)
class OwnerEndingRequest:
    """The owner's word that one bot's expired contract, which nothing will settle, is to be counted as worth nothing."""

    strategy_instance_id: str
    # The contract's OCC symbol.
    symbol: str
    # Who says so: the installation's operator, never a request's own field.
    operator: str
    # The owner's own reason, when one was given.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerEndingOutcome:
    """What came of one owner's ending.

    ``ended``: the contract was ended at the owner's word, now. ``settled``:
    it was not, because a settlement had come after all and was booked in
    its place; ``ended_by`` says how. ``refused``: nothing was booked for
    it, and ``reason_code`` and ``message`` say why.
    """

    result: Literal["ended", "settled", "refused"]
    message: str
    reason_code: str | None = None
    # How the position is recorded as ended, when it is.
    ended_by: str | None = None
    # The command of the booking that ended it, when one did.
    command_id: str | None = None


def expired_contract_hold_ref(symbol: str) -> str:
    """How the ``OPTION_EXPIRED_NOT_SETTLED`` hold names one contract."""
    return f"contract:{symbol.upper()}"


def owner_ending_offered_from_ms(expiry: date) -> int:
    """When the owner may first end a real account's contract that expires on ``expiry`` at their word.

    The close of the session ``OWNER_ENDING_WAIT_SESSIONS`` sessions after
    the contract's last: by then the broker's record of how it ended has
    had that long to be published. Every session is the calendar's, a half
    day's early close included.
    """
    session = et_date_at_ms(last_session_close_ms(expiry) - 1)
    for _ in range(OWNER_ENDING_WAIT_SESSIONS):
        session = next_trading_day(session)
    return session_close_ms_utc(session)


def late_row_read_since_ms(now_ms: int) -> int:
    """The oldest an owner's ending may be for the account's option events still to be read for its late row."""
    return now_ms - OWNER_ENDING_LATE_ROW_DAYS * _DAY_MS


def owner_ending_key(symbol: str) -> str:
    """The idempotency key of the owner's ending of one contract, without its bot."""
    return booking_key(OWNER_ENDING_KEY_PREFIX, OWNER_ENDING_SOURCE, symbol.upper())


def owner_ended(conn: sqlite3.Connection, symbol: str) -> dict[str, OptionPositionEndedFacts]:
    """Each bot whose position in one contract a real account's owner ended at their word, with the record."""
    return bookings(conn, owner_ending_key(symbol))


def recent_owner_endings(conn: sqlite3.Connection, *, since_ms: int) -> list[int]:
    """The expiry date of each contract a real account's owner ended at their word since ``since_ms``.

    Each as 00:00 New York time on the date, the anchor the booking carries.
    Read by the key every such ending is booked under: an index range,
    never a scan. A simulation's ending is booked under its settlement's own
    key and is not among them: no broker reports a row for it, late or ever.
    """
    generation = conn.execute("SELECT authority_generation FROM control_meta WHERE id = 1").fetchone()[0]
    rows = conn.execute(
        "SELECT json_extract(t.facts_json, '$.event_date_ms') AS dated_ms FROM commands c "
        "JOIN custody_transitions t ON t.effect_operation_id = c.effect_operation_id AND t.transition_kind = ? "
        "WHERE c.authority_generation = ? AND c.idempotency_key >= ? AND c.idempotency_key < ? "
        "AND c.created_at_ms >= ?",
        (
            OPTION_POSITION_ENDED, generation, OWNER_ENDING_KEY_PREFIX,
            OWNER_ENDING_KEY_PREFIX[:-1] + chr(ord(OWNER_ENDING_KEY_PREFIX[-1]) + 1), since_ms,
        ),
    ).fetchall()
    return [int(row["dated_ms"]) for row in rows if row["dated_ms"] is not None]


__all__ = [
    "END_EXPIRED_OPTION_LABEL",
    "OWNER_ENDING_KEY_PREFIX",
    "OWNER_ENDING_LATE_ROW_DAYS",
    "OWNER_ENDING_SOURCE",
    "OWNER_ENDING_WAIT_SESSIONS",
    "OwnerEndingOutcome",
    "OwnerEndingRequest",
    "expired_contract_hold_ref",
    "late_row_read_since_ms",
    "owner_ended",
    "owner_ending_key",
    "owner_ending_offered_from_ms",
    "recent_owner_endings",
]
