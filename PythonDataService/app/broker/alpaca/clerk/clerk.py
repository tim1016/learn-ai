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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.broker.alpaca.clerk import derive, reconcile
from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import (
    UNEXPLAINED_ORDER_HOLD_CODE,
    ClerkEntryKind,
    ClerkStatus,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegError,
    OrderLegResult,
    OrderSubmitResult,
    ReconciliationVerdict,
)
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.errors import (
    BrokerError,
    BrokerSubmissionHeld,
    BrokerUnavailable,
)
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerOrderRequest,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import (
    build_manual_order_namespace,
    build_order_ref,
    mint_intent_id,
    order_ref_namespace_matches,
    parse_order_ref,
)

logger = logging.getLogger(__name__)

# A response-lost POST can still be executing at Alpaca when an immediate
# by-client-id lookup says 404. Never turn that first absence into a terminal
# failure; a later recovery/sweep may do so only after this bounded grace window.
UNCERTAIN_SUBMIT_GRACE_MS = 30_000

# Reconciliation only considers orders that can still create exposure. Filled,
# canceled, expired, rejected, and replaced orders belong to historical audit;
# their settled exposure is represented by the broker positions snapshot.
_RECONCILIATION_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)

# An injected clock: the current instant as ``int64`` ms UTC. Defaults to the
# ingestion-boundary wall clock; tests inject a fixed clock (mirrors the S4
# ``TradeUpdatesConsumer`` seam) so journaled timestamps are deterministic.
type Clock = Callable[[], int]


def _now_ms() -> int:
    """Current instant as ``int64`` ms UTC (ingestion boundary)."""
    return int(datetime.now(UTC).timestamp() * 1000)


def _leg_error(exc: BrokerError) -> OrderLegError:
    """Adapt a broker exception to the clerk's typed *what/why* leg error."""
    return OrderLegError(message=exc.message, why=exc.detail)


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
    clock: Clock

    def entry(
        self,
        kind: ClerkEntryKind,
        *,
        order: BrokerOrder | None = None,
        error: OrderLegError | None = None,
    ) -> OrderJournalEntry:
        """A journal entry for this identity, stamped with ``kind`` and outcome.

        ``error`` is the clerk's own typed *what/why* — a broker exception is
        adapted with :func:`_leg_error` at the call site, and a resolution
        synthesises its own. Keeping the one error shape lets every terminal /
        uncertain line reuse this single builder instead of re-listing the six
        identity fields.
        """
        return OrderJournalEntry(
            kind=kind,
            account_id=self.account_id,
            operator=self.operator,
            intent_id=self.intent_id,
            order_ref=self.order_ref,
            client_order_id=self.order_ref,
            leg=self.leg,
            recorded_at_ms=self.clock(),
            order=order,
            error_message=error.message if error is not None else None,
            error_detail=error.why if error is not None else None,
        )

    @classmethod
    def from_entry(cls, entry: OrderJournalEntry, *, clock: Clock) -> _LegIdentity:
        """Rebuild the identity from the owning ``intent_recorded`` line (S5).

        Resolution reuses the durable identity the submit minted — never
        fabricates one. Requires a leg: every submit-side line carries one, and
        the resolver only calls this on entries whose leg is present.
        """
        if entry.leg is None:
            raise ValueError(f"intent entry {entry.order_ref!r} has no leg to resolve")
        return cls(
            account_id=entry.account_id,
            operator=entry.operator,
            intent_id=entry.intent_id,
            order_ref=entry.order_ref,
            leg=entry.leg,
            clock=clock,
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
        clock: Clock = _now_ms,
    ) -> None:
        self._read = read
        self._trade = trade
        self._clock = clock
        self._intake_lock = asyncio.Lock()
        # Recovery owns historical, already-minted refs. Keep concurrent sweep
        # replays serial without making a slow by-client-id lookup block cancel.
        self._recovery_lock = asyncio.Lock()
        self._account_id: str | None = None
        self._journal: OrderJournal | None = None
        # S4 observable counter: unexplained (foreign/absent-coid) lifecycle
        # events. S6 reads this (and the UNEXPLAINED_ORDER lines) to raise the
        # exposure hold; S4 only counts.
        self._unexplained_order_count = 0

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
        """Submit legs serially, stopping before any later leg after uncertainty.

        The exposure hold (S6) is checked FIRST, under the intake lock, BEFORE
        any intent is minted or journaled — capture-before-submit means a refused
        submit records NO intent. When held, a ``BrokerSubmissionHeld`` (409,
        ``UNEXPLAINED_ORDER_HOLD``) propagates to the router. Cancel is a separate
        path and is never held (reducing exposure is always allowed). A
        definitive rejection is independent; an ``uncertain`` result means the
        previous leg may have landed, so remaining request legs stay unsent.
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            hold = derive.hold_state(journal.read_entries())
            if hold.active:
                logger.warning(
                    "alpaca clerk refused a submit: exposure hold is active",
                    extra={
                        "action": "submit_refused_hold",
                        "account_id": account_id,
                        "reason_code": hold.reason_code,
                    },
                )
                raise BrokerSubmissionHeld(
                    "Order submission is paused while an exposure hold is active.",
                    reason_code=hold.reason_code or UNEXPLAINED_ORDER_HOLD_CODE,
                    broker=self.broker_id,
                    detail=hold.reason,
                )
            results: list[OrderLegResult] = []
            for leg in request.legs:
                result = await self._submit_leg(request.operator, leg, account_id, journal)
                results.append(result)
                if result.status == "uncertain":
                    logger.warning(
                        "alpaca clerk stopped multi-leg submit after uncertain outcome",
                        extra={
                            "action": "submit_batch_stopped_uncertain",
                            "account_id": account_id,
                            "order_ref": result.order_ref,
                            "remaining_legs": len(request.legs) - len(results),
                        },
                    )
                    break
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
                    recorded_at_ms=self._clock(),
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

    # ── S4 live-lifecycle path (trade_updates websocket) ─────────────────────

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None = None,
    ) -> ClerkEntryKind:
        """Journal one parsed ``trade_updates`` lifecycle event, with attribution.

        The consumer captures the raw frame verbatim, parses it to a
        ``BrokerOrderEvent`` (via the adapter), and hands it here with the
        wire's ``client_order_id`` and a stable ``event_key`` (the dedup key the
        consumer already resolved: ``execution_id`` for a fill, else a synthetic
        ``order_id|event|timestamp``).

        Attribution runs against **this Clerk's known namespaces** using the
        canonical ``order_ref_namespace_matches`` — exact namespace equality,
        never a prefix. OWNED (``client_order_id`` namespace is ours) → an
        ``ORDER_EVENT`` line; UNOWNED / foreign / absent / unparseable →
        an ``UNEXPLAINED_ORDER`` line plus the observable
        :pyattr:`unexplained_order_count` counter.

        **S6 seam:** the exposure hold that blocks *new submits* on an
        unexplained order is NOT implemented here — S4 only records the
        observation. S6 reads these ``UNEXPLAINED_ORDER`` lines (and/or the
        counter) to raise the hold on ``submit``. Do not couple this to submit.
        Returns the kind journaled (test/observability seam).
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            owned = order_ref_namespace_matches(
                client_order_id, self._known_namespaces(journal)
            )
            kind = (
                ClerkEntryKind.ORDER_EVENT if owned else ClerkEntryKind.UNEXPLAINED_ORDER
            )
            owning = (
                self._resolve_owning_entry_by_ref(client_order_id, journal)
                if owned and client_order_id is not None
                else None
            )
            journal.append(
                OrderJournalEntry(
                    kind=kind,
                    account_id=account_id,
                    operator=owning.operator if owning is not None else "",
                    intent_id=owning.intent_id if owning is not None else "",
                    order_ref=owning.order_ref if owning is not None else "",
                    client_order_id=client_order_id or "",
                    leg=owning.leg if owning is not None else None,
                    broker_order_id=order.order_id if order is not None else None,
                    owned=owned,
                    recorded_at_ms=self._clock(),
                    order=order,
                    event=event,
                    event_key=event_key,
                )
            )
            if not owned:
                self._unexplained_order_count += 1
                logger.warning(
                    "alpaca clerk observed an unexplained order lifecycle event",
                    extra={
                        "action": "unexplained_order",
                        "account_id": account_id,
                        "client_order_id": client_order_id,
                        "event": event.event_type,
                        "event_key": event_key,
                    },
                )
                # S6 seam: an unexplained order is a safety event — raise the
                # account exposure hold so new submits are refused until an
                # operator clears it. Idempotent: a second unexplained event does
                # not re-journal an already-active HOLD_SET.
                self._set_hold(
                    journal,
                    account_id=account_id,
                    reason_code=UNEXPLAINED_ORDER_HOLD_CODE,
                    reason=(
                        "An order this account did not submit was observed at "
                        "Alpaca. Submission is paused until an operator confirms "
                        "the account is safe."
                    ),
                )
            return kind

    @property
    def unexplained_order_count(self) -> int:
        """Observable counter: lifecycle events on orders this Clerk did not own."""
        return self._unexplained_order_count

    def _known_namespaces(self, journal: OrderJournal) -> frozenset[str]:
        """The manual-order namespaces this Clerk has minted, from the journal.

        Every owned order's ``order_ref`` parses to ``manual/{operator}/v1``;
        the set of those namespaces is the allowlist attribution matches against
        (exact equality). Rebuilt from the ledger so it survives a restart —
        the journal is the durable source of what this Clerk owns.
        """
        namespaces: set[str] = set()
        for entry in journal.read_entries():
            if not entry.order_ref:
                continue
            try:
                namespace, _ = parse_order_ref(entry.order_ref)
            except ValueError:
                continue
            namespaces.add(namespace)
        return frozenset(namespaces)

    @staticmethod
    def _resolve_owning_entry_by_ref(
        client_order_id: str, journal: OrderJournal
    ) -> OrderJournalEntry | None:
        """Find the owning submit entry for a client_order_id (== order_ref).

        Returns the most recent submit-side entry (``submit_acked`` preferred,
        else ``intent_recorded``) whose ``order_ref`` matches, so the event line
        can copy the originating identity + leg. ``None`` when unresolvable.
        """
        owning: OrderJournalEntry | None = None
        for entry in journal.read_entries():
            if (
                entry.kind
                in (ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.INTENT_RECORDED)
                and entry.order_ref == client_order_id
            ):
                owning = entry
        return owning

    # ── S6 exposure hold (account-level, journal-derived) ────────────────────

    def is_on_hold(self) -> bool:
        """True when an account-level exposure hold is active (journal-derived)."""
        # A read-only accessor for observability; the authoritative gate is the
        # under-lock check inside :meth:`submit`.
        if self._journal is None:
            return False
        return derive.hold_state(self._journal.read_entries()).active

    async def status(self) -> ClerkStatus:
        """The clerk's observable state (hold + latest verdict + outstanding
        intents): a journal-derived read under the intake lock, so it survives a
        restart and never observes a torn mid-write ledger."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            return self._status_from(account_id, journal.read_entries())

    async def clear_hold(self, *, operator: str, reason: str) -> ClerkStatus:
        """Clear the exposure hold (operator exit); journal ``HOLD_CLEARED``.

        Idempotent and benign when not held — a clear against no active hold is a
        NO-OP with no journal write, so a double-click never litters the ledger.
        Returns the updated status so the caller renders it in one round-trip.
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            entries = journal.read_entries()
            hold = derive.hold_state(entries)
            if hold.active:
                journal.append(
                    OrderJournalEntry(
                        kind=ClerkEntryKind.HOLD_CLEARED,
                        account_id=account_id,
                        operator=operator,
                        reason_code=hold.reason_code or UNEXPLAINED_ORDER_HOLD_CODE,
                        reason=reason,
                        recorded_at_ms=self._clock(),
                    )
                )
                logger.info(
                    "alpaca clerk cleared the exposure hold",
                    extra={
                        "action": "hold_cleared",
                        "account_id": account_id,
                        "operator": operator,
                        "reason_code": hold.reason_code,
                    },
                )
                entries = journal.read_entries()
            else:
                logger.info(
                    "alpaca clerk clear-hold was a no-op: no active hold",
                    extra={"action": "hold_clear_noop", "account_id": account_id},
                )
            return self._status_from(account_id, entries)

    def _status_from(
        self, account_id: str, entries: list[OrderJournalEntry]
    ) -> ClerkStatus:
        """The clerk's ``ClerkStatus`` from a pre-read ledger (adds the intent scan)."""
        return derive.build_status(
            entries,
            broker_id=self.broker_id,
            account_id=account_id,
            outstanding_intents=len(derive.unresolved_intents(entries)),
            observed_at_ms=self._clock(),
        )

    def _set_hold(
        self,
        journal: OrderJournal,
        *,
        account_id: str,
        reason_code: str,
        reason: str,
    ) -> None:
        """Raise the exposure hold; idempotent (never double-journal HOLD_SET).

        Callers already hold the intake lock (the S4 lifecycle path and the sweep
        both wrap this). A ``HOLD_SET`` is journaled only when no hold is already
        active, so a repeated unexplained observation does not litter the ledger.
        """
        last_explicit_hold: ClerkEntryKind | None = None
        for entry in journal.read_entries():
            if entry.kind in (ClerkEntryKind.HOLD_SET, ClerkEntryKind.HOLD_CLEARED):
                last_explicit_hold = entry.kind
        # An unexplained observation itself derives a fail-closed hold, but it
        # still needs the companion HOLD_SET audit receipt. Only an un-cleared
        # explicit receipt makes this append redundant.
        if last_explicit_hold is ClerkEntryKind.HOLD_SET:
            return
        journal.append(
            OrderJournalEntry(
                kind=ClerkEntryKind.HOLD_SET,
                account_id=account_id,
                reason_code=reason_code,
                reason=reason,
                recorded_at_ms=self._clock(),
            )
        )
        logger.warning(
            "alpaca clerk set an exposure hold; new submits refused",
            extra={
                "action": "hold_set",
                "account_id": account_id,
                "reason_code": reason_code,
            },
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
        identity = _LegIdentity(
            account_id, operator, intent_id, order_ref, leg, self._clock
        )

        # No journal → no order: record + fsync the intent BEFORE the broker call.
        journal.append(identity.entry(ClerkEntryKind.INTENT_RECORDED))

        try:
            order = await self._trade.submit(leg, client_order_id=order_ref)
        except BrokerUnavailable as exc:
            # S5 UNCERTAIN: the response may have been lost (timeout / 5xx /
            # network), so the order MAY have landed. The intent is already
            # durable; journal the uncertainty, then resolve by asking the vendor
            # whether the order actually exists. A resolution that is itself
            # uncertain leaves the intent at ``submit_uncertain`` for startup
            # replay / a later sweep to finish — never a fabricated terminal.
            journal.append(
                identity.entry(ClerkEntryKind.SUBMIT_UNCERTAIN, error=_leg_error(exc))
            )
            logger.warning(
                "alpaca clerk submit outcome uncertain; resolving by client_order_id",
                extra={
                    "action": "submit_uncertain",
                    "account_id": account_id,
                    "order_ref": order_ref,
                    "symbol": leg.symbol,
                    "why": exc.detail,
                },
            )
            return await self._resolve_intent(
                identity, journal, terminal_on_absence=False
            )
        except BrokerError as exc:
            # Every other BrokerError (invalid 4xx, rejected 409, auth, rate
            # limit) is a DEFINITIVE failure — the order did not land.
            failure = _leg_error(exc)
            journal.append(identity.entry(ClerkEntryKind.SUBMIT_FAILED, error=failure))
            return OrderLegResult(
                status="failed",
                order_ref=order_ref,
                intent_id=intent_id,
                error=failure,
            )

        journal.append(identity.entry(ClerkEntryKind.SUBMIT_ACKED, order=order))
        return OrderLegResult(
            status="acked", order_ref=order_ref, intent_id=intent_id, order=order
        )

    # ── S5 uncertain-submit resolution + startup replay ──────────────────────

    async def recover(self) -> None:
        """Replay the journal and resolve every unfinished intent (S5).

        Called on startup BEFORE the platform accepts new submits: an intent left
        at ``intent_recorded`` or ``submit_uncertain`` (a crash between the
        durable intent and its terminal outcome) is finished by the same
        ``client_order_id`` resolution the write path uses. Idempotent — safe to
        call repeatedly; an already-terminal intent is a NO-OP.

        A fresh install (no journal yet) resolves nothing and returns cleanly.
        Each unresolved intent is resolved independently; one leg that stays
        uncertain (the lookup is itself unreachable) does not block the others,
        and is left for a later replay / sweep.
        """
        async with self._recovery_lock:
            # Protect only the journal snapshot with the intake lock. The remote
            # lookups below intentionally run after release so a slow recovery
            # cannot delay a cancel that reduces exposure.
            async with self._intake_lock:
                account_id, journal = await self._ensure_journal()
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
                identity = _LegIdentity.from_entry(intent_entry, clock=self._clock)
                await self._resolve_intent(
                    identity,
                    journal,
                    terminal_outcomes=terminal_outcomes,
                    uncertain_recorded_at_ms=uncertain_recorded_at.get(
                        identity.order_ref
                    ),
                )

    async def _resolve_intent(
        self,
        identity: _LegIdentity,
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
            order = await self._trade.get_order_by_client_order_id(identity.order_ref)
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
            and self._clock() - uncertain_recorded_at_ms < UNCERTAIN_SUBMIT_GRACE_MS
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
            journal.append(identity.entry(ClerkEntryKind.SUBMIT_FAILED, error=failure))
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

        journal.append(identity.entry(ClerkEntryKind.SUBMIT_ACKED, order=order))
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

    # ── S6 reconciliation sweep ──────────────────────────────────────────────

    async def reconcile_once(self) -> ReconciliationVerdict:
        """Run one reconciliation pass; journal a named verdict and return it.

        First replay unresolved S5 submits so a long-running process does not
        leave terminal outcomes stranded until restart. Then read Alpaca
        *without* the intake lock so cancels remain reachable during a slow
        sweep, and reacquire only to derive and append the latest durable
        reconciliation result.
        """
        await self.recover()

        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()

        try:
            orders = await self._read.list_orders(status="all", limit=500)
            positions = await self._read.list_positions()
        except BrokerError as exc:
            logger.warning(
                "alpaca clerk reconciliation could not read the broker; stale",
                extra={
                    "action": "reconcile_stale",
                    "account_id": account_id,
                    "why": exc.detail,
                },
            )
            async with self._intake_lock:
                _, journal = await self._ensure_journal()
                plan = reconcile.plan_stale(
                    journal.read_entries(), account_id=account_id, now_ms=self._clock()
                )
                return self._apply_reconcile_plan(journal, account_id, plan)

        working_orders = [
            order
            for order in orders
            if order.status.lower() not in _RECONCILIATION_TERMINAL_ORDER_STATUSES
        ]
        async with self._intake_lock:
            _, journal = await self._ensure_journal()
            # The current ledger is authoritative because submits/cancels can
            # have completed while Alpaca was being read.
            current_entries = journal.read_entries()
            plan = reconcile.plan(
                current_entries,
                working_orders,
                positions,
                self._known_namespaces(journal),
                account_id=account_id,
                now_ms=self._clock(),
            )
            # A non-clean verdict is operationally notable (WARNING); clean is INFO.
            logger.log(
                logging.INFO if plan.verdict == "clean" else logging.WARNING,
                "alpaca clerk reconciliation: %s",
                plan.verdict,
                extra={
                    "action": "reconcile",
                    "account_id": account_id,
                    "verdict": plan.verdict,
                    "new_unexplained": plan.new_unexplained_count,
                },
            )
            return self._apply_reconcile_plan(journal, account_id, plan)

    def _apply_reconcile_plan(
        self, journal: OrderJournal, account_id: str, plan: reconcile.ReconcilePlan
    ) -> ReconciliationVerdict:
        """Apply a pure :class:`reconcile.ReconcilePlan` under the intake lock:
        append its (deduped, verdict-on-change) entries, advance the counter by
        the *new*-unexplained count only, and raise the hold when it calls for one
        (``_set_hold`` is idempotent, so a persistent foreign order does not
        re-journal HOLD_SET)."""
        for entry in plan.entries_to_append:
            journal.append(entry)
        self._unexplained_order_count += plan.new_unexplained_count
        if plan.set_hold:
            self._set_hold(
                journal,
                account_id=account_id,
                reason_code=UNEXPLAINED_ORDER_HOLD_CODE,
                reason=(
                    "The reconciliation sweep found an order this account did not "
                    "submit at Alpaca. Submission is paused until an operator "
                    "confirms the account is safe."
                ),
            )
        return plan.verdict


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
