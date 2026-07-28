"""Uncertain-submit resolution and startup replay for the Alpaca clerk (S5).

Extracted from ``clerk.py`` to keep the single-writer facade under the
1,000-line ceiling. ``AlpacaClerk.recover`` and ``AlpacaClerk._resolve_intent``
delegate here unchanged; this module is a friend of the clerk — it operates on
the clerk's private collaborators (journal, locks, trade port, clock) and holds
no state of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.journal import OrderJournal
from app.broker.alpaca.clerk.leg_identity import LegIdentity
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderLegError, OrderLegResult
from app.broker.contract.errors import BrokerError

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.clerk import AlpacaClerk

logger = logging.getLogger(__name__)


# A response-lost POST can still be executing at Alpaca when an immediate
# by-client-id lookup says 404. Never turn that first absence into a terminal
# failure; a later recovery/sweep may do so only after this bounded grace window.
UNCERTAIN_SUBMIT_GRACE_MS = 30_000


async def recover(clerk: AlpacaClerk) -> None:
    """Replay the journal and resolve every unfinished intent (S5).

    Runs on startup BEFORE new submits: intents left at ``intent_recorded``
    / ``submit_uncertain`` are finished by the same ``client_order_id``
    resolution the write path uses. Idempotent; a fresh install resolves
    nothing; each intent resolves independently (one stuck lookup does not
    block the others — it stays uncertain for a later replay / sweep).
    """
    async with clerk._recovery_lock:
        # Protect only the journal snapshot with the intake lock. The remote
        # lookups below intentionally run after release so a slow recovery
        # cannot delay a cancel that reduces exposure.
        async with clerk._intake_lock:
            account_id, journal = await clerk._ensure_journal()
            entries = journal.read_entries()
        unresolved = derive.unresolved_intents(entries)
        if not unresolved:
            logger.info(
                "alpaca clerk recovery: no unresolved intents",
                extra={"action": "recover", "account_id": account_id},
            )
            return
        logger.info(
            "alpaca clerk recovery: resolving unresolved intents",
            extra={
                "action": "recover",
                "account_id": account_id,
                "count": len(unresolved),
            },
        )
        terminal_outcomes = derive.terminal_map(entries)
        uncertain_recorded_at = derive.uncertain_timestamp_map(entries)
        for intent_entry in unresolved:
            identity = LegIdentity.from_entry(intent_entry, clock=clerk._clock)
            await resolve_intent(
                clerk,
                identity,
                journal,
                terminal_outcomes=terminal_outcomes,
                uncertain_recorded_at_ms=uncertain_recorded_at.get(
                    identity.order_ref
                ),
            )


async def resolve_intent(
    clerk: AlpacaClerk,
    identity: LegIdentity,
    journal: OrderJournal,
    *,
    terminal_outcomes: dict[str, OrderLegResult] | None = None,
    terminal_on_absence: bool = True,
    uncertain_recorded_at_ms: int | None = None,
) -> OrderLegResult:
    """Resolve one intent by ``client_order_id``; idempotent, last-write-wins.

    ``submit`` calls this while holding the intake lock; ``recover`` calls it
    under its dedicated recovery lock after releasing intake for the remote
    lookup. Idempotency: if a terminal ``submit_acked`` /
    ``submit_failed`` already exists for this ``order_ref``, this is a NO-OP —
    it re-derives and returns the existing outcome without a second write, so
    running it twice never double-writes a terminal entry or double-counts.
    ``recover`` passes a pre-scanned ``terminal_outcomes`` map so this check
    costs no disk read; the ``submit`` path passes ``None`` and scans the
    (single-account) ledger once.

    Otherwise it asks the vendor whether the order landed:

    - found → append ``submit_acked`` (carry the vendor ``BrokerOrder``),
    - ``None`` (404 absent) → append ``submit_failed`` only after the
      30-second grace period; the immediate post-timeout probe and early
      recovery/sweep leave the intent uncertain for an in-flight broker
      worker,
    - any lookup ``BrokerError`` → leave ``submit_uncertain``, no terminal
      write, return an ``uncertain`` result. Never fabricate a terminal.
    """
    if terminal_outcomes is not None:
        existing = terminal_outcomes.get(identity.order_ref)
    else:
        existing = derive.terminal_outcome(journal.read_entries(), identity.order_ref)
    if existing is not None:
        return existing

    try:
        order = await clerk._trade.get_order_by_client_order_id(identity.order_ref)
    except BrokerError as exc:
        logger.warning(
            "alpaca clerk resolution still uncertain; leaving intent for replay",
            extra={
                "action": "resolve_uncertain",
                "account_id": identity.account_id,
                "order_ref": identity.order_ref,
                "why": exc.detail,
            },
        )
        return OrderLegResult(
            status="uncertain",
            order_ref=identity.order_ref,
            intent_id=identity.intent_id,
            error=OrderLegError(
                message="The order's outcome is not yet known.",
                why=exc.detail,
            ),
        )

    if order is not None and order.client_order_id != identity.order_ref:
        # Boundary validation: the by-client-id lookup must return the order
        # we queried. A mismatch is an integrity violation, not a definitive
        # outcome — never fabricate a terminal on it; leave uncertain for a
        # later replay to re-resolve.
        logger.error(
            "alpaca clerk resolution returned a mismatched order; leaving uncertain",
            extra={
                "action": "resolve_mismatch",
                "account_id": identity.account_id,
                "order_ref": identity.order_ref,
                "returned_client_order_id": order.client_order_id,
            },
        )
        return OrderLegResult(
            status="uncertain",
            order_ref=identity.order_ref,
            intent_id=identity.intent_id,
            error=OrderLegError(
                message="The order's outcome is not yet known.",
                why="The broker returned an order for a different client_order_id.",
            ),
        )

    absence_grace_active = (
        uncertain_recorded_at_ms is not None
        and clerk._clock() - uncertain_recorded_at_ms < UNCERTAIN_SUBMIT_GRACE_MS
    )
    if order is None and (not terminal_on_absence or absence_grace_active):
        logger.info(
            "alpaca clerk absent lookup left uncertain for recovery",
            extra={
                "action": "resolve_absence_grace",
                "account_id": identity.account_id,
                "order_ref": identity.order_ref,
                "grace_active": absence_grace_active,
            },
        )
        return OrderLegResult(
            status="uncertain",
            order_ref=identity.order_ref,
            intent_id=identity.intent_id,
            error=OrderLegError(
                message="The order's outcome is not yet known.",
                why="Alpaca has not observed the order yet; it may still be in flight.",
            ),
        )

    if order is None:
        failure = OrderLegError(
            message="The order did not reach the broker.",
            why="Alpaca has no order for this client_order_id (definitively absent).",
        )
        await journal.append_async(
            identity.entry(ClerkEntryKind.SUBMIT_FAILED, error=failure)
        )
        logger.info(
            "alpaca clerk resolved uncertain submit: order absent (failed)",
            extra={
                "action": "resolve_failed",
                "account_id": identity.account_id,
                "order_ref": identity.order_ref,
            },
        )
        result = OrderLegResult(
            status="failed",
            order_ref=identity.order_ref,
            intent_id=identity.intent_id,
            error=failure,
        )
        if terminal_outcomes is not None:
            terminal_outcomes[identity.order_ref] = result
        return result

    await journal.append_async(
        identity.entry(ClerkEntryKind.SUBMIT_ACKED, order=order)
    )
    logger.info(
        "alpaca clerk resolved uncertain submit: order found (acked)",
        extra={
            "action": "resolve_acked",
            "account_id": identity.account_id,
            "order_ref": identity.order_ref,
            "broker_order_id": order.order_id,
        },
    )
    result = OrderLegResult(
        status="acked",
        order_ref=identity.order_ref,
        intent_id=identity.intent_id,
        order=order,
    )
    if terminal_outcomes is not None:
        terminal_outcomes[identity.order_ref] = result
    return result
