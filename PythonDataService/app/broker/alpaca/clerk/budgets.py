"""One derived deployment-budget and account-claims calculation (#2545).

Formula: balance = commitment + canonical FIFO gross realized P&L - fees;
free = balance - signed open FIFO basis - option collateral - unfilled orders (including provisions);
available = observed cash - sum(active max(free, 0)) - all order-overlap
claims - fees not already reflected in cash or an order-overlap claim
- option collateral (ADR 0076 decision 28).
A stopped deployment's release is the Stop's recorded fact (#2555), read back
as ``DeploymentBudget.release``; it is never re-derived from later money.
Reference: https://github.com/tim1016/learn-ai/issues/2540, Money contract.
Canonical implementation: this file; no mutable balance and no second FIFO.
Validated against: tests/broker/alpaca/clerk/test_budgets.py (conservation fixtures).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext

from app.broker.alpaca.clerk.money import (
    ZERO,
    cash_admits,
    cents_required,
    cents_spendable,
    money_context,
    normalize_money,
    notional,
)
from app.broker.alpaca.regulatory_fees import fees_for_fill, settle_session
from app.broker.contract.models import OrderSide


class BudgetUnavailable(ValueError):
    """A named unknown prevents authorizing money; it is never a zero.

    ``reason_code`` names the unknown for a caller that refuses with a code;
    ``None`` leaves the caller's own code in force.
    """

    reason_code: str | None = None


@dataclass(frozen=True)
class ReleaseAtStop:
    """What a deployment's Stop released, in display cents, as the Stop recorded it (#2555).

    ``held_cents`` is what stayed claimed at that instant -- its shares at cost
    and its entry orders. Money that comes back after the Stop is never added
    to ``released_cents``.
    """

    released_cents: int
    held_cents: int


@dataclass(frozen=True)
class DeploymentBudget:
    strategy_instance_id: str
    committed_cents: int
    active: bool
    realized_gross: Decimal
    fees: Decimal
    position_cost: Decimal
    pending_orders: Decimal
    balance: Decimal
    free: Decimal
    cash_claim: Decimal
    # The Stop's recorded release; ``None`` while running, and for a Stop
    # that recorded none (every Stop before #2555, or one that could not be
    # valued at that instant).
    release: ReleaseAtStop | None = None
    option_collateral: Decimal = ZERO

    @property
    def spendable_cents(self) -> int:
        return max(0, cents_spendable(self.free)) if self.active else 0


@dataclass(frozen=True)
class AccountBudget:
    cash: Decimal
    active_free_claims: Decimal
    order_claims: Decimal
    fee_claims: Decimal
    available: Decimal
    deployments: tuple[DeploymentBudget, ...]
    option_collateral: Decimal = ZERO

    @property
    def unreserved_cents(self) -> int:
        return max(0, cents_spendable(self.available))


def deployment_budget(
    *, strategy_instance_id: str, committed_cents: int, active: bool,
    realized_gross: object, fees: Decimal, position_cost: Decimal, pending_orders: Decimal,
    release: ReleaseAtStop | None = None,
    option_collateral: Decimal = ZERO,
) -> DeploymentBudget:
    """Project the immutable commitment over canonical custody facts."""
    with money_context():
        gross = normalize_money(realized_gross)
        balance = Decimal(committed_cents) / 100 + gross - fees
        free = balance - position_cost - pending_orders
        return DeploymentBudget(
            strategy_instance_id=strategy_instance_id, committed_cents=committed_cents,
            active=active, realized_gross=gross, fees=fees,
            position_cost=position_cost, pending_orders=pending_orders,
            balance=balance, free=free, cash_claim=max(ZERO, free) if active else ZERO,
            release=release,
            option_collateral=option_collateral,
        )


def account_budget(
    *, cash: object, deployments: Sequence[DeploymentBudget],
    order_claims: Decimal, fee_claims: Decimal,
) -> AccountBudget:
    """Subtract disjoint claims; a historical deficit never becomes a claim."""
    with money_context():
        observed = normalize_money(cash)
        active_free = sum((budget.cash_claim for budget in deployments), ZERO)
        collateral = sum((budget.option_collateral for budget in deployments), ZERO)
        return AccountBudget(
            cash=observed, active_free_claims=active_free,
            order_claims=order_claims, fee_claims=fee_claims,
            available=observed - active_free - order_claims - fee_claims - collateral,
            deployments=tuple(deployments),
            option_collateral=collateral,
        )


def entry_requirement(*, quantity: object, price: object, at_ms: int) -> tuple[Decimal, int]:
    """One position's exact notional plus canonical, disclosed fee provision.

    Formula: qty*price + settled canonical model for this prospective BUY.
    Reference: PRD #2540 admission; regulatory_fees.py dated component rules.
    Canonical implementation: this function composes those two authorities.
    Validated against: test_budgets.py; regulatory-fee independent fixtures.
    """
    with money_context():
        cost = notional(quantity, price)
        prediction = fees_for_fill(
            trade_date_ms=at_ms, side=OrderSide.BUY,
            quantity=normalize_money(quantity), fill_price=normalize_money(price),
        )
        # Canonical component settlement intentionally rounds at its cents
        # boundary. No other money operation relaxes the exact context.
        with localcontext() as settlement:
            settlement.traps[Inexact] = False
            fee = settle_session([prediction]).total
        return cost + fee, cents_required(fee)


@dataclass(frozen=True)
class BudgetEntryDecision:
    allowed: bool
    required: Decimal
    fee_cents: int
    detail: str


@money_context()
def budget_entry_decision(account: AccountBudget, *, strategy_instance_id: str, quantity: object, price: object, at_ms: int) -> BudgetEntryDecision:
    """The same exact next-position cash rule for commitment and its read view.

    Required dollars include the canonical BUY fee provision. Other active
    deployments retain their positive free claims; this deployment spends its
    own claim once. Cash projections already own order/fill/fee overlap.
    """
    required, fee_cents = entry_requirement(quantity=quantity, price=price, at_ms=at_ms)
    return _requirement_decision(account, strategy_instance_id=strategy_instance_id, required=required, fee_cents=fee_cents)


@money_context()
def option_entry_decision(
    account: AccountBudget, *, strategy_instance_id: str, commitment_cents: int, fee_provision_cents: int,
) -> BudgetEntryDecision:
    """The same next-position rule for an entry in option contracts (ADR 0076).

    What it needs is its entry commitment, in whole cents: the broker's
    requirement, plus the premium at the limit price, plus the option fee
    provision. That figure is ``app/options/requirement.py``'s; nothing is
    priced here.

    Formula: admitted when commitment <= this deployment's free budget and
      commitment + every other claim <= observed cash.
    Reference: ADR 0076 decision 2; PRD #2540 admission.
    Canonical implementation: the rule is this file's (``_requirement_decision``).
    Validated against: tests/broker/alpaca/clerk/sqlite/test_option_entry_and_exit.py.
    """
    for name, cents in (("an entry commitment", commitment_cents), ("a fee provision", fee_provision_cents)):
        if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
            raise BudgetUnavailable(f"{name} is a whole number of cents, zero or more.")
    return _requirement_decision(
        account, strategy_instance_id=strategy_instance_id,
        required=Decimal(commitment_cents) / 100, fee_cents=fee_provision_cents,
    )


def _requirement_decision(
    account: AccountBudget, *, strategy_instance_id: str, required: Decimal, fee_cents: int,
) -> BudgetEntryDecision:
    """Whether this deployment's budget and the account's cash cover ``required`` dollars. The caller holds the money context."""
    required_display = Decimal(cents_required(required)) / 100
    own = next((item for item in account.deployments if item.strategy_instance_id == strategy_instance_id), None)
    if own is None or not own.active:
        return BudgetEntryDecision(False, required, fee_cents, "This deployment is stopped. Review a fresh Deploy before creating new exposure.")
    if required > own.free:
        return BudgetEntryDecision(False, required, fee_cents, f"The next position needs {required_display:.2f} USD including estimated fees; this deployment has {Decimal(cents_spendable(own.free)) / 100:.2f} USD free.")
    with money_context():
        claims = account.active_free_claims - own.cash_claim + account.order_claims + account.fee_claims
        if not cash_admits(cash=account.cash, claims=claims, required=required):
            return BudgetEntryDecision(False, required, fee_cents, f"The next position needs {required_display:.2f} USD including estimated fees; account cash after other claims is {Decimal(cents_spendable(account.cash - claims)) / 100:.2f} USD.")
    return BudgetEntryDecision(True, required, fee_cents, f"Current budget and cash cover the next position's estimated {required_display:.2f} USD including fees.")
