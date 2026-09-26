"""Cancel a Clerk-priced extended-hours exit at the regular open (#2504).

The existing EXIT owns cancellation and exact terminal proof. Its ordinary
terminal fold closes that effect; the watchdog accepts a fresh episode-derived
EXIT for the remaining attribution. An uncertain cancellation retains custody.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.recovery_reduction import market_leg_sendable
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
    OrderCancelRequestedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_projection import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

OPEN_REPLACEMENT = "EXIT_OPEN_REPLACEMENT"


def replacement_due(
    *, order: OrderResource, accepted: ExitAcceptedFacts,
    created: ExitReducingOrderCreatedFacts, now_ms: int,
) -> bool:
    """A Clerk-priced DAY limit becomes replaceable at the actual regular open."""
    return (
        created.extended_hours and created.time_in_force == "day"
        and (created.priced_by or accepted.reducing_priced_by) == "clerk" and market_leg_sendable(now_ms)
        and (order.broker_state or "").lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
        and order.broker_order_id is not None
    )


def replacement_ready(repo: ClerkSqliteRepository, evidence_refs: tuple[str, ...]) -> bool:
    """A proven canceled original can be replaced immediately, without the retry age delay."""
    for order_ref in evidence_refs:
        row = repo.last_order_transition(order_ref=order_ref, transition_kind="ORDER_CANCEL_REQUESTED")
        order = repo.order(order_ref)
        if (
            row is not None and (OrderCancelRequestedFacts.from_facts_json(row["facts_json"]).reason_code == OPEN_REPLACEMENT
                                 or row["summary_code"] == OPEN_REPLACEMENT)
            and order is not None
            and (order.broker_state or "").lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
            and not repo.order_fills_short_of_broker_cumulative(order_ref)
        ):
            return True
    return False


def has_ready_replacement(repo: ClerkSqliteRepository) -> bool:
    """A completed open cancellation needs a new account snapshot before sizing."""
    if not market_leg_sendable(repo.clock()):
        return False
    return any(
        replacement_ready(repo, UncertaintyRaisedFacts.from_facts_json(row["facts_json"]).evidence_refs)
        for row in repo.active_uncertainties()
        if row["reason_code"] == "EXIT_NOT_FLAT"
        and repo.active_exit_for_strategy(row["strategy_instance_id"]) is None
        and repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code="EXIT_STUCK", strategy_instance_id=row["strategy_instance_id"]) is None
    )
