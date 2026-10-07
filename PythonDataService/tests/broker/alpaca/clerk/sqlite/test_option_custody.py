"""The Clerk's books for an option contract: positions per contract, money by the multiplier (#2913).

A fill reaches the books through the real fill fold and is read back through
the real readers. Only the order's acceptance is a test stand-in
(``option_custody_fixtures``). Every option case sits beside a stock case
read the same way, which must come out as it always has.
"""

from __future__ import annotations

import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl, compute_open_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import MoneyInputError
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.budget_projection import project_account_budget, project_account_money
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTermsConflict,
    contract_terms,
    order_contract_terms,
)
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.day_pnl import risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
    _CachedFifo,
    effective_fill_records,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.models import ExecutionCoverageResolutionReceipt
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement, fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ExecutionCoverageResolutionUnavailable,
)
from app.broker.alpaca.clerk.sqlite.simulated_account import _cash_after_fills
from app.broker.contract.models import BrokerActivity, BrokerOrderLeg, OrderSide
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.services.alpaca_fee_attribution import FeeAttribution
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    _broker_order_fixture,
    _clock_at,
    _register_active,
    _TestClock,
    _walk_clock_to,
    complete_fee_evidence,
    envelope_gate,
)
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    AAPL_CALL,
    AAPL_PUT,
    ACCOUNT_ID,
    RUN_ID,
    SMALL_CALL,
    accept_option_closing_order,
    accept_option_order,
    fill,
    open_option_books,
    option_fold_registry,
)
from tests.broker.alpaca.clerk.sqlite.test_folds_execution import _append_slice, _coverage_conflict_transition

# The New York date every fill here is on.
DAY = et_date_at_ms(NOON)
# Fees known and nil: the money below is before fees.
_NO_FEES = FeeAttribution(shares=(), unattributed=Decimal(0), unresolved=(), observed_total=Decimal(0), predicted_total=Decimal(0))
_MINUTE_MS = 60_000


def _accept_stock_order(
    repo: ClerkSqliteRepository, *, symbol: str, side: str, shares: float, decision_id: str, bot: str
) -> EnterSubmission:
    """The real ENTER acceptance of a stock leg, unbudgeted."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=bot,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol=symbol, side=side, quantity=shares),
    )
    assert accepted.order_ref is not None
    return accepted


def _records(repo: ClerkSqliteRepository):
    return effective_fill_records(repo._conn, account_id=ACCOUNT_ID)


# ── What a fill is read as ────────────────────────────────────────────────────


def test_an_option_fill_is_read_as_its_contract_and_a_stock_fill_as_shares(tmp_path: Path) -> None:
    """The bot is registered under AAPL. Its option fill is read as the contract, by 100; SPY shares as SPY."""
    repo = open_option_books(tmp_path, bots={"bot-a": "AAPL", "bot-s": "SPY"})
    try:
        option = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        assert fill(repo, option, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50) == "appended"
        stock = _accept_stock_order(repo, symbol="SPY", side="buy", shares=2, decision_id="d2", bot="bot-s")
        assert fill(repo, stock, execution_id="x2", symbol="SPY", side="BUY", quantity=2, price=1.50) == "appended"

        by_symbol = {record.symbol: record for record in _records(repo)}
        assert set(by_symbol) == {AAPL_CALL.symbol, "SPY"}
        contract, shares = by_symbol[AAPL_CALL.symbol], by_symbol["SPY"]
        assert (contract.quantity, contract.fill_price, contract.multiplier) == (2.0, 1.5, 100)
        assert contract.exact_notional == Decimal(300)
        assert (shares.quantity, shares.fill_price, shares.multiplier) == (2.0, 1.5, None)
        assert shares.exact_notional == Decimal(3)
        assert order_contract_terms(repo._conn, option.order_ref or "") == AAPL_CALL
        assert order_contract_terms(repo._conn, stock.order_ref or "") is None
    finally:
        repo.close()


def test_the_same_execution_delivered_twice_is_counted_once(tmp_path: Path) -> None:
    """A redelivered option execution is the one the books hold, not a contradiction of it."""
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        assert fill(repo, order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50) == "appended"
        assert fill(repo, order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50) == "duplicate"
        assert repo.position("bot-a", AAPL_CALL.symbol) == 2.0
        assert len(_records(repo)) == 1
    finally:
        repo.close()


def test_account_history_finds_a_contracts_executions_under_the_contract_and_not_its_bots_symbol(tmp_path: Path) -> None:
    """The bot is registered under AAPL and trades AAPL shares and an AAPL call. Each is found under its own symbol."""
    repo = open_option_books(tmp_path)
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        shares = _accept_stock_order(repo, symbol="AAPL", side="buy", shares=100, decision_id="d1", bot="bot-a")
        fill(repo, shares, execution_id="x1", symbol="AAPL", side="BUY", quantity=100, price=190.0)
        call = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d2")
        fill(repo, call, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=2.00)

        # Asked for in lower case, as a typed filter may be.
        of_the_contract = reader.account_executions(symbol=AAPL_CALL.symbol.lower()).executions
        of_the_shares = reader.account_executions(symbol="AAPL").executions

        assert [(row.execution_id, row.symbol, row.quantity, row.price, row.multiplier) for row in of_the_contract] == [
            ("x2", AAPL_CALL.symbol, 1.0, 2.0, 100),
        ]
        assert [(row.execution_id, row.symbol, row.quantity, row.multiplier) for row in of_the_shares] == [
            ("x1", "AAPL", 100.0, None),
        ]
    finally:
        reader.close()
        repo.close()


# ── P&L on the bot ────────────────────────────────────────────────────────────


def test_two_contracts_bought_at_1_50_and_sold_at_2_10_show_120_dollars_on_the_bot(tmp_path: Path) -> None:
    """Open: (2.10 − 1.50) × 2 × 100 = $120.00 at a $2.10 mark. Sold there: $120.00 realized today, flat."""
    repo = open_option_books(tmp_path)
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    session = session_window_for_date(et_date_at_ms(NOON))
    try:
        bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)

        held = reader.bot_economic_snapshot(
            "bot-a", session_window=session, marks={AAPL_CALL.symbol: MarketMark(price=2.10, observed_at_ms=NOON)}
        )
        assert held is not None
        assert held.exposure == {AAPL_CALL.symbol: 2.0}
        assert held.exact_open_pnl == Decimal(120)
        assert held.realized_pnl_today == 0.0

        sold = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=2, decision_id="d2")
        fill(repo, sold, execution_id="x2", symbol=AAPL_CALL.symbol, side="SELL", quantity=2, price=2.10, occurred_at_ms=NOON + _MINUTE_MS)

        flat = reader.bot_economic_snapshot("bot-a", session_window=session, marks={})
        assert flat is not None
        assert flat.exposure == {}
        assert flat.realized_pnl_today == 120.0
        assert flat.exact_open_pnl == Decimal(0)

        account = reader.account_pnl_attribution(from_ms=NOON, to_ms=NOON + _MINUTE_MS)
        assert account.exact_realized_pnl_total == Decimal(120)
        (row,) = account.attribution_rows
        assert (row.symbol, row.quantity, row.entry_price, row.exit_price, row.realized_pnl) == (
            AAPL_CALL.symbol, 2.0, 1.5, 2.1, 120.0,
        )
    finally:
        reader.close()
        repo.close()


def test_two_shares_bought_at_1_50_and_sold_at_2_10_show_a_dollar_twenty_as_before(tmp_path: Path) -> None:
    """Stock twin of the case above, through the same readers: (2.10 − 1.50) × 2 = $1.20."""
    repo = open_option_books(tmp_path, bots={"bot-s": "SPY"})
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    session = session_window_for_date(et_date_at_ms(NOON))
    try:
        bought = _accept_stock_order(repo, symbol="SPY", side="buy", shares=2, decision_id="d1", bot="bot-s")
        fill(repo, bought, execution_id="x1", symbol="SPY", side="BUY", quantity=2, price=1.50)
        held = reader.bot_economic_snapshot(
            "bot-s", session_window=session, marks={"SPY": MarketMark(price=2.10, observed_at_ms=NOON)}
        )
        assert held is not None
        assert held.exact_open_pnl == Decimal("1.2")

        # A stock bot sells through its EXIT; for the books' arithmetic the sale's execution is enough.
        fill(repo, bought, execution_id="x2", symbol="SPY", side="SELL", quantity=2, price=2.10, occurred_at_ms=NOON + _MINUTE_MS)
        flat = reader.bot_economic_snapshot("bot-s", session_window=session, marks={})
        assert flat is not None
        assert flat.realized_pnl_today == pytest.approx(1.2, abs=1e-9, rel=0)
        assert reader.account_pnl_attribution(from_ms=NOON, to_ms=NOON + _MINUTE_MS).exact_realized_pnl_total == Decimal("1.2")
    finally:
        reader.close()
        repo.close()


def test_a_sold_option_bought_back_for_less_is_a_gain(tmp_path: Path) -> None:
    """Sold 1 put at $3.07, bought back at $1.20: (3.07 − 1.20) × 100 = $187.00. The position is −1, then flat."""
    repo = open_option_books(tmp_path)
    try:
        sold = accept_option_order(repo, terms=AAPL_PUT, side="sell", contracts=1, decision_id="d1")
        fill(repo, sold, execution_id="x1", symbol=AAPL_PUT.symbol, side="SELL", quantity=1, price=3.07)
        assert repo.position("bot-a", AAPL_PUT.symbol) == -1.0
        bought_back = accept_option_order(repo, terms=AAPL_PUT, side="buy", contracts=1, decision_id="d2")
        fill(repo, bought_back, execution_id="x2", symbol=AAPL_PUT.symbol, side="BUY", quantity=1, price=1.20, occurred_at_ms=NOON + _MINUTE_MS)
        assert repo.position("bot-a", AAPL_PUT.symbol) == 0.0
        assert compute_fifo_pnl(_records(repo)).exact_realized_pnl == Decimal(187)
    finally:
        repo.close()


def test_a_contract_of_ten_shares_is_counted_by_ten(tmp_path: Path) -> None:
    """5 contracts of 10 shares, bought at $0.80 and sold at $1.05: 0.25 × 5 × 10 = $12.50."""
    repo = open_option_books(tmp_path, bots={"bot-a": "SPY"})
    try:
        bought = accept_option_order(repo, terms=SMALL_CALL, side="buy", contracts=5, decision_id="d1")
        fill(repo, bought, execution_id="x1", symbol=SMALL_CALL.symbol, side="BUY", quantity=5, price=0.80)
        sold = accept_option_order(repo, terms=SMALL_CALL, side="sell", contracts=5, decision_id="d2")
        fill(repo, sold, execution_id="x2", symbol=SMALL_CALL.symbol, side="SELL", quantity=5, price=1.05, occurred_at_ms=NOON + _MINUTE_MS)
        assert {record.multiplier for record in _records(repo)} == {10}
        assert compute_fifo_pnl(_records(repo)).exact_realized_pnl == Decimal("12.5")
    finally:
        repo.close()


def test_partial_closes_across_two_lots_take_the_oldest_contracts_first(tmp_path: Path) -> None:
    """BUY 3 @ 1.00, BUY 2 @ 1.40, SELL 4 @ 2.00: $300 + $60 realized, 1 contract left that cost $140."""
    repo = open_option_books(tmp_path)
    try:
        for index, (side, contracts, price) in enumerate([("buy", 3, 1.00), ("buy", 2, 1.40), ("sell", 4, 2.00)]):
            order = accept_option_order(repo, terms=AAPL_CALL, side=side, contracts=contracts, decision_id=f"d{index}")
            fill(
                repo, order, execution_id=f"x{index}", symbol=AAPL_CALL.symbol, side=side.upper(),
                quantity=contracts, price=price, occurred_at_ms=NOON + index * _MINUTE_MS,
            )
        result = compute_fifo_pnl(_records(repo))
        assert [lot.exact_realized_pnl for lot in result.closed_lots] == [Decimal(300), Decimal(60)]
        assert sum(lot.exact_cost_basis for lot in result.open_lots) == Decimal(140)
        assert repo.position("bot-a", AAPL_CALL.symbol) == 1.0
    finally:
        repo.close()


# ── Positions per contract ────────────────────────────────────────────────────


def test_a_bot_holds_each_contract_as_its_own_position(tmp_path: Path) -> None:
    """A call and a put on one underlying are two positions; selling the call leaves the put."""
    repo = open_option_books(tmp_path)
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        call = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1")
        fill(repo, call, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=2.00)
        put = accept_option_order(repo, terms=AAPL_PUT, side="buy", contracts=1, decision_id="d2")
        fill(repo, put, execution_id="x2", symbol=AAPL_PUT.symbol, side="BUY", quantity=1, price=1.00)
        assert repo.attributed_positions_for_strategy("bot-a") == {AAPL_CALL.symbol: 1.0, AAPL_PUT.symbol: 1.0}

        sold = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=1, decision_id="d3")
        fill(repo, sold, execution_id="x3", symbol=AAPL_CALL.symbol, side="SELL", quantity=1, price=2.50, occurred_at_ms=NOON + _MINUTE_MS)
        snapshot = reader.bot_economic_snapshot("bot-a", session_window=None, marks={})
        assert snapshot is not None
        assert snapshot.exposure == {AAPL_PUT.symbol: 1.0}
        assert compute_fifo_pnl(_records(repo)).exact_realized_pnl == Decimal(50)
    finally:
        reader.close()
        repo.close()


def test_a_contract_and_shares_of_its_underlying_are_held_side_by_side_and_never_netted(tmp_path: Path) -> None:
    """One bot holds 100 AAPL shares and 1 AAPL call. Selling the shares does not touch the call.

    Shares: (191 − 190) × 100 = $100.00. The call stays: 1 contract, up
    (2.50 − 2.00) × 100 = $50.00 at a $2.50 mark.
    """
    repo = open_option_books(tmp_path)
    reader = SqliteEconomicProjectionReader.from_repository(repo)
    try:
        shares = _accept_stock_order(repo, symbol="AAPL", side="buy", shares=100, decision_id="d1", bot="bot-a")
        fill(repo, shares, execution_id="x1", symbol="AAPL", side="BUY", quantity=100, price=190.0)
        call = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d2")
        fill(repo, call, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=2.00)
        assert repo.attributed_positions_for_strategy("bot-a") == {"AAPL": 100.0, AAPL_CALL.symbol: 1.0}

        fill(repo, shares, execution_id="x3", symbol="AAPL", side="SELL", quantity=100, price=191.0, occurred_at_ms=NOON + _MINUTE_MS)

        snapshot = reader.bot_economic_snapshot(
            "bot-a", session_window=None, marks={AAPL_CALL.symbol: MarketMark(price=2.50, observed_at_ms=NOON)}
        )
        assert snapshot is not None
        assert snapshot.exposure == {AAPL_CALL.symbol: 1.0}
        assert snapshot.exact_open_pnl == Decimal(50)
        assert compute_fifo_pnl(_records(repo)).exact_realized_pnl == Decimal(100)
    finally:
        reader.close()
        repo.close()


def test_two_bots_holding_the_same_contract_keep_their_own_positions_and_money(tmp_path: Path) -> None:
    """Bot A buys 1 at $1.00 and sells it at $1.50 ($50.00). Bot B buys 2 at $1.20 and holds ($240.00 at cost)."""
    repo = open_option_books(tmp_path, bots={"bot-a": "AAPL", "bot-b": "AAPL"})
    try:
        a_buy = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1", strategy_instance_id="bot-a")
        fill(repo, a_buy, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=1.00)
        b_buy = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1", strategy_instance_id="bot-b")
        fill(repo, b_buy, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.20, occurred_at_ms=NOON + _MINUTE_MS)
        a_sell = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=1, decision_id="d2", strategy_instance_id="bot-a")
        fill(repo, a_sell, execution_id="x3", symbol=AAPL_CALL.symbol, side="SELL", quantity=1, price=1.50, occurred_at_ms=NOON + 2 * _MINUTE_MS)

        assert repo.attributed_positions_by_subject() == {
            ("bot:bot-a", AAPL_CALL.symbol): 0.0,
            ("bot:bot-b", AAPL_CALL.symbol): 2.0,
        }
        records = _records(repo)
        a = compute_fifo_pnl([record for record in records if record.sid == "bot:bot-a"])
        b = compute_fifo_pnl([record for record in records if record.sid == "bot:bot-b"])
        assert (a.exact_realized_pnl, a.open_lots) == (Decimal(50), [])
        assert sum(lot.exact_cost_basis for lot in b.open_lots) == Decimal(240)
        # The contract's terms are one record however many bots and orders trade it.
        assert repo._conn.execute("SELECT COUNT(*) FROM option_contracts").fetchone()[0] == 1
        assert repo._conn.execute("SELECT COUNT(*) FROM order_contracts").fetchone()[0] == 3
    finally:
        repo.close()


# ── A fill the books cannot count is refused ──────────────────────────────────


def test_a_fill_in_another_symbol_than_its_orders_contract_is_refused(tmp_path: Path) -> None:
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1")
        for wrong in (AAPL_PUT.symbol, "AAPL"):
            with pytest.raises(ContractTermsConflict, match=f"which trades {AAPL_CALL.symbol}"):
                fill(repo, order, execution_id=f"x-{wrong}", symbol=wrong, side="BUY", quantity=1, price=2.00)
        assert _records(repo) == ()
        assert repo.attributed_positions_by_subject() == {}
    finally:
        repo.close()


def test_an_option_fill_on_an_order_bound_to_no_contract_is_refused(tmp_path: Path) -> None:
    """Read through an unbound order it would be 1 AAPL share at $2.00. The books take no such fill."""
    repo = open_option_books(tmp_path)
    try:
        stock_order = _accept_stock_order(repo, symbol="AAPL", side="buy", shares=1, decision_id="d1", bot="bot-a")
        with pytest.raises(ContractTermsConflict, match="not bound to a contract"):
            fill(repo, stock_order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=2.00)
        assert _records(repo) == ()
    finally:
        repo.close()


def test_a_fill_reported_in_lower_case_is_still_matched_to_its_orders_contract(tmp_path: Path) -> None:
    """A symbol's case is not a difference: on a bound order it is that contract; on an unbound one it is still refused."""
    repo = open_option_books(tmp_path)
    try:
        stock_order = _accept_stock_order(repo, symbol="AAPL", side="buy", shares=1, decision_id="d1", bot="bot-a")
        with pytest.raises(ContractTermsConflict, match="not bound to a contract"):
            fill(repo, stock_order, execution_id="x1", symbol=AAPL_CALL.symbol.lower(), side="BUY", quantity=1, price=2.00)
        assert _records(repo) == ()

        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d2")
        assert fill(repo, order, execution_id="x2", symbol=AAPL_CALL.symbol.lower(), side="BUY", quantity=1, price=2.00) == "appended"
        assert repo.attributed_positions_for_strategy("bot-a") == {AAPL_CALL.symbol: 1.0}
        (record,) = _records(repo)
        assert (record.symbol, record.multiplier) == (AAPL_CALL.symbol, 100)
    finally:
        repo.close()


def _recover_total(
    repo: ClerkSqliteRepository, accepted: EnterSubmission, *, symbol: str, wanted: float, filled: float, price: float
) -> None:
    """A REST read of a buy order's total so far: the fill the Clerk records when it holds no exact execution of it."""
    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id or "",
        order=_broker_order_fixture(
            accepted.order_ref or "", order_id=f"b-{accepted.order_ref}", symbol=symbol,
            status="filled" if filled == wanted else "partially_filled", side="buy",
            quantity=wanted, filled_quantity=filled, filled_avg_price=price,
        ).model_copy(update={"updated_at_ms": NOON, "observed_at_ms": NOON}),
    )


def test_a_fraction_of_a_contract_is_refused_on_an_order_that_trades_one(tmp_path: Path) -> None:
    """Contracts are whole. One and a half is refused as an exact execution and as a recovered total alike."""
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        with pytest.raises(ContractTermsConflict, match="whole contracts"):
            fill(repo, order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1.5, price=2.00)
        with pytest.raises(ContractTermsConflict, match="whole contracts"):
            _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=1.5, price=2.00)
        assert _records(repo) == ()
        assert repo.attributed_positions_by_subject() == {}

        assert fill(repo, order, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=2.00) == "appended"
        assert repo.position("bot-a", AAPL_CALL.symbol) == 2.0
    finally:
        repo.close()


def test_a_fraction_of_a_share_is_counted_on_a_stock_order_as_before(tmp_path: Path) -> None:
    """Half a share as an exact execution. And half a share recovered as a total, then replaced by its exact execution."""
    repo = open_option_books(tmp_path, bots={"bot-s": "SPY", "bot-q": "QQQ"})
    try:
        streamed = _accept_stock_order(repo, symbol="SPY", side="buy", shares=0.5, decision_id="d1", bot="bot-s")
        assert fill(repo, streamed, execution_id="x1", symbol="SPY", side="BUY", quantity=0.5, price=2.00) == "appended"

        recovered = _accept_stock_order(repo, symbol="QQQ", side="buy", shares=0.5, decision_id="d2", bot="bot-q")
        _recover_total(repo, recovered, symbol="QQQ", wanted=0.5, filled=0.5, price=2.00)
        assert fill(repo, recovered, execution_id="x2", symbol="QQQ", side="BUY", quantity=0.5, price=2.00) == "coverage_superseded"

        assert repo.attributed_positions_by_subject() == {("bot:bot-s", "SPY"): 0.5, ("bot:bot-q", "QQQ"): 0.5}
        assert {(record.symbol, record.event_key, record.quantity, record.multiplier, record.exact_notional) for record in _records(repo)} == {
            ("SPY", "x1", 0.5, None, Decimal(1)),
            ("QQQ", "x2", 0.5, None, Decimal(1)),
        }
    finally:
        repo.close()


# ── A coverage replacement writes a fill too, and moves no position ───────────


def _hold_aside(repo: ClerkSqliteRepository, order: EnterSubmission, *, reported: str) -> str:
    """An exact execution of 2 at $1.50, reported in ``reported``, held aside behind a coverage conflict.

    The Clerk holds an exact execution aside when it cannot match it to the
    total it already recovered for the order. Returns the conflict's id.
    """
    exact = ExecutionSliceFilledFacts(
        execution_id="x1", symbol=reported, side="BUY", slice_qty=2.0, slice_price=1.50, fee=None,
        fee_fidelity="not_reported", evidence_source="websocket", source_event_at_ms=NOON,
    )
    repo.append_transition(_coverage_conflict_transition(repo, accepted=order, facts=exact))
    assert _append_slice(repo, accepted=order, facts=exact) == "coverage_conflict_quarantined"
    (conflict,) = repo.active_uncertainties_for_admission(strategy_instance_id="bot-a")
    return conflict["uncertainty_id"]


def _put_in_the_recovered_fills_place(repo: ClerkSqliteRepository, conflict: str) -> ExecutionCoverageResolutionReceipt:
    """The operator's replacement of a recovered fill by the one exact execution held aside for it."""
    meta = repo.control_meta_snapshot()
    return repo.resolve_execution_coverage_conflict(
        uncertainty_id=conflict,
        expected_authority_generation=meta.authority_generation,
        expected_db_identity_token=meta.db_identity_token,
        expected_control_revision=meta.control_revision,
    )


@pytest.mark.parametrize("reported", [AAPL_PUT.symbol, "AAPL"])
def test_an_execution_in_another_symbol_cannot_take_the_place_of_a_contracts_recovered_fill(tmp_path: Path, reported: str) -> None:
    """The order's 2 calls at $1.50 were recovered as a total. An exact execution of the same side, count and price,
    reported in another symbol, is held aside.

    Putting it in the recovered fill's place moves no position, so it never
    meets the position write. The books would read it through its order, as
    2 of the call.
    """
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=2, price=1.50)
        conflict = _hold_aside(repo, order, reported=reported)

        # Refused as the operator's route can answer it, with the contract's own reason in it.
        with pytest.raises(ExecutionCoverageResolutionUnavailable, match=f"which trades {AAPL_CALL.symbol}"):
            _put_in_the_recovered_fills_place(repo, conflict)

        (kept,) = repo.fills_for_order(order.order_ref or "")
        assert (kept["evidence_source"], kept["execution_id"]) == ("cumulative_recovery", None)
        assert repo.attributed_positions_for_strategy("bot-a") == {AAPL_CALL.symbol: 2.0}
        assert [held["uncertainty_id"] for held in repo.active_uncertainties_for_admission(strategy_instance_id="bot-a")] == [conflict]
    finally:
        repo.close()


def test_a_contracts_recovered_fill_gives_way_to_its_own_exact_execution(tmp_path: Path) -> None:
    """The same replacement by an execution in the order's own contract goes through: 2 calls, $300.00, as before it."""
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=2, price=1.50)
        conflict = _hold_aside(repo, order, reported=AAPL_CALL.symbol)

        assert _put_in_the_recovered_fills_place(repo, conflict).applied

        (exact,) = repo.fills_for_order(order.order_ref or "")
        assert (exact["evidence_source"], exact["execution_id"]) == ("websocket", "x1")
        (record,) = _records(repo)
        assert (record.symbol, record.quantity, record.multiplier, record.exact_notional) == (AAPL_CALL.symbol, 2.0, 100, Decimal(300))
        assert repo.attributed_positions_for_strategy("bot-a") == {AAPL_CALL.symbol: 2.0}
        assert repo.active_uncertainties_for_admission(strategy_instance_id="bot-a") == []
    finally:
        repo.close()


def test_an_execution_reported_as_the_bots_own_symbol_cannot_take_the_place_of_a_contracts_recovered_fill(tmp_path: Path) -> None:
    """The bot is registered under AAPL and its order trades the call. The order's 2 calls at $1.50 were recovered as a total.

    Then an exact execution arrives reported as 2 AAPL at $1.50. It has the
    recovered fill's side, count and price, so it proves itself that fill's
    replacement, and AAPL is the symbol the order's bot is known by. It is
    not the order's contract, and the books keep the recovered fill.
    """
    repo = open_option_books(tmp_path)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        _recover_total(repo, order, symbol=AAPL_CALL.symbol, wanted=2, filled=2, price=1.50)

        with pytest.raises(ContractTermsConflict, match=f"which trades {AAPL_CALL.symbol}"):
            fill(repo, order, execution_id="x1", symbol="AAPL", side="BUY", quantity=2, price=1.50)

        (kept,) = repo.fills_for_order(order.order_ref or "")
        assert (kept["evidence_source"], kept["execution_id"]) == ("cumulative_recovery", None)
        (record,) = _records(repo)
        assert (record.symbol, record.quantity, record.multiplier, record.exact_notional) == (AAPL_CALL.symbol, 2.0, 100, Decimal(300))
        assert repo.attributed_positions_for_strategy("bot-a") == {AAPL_CALL.symbol: 2.0}
    finally:
        repo.close()


# ── The bot's running FIFO ────────────────────────────────────────────────────


def _contract_fill(side: OrderSide, contracts: float, premium: float, *, ts_ms: int, multiplier: int | None = 100) -> FillRecord:
    return FillRecord(
        account_id=ACCOUNT_ID, sid="bot-a", intent_id=f"int_{ts_ms}", order_ref=f"learn-ai/bot-a/v1:int_{ts_ms}",
        event_key=f"exec:{ts_ms}", symbol=AAPL_CALL.symbol, side=side, quantity=contracts, fill_price=premium,
        filled_at_ms=ts_ms, fee=None, multiplier=multiplier,
    )


def test_a_fill_fifo_refuses_leaves_nothing_behind_in_the_bots_running_lots() -> None:
    """Buy 1. A read of [buy 1 more, a fill FIFO refuses] fails, twice. Then the fill as it should be: sell 2. Flat.

    A refused read used to leave its "buy 1 more" in the lots each time: 4
    bought against 2 sold, and 2 open contracts nobody holds.
    (1.50 − 1.00) × 2 × 100 = $100.00 realized.
    """
    first = _contract_fill(OrderSide.BUY, 1, 1.00, ts_ms=1_000)
    second = _contract_fill(OrderSide.BUY, 1, 1.00, ts_ms=2_000)
    counted_as_shares = _contract_fill(OrderSide.SELL, 2, 1.50, ts_ms=3_000, multiplier=None)
    sale = _contract_fill(OrderSide.SELL, 2, 1.50, ts_ms=3_000)
    running = _CachedFifo()
    running.project((first,))
    for _ in range(2):
        with pytest.raises(MoneyInputError, match="open lots count contracts of 100 shares"):
            running.project((first, second, counted_as_shares))

    closed, lots, _ = running.project((first, second, sale))

    assert compute_open_pnl(lots, {}).open_lots == ()
    assert closed == compute_fifo_pnl([first, second, sale]).closed_lots
    assert sum(lot.exact_realized_pnl for lot in closed) == Decimal(100)


def test_a_refused_read_does_not_leave_a_closed_lot_to_be_counted_twice() -> None:
    """Buy 1 at $1.00 and sell it at $1.20: one closed lot. Then a read that ends in a fill FIFO refuses, and
    then the read as it should be, with a second round trip at $1.50.

    The closed lots are the two round trips, once each: (1.20 − 1.00) × 100 + (1.50 − 1.00) × 100 = $70.00.
    """
    round_trip = (_contract_fill(OrderSide.BUY, 1, 1.00, ts_ms=1_000), _contract_fill(OrderSide.SELL, 1, 1.20, ts_ms=2_000))
    again = _contract_fill(OrderSide.BUY, 1, 1.00, ts_ms=3_000)
    counted_as_shares = _contract_fill(OrderSide.SELL, 1, 1.50, ts_ms=4_000, multiplier=None)
    sale = _contract_fill(OrderSide.SELL, 1, 1.50, ts_ms=4_000)
    running = _CachedFifo()
    running.project(round_trip)
    with pytest.raises(MoneyInputError, match="open lots count contracts of 100 shares"):
        running.project((*round_trip, again, counted_as_shares))

    closed, lots, _ = running.project((*round_trip, again, sale))

    assert compute_open_pnl(lots, {}).open_lots == ()
    assert closed == compute_fifo_pnl([*round_trip, again, sale]).closed_lots
    assert sum(lot.exact_realized_pnl for lot in closed) == Decimal(70)


# ── Budget: what an open position costs ───────────────────────────────────────


def _budgeted_books(tmp_path: Path, *, bots: dict[str, str], budget_cents: int) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_noon_clock(), fold_registry=option_fold_registry()
    )
    complete_fee_evidence(repo)
    for bot, symbol in bots.items():
        _register_active(repo, repo.clock, strategy_instance_id=bot, symbol=symbol, run_id=RUN_ID, committed_cents=budget_cents)
    return repo


def _noon_clock() -> _TestClock:
    return _clock_at(NOON)


def _acknowledge_filled(repo: ClerkSqliteRepository, accepted: EnterSubmission, *, symbol: str, side: str, quantity: float, price: float, order_id: str) -> None:
    fold_order_acknowledgement(
        repo,
        effect_operation_id=accepted.effect_operation_id or "",
        order=_broker_order_fixture(
            accepted.order_ref or "", order_id=order_id, symbol=symbol, status="filled", side=side,
            quantity=quantity, filled_quantity=quantity, filled_avg_price=price,
        ).model_copy(update={"updated_at_ms": NOON, "observed_at_ms": NOON}),
    )


def test_the_budget_counts_an_open_option_position_at_contracts_times_premium_times_multiplier(tmp_path: Path) -> None:
    """A $1,000.00 budget. 2 contracts bought at $1.50 cost $300.00: $700.00 free.

    Sold at $2.10: $120.00 gained, nothing held, balance $1,120.00. All before fees.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, bought, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")

        (held,) = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON + 1, fees=_NO_FEES).deployments
        assert (held.position_cost, held.free, held.balance) == (Decimal(300), Decimal(700), Decimal(1_000))

        sold = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=2, decision_id="d2")
        fill(repo, sold, execution_id="x2", symbol=AAPL_CALL.symbol, side="SELL", quantity=2, price=2.10)
        _acknowledge_filled(repo, sold, symbol=AAPL_CALL.symbol, side="sell", quantity=2, price=2.10, order_id="b2")

        (flat,) = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON + 1, fees=_NO_FEES).deployments
        assert (flat.realized_gross, flat.position_cost, flat.balance) == (Decimal(120), Decimal(0), Decimal(1_120))
    finally:
        repo.close()


def test_the_budget_counts_an_open_stock_position_at_shares_times_price_as_before(tmp_path: Path) -> None:
    """Stock twin, through the real admission: 2 shares bought at $1.50 cost $3.00 of a $1,000.00 budget."""
    repo = _budgeted_books(tmp_path, bots={"bot-s": "SPY"}, budget_cents=100_000)
    try:
        bought = accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-s", decision_id="d1", lifecycle_run_id=RUN_ID,
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
            envelope=envelope_gate(cash=5_000.0, observed_at_ms=repo.clock(), fill_sequence=risk_fill_sequence(repo)),
            reference_price=1.50,
        )
        fill(repo, bought, execution_id="x1", symbol="SPY", side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, bought, symbol="SPY", side="buy", quantity=2, price=1.50, order_id="b1")

        (held,) = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON + 1, fees=_NO_FEES).deployments
        assert (held.position_cost, held.free) == (Decimal(3), Decimal(997))
    finally:
        repo.close()


# ── Budget: what an entry claims before cash has caught up ────────────────────


def test_an_option_entrys_fill_claims_the_same_cash_before_and_after_cash_sees_it(tmp_path: Path) -> None:
    """$5,000.00 of cash and a $1,000.00 budget. 2 contracts fill at $1.50: $300.00.

    Cash read before the fill was recorded: the bot keeps $700.00 free and the
    fill claims $300.00, so $4,000.00 is free to deploy. Once cash is
    $4,700.00 and holds the fill, nothing is claimed and $4,000.00 is free.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        bought = accept_option_order(
            repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1", reference_price="1.50", fee_provision_cents=9
        )
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, bought, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")

        unseen = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON, fees=_NO_FEES)
        seen = project_account_budget(repo._conn, cash=Decimal(4_700), seen_before_ms=NOON + 1, fees=_NO_FEES)

        assert (unseen.order_claims, unseen.available) == (Decimal(300), Decimal(4_000))
        assert (seen.order_claims, seen.available) == (Decimal(0), Decimal(4_000))
        for budget in (unseen, seen):
            (held,) = budget.deployments
            assert (held.position_cost, held.pending_orders, held.free) == (Decimal(300), Decimal(0), Decimal(700))
        # The money map counts the unseen fill once: $5,000.00 less $300.00 of cash, plus $300.00 of contracts.
        for cash, seen_before_ms in ((5_000, NOON), (4_700, NOON + 1)):
            money = project_account_money(repo._conn, cash=Decimal(cash), seen_before_ms=seen_before_ms, fees=_NO_FEES)
            assert (money.cash, money.total) == (Decimal(4_700), Decimal(5_000))
    finally:
        repo.close()


def test_a_fee_reported_on_an_option_fill_is_claimed_as_it_was_charged_not_by_the_multiplier(tmp_path: Path) -> None:
    """2 contracts fill at $1.50 with a $0.26 fee on the fill. Cash read before the fill was recorded has
    not paid either: the fill claims $300.00 and the fee $0.26, never $26.00."""
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        bought = accept_option_order(
            repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1", reference_price="1.50", fee_provision_cents=9
        )
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50, fee=0.26)
        _acknowledge_filled(repo, bought, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")

        unseen = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON, fees=_NO_FEES)

        assert unseen.order_claims == Decimal("300.26")
    finally:
        repo.close()


def test_a_stock_entrys_fill_claims_the_same_cash_before_and_after_cash_sees_it_as_before(tmp_path: Path) -> None:
    """Stock twin, through the real admission: 2 shares at $1.50 claim $3.00, and $4,000.00 is free both ways."""
    repo = _budgeted_books(tmp_path, bots={"bot-s": "SPY"}, budget_cents=100_000)
    try:
        bought = accept_enter(
            repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-s", decision_id="d1", lifecycle_run_id=RUN_ID,
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
            envelope=envelope_gate(cash=5_000.0, observed_at_ms=repo.clock(), fill_sequence=risk_fill_sequence(repo)),
            reference_price=1.50,
        )
        fill(repo, bought, execution_id="x1", symbol="SPY", side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, bought, symbol="SPY", side="buy", quantity=2, price=1.50, order_id="b1")

        unseen = project_account_budget(repo._conn, cash=Decimal(5_000), seen_before_ms=NOON, fees=_NO_FEES)
        seen = project_account_budget(repo._conn, cash=Decimal(4_997), seen_before_ms=NOON + 1, fees=_NO_FEES)

        assert (unseen.order_claims, unseen.available) == (Decimal(3), Decimal(4_000))
        assert (seen.order_claims, seen.available) == (Decimal(0), Decimal(4_000))
    finally:
        repo.close()


def test_an_option_entry_still_working_claims_what_its_contracts_will_cost(tmp_path: Path) -> None:
    """A $1,000.00 budget. 2 contracts wanted at $1.50, with 9 cents set aside for fees.

    Not filled: 2 × 1.50 × 100 + 0.09 = $300.09 claimed, $699.91 free.
    1 filled at $1.40: it cost $140.00, and the other still claims 1 × 1.50 × 100 + 0.09 = $150.09: $709.91 free.
    The other filled at $1.50: $290.00 of contracts, nothing claimed, $710.00 free.
    The account's free cash is $4,000.00 at every step: cash is $5,000.00 less what it paid.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)

    def bot_and_free_cash(cash: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        budget = project_account_budget(repo._conn, cash=Decimal(cash), seen_before_ms=NOON + 1, fees=_NO_FEES)
        (bot,) = budget.deployments
        return bot.position_cost, bot.pending_orders, bot.free, budget.available

    try:
        working = accept_option_order(
            repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1", reference_price="1.50", fee_provision_cents=9
        )
        assert bot_and_free_cash(5_000) == (Decimal(0), Decimal("300.09"), Decimal("699.91"), Decimal(4_000))

        fill(repo, working, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=1.40)
        assert bot_and_free_cash(4_860) == (Decimal(140), Decimal("150.09"), Decimal("709.91"), Decimal(4_000))

        fill(repo, working, execution_id="x2", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=1.50)
        assert bot_and_free_cash(4_710) == (Decimal(290), Decimal(0), Decimal(710), Decimal(4_000))
    finally:
        repo.close()


def test_an_option_sale_cash_has_not_seen_yet_is_on_the_money_map_at_its_proceeds(tmp_path: Path) -> None:
    """$5,000.00 to start. 2 contracts bought at $1.50 ($300.00) and sold at $2.10 ($420.00).

    Holding: $4,700.00 of cash and $300.00 of contracts, $5,000.00 in all.
    Sold, before the proceeds reach cash: $4,700.00 and $420.00 on its way, $5,120.00.
    Once cash is $5,120.00 the total is the same. The stock case is
    ``test_a_sale_the_cash_has_not_seen_settles_on_the_bar_and_the_total_holds``.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1", reference_price="1.50")
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, bought, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")
        holding = project_account_money(repo._conn, cash=Decimal(4_700), seen_before_ms=NOON + 5, fees=_NO_FEES)

        repo.clock.advance(10)
        closing = accept_option_closing_order(repo, terms=AAPL_CALL, opened=bought, decision_id="d2")
        fill(
            repo, closing, execution_id="x2", symbol=AAPL_CALL.symbol, side="SELL", quantity=2, price=2.10,
            occurred_at_ms=NOON + 10,
        )
        unseen = project_account_money(repo._conn, cash=Decimal(4_700), seen_before_ms=NOON + 5, fees=_NO_FEES)
        seen = project_account_money(repo._conn, cash=Decimal(5_120), seen_before_ms=NOON + 11, fees=_NO_FEES)

        assert (holding.unseen_sales, holding.total) == (Decimal(0), Decimal(5_000))
        assert (unseen.unseen_sales, unseen.cash, unseen.total) == (Decimal(420), Decimal(5_120), Decimal(5_120))
        assert (seen.unseen_sales, seen.cash, seen.total) == (Decimal(0), Decimal(5_120), Decimal(5_120))
        (flat,) = seen.budget.deployments
        assert (flat.realized_gross, flat.position_cost) == (Decimal(120), Decimal(0))
    finally:
        repo.close()


def test_a_stopped_bot_still_holding_is_on_the_money_map_at_what_its_contracts_or_shares_cost(tmp_path: Path) -> None:
    """Two bots with no budget, stopped while holding. 2 contracts at $1.50 cost $300.00; 2 shares at $1.50 cost $3.00.

    $5,000.00 to start: $4,697.00 of cash and $303.00 held.
    """
    repo = open_option_books(tmp_path, bots={"bot-a": "AAPL", "bot-s": "SPY"})
    try:
        contracts = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        fill(repo, contracts, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, contracts, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")
        shares = _accept_stock_order(repo, symbol="SPY", side="buy", shares=2, decision_id="d1", bot="bot-s")
        fill(repo, shares, execution_id="x2", symbol="SPY", side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, shares, symbol="SPY", side="buy", quantity=2, price=1.50, order_id="b2")
        for bot in ("bot-a", "bot-s"):
            submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=bot, lifecycle_run_id=RUN_ID, clock=repo.clock)

        money = project_account_money(repo._conn, cash=Decimal(4_697), seen_before_ms=NOON + 1, fees=_NO_FEES)

        assert {held.strategy_instance_id: held.position_cost for held in money.stopped} == {
            "bot-a": Decimal(300), "bot-s": Decimal(3),
        }
        assert money.total == Decimal(5_000)
    finally:
        repo.close()


def test_a_sale_cash_has_not_seen_yet_counts_its_proceeds_by_the_multiplier(tmp_path: Path) -> None:
    """Simulated cash: $1,000.00 less 2 × 1.50 × 100 bought, plus 2 × 2.10 × 100 sold, is $1,120.00."""
    repo = open_option_books(tmp_path)
    try:
        bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        fill(repo, bought, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        assert _cash_after_fills(Decimal(1_000), _records(repo), _NO_FEES) == Decimal(700)
        sold = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=2, decision_id="d2")
        fill(repo, sold, execution_id="x2", symbol=AAPL_CALL.symbol, side="SELL", quantity=2, price=2.10)
        assert _cash_after_fills(Decimal(1_000), _records(repo), _NO_FEES) == Decimal(1_120)
    finally:
        repo.close()


# ── Fees: each fill by its own fee model, and a day's fee rows shared among all its fills ──

_DAY_MS = 86_400_000


def _two_shares_bought(repo: ClerkSqliteRepository) -> None:
    """Bot S buys two SPY shares at $500.00 on its budget, filled at noon."""
    shares = accept_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-s", decision_id="d1", lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2), reference_price=500,
        envelope=envelope_gate(cash=5_000.0, observed_at_ms=repo.clock(), fill_sequence=risk_fill_sequence(repo)),
    )
    fill(repo, shares, execution_id="x2", symbol="SPY", side="BUY", quantity=2, price=500.0)
    _acknowledge_filled(repo, shares, symbol="SPY", side="buy", quantity=2, price=500.0, order_id="b2")


def _option_and_stock_fills(repo: ClerkSqliteRepository) -> None:
    """Bot S buys two SPY shares at $500.00 and bot A buys two AAPL calls at $1.50, both filled at noon."""
    _two_shares_bought(repo)
    _calls_bought(repo, "bot-a", contracts=2, price=1.50)


def _calls_bought(repo: ClerkSqliteRepository, bot: str, *, contracts: int, price: float) -> str:
    """``bot`` buys AAPL calls at ``price``, filled when the order is taken and shown as filled by Alpaca.

    Returns Alpaca's id of the order.
    """
    broker_order_id = f"{bot}-buy-{repo.clock()}"
    order = accept_option_order(
        repo, terms=AAPL_CALL, side="buy", contracts=contracts, decision_id=f"buy-{repo.clock()}", strategy_instance_id=bot
    )
    fill(
        repo, order, execution_id=f"{bot}-buy-{repo.clock()}", symbol=AAPL_CALL.symbol, side="BUY", quantity=contracts,
        price=price, occurred_at_ms=repo.clock(),
    )
    fold_order_acknowledgement(
        repo,
        effect_operation_id=order.effect_operation_id or "",
        order=_broker_order_fixture(
            order.order_ref or "", order_id=broker_order_id, symbol=AAPL_CALL.symbol, status="filled", side="buy",
            quantity=contracts, filled_quantity=contracts, filled_avg_price=price,
        ).model_copy(update={"updated_at_ms": repo.clock(), "observed_at_ms": repo.clock()}),
    )
    return broker_order_id


def _a_hundred_shares_bought_and_sold(repo: ClerkSqliteRepository) -> None:
    """Bot S buys a hundred SPY shares at $50.00 at noon and sells them a minute later at the same price."""
    shares = accept_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-s", decision_id="d1", lifecycle_run_id=RUN_ID,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=100), reference_price=50,
        envelope=envelope_gate(cash=20_000.0, observed_at_ms=repo.clock(), fill_sequence=risk_fill_sequence(repo)),
    )
    fill(repo, shares, execution_id="s-buy", symbol="SPY", side="BUY", quantity=100, price=50.0)
    # A stock bot sells through its EXIT; for the day's fees the sale's execution is enough.
    fill(repo, shares, execution_id="s-sell", symbol="SPY", side="SELL", quantity=100, price=50.0, occurred_at_ms=NOON + _MINUTE_MS)
    _acknowledge_filled(repo, shares, symbol="SPY", side="buy", quantity=100, price=50.0, order_id="s1")


def _ten_calls_bought_and_sold(repo: ClerkSqliteRepository) -> None:
    """Bot A buys ten AAPL calls at $1.94 at noon and sells them a minute later at $2.40."""
    bought = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=10, decision_id="d1")
    fill(repo, bought, execution_id="a-buy", symbol=AAPL_CALL.symbol, side="BUY", quantity=10, price=1.94)
    _acknowledge_filled(repo, bought, symbol=AAPL_CALL.symbol, side="buy", quantity=10, price=1.94, order_id="a1")
    sold = accept_option_order(repo, terms=AAPL_CALL, side="sell", contracts=10, decision_id="d2")
    fill(
        repo, sold, execution_id="a-sell", symbol=AAPL_CALL.symbol, side="SELL", quantity=10, price=2.40,
        occurred_at_ms=NOON + _MINUTE_MS,
    )
    _acknowledge_filled(repo, sold, symbol=AAPL_CALL.symbol, side="sell", quantity=10, price=2.40, order_id="a2")


def _fee_row(key: str, amount: float, *, day_of_ms: int = NOON, seen_at_ms: int = NOON) -> BrokerActivity:
    """A fee row as Alpaca posts one for a day: only its date and what it took."""
    return BrokerActivity(
        broker="alpaca", activity_id=key, activity_type="FEE", category="non_trade_activity", symbol=None, side=None,
        quantity=None, price=None, net_amount=amount, occurred_at_ms=et_midnight_ms(et_date_at_ms(day_of_ms)),
        observed_at_ms=seen_at_ms,
    )


def _read_a_day_later(repo: ClerkSqliteRepository, *rows: BrokerActivity) -> None:
    """A day passes, and the Clerk reads Alpaca's activities to their start: ``rows`` are the fee rows in them."""
    _walk_clock_to(repo, repo.clock() + _DAY_MS)
    record_fee_evidence(
        repo, [row.model_copy(update={"observed_at_ms": repo.clock()}) for row in rows],
        checked_at_ms=repo.clock(), history_complete=True,
    )


def test_a_real_accounts_option_fill_is_charged_by_the_option_model_as_an_estimate_and_its_budget_goes_on(
    tmp_path: Path,
) -> None:
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        # An option order that has not filled owes no fee.
        assert repo.fee_attribution(now_ms=NOON).shares == ()
        fill(repo, order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, order, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")

        fees = repo.fee_attribution(now_ms=NOON)

        assert fees.known
        # Two contracts bought owe no SEC fee and no TAF. CAT is $0.0006, ORF $0.03 and OCC $0.05;
        # each is rounded up to the cent: 1 + 3 + 5.
        assert sorted((share.charge_id, share.amount, share.state) for share in fees.shares) == [
            (f"model:{DAY}:option:cat", Decimal("0.01"), "estimated"),
            (f"model:{DAY}:option:occ", Decimal("0.05"), "estimated"),
            (f"model:{DAY}:option:orf", Decimal("0.03"), "estimated"),
        ]
        # $1,000.00 budgeted, $300.00 in the position and nine cents of estimated fees: the budget answers.
        budget = repo.account_budget(cash=5_000, seen_before_ms=NOON + 1)
        assert budget.deployments[0].fees == Decimal("0.09")
    finally:
        repo.close()


def test_a_real_accounts_option_fee_estimate_is_still_an_estimate_after_its_day_while_no_fee_row_is_seen(
    tmp_path: Path,
) -> None:
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        _calls_bought(repo, "bot-a", contracts=2, price=1.50)
        _read_a_day_later(repo)

        next_day = repo.fee_attribution(now_ms=repo.clock())

        # Only Alpaca's own fee activity settles a real fee; the model's figure never calls itself settled.
        assert next_day.known
        assert {share.state for share in next_day.shares} == {"estimated"}
        assert next_day.total_for(bot_subject_id("bot-a")) == Decimal("0.09")
    finally:
        repo.close()


def test_an_option_fill_is_left_out_of_what_the_stock_fee_model_prices(tmp_path: Path) -> None:
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-s": "SPY"}, budget_cents=200_000)
    alone = _budgeted_books(tmp_path / "stock-only", bots={"bot-s": "SPY"}, budget_cents=200_000)
    try:
        _option_and_stock_fills(repo)
        _two_shares_bought(alone)

        fees, stock_only = repo.fee_attribution(now_ms=NOON), alone.fee_attribution(now_ms=NOON)

        # The shares are charged exactly what they are charged on books that never traded an option.
        stock_rows = [share for share in fees.shares if ":option:" not in share.charge_id]
        assert stock_rows == list(stock_only.shares) and stock_rows
        assert {share.subject_id for share in stock_rows} == {bot_subject_id("bot-s")}
        # And the option fill has the option model's rows, and no other.
        assert fees.total_for(bot_subject_id("bot-a")) == Decimal("0.09")
        assert fees.known
    finally:
        repo.close()
        alone.close()


def test_a_fee_row_of_a_day_with_only_option_fills_is_shared_among_their_bots_and_the_budget_goes_on(
    tmp_path: Path,
) -> None:
    """Bot A buys ten AAPL calls and bot B one, on 2026-09-08. A buy owes no SEC fee and no TAF.

    By the option model A's fill owes CAT $0.003, ORF $0.15 and OCC $0.25, $0.403 in all, and
    B's a tenth of that, $0.0403. Alpaca posts $0.46 for the day. A's part is
    46 x 0.403 / 0.4433 = 41.82 cents and B's 4.18: 41 and 4 whole cents, and the odd cent
    goes to the larger remainder, A's. $0.42 and $0.04.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-b": "AAPL"}, budget_cents=200_000)
    try:
        _calls_bought(repo, "bot-a", contracts=10, price=1.50)
        _calls_bought(repo, "bot-b", contracts=1, price=1.50)
        _read_a_day_later(repo, _fee_row("fee-of-the-day", -0.46))

        fees = repo.fee_attribution(now_ms=repo.clock())

        assert fees.known and fees.unattributed == 0
        assert {(share.subject_id, share.charge_id, share.state): share.amount for share in fees.shares} == {
            (bot_subject_id("bot-a"), "fee-of-the-day", "observed"): Decimal("0.42"),
            (bot_subject_id("bot-b"), "fee-of-the-day", "observed"): Decimal("0.04"),
        }
        assert fees.observed_total == Decimal("0.46")
        # No charge is nobody's, so the account's budget answers and each bot carries its own part.
        budget = repo.account_budget(cash=5_000, seen_before_ms=repo.clock() + 1)
        assert {bot.strategy_instance_id: bot.fees for bot in budget.deployments} == {
            "bot-a": Decimal("0.42"), "bot-b": Decimal("0.04"),
        }
    finally:
        repo.close()


def test_a_fee_row_that_names_an_option_order_is_charged_to_that_orders_bot_whatever_the_models_weigh(
    tmp_path: Path,
) -> None:
    """Bot A buys ten calls and bot B one. A row of $0.41 names A's order: it is A's whole.

    B's fill is not in the row, so B keeps the option model's estimate of its part of the
    day: 2 of the ORF's 17 cents and 3 of the OCC's 28. The CAT cent is A's.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-b": "AAPL"}, budget_cents=200_000)
    try:
        order_of_a = _calls_bought(repo, "bot-a", contracts=10, price=1.50)
        _calls_bought(repo, "bot-b", contracts=1, price=1.50)
        _read_a_day_later(repo, _fee_row("fee-of-an-order", -0.41).model_copy(update={"native_order_id": order_of_a}))

        fees = repo.fee_attribution(now_ms=repo.clock())

        assert fees.known
        assert {(share.subject_id, share.state): share.amount for share in fees.shares if share.state == "observed"} == {
            (bot_subject_id("bot-a"), "observed"): Decimal("0.41"),
        }
        assert (fees.total_for(bot_subject_id("bot-a")), fees.total_for(bot_subject_id("bot-b"))) == (
            Decimal("0.41"), Decimal("0.05"),
        )
    finally:
        repo.close()


def test_a_day_with_stock_and_option_fills_shares_its_fee_row_among_all_of_them_each_by_its_own_fee_model(
    tmp_path: Path,
) -> None:
    """The worked example of a mixed day, 2026-09-08, in dollars.

    The fills, and what each one's own fee model predicts for it:

        bot S  buys 100 SPY at $50.00     SEC 0        TAF 0       CAT 0.0003
               sells 100 SPY at $50.00    SEC 0.103    TAF 0.0195  CAT 0.0003
               in all 0.1231              (a share: SEC 0.0000206 a dollar sold, TAF 0.000195
                                           a share sold, CAT 0.000003 a share)
        bot A  buys 10 calls at $1.94     SEC 0        TAF 0       CAT 0.003  ORF 0.15  OCC 0.25
               sells 10 calls at $2.40    SEC 0.04944  TAF 0.0329  CAT 0.003  ORF 0.15  OCC 0.25
               in all 0.88834             (a contract: SEC 0.0000206 a dollar of premium sold,
                                           TAF 0.00329 sold, CAT 0.000003 x 100, ORF 0.015, OCC 0.025)

    Before a row is seen each bot carries its own model's estimate, each fee type rounded up
    to the cent: S 11 + 2 + 1 = $0.14, and A 5 + 4 + 1 + 30 + 50 = $0.90.

    Alpaca posts one row for the day, $1.03, naming no fee type. The weights are all each
    model predicts, 0.1231 and 0.88834, 1.01144 together. S's part of the 103 cents is
    103 x 0.1231 / 1.01144 = 12.54 and A's 90.46: 12 and 90 whole cents, and the odd cent goes
    to the larger remainder, S's. S owes $0.13 and A $0.90: $1.03, what Alpaca took.

    On books that never traded an option the same row is all S's: sharing the day with the
    option fills is what changes S's figure.
    """
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-s": "SPY"}, budget_cents=1_000_000)
    alone = _budgeted_books(tmp_path / "stock-only", bots={"bot-s": "SPY"}, budget_cents=1_000_000)
    bot_a, bot_s = bot_subject_id("bot-a"), bot_subject_id("bot-s")
    try:
        _a_hundred_shares_bought_and_sold(repo)
        _ten_calls_bought_and_sold(repo)
        _a_hundred_shares_bought_and_sold(alone)
        estimate = repo.fee_attribution(now_ms=NOON)
        for books in (repo, alone):
            _read_a_day_later(books, _fee_row("fee-of-the-day", -1.03))

        fees, stock_only = repo.fee_attribution(now_ms=repo.clock()), alone.fee_attribution(now_ms=alone.clock())

        assert {share.state for share in estimate.shares} == {"estimated"}
        assert (estimate.total_for(bot_s), estimate.total_for(bot_a)) == (Decimal("0.14"), Decimal("0.90"))
        assert fees.known and fees.unattributed == 0
        assert {(share.subject_id, share.charge_id, share.state): share.amount for share in fees.shares} == {
            (bot_s, "fee-of-the-day", "observed"): Decimal("0.13"),
            (bot_a, "fee-of-the-day", "observed"): Decimal("0.90"),
        }
        assert sum((share.amount for share in fees.shares), Decimal(0)) == fees.observed_total == Decimal("1.03")
        assert stock_only.total_for(bot_s) == Decimal("1.03")
    finally:
        repo.close()
        alone.close()


def test_a_stock_only_day_beside_an_option_day_is_charged_to_the_cent_what_it_is_on_books_with_no_option(
    tmp_path: Path,
) -> None:
    """Bot S trades shares on the 8th; bot A buys two calls on the 9th. Alpaca posts $0.15 for the 8th and $0.09 for the 9th."""
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-s": "SPY"}, budget_cents=1_000_000)
    alone = _budgeted_books(tmp_path / "stock-only", bots={"bot-s": "SPY"}, budget_cents=1_000_000)
    bot_a, bot_s = bot_subject_id("bot-a"), bot_subject_id("bot-s")
    try:
        for books in (repo, alone):
            _a_hundred_shares_bought_and_sold(books)
            _read_a_day_later(books)
        _calls_bought(repo, "bot-a", contracts=2, price=1.50)
        while_estimated = [repo.fee_attribution(now_ms=repo.clock()), alone.fee_attribution(now_ms=alone.clock())]
        rows = [_fee_row("fee-of-the-8th", -0.15), _fee_row("fee-of-the-9th", -0.09, day_of_ms=NOON + _DAY_MS)]
        _read_a_day_later(repo, *rows)
        _read_a_day_later(alone, rows[0])

        fees, stock_only = repo.fee_attribution(now_ms=repo.clock()), alone.fee_attribution(now_ms=alone.clock())

        for with_options, without in (while_estimated, (fees, stock_only)):
            assert [share for share in with_options.shares if share.subject_id == bot_s] == list(without.shares)
            assert with_options.known and without.known
        assert stock_only.total_for(bot_s) == Decimal("0.15")
        assert {(share.charge_id, share.state): share.amount for share in fees.shares if share.subject_id == bot_a} == {
            ("fee-of-the-9th", "observed"): Decimal("0.09"),
        }
    finally:
        repo.close()
        alone.close()


def test_an_option_fills_fee_estimate_is_held_back_from_cash_until_cash_is_read_after_its_days_row(
    tmp_path: Path,
) -> None:
    """Two calls bought: nine cents of estimated fees (CAT 1, ORF 3, OCC 5), then a row of nine cents for the day."""
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        _calls_bought(repo, "bot-a", contracts=2, price=1.50)
        _read_a_day_later(repo)
        # No row yet: the estimate is held back however late cash was read.
        unsettled = repo.account_budget(cash=5_000, seen_before_ms=repo.clock() + 1).fee_claims
        _read_a_day_later(repo, _fee_row("fee-of-the-day", -0.09))
        row_seen_at_ms = repo.clock()

        cash_read_before_the_row = repo.account_budget(cash=5_000, seen_before_ms=row_seen_at_ms).fee_claims
        cash_read_after_the_row = repo.account_budget(cash=5_000, seen_before_ms=row_seen_at_ms + 1).fee_claims

        assert (unsettled, cash_read_before_the_row, cash_read_after_the_row) == (
            Decimal("0.09"), Decimal("0.09"), Decimal(0),
        )
    finally:
        repo.close()


def test_an_option_order_shown_filled_with_no_execution_on_the_books_still_leaves_the_fees_unresolved(
    tmp_path: Path,
) -> None:
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL", "bot-b": "AAPL"}, budget_cents=200_000)
    try:
        _calls_bought(repo, "bot-a", contracts=2, price=1.50)
        lost = accept_option_order(
            repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1", strategy_instance_id="bot-b"
        )
        _acknowledge_filled(repo, lost, symbol=AAPL_CALL.symbol, side="buy", quantity=1, price=1.50, order_id="b9")
        _read_a_day_later(repo, _fee_row("fee-of-the-day", -0.15))

        fees = repo.fee_attribution(now_ms=repo.clock())

        # A day whose fills are not all on the books shares no row: the row is nobody's and the budget waits.
        assert not fees.known and fees.unattributed == Decimal("0.15")
        assert fees.shares == ()
        with pytest.raises(BudgetUnavailable):
            repo.account_budget(cash=5_000, seen_before_ms=repo.clock() + 1)
    finally:
        repo.close()


# ── The store ─────────────────────────────────────────────────────────────────


def test_a_rebuild_from_the_mirror_brings_back_every_contract_and_binding(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    repo = open_option_books(tmp_path)
    try:
        call = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=1, decision_id="d1")
        fill(repo, call, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=1, price=2.00)
        accept_option_order(repo, terms=AAPL_PUT, side="sell", contracts=1, decision_id="d2")
        live = _contract_rows(repo)
        records = _records(repo)
        replay_root = tmp_path_factory.mktemp("replay")
        shutil.copytree(tmp_path, replay_root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("clerk.db*"))
    finally:
        repo.close()
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=replay_root, clock=_noon_clock(), fold_registry=option_fold_registry()
    )
    try:
        assert _contract_rows(rebuilt) == live
        assert len(live["option_contracts"]) == 2 and len(live["order_contracts"]) == 2
        assert _records(rebuilt) == records
        assert contract_terms(rebuilt._conn, AAPL_PUT.symbol) == AAPL_PUT
    finally:
        rebuilt.close()


def _contract_rows(repo: ClerkSqliteRepository) -> dict[str, list[tuple]]:
    return {
        table: sorted(tuple(row) for row in repo._conn.execute(f"SELECT * FROM {table}").fetchall())
        for table in ("option_contracts", "order_contracts")
    }


def test_a_stock_store_from_before_the_contract_tables_reads_the_same_after_the_clerks_next_open(tmp_path: Path) -> None:
    """A file at v22 has neither contract table. The Clerk's next open adds both, empty, and its fills read as they did."""
    repo = open_option_books(tmp_path, bots={"bot-s": "SPY"})
    try:
        order = _accept_stock_order(repo, symbol="SPY", side="buy", shares=10, decision_id="d1", bot="bot-s")
        fill(repo, order, execution_id="x1", symbol="SPY", side="BUY", quantity=10, price=500.0)
        _acknowledge_filled(repo, order, symbol="SPY", side="buy", quantity=10, price=500.0, order_id="b1")
        before = _records(repo)
        database = repo.db_path
    finally:
        repo.close()

    conn = sqlite3.connect(database)
    try:
        for trigger in (
            "trg_option_contracts_never_edited", "trg_option_contracts_never_deleted",
            "trg_order_contracts_never_edited", "trg_order_contracts_never_deleted",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP TABLE order_contracts")
        conn.execute("DROP TABLE option_contracts")
        conn.execute("UPDATE control_meta SET schema_version = 22 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_noon_clock(), fold_registry=option_fold_registry()
    )
    try:
        assert reopened._conn.execute("SELECT schema_version FROM control_meta").fetchone()[0] == schema.SCHEMA_VERSION == 23
        assert _contract_rows(reopened) == {"option_contracts": [], "order_contracts": []}
        assert _records(reopened) == before
        assert (before[0].symbol, before[0].multiplier) == ("SPY", None)
        # Books that never traded an option are told nothing about option fills.
        assert not [
            said for said in reopened.fee_attribution(now_ms=NOON, require_fresh_evidence=False).unresolved
            if "option" in said
        ]
    finally:
        reopened.close()
