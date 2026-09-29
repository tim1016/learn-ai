"""Close what an ended Dry Run left, at the last price its run saw (owner decision 2026-09-29).

A run that ends holding -- a crash, a feed death, the operator's Stop, a
restart -- leaves its position open by design: a dead run never sells on its
own, it warns (ADR 0045). A Dry Run is exempt. It holds nothing real, so
leaving its simulated shares open only strands a chore and a false alarm. Its
simulation closes them instead, at the last price the run saw -- not a live
quote, which may be what killed the run and does not exist overnight -- and on
every kind of ending.

This runs as one step of the account reconciliation pass
(``reconcile._reconcile_account_serialized``), right after the stopped run's
entries are cancelled, and only on a ``sim:`` authority: a shadow world keeps
the dead-run rule, as every real account does. The pass already runs when
the ended run's authority is released, when boot restores a Dry Run whose
process died hard, and on every read that reopens a stopped Dry Run, so the
worklist (:func:`closes_owed`) is derived from durable facts only -- no ACTIVE
run, attributed exposure, an owned entry no EXIT owns -- with no "close owed"
record of its own to lose.

"The last price the run saw" is, operatively, the newest bar the market
delivered to this Dry Run's own bar ledger, fill-evidence streams excluded
(``SourceBarLedger.latest_for_symbol(market_only=True)``). Only the bot's runs
append there -- live delivery and startup-join history, both run-scoped --
and a close is owed only while no run is active, so that bar is the last one
its last run received. A future writer to that ledger outside a run would
break the equivalence; the fill would then need pinning to the run's end.

Each close is one recovery EXIT under ``DRY_RUN_CLOSE_DECISION_PREFIX``,
keyed on the exposure's newest entry, so a re-run of the pass drives the same
EXIT and never sells twice. The EXIT machine sends it regardless of the
session and binds it to the last delivered bar (``exit_resolution``). A close
that cannot be sent folds like any EXIT (``EXIT_NOT_FLAT``); from there the
stuck-EXIT watchdog's bounded re-drive and the operator's safe flatten, both
at the live IBKR quote, take over. This step never mints a second close for
the same exposure.

The close supersedes the bot's own EXIT when one is still waiting as the run
ends (owner decision 2026-09-29) without a second custodian: a simulation
reads no market holds, so the pass's operation recovery, which runs before
this step, resolves every such EXIT -- it fills at its decision bar, or it
cannot go out and ends ``EXIT_NOT_FLAT``. Either way the entry is free when
this step runs, the close sells what is left, and its flat proof clears that
notice in the same pass. An exposure an EXIT still owns here (a claim another
owner holds) is left to that EXIT and owed again next pass: a second EXIT
would race it, and one already sent may have filled.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from functools import partial

from app.broker.alpaca.clerk.account_authority import is_synthetic_account_id
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite.exit import (
    RecoveryRunActiveError,
    accept_recovery_exit,
    exit_effect_operation_id,
    resolve_accepted_exit,
)
from app.broker.alpaca.clerk.sqlite.exit_resolution import DRY_RUN_CLOSE_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbol
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwedClose:
    """One ended Dry Run's position the simulation still has to close."""

    strategy_instance_id: str
    symbol: str
    entry_order_ref: str
    decision_id: str


def ended_dry_runs(repo: ClerkSqliteRepository) -> list[str]:
    """The strategy instances with no ACTIVE run on this authority; empty on any but a ``sim:`` one."""
    if not is_synthetic_account_id(repo.account_id):
        return []
    return [
        instance["strategy_instance_id"]
        for instance in repo.strategy_instances()
        if repo.active_run(instance["strategy_instance_id"]) is None
    ]


def closes_owed_for(repo: ClerkSqliteRepository, strategy_instance_id: str) -> list[OwedClose]:
    """Every position one ended Dry Run holds that no EXIT owns yet.

    An exposure whose close was already accepted -- driven, folded or
    finished -- is not owed again.
    """
    held = [
        symbol
        for symbol, quantity in repo.attributed_positions_for_strategy(strategy_instance_id).items()
        if position_quantity_is_nonzero(quantity)
    ]
    if not held:
        return []
    owned_entries = repo.entry_orders_for_strategy(strategy_instance_id)
    owed: list[OwedClose] = []
    for symbol in held:
        entries = [order for order in owned_entries if entry_order_symbol(repo, order.order_ref).upper() == symbol]
        if not entries or any(repo.active_exit_for_order(order.order_ref) is not None for order in entries):
            continue
        entry_order_ref = entries[-1].order_ref
        decision_id = f"{DRY_RUN_CLOSE_DECISION_PREFIX}{hashlib.sha256(entry_order_ref.encode('utf-8')).hexdigest()[:16]}"
        if repo.effect_operation(
            exit_effect_operation_id(strategy_instance_id=strategy_instance_id, decision_id=decision_id)
        ) is not None:
            continue
        owed.append(OwedClose(strategy_instance_id, symbol, entry_order_ref, decision_id))
    return owed


def closes_owed(repo: ClerkSqliteRepository) -> list[OwedClose]:
    """Every position a no-longer-running Dry Run on this authority still has to close."""
    return [close for sid in ended_dry_runs(repo) for close in closes_owed_for(repo, sid)]


async def close_exposure_of_ended_dry_runs(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
) -> None:
    """Close what each ended Dry Run on this authority left, one bot at a time.

    The close is a convenience the simulation owes its owner, not a custody
    proof the pass depends on. An unexpected failure closing one bot is
    therefore logged with its stack and the pass goes on: letting it escape
    would fail every later pass on this authority, and with them the
    operator's Reconcile now -- the fresh reconciliation the fallback flatten
    needs. The bot's position stays attributed and flagged, and the next pass
    tries again.
    """
    run = off_loop if off_loop is not None else run_inline
    for strategy_instance_id in await run(partial(ended_dry_runs, repo)):
        try:
            for close in await run(partial(closes_owed_for, repo, strategy_instance_id)):
                await _close_one(repo, close, trade=trade, intake=intake, pricing=pricing, run=run)
        except Exception:
            logger.exception(
                "a Dry Run's run-end close failed; its position stays attributed and the pass goes on",
                extra={"action": "dry_run_close_failed", "account_id": repo.account_id,
                       "strategy_instance_id": strategy_instance_id},
            )


async def _close_one(
    repo: ClerkSqliteRepository,
    close: OwedClose,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    run: OffLoop,
) -> None:
    """Accept and drive one run-end close.

    A run that re-activated before the capture, a held operation claim or a
    policy block defers it: an accepted EXIT is re-driven by the next pass's
    operation recovery, and one never accepted is owed again.
    """
    extra = {
        "account_id": repo.account_id,
        "strategy_instance_id": close.strategy_instance_id,
        "symbol": close.symbol,
        "decision_id": close.decision_id,
    }
    try:
        accepted = await intake.off_loop(
            accept_recovery_exit,
            repo,
            account_id=repo.account_id,
            strategy_instance_id=close.strategy_instance_id,
            decision_id=close.decision_id,
            entry_order_ref=close.entry_order_ref,
            forbid_active_run=True,
        )
        resolved = await resolve_accepted_exit(repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run)
    except RecoveryRunActiveError:
        logger.info(
            "deferred a Dry Run's run-end close: its bot started a new run",
            extra={"action": "dry_run_close_deferred_run_active", **extra},
        )
        return
    except (OperationClaimError, AdmissionBlockedError):
        logger.info(
            "deferred a Dry Run's run-end close: its custody is busy or blocked",
            extra={"action": "dry_run_close_deferred", **extra},
        )
        return
    extra |= {"effect_operation_id": resolved.effect_operation_id, "reducing_order_ref": resolved.reducing_order_ref}
    effect = await run(partial(
        repo.effect_operation,
        exit_effect_operation_id(strategy_instance_id=close.strategy_instance_id, decision_id=close.decision_id),
    ))
    if effect is None or effect.state != "succeeded":
        logger.warning(
            "a Dry Run's run-end close did not leave it flat; the EXIT stays with custody recovery",
            extra={"action": "dry_run_close_not_flat", "effect_state": None if effect is None else effect.state,
                   **extra},
        )
        return
    logger.warning(
        "closed an ended Dry Run's simulated position at the last price its run saw",
        extra={"action": "dry_run_run_end_close", **extra},
    )


__all__ = ["OwedClose", "close_exposure_of_ended_dry_runs", "closes_owed", "closes_owed_for", "ended_dry_runs"]
