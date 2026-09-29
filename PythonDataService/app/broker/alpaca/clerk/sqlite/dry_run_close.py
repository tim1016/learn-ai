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

Each close is one recovery EXIT under ``DRY_RUN_CLOSE_DECISION_PREFIX``,
keyed on the exposure's newest entry, so a re-run of the pass drives the same
EXIT and never sells twice. The EXIT machine sends it regardless of the
session and binds it to the last delivered bar (``exit_resolution``). A close
that cannot be sent folds like any EXIT (``EXIT_NOT_FLAT``); from there the
stuck-EXIT watchdog's bounded re-drive and the operator's safe flatten, both
at the live IBKR quote, take over. This step never mints a second close for
the same exposure.

Not covered: an exposure whose own EXIT is still working when the run ends
(a program EXIT held for the next session) -- that EXIT owns the position,
and a second one would race it.
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


def closes_owed(repo: ClerkSqliteRepository) -> list[OwedClose]:
    """Every position a no-longer-running Dry Run holds that no EXIT owns yet.

    Empty on any authority but a ``sim:`` one. An exposure whose close was
    already accepted -- driven, folded or finished -- is not owed again.
    """
    if not is_synthetic_account_id(repo.account_id):
        return []
    owed: list[OwedClose] = []
    for instance in repo.strategy_instances():
        strategy_instance_id = instance["strategy_instance_id"]
        if repo.active_run(strategy_instance_id) is not None:
            continue
        held = [
            symbol
            for symbol, quantity in repo.attributed_positions_for_strategy(strategy_instance_id).items()
            if position_quantity_is_nonzero(quantity)
        ]
        if not held:
            continue
        owned_entries = repo.entry_orders_for_strategy(strategy_instance_id)
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


async def close_exposure_of_ended_dry_runs(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
    pricing: RecoveryPricing,
    off_loop: OffLoop | None = None,
) -> None:
    """Accept and drive one run-end close for each position :func:`closes_owed` names.

    A run that re-activated before the capture, a held operation claim or a
    policy block defers that close: an accepted EXIT is re-driven by the next
    pass's operation recovery, and one never accepted is owed again.
    """
    run = off_loop if off_loop is not None else run_inline
    for close in await run(lambda: closes_owed(repo)):
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
            resolved = await resolve_accepted_exit(
                repo, accepted=accepted, trade=trade, pricing=pricing, off_loop=run
            )
        except RecoveryRunActiveError:
            logger.info(
                "deferred a Dry Run's run-end close: its bot started a new run",
                extra={"action": "dry_run_close_deferred_run_active", **extra},
            )
            continue
        except (OperationClaimError, AdmissionBlockedError):
            logger.info(
                "deferred a Dry Run's run-end close: its custody is busy or blocked",
                extra={"action": "dry_run_close_deferred", **extra},
            )
            continue
        extra |= {"effect_operation_id": resolved.effect_operation_id, "reducing_order_ref": resolved.reducing_order_ref}
        effect_operation_id = exit_effect_operation_id(
            strategy_instance_id=close.strategy_instance_id, decision_id=close.decision_id
        )
        effect = await run(partial(repo.effect_operation, effect_operation_id))
        if effect is None or effect.state != "succeeded":
            logger.warning(
                "a Dry Run's run-end close did not leave it flat; the EXIT stays with custody recovery",
                extra={"action": "dry_run_close_not_flat", "effect_state": None if effect is None else effect.state,
                       **extra},
            )
            continue
        logger.warning(
            "closed an ended Dry Run's simulated position at the last price its run saw",
            extra={"action": "dry_run_run_end_close", **extra},
        )


__all__ = ["OwedClose", "close_exposure_of_ended_dry_runs", "closes_owed"]
