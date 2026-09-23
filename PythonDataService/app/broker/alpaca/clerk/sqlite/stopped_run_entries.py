"""Cancel every working ENTRY whose run is no longer ACTIVE (#2362).

The invariant: no ENTER may stay working at the broker once the run that
placed it is no longer ACTIVE. Operator Stop's ``prove_stop_outcome`` cancels
working entries once, but a run can end without it (a crash, a feed death, a
stream that ended — #2347), its cancel can collide with an operation claim
held by the sweep or by the ENTER's own POST (#2361), and an ENTER whose POST
was still in flight at Stop has no broker state to cancel yet (#2358).

This runs as one step of the account reconciliation pass
(``reconcile._reconcile_account_serialized``), after operation recovery, so
every 15 s sweep re-drives the cancel until the order is terminal. The work
list is derived from durable facts only — the ``runs`` table and the orders
— so it survives a restart with no "cancel owed" record of its own.

Excluded, by design:

* an ENTRY of the strategy's ACTIVE run — the run still owns it;
* an ENTRY linked to a nonterminal EXIT — that EXIT's machine owns its cancel
  (``exit_resolution``), and a second canceller would race it;
* manual-custody orders — they belong to no run
  (``reads.cancellable_strategy_entry_orders``).

A partially filled ENTRY has its remainder cancelled; the filled part stays
attributed exposure, exactly as Stop's own cancel leaves it.
"""

from __future__ import annotations

import logging

from app.broker.alpaca.clerk.sqlite.exit_resolution import cancel_and_prove_owned_entry
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


def entries_owed_a_cancel(repo: ClerkSqliteRepository) -> list[OrderResource]:
    """Working strategy ENTRYs whose run is not ACTIVE and no EXIT owns."""
    owed: list[OrderResource] = []
    for order in repo.cancellable_strategy_entry_orders():
        effect = repo.effect_operation(order.effect_operation_id)
        if effect is None or effect.strategy_instance_id is None:
            continue
        active = repo.active_run(effect.strategy_instance_id)
        if active is not None and active.run_id == effect.run_id:
            continue
        if repo.active_exit_for_order(order.order_ref) is not None:
            continue
        owed.append(order)
    return owed


async def cancel_entries_of_inactive_runs(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> None:
    """Cancel and exact-prove each ENTRY that outlived its run.

    A held operation claim means another owner is acting on that order right
    now (the ENTER's POST, a Stop, an operator cancel): the entry is skipped
    and the next pass retries it. Broker failures are folded by
    ``cancel_and_prove_owned_entry`` itself (``ORDER_CANCEL_UNCERTAIN``), as
    for every other caller; anything else propagates and the pass records
    itself incomplete.
    """
    run = off_loop if off_loop is not None else run_inline
    for entry in await run(lambda: entries_owed_a_cancel(repo)):
        try:
            resolved = await cancel_and_prove_owned_entry(
                repo,
                entry_order_ref=entry.order_ref,
                trade=trade,
                off_loop=run,
            )
        except OperationClaimError:
            logger.info(
                "deferred the cancel of a stopped run's working ENTRY: its operation is claimed",
                extra={
                    "action": "stopped_run_entry_cancel_deferred",
                    "account_id": repo.account_id,
                    "order_ref": entry.order_ref,
                },
            )
            continue
        logger.warning(
            "re-drove the cancel of a working ENTRY whose run is no longer active",
            extra={
                "action": "stopped_run_entry_cancel_redriven",
                "account_id": repo.account_id,
                "order_ref": entry.order_ref,
                "broker_state": resolved.broker_state,
            },
        )


__all__ = ["cancel_entries_of_inactive_runs", "entries_owed_a_cancel"]
