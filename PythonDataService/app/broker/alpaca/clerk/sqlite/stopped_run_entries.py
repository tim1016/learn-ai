"""Cancel every working ENTRY whose run is no longer ACTIVE (#2362).

The invariant: no ENTER may stay working at the broker once the run that
placed it is no longer ACTIVE. A run can end without an operator Stop (a
crash, a feed death, a stream that ended -- #2347); a cancel can collide with
an operation claim held by the sweep or by the ENTER's own POST (#2361); and
an ENTER whose POST was still in flight at Stop has no broker state to cancel
yet (#2358).

This runs as one step of the account reconciliation pass
(``reconcile._reconcile_account_serialized``), after operation recovery. It
is the only canceller of a stopped run's entries: operator Stop reaches it
through the reconciliation inside its custody proof, and every 15 s sweep
re-drives it until the order is terminal. The worklist
(``reads.entry_orders_owed_a_cancel``) is derived from durable facts only --
the ``runs`` table and the orders -- so it survives a restart with no "cancel
owed" record of its own, and a restart (which retires every pre-restart run)
cancels those runs' working entries at boot.

Excluded, by design: an ENTRY of the strategy's ACTIVE run; an ENTRY linked
to a nonterminal EXIT (that EXIT's machine owns its cancel, and a second
canceller would race it); manual-custody orders, which belong to no run.

A partially filled ENTRY has its remainder cancelled; the filled part stays
attributed exposure.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    ENTRY_TERMINAL_PROOF,
    cancel_each_entry_as_a_pass_step,
)
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.ports import BrokerTradePort


def entries_owed_a_cancel(repo: ClerkSqliteRepository) -> list[OrderResource]:
    """Working strategy ENTRYs whose run is not ACTIVE and no EXIT owns."""
    return repo.entry_orders_owed_a_cancel()


async def cancel_entries_of_inactive_runs(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> None:
    """Cancel and exact-prove each ENTRY that outlived its run.

    A held operation claim means another owner is acting on that order right
    now (the ENTER's POST, an operator cancel): the entry is skipped and the
    next pass retries it. Broker failures are folded by
    ``cancel_and_prove_owned_entry`` itself (``ORDER_CANCEL_UNCERTAIN``), as
    for every other caller; anything else propagates and the pass records
    itself incomplete.
    """
    run = off_loop if off_loop is not None else run_inline
    entries = await run(lambda: entries_owed_a_cancel(repo))
    await cancel_each_entry_as_a_pass_step(
        repo,
        entries,
        trade=trade,
        off_loop=run,
        reason=ENTRY_TERMINAL_PROOF,
        about="a stopped run's ENTRY",
        action="stopped_run_entry",
    )


__all__ = ["cancel_entries_of_inactive_runs", "entries_owed_a_cancel"]
