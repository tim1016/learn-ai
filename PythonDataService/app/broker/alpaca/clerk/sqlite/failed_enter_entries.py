"""Cancel every working ENTRY in shares whose ENTER ended in failure while its run stays ACTIVE (#2961).

The invariant: an ENTER the books have failed may not leave an order working
at the broker, whatever its run stands at. A stock entry reaches that state
through a lost send answer voided as never sent (#2342): Alpaca later shows
the order working, the run is still ACTIVE, and before this step nothing
owned that order — the stopped-run step waits for the run to end, a stock
entry has no deadline round to be cancelled at, and no other step lists an
entry under an ENTER that has ended.

This runs as one step of the account reconciliation pass
(``reconcile._reconcile_account_serialized``), beside the stopped-run step,
and every 15 s sweep re-drives it until the order is terminal. Like that
step's, the worklist (``reads.working_share_entries_under_failed_enters``)
is derived from durable facts only — the runs, the effects and the orders —
so it survives a restart with no "cancel owed" record of its own.

Excluded, by design: option entries, which the deadline keeper cancels at
their owner's wait whatever their ENTER's state (ADR 0076), so this step
never becomes a second canceller racing it; an entry a nonterminal EXIT
links (that EXIT's machine owns its cancel); manual-custody orders, which
belong to no run; and an entry under a nonterminal ENTER, whose own machine
still owns the order.

The bot's next entry is meanwhile refused by the admission gate
(``ENTRY_ORDER_WORKING``), which asks the same question of every
instrument. A partially filled ENTRY has its remainder cancelled; the
filled part stays attributed exposure.
"""

from __future__ import annotations

import logging

from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    ENTRY_ENTER_FAILED,
    cancel_and_prove_owned_entry,
)
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.reads import CANCELLABLE_ENTRY_BROKER_STATES
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.contract.ports import BrokerTradePort

logger = logging.getLogger(__name__)


def entries_owed_a_cancel(repo: ClerkSqliteRepository) -> list[OrderResource]:
    """Working ENTRYs in shares whose ENTER failed while their run stays ACTIVE and no EXIT owns."""
    return repo.working_share_entries_under_failed_enters()


async def cancel_working_entries_of_failed_enters(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> None:
    """Cancel and exact-prove each working entry whose ENTER ended in failure under a live run.

    A held operation claim means another owner is acting on that order right
    now (the ENTER's POST, an operator cancel): the entry is skipped and the
    next pass retries it. Broker failures are folded by
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
                reason=ENTRY_ENTER_FAILED,
            )
        except OperationClaimError:
            logger.info(
                "deferred the cancel of a working entry under a failed ENTER: its operation is claimed",
                extra={
                    "action": "failed_enter_entry_cancel_deferred",
                    "account_id": repo.account_id,
                    "order_ref": entry.order_ref,
                },
            )
            continue
        if (resolved.broker_state or "").lower() in CANCELLABLE_ENTRY_BROKER_STATES:
            logger.warning(
                "an entry under a failed ENTER is still working after its cancel; the next pass re-drives it",
                extra={
                    "action": "failed_enter_entry_cancel_unconfirmed",
                    "account_id": repo.account_id,
                    "order_ref": entry.order_ref,
                    "broker_state": resolved.broker_state,
                },
            )
            continue
        logger.warning(
            "cancelled a working entry whose ENTER ended in failure while its run is active",
            extra={
                "action": "failed_enter_entry_cancelled",
                "account_id": repo.account_id,
                "order_ref": entry.order_ref,
                "broker_state": resolved.broker_state,
            },
        )


__all__ = ["cancel_working_entries_of_failed_enters", "entries_owed_a_cancel"]
