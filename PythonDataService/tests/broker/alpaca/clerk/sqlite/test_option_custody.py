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
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.budget_projection import project_account_budget
from app.broker.alpaca.clerk.sqlite.contract_terms import (
    ContractTermsConflict,
    contract_terms,
    order_contract_terms,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    MarketMark,
    SqliteEconomicProjectionReader,
    effective_fill_records,
)
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.fee_evidence import (
    OPTION_FILL_FEES_NOT_ATTRIBUTED,
    custody_fee_attribution,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.simulated_account import _cash_after_fills
from app.broker.contract.models import BrokerOrderLeg
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.services.alpaca_fee_attribution import FeeAttribution
from app.utils.session_anchors import et_date_at_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    NOON,
    _broker_order_fixture,
    _clock_at,
    _register_active,
    _TestClock,
    complete_fee_evidence,
    envelope_gate,
)
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    AAPL_CALL,
    AAPL_PUT,
    ACCOUNT_ID,
    RUN_ID,
    SMALL_CALL,
    accept_option_order,
    fill,
    open_option_books,
    option_fold_registry,
)

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


# ── Fees: the stock reconciler does not price a contract as a share ───────────


def test_the_fee_reconciler_leaves_an_option_fill_unresolved_and_the_budget_refuses(tmp_path: Path) -> None:
    repo = _budgeted_books(tmp_path, bots={"bot-a": "AAPL"}, budget_cents=100_000)
    try:
        assert repo.fee_attribution(now_ms=NOON).known
        order = accept_option_order(repo, terms=AAPL_CALL, side="buy", contracts=2, decision_id="d1")
        # An option order that has not filled owes no fee and leaves the fees known.
        assert repo.fee_attribution(now_ms=NOON).known
        fill(repo, order, execution_id="x1", symbol=AAPL_CALL.symbol, side="BUY", quantity=2, price=1.50)
        _acknowledge_filled(repo, order, symbol=AAPL_CALL.symbol, side="buy", quantity=2, price=1.50, order_id="b1")

        fees = repo.fee_attribution(now_ms=NOON)
        assert not fees.known
        assert OPTION_FILL_FEES_NOT_ATTRIBUTED in fees.unresolved
        with pytest.raises(BudgetUnavailable, match="option fills"):
            repo.account_budget(cash=5_000, seen_before_ms=NOON + 1)
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


def test_a_store_no_clerk_has_migrated_yet_still_reads_its_fills_and_fees(tmp_path: Path) -> None:
    """An offline reader opens a v22 file as it is. It has no contract tables and needs none."""
    repo = open_option_books(tmp_path, bots={"bot-s": "SPY"})
    try:
        complete_fee_evidence(repo)
        order = _accept_stock_order(repo, symbol="SPY", side="buy", shares=10, decision_id="d1", bot="bot-s")
        fill(repo, order, execution_id="x1", symbol="SPY", side="BUY", quantity=10, price=500.0)
        _acknowledge_filled(repo, order, symbol="SPY", side="buy", quantity=10, price=500.0, order_id="b1")
        at_v23 = _records(repo)
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

    offline = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    offline.row_factory = sqlite3.Row
    try:
        assert effective_fill_records(offline, account_id=ACCOUNT_ID) == at_v23
        assert (at_v23[0].symbol, at_v23[0].multiplier) == ("SPY", None)
        # Reads to the end without asking for a table this file does not have.
        custody_fee_attribution(offline, now_ms=NOON, evidence_checked_at_ms=NOON)
    finally:
        offline.close()

    # The Clerk's next open brings the file to v23 with both tables empty.
    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_noon_clock(), fold_registry=option_fold_registry()
    )
    try:
        assert reopened._conn.execute("SELECT schema_version FROM control_meta").fetchone()[0] == schema.SCHEMA_VERSION == 23
        assert _contract_rows(reopened) == {"option_contracts": [], "order_contracts": []}
        assert _records(reopened) == at_v23
    finally:
        reopened.close()
