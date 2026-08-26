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
    RedriveThenEscalate,
    raise_uncertainty,
    reason_age_policy,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


async def redrive_or_escalate_stale_exits(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> None:
    """Age-gate active EXIT_NOT_FLAT episodes: bounded re-drive, then escalate."""
    # The single declared age policy (ADR 0048 Decision 1) — replaces the
    # former EXIT_NOT_FLAT_REDRIVE_AFTER_MS / EXIT_NOT_FLAT_MAX_REDRIVES
    # module constants; the watchdog keeps its execution logic and loses
    # its policy.
    redrive_policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE)
    assert isinstance(redrive_policy, RedriveThenEscalate)
    now_ms = repo.clock()
    for instance in repo.strategy_instances():
        sid = instance["strategy_instance_id"]
        episode = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=EXIT_NOT_FLAT_REASON_CODE,
            strategy_instance_id=sid,
        )
        if episode is None or now_ms - episode["observed_at_ms"] < redrive_policy.after_ms:
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
        # Episode-scoped redrive count. `_exit_identity` keys idempotency on
        # (strategy_instance_id, decision_id) only, and a completed-but-non-flat
        # redrive REFRESHES this EXIT_NOT_FLAT episode (exit_resolution re-raises
        # with the new reducing order_ref), which overwrites observed_at_ms. A
        # time-anchored count would therefore reset to zero every cycle and the
        # watchdog would loop at attempt 1 forever, never escalating. Count by
        # the stable per-episode redrive namespace instead, minted from the
        # immutable uncertainty id (colon-bearing "uncertainty:<seq>" hashed to
        # a colon-free hex token) so successive episodes never collide.
        episode_token = hashlib.sha256(
            episode["uncertainty_id"].encode("utf-8")
        ).hexdigest()[:12]
        redrives = 0
        while (
            repo.get_command(f"cmd:{sid}:exit-redrive-{episode_token}-{redrives + 1}")
            is not None
        ):
            redrives += 1
        if redrives >= redrive_policy.max_count:
            async with intake:
                escalated = raise_uncertainty(
                    repo,
                    strategy_instance_id=sid,
                    reason_code=redrive_policy.escalate_to,
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
