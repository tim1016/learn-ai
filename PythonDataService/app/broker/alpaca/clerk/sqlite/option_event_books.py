"""What the books themselves say about option events: plain reads on one connection (ADR 0076 decision 22).

A leaf beside ``option_events``: it imports no repository, so a fold may ask
it a question. Everything here reads the books and writes nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.option_event_folds import (
    OPTION_EVENT_EFFECT_KIND,
    OPTION_EVENT_SHARE_ORDER_ROLE,
    OPTION_POSITION_ENDED,
    OptionEnding,
    OptionPositionEndedFacts,
)

# The idempotency key of a booking starts with what was booked: an activity, or an outside order's fill.
EVENT_KEY_PREFIX = "option-event:"
SALE_KEY_PREFIX = "option-outside-sale:"
# The broker whose orders the books record as outside orders.
OUTSIDE_ORDER_BROKER = "alpaca"


def books_record_no_contract(conn: sqlite3.Connection) -> bool:
    """Whether these books never recorded an option contract. One read of one small table."""
    return conn.execute("SELECT 1 FROM option_contracts LIMIT 1").fetchone() is None


def held_option_positions(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """Every bot's open position in a recorded contract, as (bot, symbol, signed contracts).

    Read from the contracts outward, so books with no contract read no position.
    """
    rows = conn.execute(
        "SELECT p.strategy_instance_id AS sid, p.symbol AS symbol, p.attributed_qty AS qty "
        "FROM option_contracts c CROSS JOIN positions p ON p.symbol = c.symbol "
        "WHERE p.strategy_instance_id IS NOT NULL AND p.attributed_qty != 0 "
        "ORDER BY p.symbol, p.strategy_instance_id"
    ).fetchall()
    return [(row["sid"], row["symbol"], int(row["qty"])) for row in rows]


def booking_key(prefix: str, broker: str, source_id: str) -> str:
    """The idempotency key of one event or one outside order, without its bot. Fixed length, so a prefix names one."""
    digest = hashlib.sha256(f"{broker}\n{source_id}".encode()).hexdigest()[:32]
    return f"{prefix}{digest}:"


def bookings(conn: sqlite3.Connection, key: str) -> dict[str, OptionPositionEndedFacts]:
    """Each bot's recorded booking under one event's or one order's key, by bot. An index range, never a scan."""
    generation = conn.execute("SELECT authority_generation FROM control_meta WHERE id = 1").fetchone()[0]
    rows = conn.execute(
        "SELECT strategy_instance_id, effect_operation_id FROM commands "
        "WHERE authority_generation = ? AND idempotency_key >= ? AND idempotency_key < ?",
        (generation, key, key[:-1] + chr(ord(key[-1]) + 1)),
    ).fetchall()
    found: dict[str, OptionPositionEndedFacts] = {}
    for row in rows:
        recorded = conn.execute(
            "SELECT facts_json FROM custody_transitions WHERE effect_operation_id = ? AND transition_kind = ? "
            "ORDER BY sequence LIMIT 1",
            (row["effect_operation_id"], OPTION_POSITION_ENDED),
        ).fetchone()
        if recorded is not None:
            found[row["strategy_instance_id"]] = OptionPositionEndedFacts.from_facts_json(recorded["facts_json"])
    return found


def outside_order_reduces_a_bots_contract(conn: sqlite3.Connection, *, symbol: str, side: str, broker_order_id: str) -> bool:
    """Whether an outside order is one the books tie to a bot: in a recorded contract a bot holds, on the reducing side.

    Such an order is held against that bot and never against the account:
    the account-wide hold refuses reductions, and a bot must stay able to
    close its own position beside an order the broker placed. An order
    already booked against a bot as a sale stays tied to it.
    """
    contract_symbol = symbol.upper()
    if books_record_no_contract(conn) or contract_terms(conn, contract_symbol) is None:
        return False
    if bookings(conn, booking_key(SALE_KEY_PREFIX, OUTSIDE_ORDER_BROKER, broker_order_id)):
        return True
    return any(
        held_symbol == contract_symbol and (quantity > 0) == (side.upper() == "SELL")
        for _sid, held_symbol, quantity in held_option_positions(conn)
    )


def event_share_order_symbol(conn: sqlite3.Connection, order_ref: str) -> str | None:
    """The symbol of the shares an option event left on ``order_ref``, or None when the order is no such thing.

    Such an order was written by the booking's fold, under its effect, and
    is bound to no contract. Its shares are counted under the symbol their
    bot is registered under, as every share fill of a bot is.
    """
    row = conn.execute(
        "SELECT s.symbol FROM orders o "
        "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "JOIN strategy_instances s ON s.strategy_instance_id = e.strategy_instance_id "
        "LEFT JOIN order_contracts bound ON bound.order_ref = o.order_ref "
        "WHERE o.order_ref = ? AND o.role = ? AND e.kind = ? AND bound.order_ref IS NULL",
        (order_ref, OPTION_EVENT_SHARE_ORDER_ROLE, OPTION_EVENT_EFFECT_KIND),
    ).fetchone()
    return None if row is None else row["symbol"]


@dataclass(frozen=True, slots=True)
class RecordedOptionEnding:
    """One option position the books recorded as ended, for the bot and the run that held it."""

    effect_operation_id: str
    strategy_instance_id: str
    # The run whose entry opened the oldest lot it took. None when that entry had none.
    run_id: str | None
    recorded_at_ms: int
    facts: OptionPositionEndedFacts

    @property
    def sentence(self) -> str:
        return describe_option_ending(self.facts)


def describe_option_ending(facts: OptionPositionEndedFacts) -> str:
    """How one option position ended, in one sentence the owner reads. Written here, never on a screen."""
    ended_by = OptionEnding(facts.ended_by)
    one = facts.contracts == 1
    what = f"{facts.contracts} {'contract' if one else 'contracts'} of {facts.contract_symbol}"
    was = "was" if one else "were"
    if ended_by is OptionEnding.EXPIRED:
        said = f"{what} expired."
    elif ended_by is OptionEnding.SOLD_OUTSIDE:
        done = "sold" if facts.held == "bought" else "bought back"
        said = f"{what} {was} {done} at {facts.close_price} by an order the Clerk did not place."
    else:
        shares = facts.shares or {}
        quantity = shares.get("quantity", 0)
        said = (
            f"{what} {was} {ended_by.value}: {abs(quantity)} {shares.get('symbol', '')} "
            f"{'bought' if quantity > 0 else 'sold'} at {shares.get('price', '')}."
        )
    return f"{said} A simulation settled it by its own rule." if facts.simulated else said


def recorded_option_endings(
    conn: sqlite3.Connection, effect_operation_ids: Sequence[str] | None = None
) -> list[RecordedOptionEnding]:
    """Every option position the books recorded as ended, oldest first; or those of the effects named.

    With no effects named it is read from the orders bound to a contract
    outward, so books with no option order read nothing else.
    """
    columns = (
        "SELECT t.effect_operation_id AS effect_operation_id, t.strategy_instance_id AS sid, t.run_id AS run_id, "
        "t.recorded_at_ms AS recorded_at_ms, t.facts_json AS facts_json, t.sequence AS sequence "
    )
    if effect_operation_ids is None:
        rows = conn.execute(
            columns + "FROM order_contracts bound "
            "JOIN orders o ON o.order_ref = bound.order_ref "
            "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id AND e.kind = ? "
            "JOIN custody_transitions t ON t.effect_operation_id = e.effect_operation_id AND t.transition_kind = ? "
            "ORDER BY t.sequence",
            (OPTION_EVENT_EFFECT_KIND, OPTION_POSITION_ENDED),
        ).fetchall()
    elif not effect_operation_ids:
        rows = []
    else:
        marks = ", ".join("?" for _ in effect_operation_ids)
        rows = conn.execute(
            columns + f"FROM custody_transitions t WHERE t.effect_operation_id IN ({marks}) "
            "AND t.transition_kind = ? ORDER BY t.sequence",
            (*effect_operation_ids, OPTION_POSITION_ENDED),
        ).fetchall()
    return [
        RecordedOptionEnding(
            effect_operation_id=row["effect_operation_id"], strategy_instance_id=row["sid"], run_id=row["run_id"],
            recorded_at_ms=int(row["recorded_at_ms"]),
            facts=OptionPositionEndedFacts.from_facts_json(row["facts_json"]),
        )
        for row in rows
    ]


__all__ = [
    "EVENT_KEY_PREFIX",
    "OUTSIDE_ORDER_BROKER",
    "SALE_KEY_PREFIX",
    "RecordedOptionEnding",
    "booking_key",
    "bookings",
    "books_record_no_contract",
    "describe_option_ending",
    "event_share_order_symbol",
    "held_option_positions",
    "outside_order_reduces_a_bots_contract",
    "recorded_option_endings",
]
