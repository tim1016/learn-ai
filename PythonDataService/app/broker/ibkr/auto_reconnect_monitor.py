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
the shared lifecycle lock in ``client.py`` serialises monitor-driven
and operator-driven reconnects, so they never race.

The monitor owns its own bookkeeping (``is_attempting``,
``current_attempt``, ``successful_reconnect_count``,
``last_transition_ms``); ``build_broker_health`` reads it and composes
the wire-level cockpit payload.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
       it enters the attempt loop with exponential backoff
       (``initial_backoff_s`` doubling per failure up to ``max_backoff_s``).
    3. ``stop()`` signals the task to exit at the next tick boundary
       (with a hard cancel as a fallback) so the FastAPI lifespan
       teardown doesn't hang on a long backoff sleep.

    State the monitor owns and publishes to ``build_broker_health``:

    * ``is_attempting`` — True while a reconnect attempt is in flight.
    * ``current_attempt`` — incrementing attempt number while in flight,
      0 otherwise.
    * ``successful_reconnect_count`` — cumulative monitor-driven
      recoveries this process.
    * ``last_transition_ms`` — wall-clock when ``is_attempting`` last
      flipped; the cockpit derives "Reconnecting since 12s ago" from this.
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
        probe_interval_s: float = 30.0,
        probe_timeout_s: float = 4.0,
        recovery_callbacks: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self._client = client
        self._poll_interval_s = poll_interval_s or self.POLL_INTERVAL_S
        self._initial_backoff_s = initial_backoff_s or self.INITIAL_BACKOFF_S
        self._max_backoff_s = max_backoff_s or self.MAX_BACKOFF_S
        self._probe_interval_s = probe_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._recovery_callbacks = list(recovery_callbacks or [])
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Monitor-owned state surfaced to the cockpit via build_broker_health.
        self._is_attempting: bool = False
        self._current_attempt: int = 0
        self._successful_reconnect_count: int = 0
        self._last_transition_ms: int = _now_ms()
        self._is_recovering: bool = False
        self._last_probe_due_ms: int = _now_ms()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_attempting(self) -> bool:
        return self._is_attempting

    @property
    def current_attempt(self) -> int:
        return self._current_attempt if self._is_attempting else 0

    @property
    def successful_reconnect_count(self) -> int:
        return self._successful_reconnect_count

    @property
    def last_transition_ms(self) -> int:
        return self._last_transition_ms

    @property
    def is_recovering(self) -> bool:
        return self._is_recovering

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
        falling back to a hard cancel. Unhandled task exceptions are
        logged at error rather than swallowed silently."""
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=6.0)
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Auto-reconnect monitor raised on cancel",
                    extra={"action": "auto_reconnect_cancel_error"},
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Auto-reconnect monitor raised on stop",
                extra={"action": "auto_reconnect_stop_error"},
            )
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
        """One observation cycle. Returns without acting in three cases:
        the operator does not want a live connection
        (``desired_connected=False``); the client looks healthy
        (``is_connected and not connection_lost``); otherwise enter the
        reconnect attempt loop."""
        if not self._client.desired_connected:
            return
        if self._client.is_connected() and not self._client.connection_lost:
            if await self._probe_if_due():
                return
            return
        await self._attempt_reconnect_loop()

    async def _probe_if_due(self) -> bool:
        """Run a bounded app-level probe on healthy-looking sockets.

        Returns True when the probe failed and a reconnect loop was entered.
        """
        now_ms = _now_ms()
        if now_ms - self._last_probe_due_ms < int(self._probe_interval_s * 1000):
            return False
        self._last_probe_due_ms = now_ms
        probe = getattr(self._client, "probe", None)
        if probe is None:
            return False
        try:
            await probe(timeout_s=self._probe_timeout_s)
        except Exception:
            logger.warning(
                "IBKR app-level probe failed; forcing reconnect",
                exc_info=True,
                extra={"action": "auto_reconnect_probe_fail"},
            )
            await self._attempt_reconnect_loop()
            return True
        return False

    async def _attempt_reconnect_loop(self) -> None:
        """Retry ``client.connect()`` with exponential backoff until it
        succeeds OR the stop event fires."""
        from app.broker.ibkr.client import get_client_lifecycle_lock

        backoff = self._initial_backoff_s
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            async with get_client_lifecycle_lock():
                # Re-check once the lock is held — an operator's manual
                # /connect, /disconnect, or /reconnect may have changed
                # the intent or the observable state between the tick and
                # this acquisition. ``desired_connected`` flipping False
                # means the operator clicked Disconnect; we exit cleanly
                # without one more attempt.
                if not self._client.desired_connected:
                    return
                if (
                    self._client.is_connected()
                    and not self._client.connection_lost
                ):
                    return
                if await self._run_one_attempt(attempt):
                    return
            # Sleep OUTSIDE the lock so an operator can still reconnect
            # manually during the backoff window without queueing behind
            # the monitor's wait.
            if await self._wait_or_stop(backoff):
                return
            backoff = min(backoff * 2.0, self._max_backoff_s)

    async def _run_one_attempt(self, attempt: int) -> bool:
        """One disconnect-then-connect cycle under the lifecycle lock.
        Returns True on success, False on failure (logged). Caller
        controls the retry loop and the backoff sleep.

        The previously-soft socket needs to be torn down before we
        reconnect — otherwise ``connectAsync`` returns the same dead
        connection. ``disconnect()`` is idempotent and tolerates a
        hard-closed socket.
        """
        self._begin_attempt(attempt)
        try:
            await self._client.disconnect()
        except Exception:
            logger.exception(
                "Pre-reconnect disconnect raised; proceeding to connect",
                extra={"action": "auto_reconnect_predisconnect_error"},
            )
        logger.info(
            "Auto-reconnect attempt %d starting",
            attempt,
            extra={"action": "auto_reconnect_attempt", "attempt": attempt},
        )
        try:
            await self._client.connect()
        except Exception as exc:
            self._end_attempt(success=False)
            logger.warning(
                "Auto-reconnect attempt %d failed: %s",
                attempt,
                exc,
                extra={"action": "auto_reconnect_fail", "attempt": attempt},
            )
            return False
        if not await self._run_recovery_callbacks():
            return False
        self._end_attempt(success=True)
        logger.info(
            "Auto-reconnect attempt %d succeeded",
            attempt,
            extra={
                "action": "auto_reconnect_success",
                "attempt": attempt,
                "recovered_count": self._successful_reconnect_count,
            },
        )
        return True

    async def _run_recovery_callbacks(self) -> bool:
        """Run post-connect recovery before the monitor reports healthy."""
        self._begin_recovery()
        try:
            for callback in self._recovery_callbacks:
                await callback()
        except Exception as exc:
            self._end_recovery(success=False)
            mark_failed = getattr(self._client, "mark_recovery_failed", None)
            if mark_failed is not None:
                mark_failed(exc)
            logger.warning(
                "IBKR post-reconnect recovery failed: %s",
                exc,
                exc_info=True,
                extra={"action": "auto_reconnect_recovery_fail"},
            )
            return False
        mark_succeeded = getattr(self._client, "mark_recovery_succeeded", None)
        if mark_succeeded is not None:
            mark_succeeded()
        self._end_recovery(success=True)
        return True

    def _begin_attempt(self, attempt: int) -> None:
        self._is_attempting = True
        self._current_attempt = attempt
        self._last_transition_ms = _now_ms()

    def _end_attempt(self, *, success: bool) -> None:
        self._is_attempting = False
        if success:
            self._current_attempt = 0
            self._successful_reconnect_count += 1
        self._last_transition_ms = _now_ms()

    def _begin_recovery(self) -> None:
        self._is_attempting = False
        self._is_recovering = True
        self._last_transition_ms = _now_ms()

    def _end_recovery(self, *, success: bool) -> None:
        self._is_recovering = False
        if success:
            self._current_attempt = 0
        self._last_transition_ms = _now_ms()


# ── module-level singleton ────────────────────────────────────────────
# Held alongside the ``IbkrClient`` singleton in the FastAPI lifespan.
# ``build_broker_health`` consults this when composing the cockpit
# payload; without it the wire model has no source of "is_attempting".

_monitor: AutoReconnectMonitor | None = None


def get_monitor() -> AutoReconnectMonitor | None:
    """Return the active monitor, or ``None`` when the lifespan has not
    installed one (broker-disabled mode, ad-hoc tests)."""
    return _monitor


def set_monitor(monitor: AutoReconnectMonitor | None) -> None:
    global _monitor
    _monitor = monitor
