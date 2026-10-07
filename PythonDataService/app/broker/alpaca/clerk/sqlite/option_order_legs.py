"""The legs of one option order of several legs, as the books keep them (ADR 0076).

A structure of two to four legs is sent as one order. A broker answers with
that order and, under it, an order of its own for each leg: its own id, its
own status, its own fills. The books keep the same shape.

**The parent** is the one order that was sent: an ``orders`` row with the
role its operation gives it (``ENTRY``, or ``REDUCING`` for a close), under
the client order id that went to the broker. It is bound to no contract and
no fill ever lands on it. Its own order line says so with a marker
(``multi_leg_parent_line``): it names no symbol and no side, because it has
none, and no limit, because a net price is not the price of a share of
anything.

**A leg** is an ``orders`` row of its own, bound to its contract like any
option order, so its fills, its position, its fees and its profit are
counted by everything that counts an option order, unchanged. What sets it
apart:

- its role is ``OTHER``, which no reader of entries or reducing orders
  lists, and it has no row in ``operation_order_links``. The role alone
  does not say "a leg": the order that ends an option lot at an expiry or
  an assignment has it too (``option_event_folds``). A leg is told by its
  row in ``option_order_legs`` and by nothing else;
- its ``order_ref`` and ``client_order_id`` are ``leg_order_ref(parent, n)``:
  the parent's own reference, a tilde and the leg's number. That id is the
  books' own. It was never sent to a broker and is never looked up at one;
- its ``broker_order_id`` is the leg's own id at the broker, written when the
  parent's answer is first read;
- it has no creation record of its own. The fold that creates the parent
  creates its legs from the same fact (``record_order_legs``), so a rebuild
  from the mirror rebuilds them.

``option_order_legs`` ties the two: one row a leg, naming its parent, its
number in the order, the plan's leg id, the contract, the side, whether it
opens or closes, its ratio and the contracts asked of it. A parent is told
from every other order by having rows here.

Schema 24 adds this one table. It is empty for a bot in shares and for a bot
of one bought option: their orders have no legs.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTerms,
    ContractTermsConflict,
    bind_order_contract,
    record_contract_terms,
)
from app.broker.contract.options import OptionLegRequest
from app.options.structure import MAX_OPTION_LEGS

# v23 -> v24: the table is new and starts empty, so every existing row and hash
# is as it was. ``IF NOT EXISTS`` lets a file built at this version and marked
# as an earlier one migrate again, as the v23 statements do.
SCHEMA_V24_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS option_order_legs ("
    "order_ref TEXT PRIMARY KEY REFERENCES orders(order_ref), "
    "parent_order_ref TEXT NOT NULL REFERENCES orders(order_ref), "
    f"leg_number INTEGER NOT NULL CHECK (leg_number BETWEEN 1 AND {MAX_OPTION_LEGS}), "
    "leg_id TEXT NOT NULL, "
    "symbol TEXT NOT NULL REFERENCES option_contracts(symbol), "
    "side TEXT NOT NULL CHECK (side IN ('buy','sell')), "
    "intent TEXT NOT NULL CHECK (intent IN ('open','close')), "
    "ratio INTEGER NOT NULL CHECK (typeof(ratio) = 'integer' AND ratio >= 1), "
    "contracts INTEGER NOT NULL CHECK (typeof(contracts) = 'integer' AND contracts >= 1), "
    "UNIQUE (parent_order_ref, leg_number), "
    "UNIQUE (parent_order_ref, symbol))",
    "CREATE TRIGGER IF NOT EXISTS trg_option_order_legs_never_edited BEFORE UPDATE ON option_order_legs "
    "BEGIN SELECT RAISE(ABORT, 'a leg of an option order is never edited'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_option_order_legs_never_deleted BEFORE DELETE ON option_order_legs "
    "BEGIN SELECT RAISE(ABORT, 'a leg of an option order is never deleted'); END",
)
SCHEMA_V24_DDL = "\n".join(f"{statement};" for statement in SCHEMA_V24_STATEMENTS)

# The role of a leg's row: neither an entry nor a reduction. An option event's closing order has
# the same role, so a leg is never told by it (see ``NOT_A_LEG_SQL``).
LEG_ROLE = "OTHER"
# What a reader of ``orders o`` adds to leave out the rows that are legs of another order.
NOT_A_LEG_SQL = "NOT EXISTS (SELECT 1 FROM option_order_legs own_leg WHERE own_leg.order_ref = o.order_ref)"
# Whether the order ``o`` is the parent of an order of several legs.
MULTI_LEG_PARENT_SQL = "EXISTS (SELECT 1 FROM option_order_legs leg WHERE leg.parent_order_ref = o.order_ref)"
# What marks a parent's own order line (``multi_leg_parent_line``). Alpaca's own word for the order class.
MULTI_LEG_ORDER_CLASS = "mleg"

_LEG_REF_SEPARATOR = "~"


def leg_order_ref(parent_order_ref: str, leg_number: int) -> str:
    """The books' own reference of leg ``leg_number`` (from one) of the order sent as ``parent_order_ref``.

    Never sent to a broker, which knows a leg by an id of its own making.
    """
    if not parent_order_ref or _LEG_REF_SEPARATOR in parent_order_ref:
        raise ValueError(f"{parent_order_ref!r} cannot be the parent of a leg: it is empty or is itself a leg's reference")
    if isinstance(leg_number, bool) or not isinstance(leg_number, int) or not 1 <= leg_number <= MAX_OPTION_LEGS:
        raise ValueError(f"a leg is numbered 1 to {MAX_OPTION_LEGS}; got {leg_number!r}")
    return f"{parent_order_ref}{_LEG_REF_SEPARATOR}{leg_number}"


@dataclass(frozen=True, slots=True)
class OrderLeg:
    """One leg of an order of several legs, as the books keep it."""

    # The leg's own row in ``orders``.
    order_ref: str
    parent_order_ref: str
    # Its place in the order, from one.
    leg_number: int
    # The plan's leg id. A close's legs carry the entry's.
    leg_id: str
    # The contract's OCC symbol.
    symbol: str
    side: Literal["buy", "sell"]
    intent: Literal["open", "close"]
    ratio: int
    # Asked of this leg: its ratio times the parent's units.
    contracts: int


def record_order_legs(
    conn: sqlite3.Connection,
    *,
    parent_order_ref: str,
    effect_operation_id: str,
    units: int,
    legs: Sequence[tuple[str, OptionLegRequest, ContractTerms]],
    recorded_at_ms: int,
) -> None:
    """Create the leg rows of the parent order ``parent_order_ref``. A fold helper.

    ``legs`` are (the plan's leg id, the leg as it was asked, its contract's
    terms), in the order's own leg order. It runs on the connection of the
    fold that creates the parent's row, inside that fold's transaction: an
    entry's acceptance, or a closing order's creation. So a parent never
    exists without its legs, each leg's contract is recorded and bound with
    it, and a rebuild from the mirror rebuilds all of it.
    """
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"an order of several legs is for a whole number of structures, one or more; got {units!r}")
    if not 2 <= len(legs) <= MAX_OPTION_LEGS:
        raise ValueError(f"an order of several legs has two to {MAX_OPTION_LEGS} legs; this one has {len(legs)}")
    for leg_number, (leg_id, leg, terms) in enumerate(legs, start=1):
        if leg.contract.occ_symbol != terms.symbol:
            raise ContractTermsConflict(
                f"leg {leg_id!r} of order {parent_order_ref!r} is asked in {leg.contract.occ_symbol} "
                f"and its terms are of {terms.symbol}"
            )
        order_ref = leg_order_ref(parent_order_ref, leg_number)
        record_contract_terms(conn, terms, recorded_at_ms=recorded_at_ms)
        conn.execute(
            "INSERT INTO orders (order_ref, effect_operation_id, client_order_id, broker_order_id, "
            "role, broker_state, submitted_at_ms, updated_at_ms) "
            "VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?)",
            (order_ref, effect_operation_id, order_ref, LEG_ROLE, recorded_at_ms),
        )
        bind_order_contract(conn, order_ref=order_ref, symbol=terms.symbol)
        conn.execute(
            "INSERT INTO option_order_legs (order_ref, parent_order_ref, leg_number, leg_id, symbol, side, intent, "
            "ratio, contracts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order_ref, parent_order_ref, leg_number, leg_id, terms.symbol, leg.side.value, leg.intent.value,
                leg.ratio, leg.ratio * units,
            ),
        )


def _order_leg(row: sqlite3.Row | tuple[Any, ...]) -> OrderLeg:
    return OrderLeg(
        order_ref=row[0], parent_order_ref=row[1], leg_number=row[2], leg_id=row[3], symbol=row[4],
        side=row[5], intent=row[6], ratio=row[7], contracts=row[8],
    )


_LEG_COLUMNS = "order_ref, parent_order_ref, leg_number, leg_id, symbol, side, intent, ratio, contracts"


def order_legs(conn: sqlite3.Connection, parent_order_ref: str) -> tuple[OrderLeg, ...]:
    """The legs of the parent order ``parent_order_ref``, in leg order. Empty for every other order."""
    rows = conn.execute(
        f"SELECT {_LEG_COLUMNS} FROM option_order_legs WHERE parent_order_ref = ? ORDER BY leg_number",
        (parent_order_ref,),
    ).fetchall()
    return tuple(_order_leg(row) for row in rows)


def order_leg(conn: sqlite3.Connection, order_ref: str) -> OrderLeg | None:
    """The leg whose own row is ``order_ref``, or ``None`` for an order that is no leg."""
    row = conn.execute(
        f"SELECT {_LEG_COLUMNS} FROM option_order_legs WHERE order_ref = ?",
        (order_ref,),
    ).fetchone()
    return None if row is None else _order_leg(row)


def leg_parent(conn: sqlite3.Connection, order_ref: str) -> str | None:
    """The parent of the leg whose row is ``order_ref``, or ``None`` for an order that is no leg."""
    row = conn.execute("SELECT parent_order_ref FROM option_order_legs WHERE order_ref = ?", (order_ref,)).fetchone()
    return None if row is None else row[0]


def is_multi_leg_parent(conn: sqlite3.Connection, order_ref: str) -> bool:
    """Whether ``order_ref`` is the parent of an order of several legs: it has legs on the books."""
    return (
        conn.execute("SELECT 1 FROM option_order_legs WHERE parent_order_ref = ? LIMIT 1", (order_ref,)).fetchone()
        is not None
    )


def multi_leg_parent_line(*, units: int, order_type: str) -> dict[str, Any]:
    """A parent's own order line, in the keys every order line is written in.

    It states no symbol and no side: a parent has neither, and a reader
    that compares an order's symbol with a ticker or a contract finds none
    here. ``quantity`` is whole structures. The limit is null whatever the
    order's kind: a net price is signed and is not a price a share, so no
    reader that multiplies a quantity by a limit can price it as one. The
    signed net limit is in the order's own facts.
    """
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError(f"an order of several legs is for a whole number of structures, one or more; got {units!r}")
    if order_type not in ("limit", "market"):
        raise ValueError(f"an option order is a limit or a market order; got {order_type!r}")
    return {
        "order_class": MULTI_LEG_ORDER_CLASS,
        "symbol": None,
        "side": None,
        "quantity": float(units),
        "order_type": order_type,
        "limit_price": None,
        "time_in_force": "day",
        "extended_hours": False,
    }


__all__ = [
    "LEG_ROLE",
    "MULTI_LEG_ORDER_CLASS",
    "MULTI_LEG_PARENT_SQL",
    "NOT_A_LEG_SQL",
    "SCHEMA_V24_DDL",
    "SCHEMA_V24_STATEMENTS",
    "OrderLeg",
    "is_multi_leg_parent",
    "leg_order_ref",
    "leg_parent",
    "multi_leg_parent_line",
    "order_leg",
    "order_legs",
    "record_order_legs",
]
