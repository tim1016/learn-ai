"""Periodic reconciliation sweep loop (Alpaca phase 2, S6).

Drives :meth:`AlpacaClerk.reconcile_once` on a fixed cadence. The loop is a thin
lifecycle wrapper (start / stop as a background task, mirroring the S4
``TradeUpdatesConsumer``) — every reconciliation decision lives in the Clerk, so
the sweep is fully deterministic under an **injected** ``sleep`` seam: a test
drives exactly one pass with no real timer, and a ``max_passes`` budget bounds
the loop so an injected fake terminates.

Reconciliation is observational — it gates no bot lifecycle (there are no bots)
— with the single exception, owned by the Clerk, that an unexplained order raises
the account exposure hold. This loop only schedules; it never interprets a
verdict.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.contract.errors import BrokerError

logger = logging.getLogger(__name__)

# An injectable inter-pass wait. Production sleeps the configured interval; a test
# injects a no-op (or a one-shot event) so the loop is deterministic and fast.
type Sleep = Callable[[float], Awaitable[None]]

_DEFAULT_INTERVAL_S = 15.0


class ReconciliationSweep:
    """Background loop that runs one Clerk reconciliation pass per interval.

    The core is transport-agnostic: it is driven by an injectable :type:`Sleep`
    and an optional ``max_passes`` budget so tests run a bounded, timer-free loop.
    A per-pass ``BrokerError`` (already surfaced as a ``stale`` verdict inside the
    Clerk) or any unexpected error is logged and the loop continues — the sweep
    never dies silently.
    """

    def __init__(
        self,
        *,
        clerk: AlpacaClerk,
        interval_s: float = _DEFAULT_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
        max_passes: int | None = None,
    ) -> None:
        self._clerk = clerk
        self._interval_s = interval_s
        self._sleep = sleep
        # ``None`` = run forever (production). A finite value bounds tests.
        self._max_passes = max_passes
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the sweep loop as a background task (lifespan wiring)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="alpaca-reconciliation-sweep")

    async def stop(self) -> None:
        """Cancel the sweep task and wait for it to unwind cleanly."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        """Sweep loop: one pass, then sleep the interval, until cancelled/budgeted.

        ``asyncio.CancelledError`` propagates so a lifespan shutdown stops the
        loop immediately. Every other error is surfaced and the loop continues —
        a single bad pass never silences the sweep.
        """
        passes = 0
        while True:
            await self._run_one_pass()
            passes += 1
            if self._max_passes is not None and passes >= self._max_passes:
                return
            await self._sleep(self._interval_s)

    async def _run_one_pass(self) -> None:
        try:
            await self._clerk.reconcile_once()
        except asyncio.CancelledError:
            raise
        except BrokerError:
            # A broker failure is already recorded as a ``stale`` verdict inside
            # the Clerk; surface it here too so the loop's health is observable.
            logger.warning(
                "alpaca reconciliation sweep pass hit a broker error",
                extra={"action": "reconcile_sweep_broker_error"},
                exc_info=True,
            )
        except Exception:
            logger.warning(
                "alpaca reconciliation sweep pass errored; will retry next interval",
                extra={"action": "reconcile_sweep_error"},
                exc_info=True,
            )


_sweep: ReconciliationSweep | None = None


def get_reconciliation_sweep() -> ReconciliationSweep | None:
    """Return the process-wide sweep, or ``None`` when not started."""
    return _sweep


def set_reconciliation_sweep(sweep: ReconciliationSweep | None) -> None:
    """Install (or clear) the process-wide sweep — lifespan wiring."""
    global _sweep
    _sweep = sweep


def reset_reconciliation_sweep_for_testing() -> None:
    """Drop the process-wide sweep so a test starts clean."""
    global _sweep
    _sweep = None
