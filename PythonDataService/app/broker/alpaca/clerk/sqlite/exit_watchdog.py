"""Stuck-EXIT watchdog: age-gate, bounded re-drive, then durable escalation.

A terminal ``EXIT_NOT_FLAT`` folds its effect to ``failed``, which
``reconcilable_effect_operations`` never re-selects, and
``_resolve_flat_exit_fences`` clears the episode only if exposure happens to
reach flat — without this step a stuck EXIT is re-driven never, forever
(research directions 2026-08-24, Direction 1 RQ2). This runs as one step of
the account reconciliation pass (``reconcile._reconcile_account_serialized``).
"""

from __future__ import annotations

import hashlib
import logging

from app.broker.alpaca.clerk.sqlite.exit import (
    accept_recovery_exit,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyRaisedFacts
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    AdmissionBlockedError,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)

# Stuck-EXIT watchdog policy (Direction 1 RQ2): after ~8 sweep cycles a
# terminal EXIT_NOT_FLAT is re-driven through a fresh recovery EXIT, at most
# EXIT_NOT_FLAT_MAX_REDRIVES times, then escalated durably as EXIT_STUCK.
EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000
EXIT_NOT_FLAT_MAX_REDRIVES = 3


async def redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> None:
    """Age-gate active EXIT_NOT_FLAT episodes: bounded re-drive, then escalate."""
    now_ms = repo.clock()
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=sid,
        )
        if episode is None or now_ms - episode["observed_at_ms"] < EXIT_NOT_FLAT_REDRIVE_AFTER_MS:
            continue
        try:
            facts = UncertaintyRaisedFacts.from_facts_json(episode["facts_json"])
            cause = ExitNotFlatCause.from_mapping(facts.cause_facts)
        except (TypeError, ValueError, KeyError):
            logger.error(
                "stale EXIT_NOT_FLAT episode carries unreadable cause facts",
                extra={
                    "action": "exit_watchdog_unreadable_cause",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                    "uncertainty_id": episode["uncertainty_id"],
                },
            )
            continue
        remaining = repo.position(sid, cause.symbol)
        if not position_quantity_is_nonzero(remaining):
            continue  # the flat fence resolver clears this episode in this pass
        redrives = repo.exit_effects_created_since(sid, episode["observed_at_ms"])
        if redrives >= EXIT_NOT_FLAT_MAX_REDRIVES:
            async with intake:
                escalated = raise_uncertainty(
                    repo,
                    strategy_instance_id=sid,
                    reason_code=EXIT_STUCK_REASON_CODE,
                    headline="A stuck EXIT exhausted automatic re-drives",
                    explanation=(
                        f"{remaining:g} {cause.symbol} remains attributed after "
                        f"{redrives} automatic EXIT re-drives."
                    ),
                    operator_impact=(
                        "New exposure stays paused for this strategy and automatic "
                        "re-drives stopped. Exact operator reduction remains available."
                    ),
                    next_step="Run Reconcile now, then execute the presented safe flatten.",
                    evidence_refs=(episode["uncertainty_id"],),
                    cause_facts=ExitStuckCause(
                        symbol=cause.symbol,
                        attributed_qty=remaining,
                        redrive_count=redrives,
                        first_observed_at_ms=episode["observed_at_ms"],
                    ).to_mapping(),
                    severity="error",
                )
            if escalated:
                logger.error(
                    "stuck EXIT escalated to a durable operator-visible EXIT_STUCK episode",
                    extra={
                        "action": "exit_stuck_escalated",
                        "account_id": repo.account_id,
                        "strategy_instance_id": sid,
                        "symbol": cause.symbol,
                        "redrive_count": redrives,
                        "age_ms": now_ms - episode["observed_at_ms"],
                    },
                )
            continue
        entries = [
            order
            for order in repo.entry_orders_for_strategy(sid)
            if entry_order_symbol(repo, order.order_ref).upper() == cause.symbol
            and repo.active_exit_for_order(order.order_ref) is None
        ]
        if not entries:
            continue
        # Episode-scoped redrive identity. `_exit_identity` keys idempotency on
        # (strategy_instance_id, decision_id) only, so a bare `exit-redrive-<n>`
        # would collide with an earlier, independent episode's redrives — either
        # replaying its terminal effect (same entry) or conflicting durably
        # (new entry). Uncertainty ids are minted as "uncertainty:<seq>"
        # (colon-bearing), so hash to a colon-free hex token.
        episode_token = hashlib.sha256(
            episode["uncertainty_id"].encode("utf-8")
        ).hexdigest()[:12]
        try:
            async with intake:
                accepted = accept_recovery_exit(
                    repo,
                    account_id=repo.account_id,
                    strategy_instance_id=sid,
                    decision_id=f"exit-redrive-{episode_token}-{redrives + 1}",
                    entry_order_ref=entries[-1].order_ref,
                )
            await resolve_accepted_exit(repo, accepted=accepted, trade=trade)
        except (OperationClaimError, AdmissionBlockedError, DurableConflictError):
            logger.info(
                "deferred a contended or policy-blocked stuck-EXIT re-drive",
                extra={
                    "action": "exit_redrive_deferred",
                    "account_id": repo.account_id,
                    "strategy_instance_id": sid,
                },
            )
            continue
        logger.warning(
            "re-drove a stale EXIT_NOT_FLAT episode with a fresh recovery EXIT",
            extra={
                "action": "exit_redrive_submitted",
                "account_id": repo.account_id,
                "strategy_instance_id": sid,
                "symbol": cause.symbol,
                "attempt": redrives + 1,
            },
        )
