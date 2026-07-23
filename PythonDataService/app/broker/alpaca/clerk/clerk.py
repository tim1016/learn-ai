"""AlpacaClerk — in-process single-writer order submission (phase 2, S1).

The Clerk is the sole author of order submission for Alpaca. For each leg it:

1. **Mints identity** via the canonical, broker-agnostic order-identity module —
   ``build_manual_order_namespace`` + ``mint_intent_id`` + ``build_order_ref``,
   failing closed over the ``order_ref`` length cap — so
   ``client_order_id == order_ref == manual/{operator}/v1:{intent_id}``.
2. **Journals ``intent_recorded`` and ``fsync``'s it** (inbox + journal) BEFORE
   any broker call. No journal → no order.
3. **Calls the trade port** to submit.
4. **Journals ``submit_acked``** (with the ``BrokerOrder``) on success, or
   **``submit_failed``** on a ``BrokerError``, and returns a per-leg result.

Serialization: a single ``asyncio.Lock`` (the intake lock) makes submission
serial per account — combined with the single-uvicorn-worker deployment
constraint documented in this package's ``__init__``. A per-leg failure never
blocks the remaining legs; each leg is an independent journaled unit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegError,
    OrderLegResult,
    OrderSubmitResult,
)
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerOrderRequest
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import (
    build_manual_order_namespace,
    build_order_ref,
    mint_intent_id,
)

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    """Current instant as ``int64`` ms UTC (ingestion boundary)."""
    return int(datetime.now(UTC).timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class _LegIdentity:
    """The minted, durable identity for one leg, plus its journal context.

    Built once per leg before any journal write, then stamped onto every
    entry — so the six identity fields are never re-listed at each append site.
    ``client_order_id == order_ref`` is the design invariant.
    """

    account_id: str
    operator: str
    intent_id: str
    order_ref: str
    leg: BrokerOrderLeg

    def entry(
        self,
        kind: ClerkEntryKind,
        *,
        order: BrokerOrder | None = None,
        error: BrokerError | None = None,
    ) -> OrderJournalEntry:
        """A journal entry for this identity, stamped with ``kind`` and outcome."""
        return OrderJournalEntry(
            kind=kind,
            account_id=self.account_id,
            operator=self.operator,
            intent_id=self.intent_id,
            order_ref=self.order_ref,
            client_order_id=self.order_ref,
            leg=self.leg,
            recorded_at_ms=_now_ms(),
            order=order,
            error_message=error.message if error is not None else None,
            error_detail=error.detail if error is not None else None,
        )


class AlpacaClerk:
    """Single-writer order-submission facade for one Alpaca account.

    ``read`` supplies ``get_account`` (to resolve + cache the account id used
    for the journal path); ``trade`` supplies ``submit``. The journal is
    constructed lazily on first submit, once the account id is known.
    """

    broker_id = BROKER_ID

    def __init__(
        self,
        *,
        read: BrokerReadPort,
        trade: BrokerTradePort,
    ) -> None:
        self._read = read
        self._trade = trade
        self._intake_lock = asyncio.Lock()
        self._account_id: str | None = None
        self._journal: OrderJournal | None = None

    async def _ensure_journal(self) -> tuple[str, OrderJournal]:
        """Resolve + cache the account id and its journal (once)."""
        if self._journal is not None and self._account_id is not None:
            return self._account_id, self._journal
        account = await self._read.get_account()
        journal = OrderJournal(
            account_id=account.account_id, root=get_clerk_settings().dir
        )
        self._account_id = account.account_id
        self._journal = journal
        return account.account_id, journal

    async def submit(self, request: BrokerOrderRequest) -> OrderSubmitResult:
        """Submit every leg serially; one journaled result per leg."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            results = [
                await self._submit_leg(request.operator, leg, account_id, journal)
                for leg in request.legs
            ]
        return OrderSubmitResult(
            broker=self.broker_id, account_id=account_id, results=results
        )

    async def cancel(self, order_id: str) -> OrderCancelResult:
        """Cancel one working order by its broker-assigned id.

        This is a **first-class path, deliberately NOT routed through ``submit``
        or its per-leg gating.** A later slice (S6) adds an exposure hold that
        blocks *new exposure* — i.e. submission — but canceling a working order
        *reduces* exposure and must never be blocked by that hold. Keeping cancel
        off the submit path means S6 can add the hold to submit alone, and cancel
        stays reachable while a hold is active. (The hold does not exist yet; do
        not add it here — this comment records the intended seam.)

        Flow, sharing the intake lock (so a cancel and a submit never interleave)
        and the same fail-closed journal:

        1. Resolve ownership from the journal: an order this Clerk submitted has a
           ``submit_acked`` line mapping ``broker order_id → order_ref``. A
           foreign/unowned order is still cancelable (safe direction), journaled
           with honest ``owned=False`` attribution — never a fabricated intent.
        2. Journal ``cancel_recorded`` and ``fsync`` it BEFORE the broker call.
        3. Call the trade port's ``cancel``.
        4. Journal ``cancel_acked`` on success, or ``cancel_failed`` on a
           ``BrokerError`` (a non-cancelable order is a typed what/why, not 500).
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            owning = self._resolve_owning_entry(order_id, journal)
            owned = owning is not None

            def _entry(
                kind: ClerkEntryKind, *, error: BrokerError | None = None
            ) -> OrderJournalEntry:
                return OrderJournalEntry(
                    kind=kind,
                    account_id=account_id,
                    operator=owning.operator if owning is not None else "",
                    intent_id=owning.intent_id if owning is not None else "",
                    order_ref=owning.order_ref if owning is not None else "",
                    client_order_id=owning.client_order_id if owning is not None else "",
                    leg=owning.leg if owning is not None else None,
                    broker_order_id=order_id,
                    owned=owned,
                    recorded_at_ms=_now_ms(),
                    error_message=error.message if error is not None else None,
                    error_detail=error.detail if error is not None else None,
                )

            order_ref = owning.order_ref if owning is not None else None

            # No journal → no cancel: record + fsync BEFORE the broker call.
            journal.append(_entry(ClerkEntryKind.CANCEL_RECORDED))

            try:
                await self._trade.cancel(order_id)
            except BrokerError as exc:
                journal.append(_entry(ClerkEntryKind.CANCEL_FAILED, error=exc))
                return OrderCancelResult(
                    broker=self.broker_id,
                    account_id=account_id,
                    order_id=order_id,
                    status="failed",
                    owned=owned,
                    order_ref=order_ref,
                    error=OrderLegError(message=exc.message, why=exc.detail),
                )

            journal.append(_entry(ClerkEntryKind.CANCEL_ACKED))
            return OrderCancelResult(
                broker=self.broker_id,
                account_id=account_id,
                order_id=order_id,
                status="acked",
                owned=owned,
                order_ref=order_ref,
            )

    @staticmethod
    def _resolve_owning_entry(
        order_id: str, journal: OrderJournal
    ) -> OrderJournalEntry | None:
        """Find the ``submit_acked`` entry that minted the given broker order_id.

        The ``submit_acked`` line is the sole place the broker-assigned
        ``order_id`` is bound to our minted ``order_ref``/leg. Return the most
        recent match (last write wins) or ``None`` when the order is unowned.
        """
        owning: OrderJournalEntry | None = None
        for entry in journal.read_entries():
            if (
                entry.kind is ClerkEntryKind.SUBMIT_ACKED
                and entry.order is not None
                and entry.order.order_id == order_id
            ):
                owning = entry
        return owning

    async def _submit_leg(
        self,
        operator: str,
        leg: BrokerOrderLeg,
        account_id: str,
        journal: OrderJournal,
    ) -> OrderLegResult:
        # Mint identity — fail closed. Two failure modes, both surfaced as a
        # typed failed leg with NO journal write and NO broker call:
        #   * a bad ``operator`` (space, '/', '\\', NUL, '.'/'..') → a
        #     ``ValueError`` from ``validate_strategy_instance_id``. The router
        #     boundary rejects this as a 422, but the clerk defends in depth so
        #     a bad value reaching it directly still fails typed, never a 500.
        #   * an ``order_ref`` over the length cap → ``OrderRefError``. A
        #     too-long id is a caller error, never truncated.
        # ``OrderRefError`` subclasses ``ValueError``, so the single ``ValueError``
        # catch covers both the bad-operator and over-cap paths.
        intent_id = mint_intent_id()
        try:
            namespace = build_manual_order_namespace(operator)
            order_ref = build_order_ref(namespace, intent_id)
        except ValueError as exc:
            logger.warning(
                "alpaca clerk rejected order identity",
                extra={"operator": operator, "symbol": leg.symbol},
            )
            return OrderLegResult(
                status="failed",
                order_ref=f"manual/{operator}/v1:{intent_id}",
                intent_id=intent_id,
                error=OrderLegError(
                    message="Could not build a durable order identity for this leg.",
                    why=str(exc),
                ),
            )
        identity = _LegIdentity(account_id, operator, intent_id, order_ref, leg)

        # No journal → no order: record + fsync the intent BEFORE the broker call.
        journal.append(identity.entry(ClerkEntryKind.INTENT_RECORDED))

        try:
            order = await self._trade.submit(leg, client_order_id=order_ref)
        except BrokerError as exc:
            journal.append(identity.entry(ClerkEntryKind.SUBMIT_FAILED, error=exc))
            return OrderLegResult(
                status="failed",
                order_ref=order_ref,
                intent_id=intent_id,
                error=OrderLegError(message=exc.message, why=exc.detail),
            )

        journal.append(identity.entry(ClerkEntryKind.SUBMIT_ACKED, order=order))
        return OrderLegResult(
            status="acked", order_ref=order_ref, intent_id=intent_id, order=order
        )


_clerk: AlpacaClerk | None = None


def get_alpaca_clerk() -> AlpacaClerk | None:
    """Return the process-wide Alpaca clerk, or ``None`` when unconfigured.

    The clerk is installed in the app lifespan only when Alpaca keys are
    present; a ``None`` return means the router surfaces "not configured".
    """
    return _clerk


def set_alpaca_clerk(clerk: AlpacaClerk | None) -> None:
    """Install (or clear) the process-wide Alpaca clerk — lifespan wiring."""
    global _clerk
    _clerk = clerk


def reset_alpaca_clerk_for_testing() -> None:
    """Drop the process-wide clerk so a test starts clean."""
    global _clerk
    _clerk = None
