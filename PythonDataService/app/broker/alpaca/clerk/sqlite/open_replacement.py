"""Cancel a Clerk-priced extended-hours exit at the regular open (#2504).

The existing EXIT owns cancellation and exact terminal proof. Its ordinary
terminal fold closes that effect; the watchdog accepts a fresh episode-derived
EXIT for the remaining attribution. An uncertain cancellation retains custody.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.recovery_reduction import market_leg_sendable
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.facts import (
    ExitAcceptedFacts,
    ExitReducingOrderCreatedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import OrderResource, TransitionInput
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_evidence,
    fold_uncertain,
    trade_port_folds_simulated_evidence,
)
from app.broker.alpaca.clerk.sqlite.order_projection import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerError

OPEN_REPLACEMENT = "EXIT_OPEN_REPLACEMENT"


async def cancel_at_regular_open(
    repo: ClerkSqliteRepository, *, broker: ClaimedBrokerIO, order: OrderResource,
    accepted: ExitAcceptedFacts, created: ExitReducingOrderCreatedFacts, run: OffLoop,
) -> None:
    """Request cancellation, then fold only the broker's exact identity evidence."""
    if (
        not created.extended_hours
        or created.time_in_force != "day"
        or (accepted.reducing_confirmed_quantity is not None and accepted.reducing_priced_by is None)
        or not market_leg_sendable(repo.clock())
        or (order.broker_state or "").lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
        or order.broker_order_id is None
    ):
        return

    def record_request() -> None:
        if repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_CANCEL_REQUESTED"):
            return
        effect = repo.effect_operation(broker.effect_operation_id)
        assert effect is not None
        repo.append_transition(TransitionInput(
            strategy_instance_id=effect.strategy_instance_id, run_id=effect.run_id,
            command_id=effect.command_id, effect_operation_id=effect.effect_operation_id,
            order_ref=order.order_ref, broker_order_id=order.broker_order_id,
            broker_state=order.broker_state, transition_kind="ORDER_CANCEL_REQUESTED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state, clerk_observed_at_ms=repo.clock(),
            summary_code=OPEN_REPLACEMENT, facts_json="{}",
        ))

    await run(record_request)
    try:
        await broker.cancel(order.broker_order_id, order_ref=order.order_ref)
    except BrokerError as exc:
        why = str(exc)
        await run(lambda: fold_uncertain(
            repo, effect_operation_id=broker.effect_operation_id, order_ref=order.order_ref,
            why=why, transition_kind="ORDER_CANCEL_UNCERTAIN",
        ))
        return
    observed = await broker.observe_exact(order.client_order_id)
    if observed is None or isinstance(observed, BrokerError):
        await run(lambda: fold_uncertain(
            repo, effect_operation_id=broker.effect_operation_id, order_ref=order.order_ref,
            why="Regular-open replacement is waiting for exact cancellation evidence.",
            transition_kind="ORDER_CANCEL_UNCERTAIN",
        ))
        return
    await run(lambda: fold_order_evidence(
        repo, effect_operation_id=broker.effect_operation_id, order=observed,
        simulated_authority=trade_port_folds_simulated_evidence(broker.trade),
    ))


def replacement_ready(repo: ClerkSqliteRepository, evidence_refs: tuple[str, ...]) -> bool:
    """A proven canceled original can be replaced immediately, without the retry age delay."""
    for order_ref in evidence_refs:
        row = repo.last_order_transition(order_ref=order_ref, transition_kind="ORDER_CANCEL_REQUESTED")
        order = repo.order(order_ref)
        if (
            row is not None and row["summary_code"] == OPEN_REPLACEMENT
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
        if row["reason_code"] == "EXIT_NOT_FLAT" and repo.active_exit_for_strategy(row["strategy_instance_id"]) is None
    )
