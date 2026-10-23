"""Whole-history crypto fees, including the captured Paper residual (#3311)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.adapter import from_alpaca_activity
from app.broker.alpaca.clerk.account_money import money_bar
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.outside_reconciliation import reconcile_outside_quantities
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, OrderSide
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _external_fill, _external_order
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _new_budget_repo

FIXTURE = Path(__file__).parents[3] / "fixtures/golden/broker-fees/CFEE-001/v1/input.json"


def _evidence() -> tuple[dict, list[BrokerActivity]]:
    capture = json.loads(FIXTURE.read_text())
    return capture, [from_alpaca_activity(row, observed_at_ms=capture["captured_at_ms"]) for row in capture["activities"]]


def _fills(rows: list[BrokerActivity]) -> list[FillRecord]:
    return [FillRecord(
        account_id="paper", sid="external:BTC/USD", intent_id=row.activity_id, order_ref=row.native_order_id,
        event_key=row.activity_id, symbol=row.symbol, side=OrderSide(row.side), quantity=row.quantity,
        fill_price=row.price, filled_at_ms=row.occurred_at_ms, fee=None,
    ) for row in rows if row.activity_type == "FILL"]


def test_recorded_btc_fees_exactly_explain_the_broker_flat_position(tmp_path: Path) -> None:
    capture, activities = _evidence()
    expected = json.loads(FIXTURE.with_name("output.json").read_text())
    assert capture["history_complete"] and capture["positions"] == []
    fills = [row for row in activities if row.activity_type == "FILL"]
    fees = [row for row in activities if row.activity_type == "CFEE" and row.quantity is not None]
    # Independent decimal oracle over broker strings: atol=0, rtol=0.
    raw = capture["activities"]
    residual = sum((Decimal(row["qty"]) * (1 if row["side"] == "buy" else -1)
                    for row in raw if row["activity_type"] == "FILL"), Decimal(0))
    debit = sum((Decimal(row["qty"]) for row in raw if row["activity_type"] == "CFEE" and "qty" in row), Decimal(0))
    assert residual == Decimal(expected["fill_net_quantity"])
    assert debit == Decimal(expected["fee_quantity"])
    assert residual + debit == Decimal(expected["net_quantity"]) == 0
    assert len(fills) == 204 and len(fees) == 93

    repo = _new_budget_repo(tmp_path)
    at = capture["captured_at_ms"]
    repo.close()
    repo = ClerkSqliteRepository.open(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=lambda: at)
    try:
        for order_id in sorted({row.native_order_id for row in fills}):
            parts = [row for row in fills if row.native_order_id == order_id]
            quantity = sum((Decimal(str(row.quantity)) for row in parts), Decimal(0))
            observe_external_order(repo, order=_external_order(state="filled", filled=float(quantity)).model_copy(update={
                "order_id": order_id, "symbol": parts[0].symbol, "side": parts[0].side,
                "quantity": float(quantity), "filled_avg_price": parts[0].price,
            }))
        record_fee_evidence(repo, fills, checked_at_ms=at, history_complete=True)
        before = repo.account_money(cash=capture["account"]["cash"], seen_before_ms=at + 1)
        assert money_bar(before).cents_of("outside") == 268
        record_fee_evidence(repo, activities, checked_at_ms=at, history_complete=True)
        original = repo.custody_transitions()
        after = repo.account_money(cash=capture["account"]["cash"], seen_before_ms=at + 1)
        assert after.outside == 0 and after.total == Decimal(expected["equity"])
        assert after.budget.available == before.budget.available
        assert after.outside_reconciliations[0].state == "resolved"
        assert len(after.outside_reconciliations[0].evidence_ids) == 93
        assert not record_fee_evidence(repo, activities, checked_at_ms=at, history_complete=True)
        assert repo.custody_transitions() == original
        path = repo.db_path
    finally:
        repo.close()
    path.rename(path.with_suffix(".backup"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=lambda: at)
    try:
        record_fee_evidence(rebuilt, activities, checked_at_ms=at, history_complete=True)
        assert rebuilt.account_money(cash=capture["account"]["cash"], seen_before_ms=at + 1) == after
        assert rebuilt.custody_transitions() == original
        conflicting = fees[0].model_copy(update={"quantity": -1})
        record_fee_evidence(rebuilt, [conflicting], checked_at_ms=at, history_complete=True)
        with pytest.raises(BudgetUnavailable, match="Conflicting crypto fee evidence"):
            rebuilt.account_money(cash=capture["account"]["cash"], seen_before_ms=at + 1)
    finally:
        rebuilt.close()


@pytest.mark.parametrize("mutation", ["missing_fee", "positive_fee", "cash_fee", "unknown_quantity", "owned_symbol", "other_symbol", "prehistory"])
def test_insufficient_quantity_evidence_never_resolves_the_residual(mutation: str) -> None:
    _, activities = _evidence()
    fees = [row for row in activities if row.activity_type == "CFEE" and row.quantity is not None]
    excluded: set[str] = set()
    match mutation:
        case "missing_fee":
            fees = fees[1:]
        case "positive_fee":
            fees[0] = fees[0].model_copy(update={"quantity": -fees[0].quantity})
        case "cash_fee":
            fees[0] = fees[0].model_copy(update={"net_amount": -1})
        case "unknown_quantity":
            fees[0] = fees[0].model_copy(update={"quantity": None})
        case "owned_symbol":
            excluded = {"BTC/USD"}
        case "other_symbol":
            fees = [row.model_copy(update={"symbol": "ETHUSD"}) for row in fees]
        case "prehistory":
            fees[0] = fees[0].model_copy(update={"occurred_at_ms": 0})
    answers = reconcile_outside_quantities(_fills(activities), fees, excluded_symbols=excluded)
    assert all(row.state == "unresolved" and row.next_action for row in answers)


def test_fee_symbol_aliases_share_one_evidence_population() -> None:
    _, activities = _evidence()
    fees = [row for row in activities if row.activity_type == "CFEE" and row.quantity is not None]
    fees[0] = fees[0].model_copy(update={"symbol": "BTC/USD"})
    answers = reconcile_outside_quantities(_fills(activities), fees, excluded_symbols=set())
    assert len(answers) == 1 and answers[0].state == "resolved"


def test_unmapped_outside_fill_keeps_the_population_unresolved(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        fills = []
        for index, (order_id, side, quantity) in enumerate((("buy", "buy", 10), ("sell", "sell", 9), ("unknown", "buy", 5))):
            if order_id != "unknown":
                observe_external_order(repo, order=_external_order(state="filled", filled=quantity).model_copy(update={
                    "order_id": order_id, "symbol": "BTC/USD", "side": side, "quantity": quantity,
                }))
            fills.append(_external_fill(quantity=quantity).model_copy(update={
                "activity_id": order_id, "native_order_id": order_id, "symbol": "BTC/USD", "side": side,
                "occurred_at_ms": NOON + index,
            }))
        fee = BrokerActivity(
            broker="alpaca", activity_id="quantity-fee", activity_type="CFEE", category="non_trade_activity",
            symbol="BTCUSD", side=None, price=None, quantity=-1, net_amount=0, occurred_at_ms=NOON, observed_at_ms=NOON,
        )
        record_fee_evidence(repo, [*fills, fee], checked_at_ms=NOON, history_complete=True)
        money = repo.account_money(cash=1000, seen_before_ms=NOON + 10)
        assert money.outside == 100
        assert "5 shares from outside order unknown" in money.unvalued
        assert money.outside_reconciliations[0].state == "unresolved"
        assert money.outside_reconciliations[0].next_action
    finally:
        repo.close()


@pytest.mark.parametrize("occurred_at", [NOON - 86_400_000, NOON, None])
def test_crypto_fee_conflicts_respect_the_custody_history_floor(tmp_path: Path, occurred_at: int | None) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        fee = BrokerActivity(
            broker="alpaca", activity_id="conflicting-fee", activity_type="CFEE", category="non_trade_activity",
            symbol="BTCUSD", side=None, price=None, quantity=-.1, net_amount=0, occurred_at_ms=occurred_at, observed_at_ms=NOON,
        )
        record_fee_evidence(repo, [fee], checked_at_ms=NOON, history_complete=True)
        first = repo.custody_transitions()
        record_fee_evidence(repo, [fee.model_copy(update={"quantity": -.2})], checked_at_ms=NOON, history_complete=True)
        assert repo.custody_transitions()[:len(first)] == first
        if occurred_at is not None and occurred_at < NOON:
            assert repo.account_budget(cash=1000, seen_before_ms=NOON + 1).available == 1000
        else:
            with pytest.raises(BudgetUnavailable, match="Conflicting crypto fee evidence"):
                repo.account_budget(cash=1000, seen_before_ms=NOON + 1)
    finally:
        repo.close()
