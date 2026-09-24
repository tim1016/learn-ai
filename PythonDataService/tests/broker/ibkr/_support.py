"""Shared test doubles for ``tests/broker/ibkr``.

Hoisted out of ``test_auto_reconnect_monitor.py`` (#2080) so
``test_connect_outage_budget.py`` can reuse the same fake and polling
helper without importing across test modules.
"""

from __future__ import annotations

import asyncio


class _FakeClient:
    """Just enough surface for the monitor: ``is_connected``,
    ``connection_lost``, ``connect()``, ``disconnect()``. Notably no
    monitor-related bookkeeping fields — that state lives entirely on
    ``AutoReconnectMonitor`` now."""

    def __init__(
        self,
        *,
        is_connected: bool = True,
        connection_lost: bool = False,
        connect_outcomes: list[bool | Exception] | None = None,
        desired_connected: bool = True,
        probe_outcomes: list[bool | Exception] | None = None,
        subscriptions_stale: bool = False,
        last_ibkr_code: int | None = None,
        reachable: bool = True,
    ) -> None:
        self._is_connected = is_connected
        self._connection_lost = connection_lost
        # Each outcome is True (success) or an exception instance (raised).
        # After exhausting the list, defaults to ``reachable`` so a
        # long-running test doesn't need to enumerate every tick.
        self._connect_outcomes = list(connect_outcomes or [])
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.probe_calls = 0
        self._probe_outcomes = list(probe_outcomes or [])
        # Operator-intended state. True is the common case for monitor
        # tests (we're testing recovery); set False to exercise the
        # short-circuit on intentional disconnects.
        self._desired_connected = desired_connected
        self._subscriptions_stale = subscriptions_stale
        # As on the real client, stale subscriptions come from an IBKR 1101,
        # which advances the data-loss epoch the monitor recovers once each.
        self._data_loss_epoch = 1 if subscriptions_stale else 0
        self._last_ibkr_code = last_ibkr_code
        self.recovery_succeeded_calls = 0
        self.recovery_failed_calls = 0
        # Opt-in gateway-reachability flag (#2080). Defaults to True, which
        # keeps every pre-existing test's behaviour identical to before
        # this flag existed: with no ``connect_outcomes`` queued, connect()
        # still succeeds unconditionally. Only a caller that explicitly
        # passes ``reachable=False`` (or calls ``set_reachable(False)``)
        # gets a ``connect()`` that keeps failing until told otherwise —
        # needed to simulate a real gateway blackout without hand-rolling
        # an unbounded ``connect_outcomes`` list.
        self._reachable = reachable

    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def connection_lost(self) -> bool:
        return self._connection_lost

    @property
    def desired_connected(self) -> bool:
        return self._desired_connected

    @property
    def subscriptions_stale(self) -> bool:
        return self._subscriptions_stale

    @property
    def data_loss_epoch(self) -> int:
        return self._data_loss_epoch

    @property
    def last_ibkr_code(self) -> int | None:
        return self._last_ibkr_code

    def set_desired_connected(self, value: bool) -> None:
        self._desired_connected = value

    def set_reachable(self, value: bool) -> None:
        """Flip whether the *next* ``connect()`` call succeeds.

        Deliberately does not touch ``_is_connected`` directly — the
        monitor must observe recovery through its own ``connect()`` call
        (the real ``_tick`` -> ``_attempt_under_lifecycle_lock`` path),
        not through a poke that short-circuits it.
        """
        self._reachable = value

    def mark_recovery_succeeded(self) -> None:
        # The real client keeps the flag while a real-time-bar line lost at
        # 1101 still owes a bar (#2393); this fake never holds one.
        self.recovery_succeeded_calls += 1
        self._subscriptions_stale = False

    def mark_recovery_failed(self, exc: Exception) -> None:
        self.recovery_failed_calls += 1

    async def connect(self):
        self.connect_calls += 1
        if self._connect_outcomes:
            outcome = self._connect_outcomes.pop(0)
        elif self._reachable:
            outcome = True
        else:
            outcome = OSError("Gateway unreachable")
        if isinstance(outcome, Exception):
            raise outcome
        self._is_connected = True
        self._connection_lost = False
        return None

    async def disconnect(self):
        self.disconnect_calls += 1
        self._is_connected = False

    async def probe(self, *, timeout_s: float = 4.0) -> None:
        self.probe_calls += 1
        outcome = self._probe_outcomes.pop(0) if self._probe_outcomes else True
        if isinstance(outcome, Exception):
            raise outcome


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    """Poll ``predicate`` until it's truthy or ``timeout_s`` elapses."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)
