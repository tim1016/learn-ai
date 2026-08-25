"""Execute one prepared SafeFlattenPlan as reduction-only recovery EXITs (F18).

``recovery_policy`` builds and version-token-gates the plan
(`_build_safe_flatten_plan`); this module owns only the execute side: for each
leg, capture a run-fence-exempt recovery EXIT (``accept_recovery_exit``)
against the newest owned entry order for the leg's symbol, then drive it
through the standard EXIT machine (``resolve_accepted_exit`` -> ``resolve_exit``
-> per-op claim CAS -> ``ClaimedBrokerIO``). The reducing quantity is derived
downstream from durable attributed custody (``repo.position``), never from the
plan leg -- attributed-quantity-exact by construction; the leg is presentation
and gating evidence. Idempotent: the decision id is derived from the plan's
version token, so a retried execute re-drives the same durable EXIT instead of
minting a second reduction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.exit import (
    RecoveryRunActiveError,
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


class SafeFlattenExecutionError(Exception):
    """The prepared plan cannot be executed against current custody."""


# An EXIT effect the reducing broker order failed to reach (broker rejection,
# folded via ``fold_failed``). The order row still exists, so reference
# presence alone must never be read as a successful reduction — the terminal
# state is the truth (Codex review 2026-08-25 P1).
_FAILED_EFFECT_STATES = frozenset({"failed", "rejected"})


@dataclass(frozen=True)
class SafeFlattenResult:
    """The durable outcome of executing one prepared plan.

    ``orders`` are reducing orders that reached the broker. Every captured
    recovery EXIT — whether its order is already working or the transient sweep
    will re-drive it — appears in ``accepted_effect_operation_ids``; a leg whose
    reduction was *rejected* never reaches here (the executor raises). So an
    empty ``orders`` with a non-empty accepted list is a durably-committed,
    sweep-pending reduction, not a failed one.
    """

    orders: tuple[OrderResource, ...]
    accepted_effect_operation_ids: tuple[str, ...]
    recorded_at_ms: int


async def execute_safe_flatten_plan(
    repo: ClerkSqliteRepository,
    *,
    plan: SafeFlattenPlan,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    account_id: str,
) -> SafeFlattenResult:
    if plan.account_id != account_id:
        raise SafeFlattenExecutionError(
            "The prepared plan belongs to a different account authority."
        )
    if not plan.legs:
        raise SafeFlattenExecutionError("The prepared plan has no reduction legs.")
    # Preflight before any broker contact so an unsupported leg never leaves a
    # partially applied mutation (Codex review 2026-08-25 P1). Manual-custody
    # legs (NULL strategy) are prepare-only; the recovery-policy gate already
    # refuses to present them, this is the executor-side backstop.
    for leg in plan.legs:
        if not leg.strategy_instance_id:
            raise SafeFlattenExecutionError(
                "A manual-custody leg cannot be flattened through strategy recovery EXITs."
            )
    decision_token = hashlib.sha256(plan.version_token.encode("utf-8")).hexdigest()[:16]
    submitted: list[OrderResource] = []
    accepted_effect_operation_ids: list[str] = []
    for leg in plan.legs:
        # Re-check expiry before each leg so a long, multi-leg run cannot submit
        # a later reduction on reconciliation evidence that has gone stale.
        if repo.clock() > plan.expires_at_ms:
            raise SafeFlattenExecutionError(
                "The prepared reduction plan expired; prepare a fresh plan."
            )
        async with intake:
            entries = [
                order
                for order in repo.entry_orders_for_strategy(leg.strategy_instance_id)
                if entry_order_symbol(repo, order.order_ref).upper() == leg.symbol.upper()
                and repo.active_exit_for_order(order.order_ref) is None
            ]
            if not entries:
                raise SafeFlattenExecutionError(
                    f"No owned entry order proves a reduction target for {leg.symbol!r}."
                )
            try:
                accepted = accept_recovery_exit(
                    repo,
                    account_id=account_id,
                    strategy_instance_id=leg.strategy_instance_id,
                    decision_id=f"recovery-flatten-{decision_token}",
                    entry_order_ref=entries[-1].order_ref,
                    # Re-asserted inside the capture transaction: recovery
                    # policy refused presentation with RUN_STILL_ACTIVE, but a
                    # Resume can land between recheck and capture (approved-
                    # carryover resumes are legitimate with exposure held).
                    forbid_active_run=True,
                )
            except RecoveryRunActiveError as exc:
                raise SafeFlattenExecutionError(
                    f"A run re-activated for {leg.strategy_instance_id!r} after "
                    "the flatten was presented; stop the bot and prepare a fresh plan."
                ) from exc
        resolved = await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
        assert accepted.effect_operation_id is not None
        effect = repo.effect_operation(accepted.effect_operation_id)
        if effect is not None and effect.state in _FAILED_EFFECT_STATES:
            # The broker rejected this reduction (the order row exists but its
            # effect folded to failed). Never report success while exposure
            # remains — surface an honest failure.
            raise SafeFlattenExecutionError(
                f"The broker rejected the {leg.symbol} reduction; attributed exposure "
                "remains. Reconcile the account and retry the flatten."
            )
        accepted_effect_operation_ids.append(accepted.effect_operation_id)
        if resolved.reducing_order_ref is not None:
            reducing = repo.order(resolved.reducing_order_ref)
            if reducing is not None:
                submitted.append(reducing)
        logger.info(
            "safe-flatten leg driven through recovery EXIT custody",
            extra={
                "action": "safe_flatten_leg_executed",
                "account_id": account_id,
                "strategy_instance_id": leg.strategy_instance_id,
                "symbol": leg.symbol,
                "effect_operation_id": accepted.effect_operation_id,
                "reducing_order_ref": resolved.reducing_order_ref,
            },
        )
    recorded_at_ms = (
        max(order.updated_at_ms for order in submitted) if submitted else repo.clock()
    )
    return SafeFlattenResult(
        orders=tuple(submitted),
        accepted_effect_operation_ids=tuple(accepted_effect_operation_ids),
        recorded_at_ms=recorded_at_ms,
    )
