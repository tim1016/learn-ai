"""What the books themselves say about option events: plain reads on one connection (ADR 0076 decision 22).

A leaf beside ``option_events``: it imports no repository, so a fold may ask
it a question. Everything here reads the books and writes nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3

from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.option_event_folds import OPTION_POSITION_ENDED, OptionPositionEndedFacts

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


__all__ = [
    "EVENT_KEY_PREFIX",
    "OUTSIDE_ORDER_BROKER",
    "SALE_KEY_PREFIX",
    "booking_key",
    "bookings",
    "books_record_no_contract",
    "held_option_positions",
    "outside_order_reduces_a_bots_contract",
]
