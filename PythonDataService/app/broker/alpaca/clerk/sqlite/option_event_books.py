"""What the books themselves say about option events: plain reads on one connection (ADR 0076 decision 22).

A leaf beside ``option_events``: it imports no repository, so a fold may ask
it a question. Everything here reads the books and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.contract_terms import contract_terms
from app.broker.alpaca.clerk.sqlite.models import ExternalOrderResource
from app.broker.alpaca.clerk.sqlite.option_event_folds import (
    OPTION_EVENT_EFFECT_KIND,
    OPTION_EVENT_SHARE_ORDER_ROLE,
    OPTION_POSITION_ENDED,
    OptionEnding,
    OptionPositionEndedFacts,
)
from app.broker.alpaca.clerk.sqlite.order_projection import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_ORDER_NOT_OURS_REASON_CODE

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
    the account-wide hold refuses every reduction of every bot, for one
    bot's one contract. An order already booked against a bot as a sale
    stays tied to it.
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


# ── What the owner has acknowledged ──────────────────────────────────────────

# The summary code of the resolution an owner's acknowledgement of an option-event hold writes.
OPTION_EVENT_HOLD_ACKNOWLEDGED = "OPTION_EVENT_HOLD_ACKNOWLEDGED"
ACK_ITEM_REF_PREFIX = "item:"


def hold_item_ack_ref(strategy_instance_id: str | None, reason_code: str, item_ref: str) -> str:
    """The reference an acknowledgement carries for one thing one hold named."""
    return f"{ACK_ITEM_REF_PREFIX}{strategy_instance_id or '-'}:{reason_code}:{item_ref}"


def acknowledged_hold_items(conn: sqlite3.Connection) -> frozenset[str]:
    """Every (bot, reason, thing) the owner has acknowledged, as the references the acknowledgements carry."""
    rows = conn.execute(
        "SELECT facts_json FROM custody_transitions WHERE transition_kind = 'UNCERTAINTY_RESOLVED' AND summary_code = ?",
        (OPTION_EVENT_HOLD_ACKNOWLEDGED,),
    ).fetchall()
    return frozenset(
        ref
        for row in rows
        for ref in json.loads(row["facts_json"])["evidence_refs"]
        if ref.startswith(ACK_ITEM_REF_PREFIX)
    )


# ── Orders the Clerk did not place, in a contract a bot holds ────────────────


class OutsideFillUnknown(Exception):
    """An outside order does not state what it filled."""


def outside_orders_in(conn: sqlite3.Connection, symbol: str) -> list[ExternalOrderResource]:
    """The outside orders the books recorded in one contract, oldest first."""
    rows = conn.execute(
        "SELECT broker_order_id FROM external_orders WHERE UPPER(symbol) = ? ORDER BY observed_at_ms, broker_order_id",
        (symbol,),
    ).fetchall()
    orders = [reads.external_order_by_broker_order_id(conn, row["broker_order_id"]) for row in rows]
    return [order for order in orders if order is not None]


def outside_fill(order: ExternalOrderResource) -> int:
    """Whole contracts an outside order filled, signed as it changed the account's position."""
    filled = order.filled_quantity
    if filled is None:
        if order.filled_avg_price is None and (order.broker_state or "").lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES:
            # Ended with no fill price: nothing filled.
            return 0
        raise OutsideFillUnknown(order.broker_order_id)
    if not float(filled).is_integer():
        raise OutsideFillUnknown(order.broker_order_id)
    return int(filled) if order.side == "BUY" else -int(filled)


def outside_order_hold_ref(broker_order_id: str) -> str:
    """How a hold names one outside order."""
    return f"outside-order:{broker_order_id}"


@dataclass(frozen=True, slots=True)
class UnsettledOutsideOrder:
    """An outside order in a recorded contract that the books have not settled, and the bots it stands against."""

    order: ExternalOrderResource
    # Whole contracts it filled. None when the order does not state it.
    filled: int | None
    # Whether the broker's last word on the order is that it has ended.
    ended: bool
    # The bots that hold the contract on the side the order reduces. Every holder when its fill is not stated.
    against: tuple[str, ...]

    @property
    def sentence(self) -> str:
        """What the order is, in one sentence the owner reads."""
        order = self.order
        named = f"Order {order.broker_order_id} to {order.side.lower()} {order.qty:g} of {order.symbol.upper()}"
        if self.filled is None:
            return f"{named} does not state what it filled."
        if not self.ended:
            return f"{named} is {(order.broker_state or '').lower() or 'in a state the broker did not give'}."
        return f"{named} filled {self.filled}."


def unsettled_outside_order(conn: sqlite3.Connection, order: ExternalOrderResource) -> UnsettledOutsideOrder | None:
    """What one outside order still asks of the books, or None when it asks nothing.

    It asks nothing when no bot holds its contract on the side it reduces,
    when its fill is already booked against a bot as a sale, and when it
    ended having filled nothing. Until then it is unsettled: still working,
    or filled and not yet booked.
    """
    symbol = order.symbol.upper()
    held_by = {sid: quantity for sid, held_symbol, quantity in held_option_positions(conn) if held_symbol == symbol}
    if not held_by or bookings(conn, booking_key(SALE_KEY_PREFIX, OUTSIDE_ORDER_BROKER, order.broker_order_id)):
        return None
    ended = (order.broker_state or "").lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
    try:
        filled = abs(outside_fill(order))
    except OutsideFillUnknown:
        return UnsettledOutsideOrder(order=order, filled=None, ended=ended, against=tuple(sorted(held_by)))
    against = tuple(sorted(sid for sid, quantity in held_by.items() if (quantity > 0) == (order.side == "SELL")))
    if not against or (ended and not filled):
        return None
    return UnsettledOutsideOrder(order=order, filled=filled, ended=ended, against=against)


def outside_order_reviewed(
    conn: sqlite3.Connection, strategy_instance_id: str, order: ExternalOrderResource
) -> bool:
    """Whether the owner has looked at an outside order: reviewed the order itself, or acknowledged this bot's hold for it."""
    if order.acknowledged_at_ms is not None:
        return True
    return hold_item_ack_ref(
        strategy_instance_id, OPTION_ORDER_NOT_OURS_REASON_CODE, outside_order_hold_ref(order.broker_order_id)
    ) in acknowledged_hold_items(conn)


def outside_order_in_the_way(
    conn: sqlite3.Connection, *, strategy_instance_id: str, symbol: str | None = None
) -> UnsettledOutsideOrder | None:
    """The oldest unsettled outside order that stands against one bot and that the owner has not reviewed.

    The fact every check of a bot's own order reads (ADR 0076 decision 22).
    While it is not None, an order of the bot to close ``symbol`` would be a
    second order on contracts the outside order may sell, or has sold and
    the books have not yet counted. ``symbol`` names one contract; without
    it every contract the bot holds is asked about.

    Read from the books alone, from the record the observation's own
    transition wrote, so it is true from that transition on and needs no
    hold raised first. Books that never recorded a contract stop at the
    first read.
    """
    if books_record_no_contract(conn):
        return None
    wanted = None if symbol is None else symbol.upper()
    held = sorted(
        {
            held_symbol
            for sid, held_symbol, _quantity in held_option_positions(conn)
            if sid == strategy_instance_id and wanted in (None, held_symbol)
        }
    )
    for held_symbol in held:
        for order in outside_orders_in(conn, held_symbol):
            unsettled = unsettled_outside_order(conn, order)
            if (
                unsettled is not None
                and strategy_instance_id in unsettled.against
                and not outside_order_reviewed(conn, strategy_instance_id, order)
            ):
                return unsettled
    return None


def written_off_contracts(conn: sqlite3.Connection, strategy_instance_id: str, symbol: str) -> int:
    """Contracts of ``symbol`` the owner wrote off one bot's books, signed as they were held.

    Read from the write-offs themselves. A position that was written off is
    on the books no more, and the lots that made it stay open in the fills.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(json_extract(d.facts_json, '$.discharged_qty')), 0) AS contracts "
        "FROM custody_transitions d WHERE d.strategy_instance_id = ? "
        "AND d.transition_kind = 'ATTRIBUTED_RESIDUE_DISCHARGED' "
        "AND UPPER(json_extract(d.facts_json, '$.symbol')) = ?",
        (strategy_instance_id, symbol.upper()),
    ).fetchone()
    return int(row["contracts"])


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
    elif ended_by is OptionEnding.SETTLED_IN_CASH:
        said = f"{what} {was} settled in cash at {facts.close_price} a share."
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
