"""Periodic driver for the ordered account reconciliation pass."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_ports
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
    reconcile_account,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

logger = logging.getLogger(__name__)
type Sleep = Callable[[float], Awaitable[None]]
# Notified with each completed pass's verdict. This is how the reads that
# project custody learn what the sweep found -- the sweep, not the facade,
# is the sole automatic reconciler (#1776 WP2).
type ReconciliationListener = Callable[[AccountReconciliationResult], object]
# Awaited after each successful custody pass, once the verdict is published.
# "Successful" is strict: a ``stale`` verdict skips the hook entirely, so the
# hook never spends its own broker timeouts on a broker custody just failed to
# reach. The one consumer today is the symbol-validity probe (#1795): custody
# always reconciles first, and a hook failure is isolated -- it never turns a
# succeeded custody pass into a backoff.
type AfterPassHook = Callable[[], Awaitable[None]]
# Awaited once after a successful lease revival (ADR 0050): closes the
# terminal-evidence hole in-process by re-running the boot scan's lifecycle
# repair. A hook failure is isolated -- the lease stays revived and the next
# boot scan remains the backstop.
type LeaseRevivedHook = Callable[[], Awaitable[None]]
# Renew the execution lease three times per TTL. This is a safety-margin
# choice, not ported math: at 3x cadence a single missed renewal (transient
# disk stall, scheduler delay) still leaves ~2/3 of the TTL before the lease
# expires and writes fail closed. Pinned by test_reconcile.py's cadence assert.
# Rationale: docs/references/alpaca-sqlite-clerk-lease-heartbeat-cadence.md
_LEASE_HEARTBEATS_PER_TTL = 3


class ReconciliationSweep:
    """Periodic account recovery loop with deterministic test seams."""

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        interval_s: float = 15.0,
        max_backoff_s: float = 300.0,
        sleep: Sleep = asyncio.sleep,
        lease_sleep: Sleep = asyncio.sleep,
        max_passes: int | None = None,
        intake: ReentrantAsyncLock | None = None,
        on_result: ReconciliationListener | None = None,
        after_pass: AfterPassHook | None = None,
        on_lease_revived: LeaseRevivedHook | None = None,
    ) -> None:
        self._repo = repo
        self._on_result = on_result
        self._after_pass = after_pass
        self._on_lease_revived = on_lease_revived
        self._intake = intake or ReentrantAsyncLock()
        self._read, self._trade = guard_broker_ports(read=read, trade=trade, intake=self._intake)
        self._interval_s = interval_s
        self._max_backoff_s = max(max_backoff_s, interval_s)
        self._sleep = sleep
        self._lease_sleep = lease_sleep
        self._lease_heartbeat_interval_s = max(
            repo.lease_ttl_ms / (_LEASE_HEARTBEATS_PER_TTL * 1000),
            0.001,
        )
        self._max_passes = max_passes
        self._task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None

    def set_on_lease_revived(self, hook: LeaseRevivedHook | None) -> None:
        """Late-bind the post-revival recovery hook (ADR 0050).

        The sweep is constructed with the authority, before the bot task
        registry exists; main.py binds the recovery pass here right before
        :meth:`start`, mirroring how the sweep itself is started late.
        """
        self._on_lease_revived = hook

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="alpaca-sqlite-reconciliation-sweep"
            )
        self.start_lease_heartbeat()

    def start_lease_heartbeat(self) -> None:
        """Begin (or ensure) only the execution-lease heartbeat.

        Started as soon as the repository is acquired — before the startup
        recovery passes and before the reconcile loop — so a slow
        clean-account boot that only reads from the broker cannot let the
        lease expire before the sweep exists. Idempotent: a later
        :meth:`start` reuses the already-running task.
        """
        if self._lease_task is None or self._lease_task.done():
            self._lease_task = asyncio.create_task(
                self._run_lease_heartbeat(),
                name="alpaca-sqlite-execution-lease-heartbeat",
            )

    async def stop(self) -> None:
        tasks = tuple(
            task for task in (self._task, self._lease_task) if task is not None
        )
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._lease_task = None

    async def _run_lease_heartbeat(self) -> None:
        """Renew forever; exit only on a store-proven lease loss (ADR 0050).

        Before ADR 0050 any exception here ended the heartbeat permanently,
        so one transient disk stall — or a process freeze past the TTL —
        bricked the account handle until a container restart (T7,
        2026-08-26). Now a transient failure retries next tick, an expired
        lease attempts the fenced revival, and only a revival the store
        refuses (another writer or an authority ceremony held the account)
        ends the loop with writes permanently fail-closed.
        """
        while True:
            await self._lease_sleep(self._lease_heartbeat_interval_s)
            try:
                await asyncio.to_thread(self._repo.renew_execution_lease)
            except asyncio.CancelledError:
                raise
            except ExecutionLeaseLost:
                logger.critical(
                    "alpaca sqlite execution lease expired; attempting supervised revival",
                    extra={
                        "action": "execution_lease_heartbeat_failed",
                        "account_id": self._repo.account_id,
                    },
                    exc_info=True,
                )
                if not await self._attempt_lease_revival():
                    return
            except Exception:
                logger.critical(
                    "alpaca sqlite execution lease heartbeat errored; retrying "
                    "(writes remain independently fail-closed)",
                    extra={
                        "action": "execution_lease_heartbeat_transient_error",
                        "account_id": self._repo.account_id,
                    },
                    exc_info=True,
                )

    async def revive_now(self) -> bool:
        """One fenced ADR 0050 revival attempt -- the sole implementation.

        Both the lease heartbeat (:meth:`_attempt_lease_revival`) and any
        write path that discovers a lost lease in the same instant (a panel
        action -- see ``panel_data_source._revive_lease_or_raise``) call this
        and only this, so the CAS (``ClerkSqliteRepository.revive_execution_lease``),
        its CRITICAL logging, and firing ``on_lease_revived`` on success never
        drift into two implementations that could disagree on the outcome
        vocabulary or the ADR 0050 §3 post-revival recovery pass
        (``BotTaskRegistry.run_lease_recovery``, which repairs lifecycle
        artifacts for runs that died during the freeze).

        That does not make the two call sites single-flighted, and this
        method takes no lock: a concurrent heartbeat tick can call this on
        the same sweep instance at the same moment a write path does, so the
        CAS can run twice and the hook can fire twice. Both are harmless.
        The CAS's condition (owner unchanged AND generation unchanged) does
        not become false once satisfied -- a second concurrent revival still
        matches it and simply re-extends the same expiry, which is exactly
        what a renewal does anyway. A second hook invocation either repeats
        an idempotent step (``AlpacaLifecycleProjector.refresh`` for a run
        still live) or loses a race to re-validate a stop that the other
        invocation already committed (``stop_interrupted_alpaca_duty_run``
        re-checks ``sqlite_active`` immediately before its own commit) and
        raises -- caught here and logged, isolated, with the next boot scan
        remaining the backstop, exactly like any other hook failure.

        A synthetic authority's carve-out (no hook bound at its construction
        site -- see ``active_authority.py``'s synthetic branch) is real for a
        caller that already holds a reference to *that* sweep instance, but
        it is not "inherited" by the write path: ``active_reconciliation_sweep``
        gates on ``authority_kind == "sqlite"``, identically to
        ``active_sqlite_facade``, so the write path never reaches a synthetic
        sweep's ``revive_now()`` at all -- it is refused earlier, as "no
        supervised revival available for this authority".

        Returns ``True`` once the lease is confirmed revived and the hook (if
        any) has run or failed in isolation. Raises :class:`ExecutionLeaseLost`
        when the store proves revival is impossible (another writer or an
        authority ceremony held the account) -- callers translate that into
        their own terminal handling. Any other exception is a transient store
        error and propagates uninterpreted: the CAS never reached a confirmed
        outcome, so no lease/hook logging happens for it.

        The unit is cancellation-safe from the caller's side. The write path
        awaits this from an HTTP request a client can abandon mid-flight, and
        the CAS runs on a worker thread that a cancelled ``await`` does not
        unwind: the lease can come back revived after the caller is already
        gone. If the cancellation were simply propagated, the CRITICAL record
        and ``on_lease_revived`` would never run, and the heartbeat -- seeing
        a healthy lease -- would only renew it, leaving the lifecycle
        artifacts of runs that died during the freeze unrepaired until the
        next boot scan. So the CAS-log-hook unit runs as its own task, the
        caller observes it under ``asyncio.shield``, and a cancelled caller
        waits for the unit to finish before honouring the cancellation.
        """
        revival = asyncio.ensure_future(self._revive_and_repair())
        try:
            return await asyncio.shield(revival)
        except asyncio.CancelledError:
            await self._finish_orphaned_revival(revival)
            raise

    async def _finish_orphaned_revival(self, revival: asyncio.Future[bool]) -> None:
        """Let a revival whose caller was cancelled reach its outcome.

        The unit's own logging is the record of that outcome; an error is
        logged here only because the caller who would have received it is
        gone. A second cancellation while waiting (shutdown) propagates and
        leaves the unit running on the loop.
        """
        try:
            await asyncio.shield(revival)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "execution lease revival errored after its caller was cancelled",
                extra={
                    "action": "execution_lease_revival_orphaned_error",
                    "account_id": self._repo.account_id,
                },
                exc_info=True,
            )

    async def _revive_and_repair(self) -> bool:
        """The CAS, its CRITICAL record, and the recovery hook, as one unit."""
        try:
            await asyncio.to_thread(self._repo.revive_execution_lease)
        except asyncio.CancelledError:
            raise
        except ExecutionLeaseLost:
            logger.critical(
                "alpaca sqlite execution lease is unrecoverable; writes remain "
                "fail-closed until the data plane restarts (ADR 0047)",
                extra={
                    "action": "execution_lease_revival_refused",
                    "account_id": self._repo.account_id,
                },
                exc_info=True,
            )
            raise
        logger.critical(
            "alpaca sqlite execution lease revived after expiry; no other "
            "writer held the account (ADR 0050)",
            extra={
                "action": "execution_lease_revived",
                "account_id": self._repo.account_id,
            },
        )
        if self._on_lease_revived is not None:
            try:
                await self._on_lease_revived()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Isolated on purpose: the lease is revived either way, and
                # the next boot scan remains the backstop for whatever the
                # failed recovery pass left open.
                logger.error(
                    "post-revival recovery hook errored; boot scan remains the backstop",
                    extra={
                        "action": "execution_lease_revival_hook_error",
                        "account_id": self._repo.account_id,
                    },
                    exc_info=True,
                )
        return True

    async def _attempt_lease_revival(self) -> bool:
        """One fenced revival attempt via :meth:`revive_now`; ``False`` means
        proven-terminal loss and the heartbeat should exit."""
        try:
            return await self.revive_now()
        except asyncio.CancelledError:
            raise
        except ExecutionLeaseLost:
            return False
        except Exception:
            # Transient store error: stay alive. The next tick's renewal will
            # raise ExecutionLeaseLost again and land back here.
            logger.critical(
                "alpaca sqlite execution lease revival errored; retrying",
                extra={
                    "action": "execution_lease_revival_transient_error",
                    "account_id": self._repo.account_id,
                },
                exc_info=True,
            )
            return True

    async def run(self) -> None:
        passes = 0
        consecutive_failures = 0
        while True:
            succeeded = await self._run_one_pass()
            consecutive_failures = 0 if succeeded else consecutive_failures + 1
            passes += 1
            if self._max_passes is not None and passes >= self._max_passes:
                return
            delay = (
                self._interval_s
                if consecutive_failures == 0
                else min(
                    self._interval_s * (2**consecutive_failures),
                    self._max_backoff_s,
                )
            )
            await self._sleep(delay)

    async def _run_one_pass(self) -> bool:
        try:
            result = await reconcile_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
                intake=self._intake,
            )
            if self._on_result is not None:
                self._on_result(result)
            succeeded = result.verdict != "stale"
            # Custody first, evidence second -- and only once custody actually
            # succeeded. A "stale" verdict means the broker snapshot itself did
            # not arrive, so running the hook here would spend its own per-probe
            # timeouts against the same unreachable broker and delay the next
            # custody-recovery attempt before backoff even begins.
            if succeeded and self._after_pass is not None:
                try:
                    await self._after_pass()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Isolated on purpose: the custody pass above succeeded,
                    # and a probe failure must not push the sweep into backoff.
                    logger.warning(
                        "reconciliation sweep after-pass hook errored; continuing",
                        extra={
                            "action": "reconcile_sweep_after_pass_error",
                            "account_id": self._repo.account_id,
                        },
                        exc_info=True,
                    )
            return succeeded
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "alpaca sqlite reconciliation sweep pass errored; retrying",
                extra={"action": "reconcile_sweep_error", "account_id": self._repo.account_id},
                exc_info=True,
            )
            return False


__all__ = ["AfterPassHook", "LeaseRevivedHook", "ReconciliationListener", "ReconciliationSweep"]
