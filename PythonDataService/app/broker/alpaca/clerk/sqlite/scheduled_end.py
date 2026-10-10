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
   after its own STOP commits. The runner proves that stop from this pass
   once it is published, never with a pass of its own.
2. **Cancel** the run's working entries: the pass's #2362 step, unchanged,
   cancels every ENTER whose run is no longer ACTIVE.
3. **Sell** (:func:`finish_bots_at_their_end`, after the Dry Run close): when
   the owner chose SELL and the end is still pending -- the owner's Stop
   cancels it -- close every holding with one recovery EXIT under
   ``SCHEDULED_END_DECISION_PREFIX``, through ``ended_run_close`` exactly as a
   Dry Run's run-end close does. Its acceptance records
   ``regular_session_only``: the EXIT machine sends it only as a market order
   inside the regular session, and outside it holds and sells at the next
   open, never after hours -- the watchdog's re-drives of it included
   (``exit_resolution``, ``exit_watchdog``). A sale already sent whose answer
   was lost is not sent again at the next open: its EXIT keeps custody until
   Alpaca shows the order or an operator settles it (#2854). A Dry Run's
   shares are not sold here: #2641's run-end close sells them at the last
   price the run saw, on this ending as on every other.
4. **Record** the end carried out, once the run is stopped, none of the
   bot's orders can still fill outside an EXIT's custody, and every holding
   a SELL owes has its sale accepted.

This is the owner-scheduled exception to #2504's Q3 ("a dead run never sells
by itself"): of a position in shares, only the sale the owner scheduled is
made for a run that died. An option position has one more, owed by what is
held and not by this schedule: its scheduled close (``scheduled_close``,
ADR 0076).

The worklist is the runner's: the bots' ends live in their desired state,
which only the runner reads and writes. The runner and the Clerk share one
process (``run_ownership``), so the runner installs its end schedule here
once (:func:`install_bot_end_schedule`); a process with no runner installs
none and ends nothing. Every step is idempotent and derived from durable
facts -- the STOP is keyed on its run, each sale on the holding's entry
updated last -- so a pass that dies half-way is completed by the next one, and an
end found late (the Clerk was down) is carried out when the Clerk is back.

A sale waiting for the open is also the owner's to know about: the lane's
attention bell lists it (:func:`end_sales_waiting_for_open`) until it is sent.

No sale is accepted while the Clerk has stopped exiting the bot's position by
itself (``EXIT_STUCK``, #2854): the end stays pending, the bell and the bot's
page say why (:func:`end_sales_waiting_for_operator`), and once the bot is
flat the end is recorded carried out with nothing sold. A sale of the end's
own that ended failed does not complete an end still pending either: its send
was lost and an operator settled it as not sent, which opens that episode
(``unknown_order_attestation``), and its holding is left to the operator with
the rest. A sale accepted before the episode opened and still in progress is
sent as accepted; a store this code wrote holds none.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from app.broker.alpaca.clerk.account_authority import is_synthetic_account_id
from app.broker.alpaca.clerk.account_money import quantity_text
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.ended_run_close import (
    CloseDeferred,
    OwedClose,
    bot_holdings,
    closes_owed_under,
    drive_close,
)
from app.broker.alpaca.clerk.sqlite.exit import exit_effect_operation_id
from app.broker.alpaca.clerk.sqlite.exit_recovery import attested_absent_order_ref
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    SCHEDULED_END_DECISION_PREFIX,
    SCHEDULED_END_WAITS_FOR_OPEN,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.order_evidence import unresolved_order_refs, working_order_refs
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.uncertainty import automatic_exit_stopped, open_exit_stuck
from app.broker.contract.ports import BrokerTradePort
from app.schemas.bot_end import BotEnd

logger = logging.getLogger(__name__)

#: The ``operator_reason`` of the STOP the Clerk commits at a bot's end.
SCHEDULED_END_REASON = "scheduled_end"

#: The bell's line for an end whose sale the Clerk leaves to the operator
#: (:func:`end_sales_waiting_for_operator`, #2854).
SCHEDULED_END_WAITS_FOR_OPERATOR = "SCHEDULED_END_WAITS_FOR_OPERATOR"


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


def _live_registrations(repo: ClerkSqliteRepository) -> list[str]:
    return [
        instance["strategy_instance_id"]
        for instance in repo.strategy_instances()
        if instance["retired_at_ms"] is None
    ]


def _due_ends(repo: ClerkSqliteRepository, schedule: BotEndSchedule) -> list[ScheduledEnd]:
    """The ends due on this authority: every live registration's, at or before the Clerk's clock."""
    now_ms = repo.clock()
    return [end for end in schedule.pending_ends(_live_registrations(repo)) if end.end.end_at_ms <= now_ms]


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
    next pass carries it out. A lost execution lease fails the pass, as it
    does in the fence and in every other write.
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
                for close in await run(partial(_sales_owed, repo, schedule, end)):
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
        except ExecutionLeaseLost:
            raise
        except Exception:
            logger.exception(
                "a bot's scheduled end could not be carried out; it stays pending and the pass goes on",
                extra={
                    "action": "scheduled_end_failed",
                    "account_id": repo.account_id,
                    "strategy_instance_id": end.strategy_instance_id,
                },
            )


def _sales_owed(
    repo: ClerkSqliteRepository, schedule: BotEndSchedule, end: ScheduledEnd, *, left_to_operator: bool = False
) -> list[OwedClose]:
    """What a SELL end still has to sell: nothing while the bot's run is ACTIVE, or once the end is gone.

    The end is read again right before the sale: the owner's Stop in the
    same minute cancels it (Stop keeps the shares), and an edit replaces it.
    Nothing either while the Clerk has stopped exiting the bot by itself
    (#2854): those sales are the operator's, and ``left_to_operator`` asks
    for exactly them instead -- every holding the end's own sale has not
    taken or, once accepted, ended failed on, the operator's Flatten working
    on it or not.
    """
    sid = end.strategy_instance_id
    if repo.active_run(sid) is not None or end not in schedule.pending_ends([sid]):
        return []
    if automatic_exit_stopped(repo, sid) != left_to_operator:
        return []
    if left_to_operator:
        holdings = bot_holdings(repo, sid, decision_prefix=SCHEDULED_END_DECISION_PREFIX)
        return [holding.close for holding in holdings if holding.state != "closing"]
    return closes_owed_under(repo, sid, decision_prefix=SCHEDULED_END_DECISION_PREFIX)


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
    resolved = await drive_close(
        repo, close, trade=trade, intake=intake, pricing=pricing, run=run, regular_session_only=True,
    )
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

    The run is stopped; none of the bot's orders -- working, or with an
    outcome not yet known, by the STOP proof's own definitions -- can still
    fill outside an EXIT's custody; and, for a SELL end, every holding's sale
    is accepted -- from then on it is that EXIT's, held for the open if it
    must be, and the watchdog's to send again if it ended failed. A holding
    another EXIT still owns keeps the end pending until that EXIT resolves.
    While the Clerk has stopped exiting the bot by itself nothing of its own
    sells a holding, an accepted sale that ended failed included, so only a
    flat bot's end is carried out (#2854).
    """
    sid = end.strategy_instance_id
    if repo.active_run(sid) is not None:
        return False
    unsettled = {*working_order_refs(repo, sid), *unresolved_order_refs(repo, sid)}
    if any(_outside_an_exit(repo, order_ref) for order_ref in unsettled):
        return False
    if not sells or end.end.end_action == "KEEP":
        return True
    holdings = bot_holdings(repo, sid, decision_prefix=SCHEDULED_END_DECISION_PREFIX)
    if automatic_exit_stopped(repo, sid):
        return not holdings
    return all(holding.state in ("closing", "failed") for holding in holdings)


def _outside_an_exit(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    """An entry no EXIT owns: a reducing order is always an EXIT's."""
    order = repo.order(order_ref)
    return order is not None and order.role == "ENTRY" and repo.active_exit_for_order(order_ref) is None


@dataclass(frozen=True)
class EndSaleWaiting:
    """A bot's end sale not sent: held for the next regular open, or left to the operator."""

    strategy_instance_id: str
    symbol: str
    quantity: float


def end_sales_waiting_for_open(repo: ClerkSqliteRepository) -> list[EndSaleWaiting]:
    """Every live bot's end sale waiting for the next regular open, read from the ledger (#2607).

    Waiting: the bot's EXIT is still active, its newest market hold is the
    regular-session sale's (``SCHEDULED_END_WAITS_FOR_OPEN``), and no
    order of that EXIT has been sent. The sale's first send ends the wait,
    and the EXIT's end ends it too. Blocking: runs off the event loop.
    """
    waiting: list[EndSaleWaiting] = []
    for sid in sorted(repo.strategies_with_active_exit(_live_registrations(repo))):
        effect = repo.active_exit_for_strategy(sid)
        hold = repo.last_strategy_transition(strategy_instance_id=sid, transition_kind="EXIT_MARKET_HOLD")
        if (
            effect is None
            or hold is None
            or hold["effect_operation_id"] != effect.effect_operation_id
            or hold["summary_code"] != SCHEDULED_END_WAITS_FOR_OPEN
        ):
            continue
        if any(
            order.effect_operation_id == effect.effect_operation_id
            and repo.has_order_transition(order_ref=order.order_ref, transition_kind="ORDER_SUBMIT_REQUESTED")
            for order in repo.orders_for_strategy(sid)
        ):
            continue
        symbol = json.loads(hold["facts_json"])["symbol"]
        waiting.append(EndSaleWaiting(sid, symbol, repo.position(sid, symbol)))
    return waiting


def end_sales_waiting_for_operator(
    repo: ClerkSqliteRepository, strategy_instance_ids: Sequence[str] | None = None
) -> list[EndSaleWaiting]:
    """Each due end sale not accepted because the Clerk stopped exiting its bot's position by itself (#2854).

    What :func:`_sales_owed` leaves to the operator, by its own rule, read from
    the end schedule and the ledger with nothing recorded for it: the wait
    ends once the bot is flat or its end is cancelled. Every live bot's,
    unless ``strategy_instance_ids`` names them.
    """
    schedule = installed_bot_end_schedule()
    if schedule is None or is_synthetic_account_id(repo.account_id):
        return []
    sids = _live_registrations(repo) if strategy_instance_ids is None else strategy_instance_ids
    # Asked on every poll of the bell: the runner is read only for a bot the Clerk has stopped exiting.
    stopped = [sid for sid in sids if automatic_exit_stopped(repo, sid)]
    now_ms = repo.clock()
    return [
        EndSaleWaiting(close.strategy_instance_id, close.symbol, repo.position(close.strategy_instance_id, close.symbol))
        for end in schedule.pending_ends(stopped)
        if end.end.end_action == "SELL" and end.end.end_at_ms <= now_ms
        for close in _sales_owed(repo, schedule, end, left_to_operator=True)
    ]


def end_sale_waits_for_operator_words(waiting: EndSaleWaiting) -> str:
    """Why a sale :func:`end_sales_waiting_for_operator` lists was not sent, and what completes the end.

    The sale is never sent later: the operator closes the position, and the end completes without one.
    """
    long = waiting.quantity > 0
    order = "sale" if long else "buy-back"
    return (
        f"The Clerk stopped exiting this bot's position by itself, so the end's {order} of "
        f"{quantity_text(abs(waiting.quantity))} {waiting.symbol} was not sent. "
        f"{'Sell the position' if long else 'Buy the position back'} or write it off from the bot's page; "
        f"the end then completes with no {order}."
    )


def attested_absent_end_sale(repo: ClerkSqliteRepository, strategy_instance_id: str) -> EndSaleWaiting | None:
    """The end's own sale an operator settled as "Alpaca has no such order", while its bot is stuck (#2854).

    The end was recorded carried out when its sale was accepted; the
    settlement ends that sale's exit as not sent and leaves the position to
    the operator, but nothing re-records the end, so the panel corrects the
    end's words from here instead. The stuck episode's attested order must
    be one of this bot's end-sale decisions (``SCHEDULED_END_DECISION_PREFIX``):
    an order of any other exit settled as not sent does not change what the
    end did. Read from the records with nothing recorded for it.
    """
    stuck = open_exit_stuck(repo, strategy_instance_id)
    if stuck is None:
        return None
    order_ref = attested_absent_order_ref(stuck)
    if order_ref is None:
        return None
    order = repo.order(order_ref)
    if order is None:
        return None
    for holding in bot_holdings(repo, strategy_instance_id, decision_prefix=SCHEDULED_END_DECISION_PREFIX):
        if order.effect_operation_id == exit_effect_operation_id(
            strategy_instance_id=strategy_instance_id, decision_id=holding.close.decision_id
        ):
            return EndSaleWaiting(
                strategy_instance_id,
                holding.close.symbol,
                repo.position(strategy_instance_id, holding.close.symbol),
            )
    return None


def attested_absent_end_sale_words(waiting: EndSaleWaiting) -> str:
    """Why an end recorded as "sale put in" no longer names an order Alpaca has, and what completes the end (#2854)."""
    long = waiting.quantity > 0
    order = "sale" if long else "buy-back"
    return (
        f"The end's {order} of {quantity_text(abs(waiting.quantity))} {waiting.symbol} was sent, its answer "
        f"was lost, and an operator settled that Alpaca has no such order; the exit was ended as not sent. "
        f"{'Sell the position' if long else 'Buy the position back'} or write it off from the bot's page."
    )


__all__ = [
    "SCHEDULED_END_REASON",
    "SCHEDULED_END_WAITS_FOR_OPERATOR",
    "BotEndSchedule",
    "EndSaleWaiting",
    "ScheduledEnd",
    "attested_absent_end_sale",
    "attested_absent_end_sale_words",
    "end_sale_waits_for_operator_words",
    "end_sales_waiting_for_open",
    "end_sales_waiting_for_operator",
    "fence_bots_at_their_end",
    "finish_bots_at_their_end",
    "install_bot_end_schedule",
    "installed_bot_end_schedule",
]
