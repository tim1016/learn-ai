"""Independent dollar conservation examples for the budget authority."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.broker.alpaca.clerk.budgets import (
    AccountBudget,
    BudgetEntryDecision,
    DeploymentBudget,
    account_budget,
    deployment_budget,
    option_entry_decision,
)


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
    result = budget_entry_decision(pool, strategy_instance_id="a", quantity=1, price=100, at_ms=NOON, observed_at_ms=NOON)
    assert not result.allowed
    assert result.required == D("100.01") and result.fee_cents == 1
    assert "This bot has only $0.01 free." in result.detail
    assert "Shortfall: $100.00." in result.detail


def test_affordability_copy_rounds_required_cash_up_and_spendable_cash_down() -> None:
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    own = deployment_budget(strategy_instance_id="a", committed_cents=10_001, active=True,
        realized_gross="0.0009", fees=D(0), position_cost=D(0), pending_orders=D(0))
    pool = account_budget(cash=1000, deployments=[own], order_claims=D(0), fee_claims=D(0))
    result = budget_entry_decision(
        pool, strategy_instance_id="a", quantity=1, price="100.001", at_ms=NOON, observed_at_ms=NOON,
    )
    assert not result.allowed
    assert result.required == D("100.011")
    assert "needs $100.02" in result.detail and "has only $100.01 free" in result.detail
    assert "Shortfall: $0.01." in result.detail


# One cash rule (#3096). An entry is admitted when it fits its bot's free budget and the account's
# headroom for that bot: the account's unreserved cash plus that bot's own claim. Unreserved cash is
# cash less every active bot's free claim, the working-order and fee claims, and all option collateral,
# a stopped bot's included. Every figure below is worked by hand from that rule.


def _bot(
    strategy_instance_id: str, *, committed: int = 1000, active: bool = True, realized: int = 0,
    position_cost: int = 0, pending: int = 0, collateral: int = 0,
) -> DeploymentBudget:
    """A bot's budget, in whole dollars; ``position_cost`` is the signed basis plus the collateral held."""
    return deployment_budget(
        strategy_instance_id=strategy_instance_id, committed_cents=committed * 100, active=active,
        realized_gross=realized, fees=D(0), position_cost=D(position_cost), pending_orders=D(pending),
        option_collateral=D(collateral),
    )


def _asks_in_contracts(account: AccountBudget, strategy_instance_id: str, cents: int) -> BudgetEntryDecision:
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    return option_entry_decision(
        account, strategy_instance_id=strategy_instance_id, commitment_cents=cents, fee_provision_cents=0,
        at_ms=NOON, observed_at_ms=NOON,
    )


def _admitted_up_to(account: AccountBudget, strategy_instance_id: str, cents: int) -> bool:
    """Whether an entry of exactly ``cents`` is admitted and one cent more is refused."""
    return (
        _asks_in_contracts(account, strategy_instance_id, cents).allowed
        and not _asks_in_contracts(account, strategy_instance_id, cents + 1).allowed
    )


def test_an_entry_is_refused_when_collateral_held_by_another_bot_leaves_the_account_too_little() -> None:
    """The case #3096 reproduced on master 90937b3c.

    Cash $1,500. Bot A lost its whole $1,000 budget and holds a $500 position
    (signed basis plus collateral) with $1,000 of collateral: free -$500, claim
    $0. Bot B has $1,000 free, claim $1,000. Unreserved cash is
    1,500 - 1,000 - 1,000 = -$500, so the account can fund B -500 + 1,000 = $500.
    B asks for $800: refused, $300 short.
    """
    a = _bot("a", realized=-1000, position_cost=500, collateral=1000)
    b = _bot("b")
    account = account_budget(cash=1500, deployments=[a, b], order_claims=D(0), fee_claims=D(0))
    assert (a.free, a.cash_claim, b.free, b.cash_claim, account.available) == (-500, 0, 1000, 1000, -500)

    decision = _asks_in_contracts(account, "b", 80_000)

    assert not decision.allowed
    assert decision.required == D(800)
    assert decision.detail == (
        "This bot has $1,000.00 free, but the account can fund only $500.00 after protecting other positions. "
        "The entry needs $800.00 including its fee provision. Shortfall: $300.00."
    )
    assert _admitted_up_to(account, "b", 50_000)


@pytest.mark.parametrize(
    ("collateral", "can_fund_cents"),
    [(600, 90_000), (800, 70_000), (1000, 50_000)],
)
def test_a_stopped_bot_still_holding_collateral_reduces_another_bots_headroom_by_it(
    collateral: int, can_fund_cents: int,
) -> None:
    """Stopping bot A frees nothing it pledged: of cash $1,500, B can be funded what A's collateral leaves.

    A's basis is a $500 credit whatever it holds, so its position is its
    collateral less $500. It is stopped, so it claims nothing.
    """
    stopped = _bot("a", active=False, realized=-1000, position_cost=collateral - 500, collateral=collateral)
    account = account_budget(cash=1500, deployments=[stopped, _bot("b")], order_claims=D(0), fee_claims=D(0))
    assert (stopped.cash_claim, account.option_collateral) == (0, collateral)

    assert _admitted_up_to(account, "b", can_fund_cents)


def test_an_entry_in_shares_is_held_to_the_headroom_an_entry_in_contracts_is() -> None:
    """The reproduced account again: B can be funded $500. Five shares at $100 are $500 and a one-cent
    fee provision; four are $400.01."""
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    a = _bot("a", realized=-1000, position_cost=500, collateral=1000)
    account = account_budget(cash=1500, deployments=[a, _bot("b")], order_claims=D(0), fee_claims=D(0))

    five = budget_entry_decision(account, strategy_instance_id="b", quantity=5, price=100, at_ms=NOON, observed_at_ms=NOON)
    four = budget_entry_decision(account, strategy_instance_id="b", quantity=4, price=100, at_ms=NOON, observed_at_ms=NOON)

    assert not five.allowed
    assert five.required == D("500.01")
    assert "the account can fund only $500.00 after protecting other positions" in five.detail
    assert "Shortfall: $0.01." in five.detail
    assert four.allowed


def test_a_free_claim_a_working_order_a_fee_and_collateral_each_reduce_the_headroom_once() -> None:
    """Cash $6,000; B has $5,000 free. Spoken for besides: bot A's free claim $400 (a $1,000 budget and
    a $600 position of which $500 is collateral), $200 of working-order claims, $5 of fee claims.
    B can be funded 6,000 - 400 - 200 - 5 - 500 = $4,895."""
    a = _bot("a", position_cost=600, collateral=500)
    b = _bot("b", committed=5000)
    account = account_budget(cash=6000, deployments=[a, b], order_claims=D(200), fee_claims=D(5))
    assert (a.cash_claim, b.cash_claim) == (400, 5000)

    assert _admitted_up_to(account, "b", 489_500)


def test_with_no_collateral_anywhere_the_account_can_fund_cash_less_every_other_claim() -> None:
    """The rule as it was, unchanged: cash $3,500 less bot A's $400 free claim, $100 of working orders
    and $0.50 of fees is $2,999.50 for B, whose own free budget is $3,000."""
    a = _bot("a", position_cost=600)
    b = _bot("b", committed=3000)
    account = account_budget(cash=3500, deployments=[a, b], order_claims=D(100), fee_claims=D("0.50"))

    assert _admitted_up_to(account, "b", 299_950)


def test_a_bots_own_collateral_is_not_charged_a_second_time() -> None:
    """Bot A alone, a $1,000 budget, sold a spread for a $307 credit against $1,000 of collateral: a
    $693 position and $307 free. Cash is $1,307 and nothing else is claimed, so the account can fund
    it exactly what its budget has free."""
    only = _bot("a", position_cost=693, collateral=1000)
    account = account_budget(cash=1307, deployments=[only], order_claims=D(0), fee_claims=D(0))
    assert only.free == 307

    assert _admitted_up_to(account, "a", 30_700)


def test_an_account_overcommitted_by_collateral_can_fund_nothing_and_says_zero() -> None:
    """The reproduced account with $900 of cash: unreserved -$1,100, so B can be funded -$100. The
    sentence says the account can fund nothing and names the $100.00 it is over-committed by; a
    one-cent entry is then $100.01 short, which is what it would take to fund it."""
    a = _bot("a", realized=-1000, position_cost=500, collateral=1000)
    account = account_budget(cash=900, deployments=[a, _bot("b")], order_claims=D(0), fee_claims=D(0))

    decision = _asks_in_contracts(account, "b", 1)

    assert not decision.allowed
    assert "the account can fund nothing for it after protecting other positions, because it is $100.00 over-committed" in decision.detail
    assert "The entry needs $0.01 including its fee provision. Shortfall: $100.01." in decision.detail


def test_the_headroom_comparison_has_no_epsilon() -> None:
    """Pins the edge where the limit rounds down: one share at $100 with its one-cent fee provision needs
    $100.01. Cash of exactly that admits it; a ten-billionth of a dollar less floors to $100.00 and does not."""
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    def decide(cash: str) -> BudgetEntryDecision:
        account = account_budget(cash=cash, deployments=[_bot("a")], order_claims=D(0), fee_claims=D(0))
        return budget_entry_decision(account, strategy_instance_id="a", quantity=1, price=100, at_ms=NOON, observed_at_ms=NOON)

    assert decide("100.01").allowed
    assert not decide("100.0099999999").allowed


@pytest.mark.parametrize(
    ("realized", "exact_comparison_admits", "allowed"),
    [("0.0109", False, False), ("0.0115", True, False), ("0.0199", True, False), ("0.02", True, True)],
)
def test_a_share_entry_that_fits_a_limit_only_within_the_same_cent_is_refused(
    realized: str, exact_comparison_admits: bool, allowed: bool,
) -> None:
    """The entry test compares whole cents (#3097): the entry rounds up and each limit rounds down.

    One share at $100.001 with its one-cent fee provision needs $100.011, which is $100.02 to the cent. A
    $100.00 budget with $0.0115 realized has $100.0115 free, which is $100.01 to the cent: the exact
    comparison admitted it and the cent comparison refuses it, $0.01 short. Nothing the exact comparison
    refused is admitted ($100.0109), and at $100.02 free both admit.
    """
    from app.broker.alpaca.clerk.budgets import budget_entry_decision
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON

    own = deployment_budget(
        strategy_instance_id="a", committed_cents=10_000, active=True, realized_gross=realized,
        fees=D(0), position_cost=D(0), pending_orders=D(0),
    )
    account = account_budget(cash=1000, deployments=[own], order_claims=D(0), fee_claims=D(0))

    result = budget_entry_decision(
        account, strategy_instance_id="a", quantity=1, price="100.001", at_ms=NOON, observed_at_ms=NOON,
    )

    assert result.required == D("100.011")
    assert (result.required <= own.free) is exact_comparison_admits
    assert result.allowed is allowed
    assert not result.allowed or exact_comparison_admits, "the cent comparison never admits more than the exact one"
    if not allowed:
        assert result.detail == (
            "This bot has only $100.01 free. The entry needs $100.02 including its fee provision. Shortfall: $0.01."
        )
