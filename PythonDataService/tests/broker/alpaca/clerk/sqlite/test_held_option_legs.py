"""What one bot holds of option contracts now: signed whole contracts at the multiplier recorded for each (#3100).

The books are real, and a fill reaches them through the Clerk's own fill fold
(``option_custody_fixtures``). Holdings are SPY calls that expire 2026-09-18.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.option_entry import held_option_legs
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    accept_option_order,
    fill,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import call

C580, C585, C590 = call(580).occ_symbol, call(585).occ_symbol, call(590).occ_symbol
# A contract of ten shares: no listing a bot would pick has one, and the books must still count it right.
SMALL_C500 = "SPY260918C00500000"


@pytest.fixture
def books(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY", "bot-b": "SPY"})
    try:
        yield repo
    finally:
        repo.close()


def _trade(
    books: ClerkSqliteRepository, sid: str, symbol: str, contracts: int, *, multiplier: int = 100, decision: str = "d"
) -> None:
    """``sid`` buys (above zero) or sells (below) ``contracts`` of ``symbol``, through the real fill fold."""
    side = "buy" if contracts > 0 else "sell"
    name = f"{sid}-{side}-{symbol}-{decision}"
    accepted = accept_option_order(
        books, terms=ContractTerms(symbol, multiplier), side=side, contracts=abs(contracts), decision_id=name,
        strategy_instance_id=sid,
    )
    fill(books, accepted, execution_id=f"x-{name}", symbol=symbol, side=side.upper(), quantity=abs(contracts), price=1.0)


def test_a_bot_holding_no_option_contract_has_no_legs(books: ClerkSqliteRepository) -> None:
    assert held_option_legs(books, "bot-a") == {}
    assert held_option_legs(books, "a-bot-nobody-registered") == {}


def test_each_bots_legs_are_its_own_signed_contracts_at_the_recorded_multiplier(books: ClerkSqliteRepository) -> None:
    _trade(books, "bot-a", C585, 2)
    _trade(books, "bot-a", C580, -1)
    _trade(books, "bot-a", SMALL_C500, 3, multiplier=10)
    _trade(books, "bot-b", C585, 1)

    assert held_option_legs(books, "bot-a") == {(C585, 100): 2, (C580, 100): -1, (SMALL_C500, 10): 3}
    assert held_option_legs(books, "bot-b") == {(C585, 100): 1}


def test_two_orders_in_one_contract_are_one_holding_and_a_closed_one_is_not_held(books: ClerkSqliteRepository) -> None:
    _trade(books, "bot-a", C585, 2, decision="first")
    _trade(books, "bot-a", C585, 1, decision="second")
    _trade(books, "bot-a", C590, 1)
    _trade(books, "bot-a", C590, -1, decision="closed")

    assert held_option_legs(books, "bot-a") == {(C585, 100): 3}


def test_a_fraction_of_a_contract_is_refused_and_never_rounded(books: ClerkSqliteRepository) -> None:
    _trade(books, "bot-a", C585, 1)
    with books.write_fence() as conn:
        conn.execute("UPDATE positions SET attributed_qty = 1.5 WHERE strategy_instance_id = 'bot-a'")

    with pytest.raises(ValueError, match="whole contracts"):
        held_option_legs(books, "bot-a")
