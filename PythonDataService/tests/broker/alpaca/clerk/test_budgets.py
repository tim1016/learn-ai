"""Independent dollar conservation examples from the owner-approved PRD."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.budgets import account_budget, deployment_budget


@pytest.mark.parametrize(
    ("cash", "position", "pending", "overlap", "expected_free"),
    [
        ("1000", "0", "0", "0", "1000"),
        ("1000", "0", "600", "600", "400"),
        ("1000", "300", "300", "600", "400"),
        ("700", "300", "300", "300", "400"),
        ("400", "600", "0", "0", "400"),
    ],
)
def test_one_thousand_assigned_to_a_never_becomes_a_second_budget(
    cash: str, position: str, pending: str, overlap: str, expected_free: str,
) -> None:
    a = deployment_budget(
        strategy_instance_id="a", committed_cents=100_000, active=True,
        realized_gross=0, fees=D(0), position_cost=D(position), pending_orders=D(pending),
    )
    pool = account_budget(cash=cash, deployments=[a], order_claims=D(overlap), fee_claims=D(0))
    assert a.free == D(expected_free)
    assert pool.available == 0


def test_stop_releases_only_free_cash_and_pending_order_stays_claimed() -> None:
    stopped = deployment_budget(
        strategy_instance_id="a", committed_cents=100_000, active=False,
        realized_gross=0, fees=D(0), position_cost=D(0), pending_orders=D(600),
    )
    pool = account_budget(cash=1000, deployments=[stopped], order_claims=D(600), fee_claims=D(0))
    assert pool.available == 400
    assert stopped.spendable_cents == 0
    settled = account_budget(cash=1000, deployments=[stopped], order_claims=D(0), fee_claims=D(0))
    assert settled.available == 1000


@pytest.mark.parametrize("active", [True, False])
def test_overrun_is_visible_but_does_not_claim_future_deposits(active: bool) -> None:
    lost = deployment_budget(
        strategy_instance_id="a", committed_cents=100_000, active=active,
        realized_gross=-1100, fees=D(0), position_cost=D(0), pending_orders=D(0),
    )
    pool = account_budget(cash=500, deployments=[lost], order_claims=D(0), fee_claims=D(0))
    assert lost.free == -100
    assert lost.cash_claim == 0
    assert pool.available == 500


def test_pending_fee_replaces_cash_claim_when_broker_cash_observes_it() -> None:
    a = deployment_budget(
        strategy_instance_id="a", committed_cents=100_000, active=True,
        realized_gross=10, fees=D("0.05"), position_cost=D(0), pending_orders=D(0),
    )
    before = account_budget(cash=1010, deployments=[a], order_claims=D(0), fee_claims=D("0.05"))
    after = account_budget(cash="1009.95", deployments=[a], order_claims=D(0), fee_claims=D(0))
    assert before.available == after.available == 0
    assert a.free == D("1009.95")


def test_recorded_fill_fee_is_not_added_to_separate_account_claim() -> None:
    a = deployment_budget(
        strategy_instance_id="a", committed_cents=100_000, active=True,
        realized_gross=0, fees=D("0.03"), position_cost=D(600), pending_orders=D(0),
    )
    before = account_budget(cash=1000, deployments=[a], order_claims=D("600.03"), fee_claims=D(0))
    assert before.available == 0


def test_entry_affordability_uses_exact_position_and_fee_not_positive_free_cash() -> None:
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    own = deployment_budget(strategy_instance_id="a", committed_cents=1, active=True,
        realized_gross=0, fees=D(0), position_cost=D(0), pending_orders=D(0))
    pool = account_budget(cash=1000, deployments=[own], order_claims=D(0), fee_claims=D(0))
    result = budget_entry_decision(pool, strategy_instance_id="a", quantity=1, price=100, at_ms=NOON)
    assert not result.allowed
    assert result.required == D("100.01") and result.fee_cents == 1
    assert "0.01 USD free" in result.detail


def test_affordability_copy_rounds_required_cash_up_and_spendable_cash_down() -> None:
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    own = deployment_budget(strategy_instance_id="a", committed_cents=10_001, active=True,
        realized_gross="0.0009", fees=D(0), position_cost=D(0), pending_orders=D(0))
    pool = account_budget(cash=1000, deployments=[own], order_claims=D(0), fee_claims=D(0))
    result = budget_entry_decision(pool, strategy_instance_id="a", quantity=1, price="100.001", at_ms=NOON)
    assert not result.allowed
    assert result.required == D("100.011")
    assert "needs 100.02 USD" in result.detail and "100.01 USD free" in result.detail
