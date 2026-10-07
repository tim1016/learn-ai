"""Cancel what has not filled of every option entry whose wait has run out (ADR 0076).

An option entry is a limit order that waits at the broker. Its acceptance
records when the Clerk cancels what has not filled: ``cancel_at_ms``, the
Clerk's own clock at admission plus the owner's entry wait. This is the step
that acts on it.

It keeps no list. What is due is read from the books each time, as the
stopped-run step reads its own worklist (``stopped_run_entries``), so a
restart, a dead task or a skipped round loses nothing: the next reader finds
the same entries. Two callers run it: the deadline keeper, on time
(``deadline_keeper``), and the reconciliation pass, as its backstop.

The cancel itself is the Clerk's one cancel of an owned entry
(``exit_resolution.cancel_and_prove_owned_entry``). It records that it is
asking before it asks, and then believes only a lookup of the order by its
own id: the answer to the cancel request proves nothing. So an entry reads
"cancellation requested" until the lookup shows it ended, a fill that
arrived during or after the cancel is counted, and a lookup that fails
leaves the outcome unknown and the whole commitment set aside.

Left alone, by design: an entry before its deadline; an entry from before
entries could rest, which has no deadline; an entry whose order has ended
or whose ENTER has ended; and an entry a live EXIT owns, because that
EXIT's own machine cancels it and a second canceller would race it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.exit_resolution import ENTRY_WAIT_ENDED, cancel_and_prove_owned_entry
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline
from app.broker.alpaca.clerk.sqlite.reads import CANCELLABLE_ENTRY_BROKER_STATES, NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.contract.ports import BrokerTradePort

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)

# An entry's cancel time, read from its own acceptance. An entry in shares, and an
# option entry from before entries could rest, has none: the value is then NULL.
_CANCEL_AT_SQL = "CAST(json_extract(accepted.facts_json, '$.option.cancel_at_ms') AS INTEGER)"

# Option entries that can still fill and that the keeper may cancel: a strategy's
# ENTRY, working or not yet answered by the broker, under an ENTER that has not
# ended, that no live EXIT owns. The predicate of
# ``reads.entry_orders_owed_a_cancel``, for a run of any state and with a deadline.
# Read from the orders the books bind to a contract, so books that never traded
# an option answer from an empty table.
_RESTING_ENTRIES_SQL = (
    f"SELECT o.order_ref, o.effect_operation_id, o.client_order_id, o.broker_order_id, o.role, o.broker_state, "
    f"o.submitted_at_ms, o.updated_at_ms, {_CANCEL_AT_SQL} AS cancel_at_ms FROM order_contracts bound "
    "JOIN orders o ON o.order_ref = bound.order_ref "
    "JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
    "JOIN custody_transitions accepted ON accepted.order_ref = o.order_ref "
    "AND accepted.transition_kind = 'ENTER_ACCEPTED' "
    "WHERE o.role = 'ENTRY' AND e.kind = 'ENTER' AND e.strategy_instance_id IS NOT NULL "
    f"AND e.state IN ({', '.join('?' for _ in NONTERMINAL_EFFECT_STATES)}) "
    f"AND (o.broker_state IS NULL OR LOWER(o.broker_state) IN ({', '.join('?' for _ in CANCELLABLE_ENTRY_BROKER_STATES)})) "
    f"AND {_CANCEL_AT_SQL} IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM operation_order_links l "
    "JOIN effect_operations x ON x.effect_operation_id = l.effect_operation_id "
    "WHERE l.order_ref = o.order_ref AND x.kind = 'EXIT' "
    "AND x.state NOT IN ('succeeded','failed','rejected')) "
    "ORDER BY cancel_at_ms ASC, o.order_ref ASC"
)
_ORDER_COLUMNS = (
    "order_ref", "effect_operation_id", "client_order_id", "broker_order_id", "role", "broker_state",
    "submitted_at_ms", "updated_at_ms",
)
_RESTING_ENTRIES_PARAMS = (*sorted(NONTERMINAL_EFFECT_STATES), *sorted(CANCELLABLE_ENTRY_BROKER_STATES))


@dataclass(frozen=True, slots=True)
class RestingEntry:
    """One option entry that may still fill, and when the Clerk cancels what is left of it."""

    order: OrderResource
    cancel_at_ms: int


def resting_entries(repo: ClerkSqliteRepository) -> list[RestingEntry]:
    """Every option entry that can still fill and has a deadline, the earliest deadline first."""
    with repo.write_fence() as conn:
        return _read_resting_entries(conn)


def _read_resting_entries(conn: sqlite3.Connection) -> list[RestingEntry]:
    rows = conn.execute(_RESTING_ENTRIES_SQL, _RESTING_ENTRIES_PARAMS).fetchall()
    return [
        RestingEntry(
            order=OrderResource(**{name: row[name] for name in _ORDER_COLUMNS}),
            cancel_at_ms=row["cancel_at_ms"],
        )
        for row in rows
    ]


def entries_past_their_wait(repo: ClerkSqliteRepository, now_ms: int) -> list[RestingEntry]:
    """The resting entries whose owner's wait has run out by ``now_ms``."""
    return [entry for entry in resting_entries(repo) if entry.cancel_at_ms <= now_ms]


@dataclass(frozen=True, slots=True)
class WaitEndedRound:
    """What one run of the step did, by order reference."""

    # Proven ended: the lookup showed the order ended, or that it never reached the broker.
    ended: tuple[str, ...] = ()
    # Past the deadline and not proven ended yet: the cancel is asked for, uncertain, or its
    # operation is claimed by someone else. The next run looks again.
    still_due: tuple[str, ...] = ()


async def cancel_entries_past_their_wait(
    repo: ClerkSqliteRepository,
    *,
    trade: BrokerTradePort,
    off_loop: OffLoop | None = None,
) -> WaitEndedRound:
    """Cancel and exact-prove each option entry whose wait has run out, at the Clerk's own clock.

    A held operation claim means another owner is acting on that order right
    now: its own POST still in flight, an EXIT, a Stop. The entry is skipped
    and the next run retries it; nothing could be cancelled meanwhile anyway.
    Broker failures are folded by ``cancel_and_prove_owned_entry`` itself
    (``ORDER_CANCEL_UNCERTAIN``), as for every other caller.

    On books that hold no option entry with a deadline this reads one empty
    worklist and writes nothing.
    """
    run = off_loop if off_loop is not None else run_inline
    ended: list[str] = []
    still_due: list[str] = []
    for entry in await run(lambda: entries_past_their_wait(repo, repo.clock())):
        order_ref = entry.order.order_ref
        try:
            resolved = await cancel_and_prove_owned_entry(
                repo, entry_order_ref=order_ref, trade=trade, off_loop=run, reason=ENTRY_WAIT_ENDED
            )
        except OperationClaimError:
            logger.info(
                "deferred the cancel of a resting option entry: its operation is claimed",
                extra={
                    "action": "resting_entry_cancel_deferred",
                    "account_id": repo.account_id,
                    "order_ref": order_ref,
                },
            )
            still_due.append(order_ref)
            continue
        state = (resolved.broker_state or "").lower()
        still_resting = await run(lambda ref=order_ref: any(left.order.order_ref == ref for left in resting_entries(repo)))
        if still_resting:
            still_due.append(order_ref)
            logger.warning(
                "a resting option entry is past its wait and not proven ended; the next round looks again",
                extra={
                    "action": "resting_entry_cancel_unconfirmed",
                    "account_id": repo.account_id,
                    "order_ref": order_ref,
                    "broker_state": state or None,
                    "cancel_at_ms": entry.cancel_at_ms,
                },
            )
            continue
        ended.append(order_ref)
        logger.info(
            "a resting option entry reached the end of its wait and is proven ended",
            extra={
                "action": "resting_entry_wait_ended",
                "account_id": repo.account_id,
                "order_ref": order_ref,
                "broker_state": state or None,
                "cancel_at_ms": entry.cancel_at_ms,
            },
        )
    return WaitEndedRound(ended=tuple(ended), still_due=tuple(still_due))


__all__ = [
    "RestingEntry",
    "WaitEndedRound",
    "cancel_entries_past_their_wait",
    "entries_past_their_wait",
    "resting_entries",
]
