"""Open holdings are a portfolio, not one four-leg order (#2985)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.account_money import bot_segment, money_bar
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fifo_pnl import OpenLot
from app.broker.alpaca.clerk.option_position_money import option_position_money
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import OrderSide
from app.options.contract import OptionContract
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import RUN_ID, accept_option_order, fill
from tests.broker.alpaca.clerk.sqlite.test_option_event_money import books as _budgeted_books

books = _budgeted_books


def _lot(
    strike: str, *, expiry: date = date(2026, 12, 18), side: OrderSide = OrderSide.BUY,
    quantity: int = 1, price: str = "2", root: str = "SPY", multiplier: int = 100,
) -> OpenLot:
    contract = OptionContract.of(root=root, expiry_date=expiry, right="call", strike=Decimal(strike))
    return OpenLot(
        symbol=contract.occ_symbol, exact_qty=Decimal(quantity), exact_cost=Decimal(price),
        opened_at_ms=NOON, side=side, multiplier=multiplier,
    )


def test_holdings_are_valued_without_an_order_leg_cap() -> None:
    # Three $5-wide sold spreads lose $15 a share together above the last strike.
    sold_spreads = [lot for strike in ("450", "460", "470") for lot in (
        _lot(strike, side=OrderSide.SELL, price="5"), _lot(str(Decimal(strike) + 5)),
    )]
    assert option_position_money(sold_spreads) == (Decimal("600"), Decimal("1500")), (
        "six distinct contracts are valued without an order leg cap (#2985)"
    )
    bought = [_lot(str(strike)) for strike in range(450, 456)]
    assert option_position_money(bought) == (Decimal("1200"), Decimal(0)), (
        "bought holdings are not limited to four contracts (#2985)"
    )


def test_a_later_expiry_cannot_cover_an_unbounded_sold_call() -> None:
    with pytest.raises(BudgetUnavailable, match="without limit"):
        option_position_money([
            _lot("450", side=OrderSide.SELL, price="5"), _lot("455", expiry=date(2027, 1, 15)),
        ])


def test_requirements_are_kept_per_expiry_and_do_not_offset_across_underlyings_or_multipliers() -> None:
    # December: $5 × 100 = $500. January: $10 × 2 × 100 = $2,000.
    # Alpaca's rule takes their maximum; total signed basis is -$900.
    later = date(2027, 1, 15)
    expiries = [
        _lot("450", side=OrderSide.SELL, price="5"), _lot("455"),
        _lot("450", expiry=later, side=OrderSide.SELL, quantity=2, price="5"),
        _lot("460", expiry=later, quantity=2),
    ]
    assert option_position_money(expiries) == (Decimal("1100"), Decimal("2000")), (
        "separately bounded expiries keep the largest requirement (#2985)"
    )
    underlyings = [lot for root, multiplier in (("SPY", 100), ("QQQ", 100), ("SPY", 10)) for lot in (
        _lot("450", root=root, multiplier=multiplier, side=OrderSide.SELL, price="5"),
        _lot("455", root=root, multiplier=multiplier),
    )]
    assert option_position_money(underlyings) == (Decimal("420"), Decimal("1050")), (
        "independent underlyings and multipliers do not offset requirements (#2985)"
    )


@pytest.mark.parametrize("different_expiries,last_buy_price", [
    (False, "1.50"), (True, "1.50"), (True, "2"),
], ids=["six held contracts", "net credit across expiries", "zero commitment across expiries"])
def test_accumulated_custody_keeps_budget_and_money_views_available(
    books: ClerkSqliteRepository, different_expiries: bool, last_buy_price: str,
) -> None:
    # Record a portfolio history through the custody folds, independently of
    # the deciding program's one-open-entry fence.
    for number in range(3):
        for side, offset, price in (("sell", 0, "5"), ("buy", 5, last_buy_price if number == 2 else "1.50")):
            lot = _lot(
                str(450 + offset + (0 if different_expiries else number * 10)),
                expiry=date(2027, 1, 15) if different_expiries and number else date(2026, 12, 18),
            )
            order = accept_option_order(
                books, terms=ContractTerms(symbol=lot.symbol, multiplier=100), side=side, contracts=1,
                decision_id=f"entry-{number}-{side}", strategy_instance_id="a", reference_price=price,
            )
            fill(
                books, order, execution_id=f"fill-{number}-{side}", symbol=lot.symbol, side=side.upper(),
                quantity=1, price=float(price), occurred_at_ms=books.clock(),
            )
    credits = Decimal(1200) - Decimal(last_buy_price) * 100
    observed_cash = Decimal(1000) + credits
    budget = books.account_budget(cash=observed_cash, seen_before_ms=books.clock() + 1)
    item = next(item for item in budget.deployments if item.strategy_instance_id == "a")
    collateral = Decimal("1000" if different_expiries else "1500")
    assert item.option_collateral == collateral
    assert item.position_cost == collateral - credits
    assert item.free > 0
    money = books.account_money(cash=observed_cash, seen_before_ms=books.clock() + 1)
    bar = money_bar(money)
    assert bar.total_cents == 100_000
    assert money.holds_positions
    shown = bot_segment(money, "a")
    assert shown.parts.in_shares_cents == int(item.position_cost * 100)
    if different_expiries:
        assert shown.parts.in_shares_bps == 0
    submit_stop_run(
        books, account_id=books.account_id, strategy_instance_id="a", lifecycle_run_id=RUN_ID,
        operator_reason="owner stopped", clock=books.clock,
    )
    stopped = books.account_money(cash=observed_cash, seen_before_ms=books.clock() + 1)
    assert money_bar(stopped).total_cents == bar.total_cents
    assert stopped.holds_positions
    assert bot_segment(stopped, "a").cents == int(item.position_cost * 100)
    own = next(item for item in stopped.stopped if item.strategy_instance_id == "a")
    assert own.held_cents == max(0, int(item.position_cost * 100))
