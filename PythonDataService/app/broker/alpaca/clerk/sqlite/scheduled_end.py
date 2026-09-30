"""Carry out a bot's owner-set end on the account reconciliation pass (#2607).

Each deployed bot may carry a one-time end the owner chose (``app.schemas.bot_end``):
an instant, and whether its shares are sold or kept then. The Clerk carries
out every end on its authority, whether the bot is still running or its run
already died (grill 2026-09-29, decision 9). On the pass at or after the end:

1. **Fence** (:func:`fence_bots_at_their_end`, the first step of the pass,
   before any broker read): commit Stop's own STOP for the bot's ACTIVE run
   -- the same ``submit_stop_run`` the panel's Stop commits, so the Clerk
   refuses the run's decisions from here and its budget is released (#2540)
   -- and ask the runner to stop the bot's process, as the panel's Stop does
   after its own STOP commits.
2. **Cancel** the run's working entries: the pass's #2362 step, unchanged,
   cancels every ENTER whose run is no longer ACTIVE.
3. **Sell** (:func:`finish_bots_at_their_end`, after the Dry Run close): when
   the owner chose SELL, close every holding with one recovery EXIT under
   ``SCHEDULED_END_DECISION_PREFIX``, through ``ended_run_close`` exactly as a
   Dry Run's run-end close does. The EXIT machine sends it only as a market
   order inside the regular session; outside it, it holds and sells at the
   next open, never after hours (``exit_resolution``). A Dry Run's shares are
   not sold here: #2641's run-end close sells them at the last price the run
   saw, on this ending as on every other.
4. **Record** the end carried out, once the run is stopped, no entry can
   still fill, and every holding a SELL owes has its sale accepted.

This is the owner-scheduled exception to #2504's Q3 ("a dead run never sells
by itself"): only the sale the owner scheduled is made for a run that died.

The worklist is the runner's: the bots' ends live in their desired state,
which only the runner reads and writes. The runner and the Clerk share one
process (``run_ownership``), so the runner installs its end schedule here
once (:func:`install_bot_end_schedule`); a process with no runner installs
none and ends nothing. Every step is idempotent and derived from durable
facts -- the STOP is keyed on its run, each sale on the holding's newest
entry -- so a pass that dies half-way is completed by the next one, and an
end found late (the Clerk was down) is carried out when the Clerk is back.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from app.broker.alpaca.clerk.account_authority import is_synthetic_account_id
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.ended_run_close import (
    CloseDeferred,
    OwedClose,
    bot_holdings,
    drive_close,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import SCHEDULED_END_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.reads import (
    CANCELLABLE_ENTRY_BROKER_STATES,
    NONTERMINAL_EFFECT_STATES,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.contract.ports import BrokerTradePort
from app.schemas.bot_end import BotEnd

logger = logging.getLogger(__name__)

#: The ``operator_reason`` of the STOP the Clerk commits at a bot's end.
SCHEDULED_END_REASON = "scheduled_end"


@dataclass(frozen=True)
class ScheduledEnd:
    """One bot's end, as the runner's schedule answers it."""

    strategy_instance_id: str
    end: BotEnd


class BotEndSchedule(Protocol):
    """The runner's side of the bots' ends, as the Clerk reads and reports them."""

    def pending_ends(self, strategy_instance_ids: Sequence[str]) -> list[ScheduledEnd]:
        """Each named bot's end not yet carried out, due or not."""
        ...

    def stop_bot_at_its_end(self, strategy_instance_id: str, lifecycle_run_id: str) -> None:
        """The Clerk committed the run's STOP at its end: stop its process. Must not block."""
        ...

    def record_end_carried_out(self, end: ScheduledEnd, *, at_ms: int) -> None:
        """The Clerk carried ``end`` out."""
        ...


_INSTALLED: BotEndSchedule | None = None


def install_bot_end_schedule(schedule: BotEndSchedule | None) -> None:
    """Install (or clear) the process's end schedule: the runner's, at startup."""
    global _INSTALLED
    _INSTALLED = schedule


def installed_bot_end_schedule() -> BotEndSchedule | None:
    return _INSTALLED


def _due_ends(repo: ClerkSqliteRepository, schedule: BotEndSchedule) -> list[ScheduledEnd]:
    """The ends due on this authority: every live registration's, at or before the Clerk's clock."""
    now_ms = repo.clock()
    registered = [
        instance["strategy_instance_id"]
        for instance in repo.strategy_instances()
        if instance["retired_at_ms"] is None
    ]
    return [end for end in schedule.pending_ends(registered) if end.end.end_at_ms <= now_ms]


def _stop_runs_at_their_end(repo: ClerkSqliteRepository, due: list[ScheduledEnd]) -> list[tuple[str, str]]:
    """Commit Stop's STOP for each due bot's ACTIVE run; under intake, no broker I/O.

    One bot's failure is logged and the others are still stopped; a lost
    execution lease fails the pass, as it fails every other write.
    """
    fenced: list[tuple[str, str]] = []
    for end in due:
        try:
            active = repo.active_run(end.strategy_instance_id)
            if active is None:
                continue
            submit_stop_run(
                repo,
                account_id=repo.account_id,
                strategy_instance_id=end.strategy_instance_id,
                lifecycle_run_id=active.lifecycle_run_id,
                operator_reason=SCHEDULED_END_REASON,
                clock=repo.clock,
            )
        except ExecutionLeaseLost:
            raise
        except Exception:
            logger.exception(
                "a bot's run could not be stopped at its end; the next pass tries again",
                extra={
                    "action": "scheduled_end_stop_failed",
                    "account_id": repo.account_id,
                    "strategy_instance_id": end.strategy_instance_id,
                },
            )
            continue
        fenced.append((end.strategy_instance_id, active.lifecycle_run_id))
    return fenced


async def fence_bots_at_their_end(
    repo: ClerkSqliteRepository,
    *,
    intake: ReentrantAsyncLock,
    off_loop: OffLoop | None = None,
) -> list[ScheduledEnd]:
    """Step 1: stop the run of every bot whose end has come; return the due ends.

    Runs before the pass reads the broker, so a bot is stopped at its end even
    while Alpaca cannot be read; the sale waits for a pass that can. An end
    schedule that cannot be read is logged and ends nothing this pass: it
    never fails the account's reconciliation.
    """
    schedule = installed_bot_end_schedule()
    if schedule is None:
        return []
    run = off_loop if off_loop is not None else run_inline
    try:
        due = await run(partial(_due_ends, repo, schedule))
    except Exception:
        logger.exception(
            "the bots' ends could not be read; none is carried out this pass",
            extra={"action": "scheduled_end_schedule_unreadable", "account_id": repo.account_id},
        )
        return []
    if not due:
        return []
    for strategy_instance_id, lifecycle_run_id in await intake.off_loop(_stop_runs_at_their_end, repo, due):
        logger.warning(
            "stopped a bot's run at the end its owner set",
            extra={
                "action": "scheduled_end_run_stopped",
                "account_id": repo.account_id,
                "strategy_instance_id": strategy_instance_id,
                "lifecycle_run_id": lifecycle_run_id,
            },
        )
        schedule.stop_bot_at_its_end(strategy_instance_id, lifecycle_run_id)
    return due


async def finish_bots_at_their_end(
    repo: ClerkSqliteRepository,
    due: list[ScheduledEnd],
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
) -> None:
    """Steps 3 and 4: sell what a SELL end owes, then record each end carried out.

    One bot at a time: an unexpected failure is logged with its stack and the
    pass goes on, so one unreadable bot never fails every later pass -- and
    with them the operator's Reconcile now. Its end stays pending, and the
    next pass carries it out.
    """
    schedule = installed_bot_end_schedule()
    if schedule is None or not due:
        return
    run = off_loop if off_loop is not None else run_inline
    # A Dry Run's shares are its run-end close's to sell (#2641).
    sells = not is_synthetic_account_id(repo.account_id)
    for end in due:
        try:
            if sells and end.end.end_action == "SELL":
                for close in await run(partial(_sales_owed, repo, end)):
                    await _sell_one(repo, close, trade=trade, intake=intake, pricing=pricing, run=run)
            if await run(partial(_carried_out, repo, end, sells=sells)):
                await run(partial(schedule.record_end_carried_out, end, at_ms=repo.clock()))
                logger.warning(
                    "carried out a bot's scheduled end",
                    extra={
                        "action": "scheduled_end_carried_out",
                        "account_id": repo.account_id,
                        "strategy_instance_id": end.strategy_instance_id,
                        "end_at_ms": end.end.end_at_ms,
                        "end_action": end.end.end_action,
                    },
                )
        except Exception:
            logger.exception(
                "a bot's scheduled end could not be carried out; it stays pending and the pass goes on",
                extra={
                    "action": "scheduled_end_failed",
                    "account_id": repo.account_id,
                    "strategy_instance_id": end.strategy_instance_id,
                },
            )


def _sales_owed(repo: ClerkSqliteRepository, end: ScheduledEnd) -> list[OwedClose]:
    """What a SELL end still has to sell; nothing while the bot's run is ACTIVE."""
    if repo.active_run(end.strategy_instance_id) is not None:
        return []
    return [
        holding.close
        for holding in bot_holdings(repo, end.strategy_instance_id, decision_prefix=SCHEDULED_END_DECISION_PREFIX)
        if holding.state == "owed"
    ]


async def _sell_one(
    repo: ClerkSqliteRepository,
    close: OwedClose,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> None:
    extra = {
        "account_id": repo.account_id,
        "strategy_instance_id": close.strategy_instance_id,
        "symbol": close.symbol,
        "decision_id": close.decision_id,
    }
    resolved = await drive_close(repo, close, trade=trade, intake=intake, pricing=pricing, run=run)
    if isinstance(resolved, CloseDeferred):
        logger.info(
            "deferred the sale at a bot's scheduled end; the next pass tries again",
            extra={"action": "scheduled_end_sale_deferred", "deferred": resolved.value, **extra},
        )
        return
    logger.warning(
        "put in the sale at a bot's scheduled end",
        extra={
            "action": "scheduled_end_sale",
            "effect_operation_id": resolved.effect_operation_id,
            "reducing_order_ref": resolved.reducing_order_ref,
            **extra,
        },
    )


def _carried_out(repo: ClerkSqliteRepository, end: ScheduledEnd, *, sells: bool) -> bool:
    """Whether the Clerk has done everything this end asks.

    The run is stopped; no entry of the bot can still fill outside an EXIT's
    custody; and, for a SELL end, every holding's sale is accepted -- from
    then on it is that EXIT's, held for the open if it must be. A holding
    another EXIT still owns keeps the end pending until that EXIT resolves.
    """
    sid = end.strategy_instance_id
    if repo.active_run(sid) is not None:
        return False
    if any(_entry_may_still_fill(repo, order) for order in repo.entry_orders_for_strategy(sid)):
        return False
    if not sells or end.end.end_action == "KEEP":
        return True
    return all(
        holding.state == "closing"
        for holding in bot_holdings(repo, sid, decision_prefix=SCHEDULED_END_DECISION_PREFIX)
    )


def _entry_may_still_fill(repo: ClerkSqliteRepository, order: OrderResource) -> bool:
    """An entry outside any EXIT's custody that the broker may still fill, or whose POST is in flight."""
    if repo.active_exit_for_order(order.order_ref) is not None:
        return False
    if order.broker_state is None:
        effect = repo.effect_operation(order.effect_operation_id)
        return effect is not None and effect.state in NONTERMINAL_EFFECT_STATES
    return order.broker_state.lower() in CANCELLABLE_ENTRY_BROKER_STATES


__all__ = [
    "SCHEDULED_END_REASON",
    "BotEndSchedule",
    "ScheduledEnd",
    "fence_bots_at_their_end",
    "finish_bots_at_their_end",
    "install_bot_end_schedule",
    "installed_bot_end_schedule",
]
