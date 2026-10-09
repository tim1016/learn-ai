"""Which option contracts an account's books say must be quoted: what a bot holds, and what an order is working in."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _broker_order_fixture
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    AAPL_CALL,
    AAPL_PUT,
    accept_option_closing_order,
    accept_option_order,
    fill,
    open_option_books,
)
from tests.broker.alpaca.clerk.sqlite.test_option_custody import _accept_stock_order


def _the_broker_says(repo: ClerkSqliteRepository, accepted, *, symbol: str, status: str, contracts: int) -> None:
    """Alpaca's own word on an order: working, or ended with nothing filled."""
    fold_order_acknowledgement(
        repo,
        effect_operation_id=accepted.effect_operation_id or "",
        order=_broker_order_fixture(
            accepted.order_ref or "", order_id=f"alpaca-{accepted.order_ref}", symbol=symbol, status=status,
            side="buy", quantity=contracts,
        ).model_copy(update={"updated_at_ms": NOON, "observed_at_ms": NOON}),
    )


def test_a_contract_is_to_be_watched_from_the_moment_it_is_held_until_it_is_closed(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path)
    try:
        nothing_yet = repo.option_contracts_requiring_quotes()
        bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        held = repo.option_contracts_requiring_quotes()
        closing = accept_option_closing_order(repo, terms=AAPL_CALL, opened=bought, decision_id="d2")
        fill(repo, closing, execution_id="x2", symbol=AAPL_CALL.symbol, side="SELL", quantity=1, price=2.10)
        half_closed = repo.option_contracts_requiring_quotes()
        fill(repo, closing, execution_id="x3", symbol=AAPL_CALL.symbol, side="SELL", quantity=1, price=2.10)

        assert nothing_yet == ()
        assert held == half_closed == (AAPL_CALL.symbol,)
        assert repo.option_contracts_requiring_quotes() == ()
    finally:
        repo.close()


def test_a_contract_an_order_is_working_in_at_the_broker_is_watched_though_nothing_is_held(tmp_path: Path) -> None:
    """A resting entry has filled nothing yet. Its contract's quotes are still what its outcome is judged by."""
    repo = open_option_books(tmp_path)
    try:
        resting = accept_option_order(repo, terms=AAPL_PUT, side="buy", contracts=1, decision_id="d1")
        before_the_broker_answers = repo.option_contracts_requiring_quotes()
        _the_broker_says(repo, resting, symbol=AAPL_PUT.symbol, status="new", contracts=1)
        working = repo.option_contracts_requiring_quotes()
        _the_broker_says(repo, resting, symbol=AAPL_PUT.symbol, status="canceled", contracts=1)

        assert before_the_broker_answers == ()
        assert working == (AAPL_PUT.symbol,)
        assert repo.option_contracts_requiring_quotes() == ()
    finally:
        repo.close()


def test_shares_a_bot_holds_are_never_among_the_contracts_to_watch(tmp_path: Path) -> None:
    """A contract is told by its recorded terms. A stock position, on the contract's own underlying, is not one."""
    repo = open_option_books(tmp_path, bots={"bot-a": "AAPL", "bot-b": "AAPL"})
    try:
        shares = _accept_stock_order(repo, symbol="AAPL", side="buy", shares=5, decision_id="s1", bot="bot-b")
        fill(repo, shares, execution_id="x1", symbol="AAPL", side="BUY", quantity=5, price=200.0)
        calls = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1")
        fill(repo, calls, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=1.50)
        puts = accept_option_order(repo, terms=AAPL_PUT, side="buy", contracts=1, decision_id="d2")
        fill(repo, puts, execution_id="x3", symbol=AAPL_PUT.symbol, side="BUY", quantity=1, price=0.90)

        assert repo.option_contracts_requiring_quotes() == (AAPL_CALL.symbol, AAPL_PUT.symbol)
    finally:
        repo.close()
