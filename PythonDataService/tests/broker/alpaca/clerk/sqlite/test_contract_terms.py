"""The record of an option contract's multiplier, and of which contract an order trades (#2913)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import database_verification, schema
from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTerms,
    ContractTermsConflict,
    bind_order_contract,
    contract_terms,
    is_option_contract_symbol,
    order_contract_terms,
    record_contract_terms,
    require_fill_in_its_orders_contract,
)
from tests.broker.alpaca.clerk.sqlite.test_schema_parity import (
    _authority_built_up_to,
    _seed_one_active_run_position_and_working_order,
)

_CALL = "AAPL261016C00200000"


def _books_with_one_order() -> sqlite3.Connection:
    """A fresh schema holding one order, ``order-1``, as an order's fold would find it."""
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)
    _seed_one_active_run_position_and_working_order(conn)
    return conn


# ── What may be recorded ──────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol", [_CALL, "SPY261016P00500000", "F270115C00012500"])
def test_an_occ_symbol_is_an_option_contract(symbol: str) -> None:
    assert is_option_contract_symbol(symbol)
    assert ContractTerms(symbol=symbol, multiplier=100).symbol == symbol


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL",  # shares
        "BRK.B",  # shares, with a class
        "aapl261016c00200000",  # not the broker's spelling
        "AAPL1261016C00200000",  # an adjusted contract's root, which the contract identity refuses
        "AAPL261316C00200000",  # month 13
        "O:AAPL261016C00200000",  # another vendor's prefix
    ],
)
def test_anything_else_is_not_and_cannot_be_given_a_multiplier(symbol: str) -> None:
    assert not is_option_contract_symbol(symbol)
    with pytest.raises(ValueError, match="not an option contract symbol"):
        ContractTerms(symbol=symbol, multiplier=100)


@pytest.mark.parametrize("multiplier", [0, -1, True, 100.0, "100", None])
def test_a_multiplier_must_be_a_whole_number_of_one_or_more(multiplier: object) -> None:
    with pytest.raises(ValueError, match="whole number, one or more"):
        ContractTerms(symbol=_CALL, multiplier=multiplier)  # type: ignore[arg-type]


def test_a_listings_decimal_multiplier_becomes_a_whole_number_or_is_refused() -> None:
    """A contract reference reports "100". "100.0" is the same number; "0.5" and "100.5" are no multiplier."""
    assert ContractTerms.listed(symbol=_CALL, multiplier=Decimal("100")) == ContractTerms(symbol=_CALL, multiplier=100)
    assert ContractTerms.listed(symbol=_CALL, multiplier=Decimal("100.0")).multiplier == 100
    for bad in (Decimal("100.5"), Decimal("0.5"), Decimal("0"), Decimal("NaN")):
        with pytest.raises(ValueError):
            ContractTerms.listed(symbol=_CALL, multiplier=bad)


# ── One contract, one multiplier ──────────────────────────────────────────────


def test_a_contract_is_recorded_once_and_read_back() -> None:
    conn = _books_with_one_order()
    assert contract_terms(conn, _CALL) is None
    terms = ContractTerms(symbol=_CALL, multiplier=100)
    record_contract_terms(conn, terms, recorded_at_ms=5)
    record_contract_terms(conn, terms, recorded_at_ms=9)
    assert contract_terms(conn, _CALL) == terms
    # The first record stands; saying the same thing again writes nothing.
    assert conn.execute("SELECT symbol, multiplier, recorded_at_ms FROM option_contracts").fetchall() == [(_CALL, 100, 5)]


def test_a_contract_cannot_be_recorded_again_with_another_multiplier() -> None:
    conn = _books_with_one_order()
    record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=100), recorded_at_ms=5)
    with pytest.raises(ContractTermsConflict, match="multiplier of 100"):
        record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=10), recorded_at_ms=6)
    assert contract_terms(conn, _CALL) == ContractTerms(symbol=_CALL, multiplier=100)


@pytest.mark.parametrize("multiplier", [0, -100, 100.5, "a hundred", None])
def test_the_store_itself_refuses_a_multiplier_that_is_not_a_whole_number_of_one_or_more(multiplier: object) -> None:
    """Whoever writes the row, and however: the table takes no other multiplier."""
    conn = _books_with_one_order()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO option_contracts (symbol, multiplier, recorded_at_ms) VALUES (?, ?, 5)", (_CALL, multiplier)
        )
    assert contract_terms(conn, _CALL) is None


def test_a_recorded_contract_is_never_edited_or_deleted() -> None:
    conn = _books_with_one_order()
    record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=100), recorded_at_ms=5)
    bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    for statement in (
        "UPDATE option_contracts SET multiplier = 10",
        "DELETE FROM option_contracts",
        "UPDATE order_contracts SET symbol = 'SPY261016P00500000'",
        "DELETE FROM order_contracts",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="never"):
            conn.execute(statement)
    assert order_contract_terms(conn, "order-1") == ContractTerms(symbol=_CALL, multiplier=100)


# ── One order, one contract ───────────────────────────────────────────────────


def test_an_order_is_bound_to_its_contract_and_an_unbound_order_trades_shares() -> None:
    conn = _books_with_one_order()
    assert order_contract_terms(conn, "order-1") is None
    record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=100), recorded_at_ms=5)
    bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    assert order_contract_terms(conn, "order-1") == ContractTerms(symbol=_CALL, multiplier=100)
    assert conn.execute("SELECT COUNT(*) FROM order_contracts").fetchone()[0] == 1


def test_an_order_cannot_be_bound_to_a_contract_whose_multiplier_is_not_recorded() -> None:
    conn = _books_with_one_order()
    with pytest.raises(ContractTermsConflict, match="terms are not recorded"):
        bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    # The store refuses it too, whoever writes the row.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_contracts (order_ref, symbol) VALUES ('order-1', ?)", (_CALL,))


def test_an_order_cannot_be_bound_to_a_second_contract() -> None:
    conn = _books_with_one_order()
    other = "AAPL261016P00190000"
    for symbol in (_CALL, other):
        record_contract_terms(conn, ContractTerms(symbol=symbol, multiplier=100), recorded_at_ms=5)
    bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    with pytest.raises(ContractTermsConflict, match=f"trades {_CALL}"):
        bind_order_contract(conn, order_ref="order-1", symbol=other)


def test_a_binding_needs_an_order_the_books_hold() -> None:
    conn = _books_with_one_order()
    record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=100), recorded_at_ms=5)
    with pytest.raises(sqlite3.IntegrityError):
        bind_order_contract(conn, order_ref="no-such-order", symbol=_CALL)


# ── What a fill of an order may be ────────────────────────────────────────────


def test_a_fill_of_an_order_that_trades_a_contract_is_a_whole_number_of_contracts() -> None:
    conn = _books_with_one_order()
    record_contract_terms(conn, ContractTerms(symbol=_CALL, multiplier=100), recorded_at_ms=5)
    bind_order_contract(conn, order_ref="order-1", symbol=_CALL)
    for whole in (1, 2.0, 250.0):
        require_fill_in_its_orders_contract(conn, order_ref="order-1", symbol=_CALL, quantity=whole)
    # A hair over two is not two: a count is refused, never rounded.
    for part in (0.5, 1.5, 2.000000001, float("inf"), float("nan")):
        with pytest.raises(ContractTermsConflict, match="whole contracts"):
            require_fill_in_its_orders_contract(conn, order_ref="order-1", symbol=_CALL, quantity=part)


def test_a_fill_of_an_order_in_shares_may_be_any_quantity() -> None:
    conn = _books_with_one_order()
    for shares in (1, 0.5, 0.000000001, 12.000000001):
        require_fill_in_its_orders_contract(conn, order_ref="order-1", symbol="SPY", quantity=shares)


# ── The migration ─────────────────────────────────────────────────────────────


def test_a_v22_store_gains_two_empty_tables_and_keeps_every_row(tmp_path: Path) -> None:
    """What a running Clerk's file meets on its first open by this build."""
    db_path = tmp_path / "clerk.db"
    conn = sqlite3.connect(db_path)
    schema.configure_connection(conn)
    _authority_built_up_to(conn, 22)
    _seed_one_active_run_position_and_working_order(conn)
    assert _contract_tables(conn) == set()
    tables = ("strategy_instances", "runs", "commands", "effect_operations", "positions", "custody_transitions")
    before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}
    order_columns = ", ".join(row[1] for row in conn.execute("PRAGMA table_info(orders)"))
    orders_before = conn.execute(f"SELECT {order_columns} FROM orders ORDER BY order_ref").fetchall()

    schema.migrate_schema(conn, from_version=22)

    # The chain runs on to the build's own version, through the v24 legs table and v25 entry index.
    assert conn.execute("SELECT schema_version FROM control_meta WHERE id = 1").fetchone()[0] == schema.SCHEMA_VERSION
    assert _contract_tables(conn) == {"option_contracts", "order_contracts"}
    assert {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables} == before
    assert conn.execute(f"SELECT {order_columns} FROM orders ORDER BY order_ref").fetchall() == orders_before
    assert conn.execute("SELECT COUNT(*) FROM option_contracts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM order_contracts").fetchone()[0] == 0
    assert order_contract_terms(conn, "order-1") is None
    conn.close()
    assert (
        database_verification.verify_database(db_path, expected_account_id="PA1").schema_version
        == schema.SCHEMA_VERSION
    )


def _contract_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('option_contracts', 'order_contracts')"
        )
    }
