"""Background reconnect monitor for the IBKR client.

The IBKR Gateway and its uplinks are not stable enough to rely on a
single-shot ``connect()`` for the lifetime of a process. Two classes of
drop happen in practice:

* **Hard close** — the asyncio transport is gone; ``isConnected()``
  flips to False. The ``ib_async`` library does not surface a separate
  signal beyond that flip.
* **Soft loss** (TWS Error 1100 / 504) — the API socket stays open but
  the data feed is dead. ``IbkrClient`` tracks this as
  ``connection_lost`` so streaming loops can halt.

Both cases used to require an operator click on the cockpit to recover.
This module replaces that with an asyncio background task that polls
the client, observes either failure mode, and reconnects with
exponential backoff. Existing manual reconnect controls keep working —
the asyncio lock in ``routers.broker`` serialises monitor-driven and
operator-driven reconnects, so they never race.

The monitor's published state (``is_attempting``, ``attempt_number``,
last transition timestamp) is read by ``IbkrConnectionHealth`` so the
cockpit can render "Reconnecting (attempt 3)" without losing fidelity
between polls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.broker.ibkr.client import IbkrClient

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


class AutoReconnectMonitor:
    """Background task that watches an ``IbkrClient`` and reconnects on loss.

    Lifecycle:

    1. ``start()`` spawns the monitor task. Idempotent — re-starts a
       previously-stopped monitor cleanly.
    2. The task polls every ``poll_interval_s`` seconds. On observing
       either ``not client.is_connected()`` OR ``client.connection_lost``,
       it transitions the client to ``reconnecting`` and calls
       ``client.connect()`` with exponential backoff
       (``initial_backoff_s`` doubling per failure up to ``max_backoff_s``).
    3. Each attempt is bracketed by ``client._mark_reconnect_started``
       and ``client._mark_reconnect_resolved`` so ``health()`` can
       publish "attempt N in flight" between calls.
    4. ``stop()`` signals the task to exit at the next tick boundary
       (with a hard cancel as a fallback) so the FastAPI lifespan
       teardown doesn't hang on a long backoff sleep.
    """

    POLL_INTERVAL_S = 3.0
    """Cadence the monitor wakes to check the client. Tuned to be tighter
    than the cockpit's 5s broker-health poll so a transition is observed
    before the next UI tick rather than between them."""

    INITIAL_BACKOFF_S = 1.0
    """First retry delay. Doubles per failure (1s, 2s, 4s, 8s, ...)
    capped at ``MAX_BACKOFF_S``."""

    MAX_BACKOFF_S = 60.0
    """Backoff ceiling. Past this the monitor keeps trying at one-minute
    intervals — the IBKR Gateway sometimes takes that long to recover,
    and reconnecting more aggressively only generates rejected attempts."""

    def __init__(
        self,
        client: IbkrClient,
        *,
        poll_interval_s: float | None = None,
        initial_backoff_s: float | None = None,
        max_backoff_s: float | None = None,
    ) -> None:
        self._client = client
        self._poll_interval_s = poll_interval_s or self.POLL_INTERVAL_S
        self._initial_backoff_s = initial_backoff_s or self.INITIAL_BACKOFF_S
        self._max_backoff_s = max_backoff_s or self.MAX_BACKOFF_S
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Spawn the monitor task. No-op if already running."""
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="ibkr-auto-reconnect")
        logger.info(
            "IBKR auto-reconnect monitor started",
            extra={
                "action": "auto_reconnect_start",
                "poll_interval_s": self._poll_interval_s,
                "initial_backoff_s": self._initial_backoff_s,
                "max_backoff_s": self._max_backoff_s,
            },
        )

    async def stop(self) -> None:
        """Stop the monitor task. Safe to call from the FastAPI lifespan
        teardown; waits up to ~6s for the task to exit cleanly before
        falling back to a hard cancel."""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=6.0)
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._task = None
        logger.info(
            "IBKR auto-reconnect monitor stopped",
            extra={"action": "auto_reconnect_stop"},
        )

    async def _wait_or_stop(self, timeout_s: float) -> bool:
        """Sleep up to ``timeout_s``, returning True iff the stop event
        fired. Used both for the inter-poll interval and the inter-
        attempt backoff so a teardown is responsive."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout_s)
            return True
        except TimeoutError:
            return False

    async def _run(self) -> None:
        """Main loop. Exits cleanly on stop; logs unhandled exceptions
        with ``logger.exception`` and continues so a transient broker
        glitch never kills the monitor for the rest of the process."""
        while not self._stop_event.is_set():
            stopped = await self._wait_or_stop(self._poll_interval_s)
            if stopped:
                return
            try:
                await self._tick()
            except Exception:
                logger.exception(
                    "Auto-reconnect monitor tick raised; continuing",
                    extra={"action": "auto_reconnect_tick_error"},
                )

    async def _tick(self) -> None:
        """One observation cycle. If the client looks healthy, return.
        Otherwise enter the reconnect attempt loop."""
        if self._client.is_connected() and not self._client.connection_lost:
            return
        await self._attempt_reconnect_loop()

    async def _attempt_reconnect_loop(self) -> None:
        """Retry ``client.connect()`` with exponential backoff until it
        succeeds OR the stop event fires.

        Acquires the shared lifecycle lock so a manual operator reconnect
        in flight is serialised: the monitor waits for the operator's
        call to finish, observes the post-call state, and either exits
        (operator succeeded → ``is_connected`` is True) or proceeds with
        its own attempt (operator failed too).

        The previously-soft socket needs to be torn down before we
        reconnect — otherwise ``connectAsync`` returns the same dead
        connection. ``disconnect()`` is idempotent and tolerates a
        hard-closed socket.
        """
        from app.broker.ibkr.client import get_client_lifecycle_lock

        backoff = self._initial_backoff_s
        attempt = 0
        while not self._stop_event.is_set():
            async with get_client_lifecycle_lock():
                # Re-check once the lock is held — an operator's manual
                # reconnect may have restored the connection between the
                # tick observation and this acquisition.
                if (
                    self._client.is_connected()
                    and not self._client.connection_lost
                ):
                    return
                try:
                    await self._client.disconnect()
                except Exception:
                    logger.exception(
                        "Pre-reconnect disconnect raised; proceeding to connect",
                        extra={"action": "auto_reconnect_predisconnect_error"},
                    )
                attempt += 1
                self._client._mark_reconnect_started(attempt)
                logger.info(
                    "Auto-reconnect attempt %d starting",
                    attempt,
                    extra={"action": "auto_reconnect_attempt", "attempt": attempt},
                )
                try:
                    await self._client.connect()
                    self._client._mark_reconnect_resolved(success=True)
                    logger.info(
                        "Auto-reconnect attempt %d succeeded",
                        attempt,
                        extra={
                            "action": "auto_reconnect_success",
                            "attempt": attempt,
                            "recovered_count": self._client.successful_reconnect_count,
                        },
                    )
                    return
                except Exception as exc:
                    self._client._mark_reconnect_resolved(success=False)
                    logger.warning(
                        "Auto-reconnect attempt %d failed: %s; next try in %.1fs",
                        attempt,
                        exc,
                        backoff,
                        extra={
                            "action": "auto_reconnect_fail",
                            "attempt": attempt,
                            "next_backoff_s": backoff,
                        },
                    )
            # Sleep OUTSIDE the lock so an operator can still reconnect
            # manually during the backoff window without queueing behind
            # the monitor's wait.
            stopped = await self._wait_or_stop(backoff)
            if stopped:
                return
            backoff = min(backoff * 2.0, self._max_backoff_s)


# ── module-level singleton ────────────────────────────────────────────
# Held alongside the ``IbkrClient`` singleton in the FastAPI lifespan.

_monitor: AutoReconnectMonitor | None = None


def get_monitor() -> AutoReconnectMonitor | None:
    """Return the active monitor or ``None`` if the lifespan hasn't
    installed one (tests, broker-disabled mode)."""
    return _monitor


def set_monitor(monitor: AutoReconnectMonitor | None) -> None:
    global _monitor
    _monitor = monitor
