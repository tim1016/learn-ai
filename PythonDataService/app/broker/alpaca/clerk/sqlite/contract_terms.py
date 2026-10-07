"""Which option contract an order trades, and the multiplier its money is counted with (ADR 0076 decision 13).

A contract's multiplier is a fact of its listing: how many shares one
contract's per-share price stands for. It is recorded once, when the contract
first enters the books, in ``option_contracts``. ``order_contracts`` says
which contract an order trades. A fill names its order, so custody reads a
fill's contract and its multiplier through the order.

A stock order has no row in either table. Its fills are shares, counted as
they always were: the readers join these tables and take "no row" to mean
"no multiplier".

Nothing here reads a multiplier off a symbol. A symbol's shape is used for one
thing only, to refuse an option fill the books cannot count.

The two writers are fold helpers. They run on the connection of the fold that
creates the order, inside that fold's transaction, so an option order never
exists without its contract, and a rebuild from the mirror rebuilds both
tables. Neither is called by any fold yet: the option order path writes them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from app.broker.alpaca.clerk.money import MoneyInputError, contract_multiplier
from app.options.contract import OptionContract
from app.options.exact import whole_units

# v22 -> v23: both tables are new and start empty, so every existing row and
# hash is as it was. ``IF NOT EXISTS`` lets a file built at this version and
# marked as an earlier one migrate again, as the v17 and v19 statements do.
SCHEMA_V23_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS option_contracts ("
    "symbol TEXT PRIMARY KEY, "
    "multiplier INTEGER NOT NULL CHECK (typeof(multiplier) = 'integer' AND multiplier >= 1), "
    "recorded_at_ms INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS order_contracts ("
    "order_ref TEXT PRIMARY KEY REFERENCES orders(order_ref), "
    "symbol TEXT NOT NULL REFERENCES option_contracts(symbol))",
    "CREATE TRIGGER IF NOT EXISTS trg_option_contracts_never_edited BEFORE UPDATE ON option_contracts "
    "BEGIN SELECT RAISE(ABORT, 'a recorded option contract is never edited'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_option_contracts_never_deleted BEFORE DELETE ON option_contracts "
    "BEGIN SELECT RAISE(ABORT, 'a recorded option contract is never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_order_contracts_never_edited BEFORE UPDATE ON order_contracts "
    "BEGIN SELECT RAISE(ABORT, 'the contract an order trades is never edited'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_order_contracts_never_deleted BEFORE DELETE ON order_contracts "
    "BEGIN SELECT RAISE(ABORT, 'the contract an order trades is never deleted'); END",
)
SCHEMA_V23_DDL = "\n".join(f"{statement};" for statement in SCHEMA_V23_STATEMENTS)

# What a reader of fills adds to resolve an order's contract. ``{order}`` is the
# reader's alias for ``orders``. A stock order joins to nothing: its symbol and
# its multiplier come back NULL here and the reader's own symbol stands.
ORDER_CONTRACT_JOINS_SQL = (
    "LEFT JOIN order_contracts order_contract ON order_contract.order_ref = {order}.order_ref "
    "LEFT JOIN option_contracts option_contract ON option_contract.symbol = order_contract.symbol"
)
ORDER_CONTRACT_SYMBOL_SQL = "order_contract.symbol"
ORDER_CONTRACT_MULTIPLIER_SQL = "option_contract.multiplier"


class ContractTermsConflict(ValueError):
    """What is being recorded about a contract or an order disagrees with what the books hold."""


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """One option contract as custody counts it: its OCC symbol and its multiplier."""

    symbol: str
    multiplier: int

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not is_option_contract_symbol(self.symbol):
            raise ValueError(f"{self.symbol!r} is not an option contract symbol")
        try:
            contract_multiplier(self.multiplier)
        except MoneyInputError as exc:
            raise ValueError(f"the multiplier of {self.symbol} must be a whole number, one or more") from exc

    @classmethod
    def listed(cls, *, symbol: str, multiplier: Decimal) -> ContractTerms:
        """The terms of a contract as a listing states them, its multiplier an exact decimal.

        A broker's contract reference reports the multiplier as a decimal
        ("100"). One that is not a whole number is refused, never rounded.
        """
        return cls(
            symbol=symbol,
            multiplier=whole_units(multiplier, units_per_one=1, what=f"the multiplier of {symbol}", unit="shares"),
        )


def is_option_contract_symbol(symbol: str) -> bool:
    """Whether ``symbol`` is spelled as an option contract (``AAPL240119C00100000``).

    The spelling says a symbol is a contract. It never says what the contract
    multiplies by.
    """
    try:
        return OptionContract.from_occ_symbol(symbol).occ_symbol == symbol
    except ValueError:
        return False


def record_contract_terms(conn: sqlite3.Connection, terms: ContractTerms, *, recorded_at_ms: int) -> None:
    """Record a contract's multiplier the first time the books meet it. A fold helper.

    Recording the same terms again changes nothing. Recording another
    multiplier for the same symbol is refused: an adjusted contract trades
    under another symbol, so one symbol has one multiplier for good.
    """
    held = contract_terms(conn, terms.symbol)
    if held is None:
        conn.execute(
            "INSERT INTO option_contracts (symbol, multiplier, recorded_at_ms) VALUES (?, ?, ?)",
            (terms.symbol, terms.multiplier, recorded_at_ms),
        )
    elif held != terms:
        raise ContractTermsConflict(
            f"{terms.symbol} is recorded with a multiplier of {held.multiplier}; "
            f"it cannot be recorded again with {terms.multiplier}"
        )


def bind_order_contract(conn: sqlite3.Connection, *, order_ref: str, symbol: str) -> None:
    """Record that the order ``order_ref`` trades the contract ``symbol``. A fold helper.

    Called in the fold that creates the order's row, after the contract's
    terms are recorded. An order is bound once, to one contract.
    """
    if contract_terms(conn, symbol) is None:
        raise ContractTermsConflict(
            f"order {order_ref!r} cannot be bound to {symbol!r}: that contract's terms are not recorded"
        )
    held = conn.execute("SELECT symbol FROM order_contracts WHERE order_ref = ?", (order_ref,)).fetchone()
    if held is None:
        conn.execute("INSERT INTO order_contracts (order_ref, symbol) VALUES (?, ?)", (order_ref, symbol))
    elif held[0] != symbol:
        raise ContractTermsConflict(
            f"order {order_ref!r} trades {held[0]}; it cannot also be bound to {symbol}"
        )


def contract_terms(conn: sqlite3.Connection, symbol: str) -> ContractTerms | None:
    """The recorded terms of the contract ``symbol``, or ``None`` when the books do not hold it."""
    row = conn.execute("SELECT symbol, multiplier FROM option_contracts WHERE symbol = ?", (symbol,)).fetchone()
    return None if row is None else ContractTerms(symbol=row[0], multiplier=row[1])


def order_contract_terms(conn: sqlite3.Connection, order_ref: str) -> ContractTerms | None:
    """The contract the order ``order_ref`` trades, or ``None`` for an order in shares."""
    row = conn.execute(
        "SELECT contract.symbol, contract.multiplier FROM order_contracts bound "
        "JOIN option_contracts contract ON contract.symbol = bound.symbol WHERE bound.order_ref = ?",
        (order_ref,),
    ).fetchone()
    return None if row is None else ContractTerms(symbol=row[0], multiplier=row[1])


def require_fill_in_its_orders_contract(
    conn: sqlite3.Connection, *, order_ref: str, symbol: str, quantity: float
) -> None:
    """Refuse a fill the books could not count: one whose symbol and whose order's contract disagree.

    A fill of an order bound to a contract must be in that contract, and a
    whole number of contracts: a fraction of one is refused, never rounded. A
    fill whose symbol is spelled as an option contract must be of a bound
    order; without the binding custody would read it as the bot's own symbol,
    in shares. A fill in shares of an order in shares passes untouched,
    whatever its quantity.

    Every write of a fill is held to this: the write that moves a position,
    and a coverage replacement, which puts an exact execution into the books
    without moving one.
    """
    bound = conn.execute("SELECT symbol FROM order_contracts WHERE order_ref = ?", (order_ref,)).fetchone()
    if bound is None:
        if is_option_contract_symbol(symbol.upper()):
            raise ContractTermsConflict(
                f"a fill in the option contract {symbol.upper()} arrived on order {order_ref!r}, "
                "which is not bound to a contract"
            )
    elif bound[0] != symbol.upper():
        raise ContractTermsConflict(
            f"a fill in {symbol.upper()} arrived on order {order_ref!r}, which trades {bound[0]}"
        )
    elif not float(quantity).is_integer():
        raise ContractTermsConflict(
            f"a fill of {quantity!r} arrived on order {order_ref!r}, which trades {bound[0]}: "
            "an option contract fills in whole contracts"
        )


__all__ = [
    "ORDER_CONTRACT_JOINS_SQL",
    "ORDER_CONTRACT_MULTIPLIER_SQL",
    "ORDER_CONTRACT_SYMBOL_SQL",
    "SCHEMA_V23_DDL",
    "SCHEMA_V23_STATEMENTS",
    "ContractTerms",
    "ContractTermsConflict",
    "bind_order_contract",
    "contract_terms",
    "is_option_contract_symbol",
    "order_contract_terms",
    "record_contract_terms",
    "require_fill_in_its_orders_contract",
]
