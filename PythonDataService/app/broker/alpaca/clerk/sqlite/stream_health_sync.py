"""Independent fixed-cadence lifecycle for the stream-health account hold.

#1777 WP4, finding S10. The hold used to be raised and released only
inside ENTER-purpose effect execution, which made it three things it
should never have been: account-wide blast radius from one sample,
revision churn on every retry of an unchanged outage, and -- worst --
unreleasable on a quiet fleet, because releasing required an ENTER that
was itself blocked by the hold.

Ownership is now split cleanly:

- **This sync owns the durable hold.** Fixed cadence, decoupled from the
  reconciliation pass (whose backoff reaches 300 s on failure, precisely
  when holds matter most). Both providers are process-local, so no broker
  I/O is involved and a broker outage cannot delay raise or release.
- **ENTER owns its own refusal.** The entry-time check is unchanged and
  still refuses immediately on unhealthy channels; it simply no longer
  writes the account-scoped record. That is the PRD's governing rule:
  state is produced by one background tap and consumed by another.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.broker.alpaca.clerk.hold_debounce import (
    HoldDebounceState,
    HoldSyncAction,
    advance_hold_debounce,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    raise_account_hold,
    resolve_account_hold,
)
from app.broker.alpaca.clerk.stream_health import (
    HOLD_SYNC_INTERVAL_S,
    STREAM_HEALTH_REASON_CODE,
    StreamHealthGate,
    channel_evidence_refs,
    sample_channels,
)

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]


class StreamHealthHoldSync:
    """Drives the stream-health hold on its own cadence.

    :meth:`tick` is the whole decision: it is synchronous, does one
    observation, and returns what it did -- which is what lets the timing
    promises be asserted over virtual time without sleeping.
    """

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        gate: StreamHealthGate | None,
        interval_s: float = HOLD_SYNC_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
        max_ticks: int | None = None,
    ) -> None:
        self._repo = repo
        self._gate = gate
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_ticks = max_ticks
        self._state = HoldDebounceState()
        # None = unknown (fresh process, a hold may already stand in the
        # journal). Anything but False must still reach the ledger.
        self._hold_stands: bool | None = None
        self._task: asyncio.Task[None] | None = None

    def tick(self) -> HoldSyncAction:
        """Observe once, fold into the debounce, and act on the verdict."""
        # Account-scoped, so this reads the unscoped snapshot and judges it
        # with the canonical `account_scope_satisfied` -- market data on
        # connectivity (its unscoped *usability* verdict folds per-symbol
        # warmup, which would freeze every bot on one symbol -- finding S6,
        # at account scope), execution on health (it has no per-symbol
        # dimension, so an unusable evidence frame is broken account-wide).
        channels = None if self._gate is None else self._gate.snapshot()
        step = advance_hold_debounce(self._state, sample_channels(channels))
        self._state = step.state

        if step.action == "raise" and channels is not None:
            self._raise(channel_evidence_refs(channels))
            self._hold_stands = True
        elif step.action == "release" and self._hold_stands is not False:
            self._release()
            self._hold_stands = False
        return step.action

    def _raise(self, evidence_refs: list[str]) -> None:
        # `raise_account_hold` inherits the uncertainty path's
        # append-on-change-only gate: an unchanged envelope appends nothing,
        # so a persisting outage costs one append, not one per sample.
        outcome = raise_account_hold(
            self._repo,
            reason_code=STREAM_HEALTH_REASON_CODE,
            evidence_refs=evidence_refs,
        )
        if outcome != "unchanged":
            logger.warning(
                "Stream-health hold %s",
                outcome,
                extra={
                    "action": f"stream_health_hold_{outcome}",
                    "evidence_refs": evidence_refs,
                },
            )

    def _release(self) -> None:
        # Idempotent: a no-op while no episode is active, so repeated healthy
        # samples append nothing.
        released = resolve_account_hold(
            self._repo,
            reason_code=STREAM_HEALTH_REASON_CODE,
            summary_code="ACCOUNT_HOLD_RESOLVED_BY_STREAM_RECOVERY",
        )
        if released:
            logger.info(
                "Stream-health hold released",
                extra={"action": "stream_health_hold_released"},
            )

    async def run(self) -> None:
        ticks = 0
        while self._max_ticks is None or ticks < self._max_ticks:
            try:
                self.tick()
            except Exception:
                # A failed observation must not kill the loop -- an
                # unattended dead sync is how a hold becomes permanent.
                logger.exception(
                    "Stream-health hold sync tick failed",
                    extra={"action": "stream_health_hold_sync_failed"},
                )
            ticks += 1
            await self._sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(), name="alpaca-sqlite-stream-health-hold-sync"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = [
    "STREAM_HEALTH_REASON_CODE",
    "StreamHealthHoldSync",
]
