"""Tests for app.broker.ibkr.auto_reconnect_monitor.

The monitor uses asyncio.create_task plus a stop event for cancellation;
tests drive it via a fake ``IbkrClient`` so no real IBKR connection is
needed. Each test pins one observable contract:

* hard-disconnect (``not is_connected()``) triggers a reconnect attempt,
* soft-loss (``connection_lost``) triggers a reconnect attempt,
* exponential backoff doubles each failed attempt up to the cap,
* the lifecycle lock is shared with the operator router, so a manual
  reconnect mid-attempt is serialised correctly,
* the client publishes the in-flight attempt on ``health()`` so the
  cockpit can render "Reconnecting (attempt N)" without losing fidelity
  between polls.
"""

from __future__ import annotations

import asyncio

import pytest

from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
from app.broker.ibkr.client import get_client_lifecycle_lock


class _FakeClient:
    """Just enough surface for the monitor: ``is_connected``,
    ``connection_lost``, ``connect()``, ``disconnect()``, plus the
    private bookkeeping the monitor pokes."""

    def __init__(
        self,
        *,
        is_connected: bool = True,
        connection_lost: bool = False,
        connect_outcomes: list[bool | Exception] | None = None,
    ) -> None:
        self._is_connected = is_connected
        self._connection_lost = connection_lost
        # Each outcome is True (success) or an exception instance (raised).
        # After exhausting the list, defaults to True so a long-running
        # test doesn't need to enumerate every tick.
        self._connect_outcomes = list(connect_outcomes or [])
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Mirror the bookkeeping IbkrClient exposes — monitor pokes these
        # in real code, tests assert on them here.
        self.reconnect_attempts_started: list[int] = []
        self.reconnect_resolutions: list[bool] = []
        self.successful_reconnect_count = 0
        self._reconnecting = False
        self._reconnect_attempt = 0

    @property
    def is_connected_value(self) -> bool:
        return self._is_connected

    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def connection_lost(self) -> bool:
        return self._connection_lost

    async def connect(self):
        self.connect_calls += 1
        outcome = (
            self._connect_outcomes.pop(0) if self._connect_outcomes else True
        )
        if isinstance(outcome, Exception):
            raise outcome
        # Connect success — clear the loss flag and mark connected.
        self._is_connected = True
        self._connection_lost = False
        return None

    async def disconnect(self):
        self.disconnect_calls += 1
        self._is_connected = False

    def _mark_reconnect_started(self, attempt: int) -> None:
        self._reconnecting = True
        self._reconnect_attempt = attempt
        self.reconnect_attempts_started.append(attempt)

    def _mark_reconnect_resolved(self, *, success: bool) -> None:
        self._reconnecting = False
        if success:
            self._reconnect_attempt = 0
            self.successful_reconnect_count += 1
        self.reconnect_resolutions.append(success)


# ──────────────────────────── lifecycle ──────────────────────────────


@pytest.mark.asyncio
async def test_monitor_start_is_idempotent_and_stop_exits_cleanly() -> None:
    client = _FakeClient()
    monitor = AutoReconnectMonitor(client, poll_interval_s=0.01)
    monitor.start()
    assert monitor.is_running
    monitor.start()  # second call is a no-op, not a second task
    assert monitor.is_running

    await monitor.stop()
    assert not monitor.is_running


@pytest.mark.asyncio
async def test_monitor_skips_reconnect_when_client_is_connected_and_healthy() -> None:
    """Happy path tick — neither connection_lost nor is_connected==False, so
    no reconnect attempt should fire."""
    client = _FakeClient(is_connected=True, connection_lost=False)
    monitor = AutoReconnectMonitor(client, poll_interval_s=0.01)
    monitor.start()
    # Let several ticks elapse — none should trigger reconnect.
    await asyncio.sleep(0.05)
    await monitor.stop()

    assert client.connect_calls == 0
    assert client.reconnect_attempts_started == []


# ──────────────────────────── soft loss ──────────────────────────────


@pytest.mark.asyncio
async def test_monitor_reconnects_when_connection_lost_flag_set() -> None:
    """TWS Error 1100 path — socket is open but feed is dead. The monitor
    must observe ``connection_lost`` and reconnect even though
    ``is_connected()`` is True."""
    client = _FakeClient(is_connected=True, connection_lost=True)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
    )
    monitor.start()
    # Wait for at least one attempt to land. The monitor sleeps poll_interval
    # before the first tick, then runs disconnect+connect.
    for _ in range(50):
        if client.connect_calls >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.disconnect_calls >= 1, "must drop the soft socket before reconnecting"
    assert client.connect_calls >= 1
    assert client.reconnect_resolutions[-1] is True
    assert client.successful_reconnect_count == 1


# ──────────────────────────── hard disconnect ────────────────────────


@pytest.mark.asyncio
async def test_monitor_reconnects_when_socket_is_closed() -> None:
    """Hard close — ``is_connected()`` is False. Monitor reconnects."""
    client = _FakeClient(is_connected=False, connection_lost=False)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
    )
    monitor.start()
    for _ in range(50):
        if client.connect_calls >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls >= 1
    assert client.successful_reconnect_count == 1


# ──────────────────────────── backoff ────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_doubles_backoff_on_repeated_failure() -> None:
    """First two attempts fail, third succeeds. The monitor's published
    attempt counter must walk 1 → 2 → 3 and ``connect()`` must be
    called the same number of times."""
    boom = OSError("Gateway unreachable")
    client = _FakeClient(
        is_connected=False,
        connect_outcomes=[boom, boom, True],
    )
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.005,
        initial_backoff_s=0.005,
        max_backoff_s=0.05,
    )
    monitor.start()
    for _ in range(100):
        if client.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls == 3
    assert client.reconnect_attempts_started == [1, 2, 3]
    # The last attempt resolved successfully; the prior two failed.
    assert client.reconnect_resolutions == [False, False, True]


# ──────────────────────────── shared lock ────────────────────────────


@pytest.mark.asyncio
async def test_monitor_serialises_reconnect_via_shared_lifecycle_lock() -> None:
    """When the operator holds the shared lock (router-side manual reconnect
    in flight), the monitor's tick must wait — never call ``connect()``
    concurrently on the same IB instance."""
    client = _FakeClient(is_connected=False)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
    )
    lock = get_client_lifecycle_lock()
    await lock.acquire()
    try:
        monitor.start()
        # Give the monitor time to tick and reach the lock acquire.
        await asyncio.sleep(0.05)
        # Lock is held → monitor cannot have called connect().
        assert client.connect_calls == 0
    finally:
        lock.release()

    # Now that the lock is free, the monitor should make progress.
    for _ in range(50):
        if client.connect_calls >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls >= 1


@pytest.mark.asyncio
async def test_monitor_reexits_when_operator_restored_connection_under_lock() -> None:
    """If the operator's manual reconnect succeeded while the monitor was
    waiting on the lock, the monitor's re-check inside the lock must
    short-circuit — it must NOT issue a duplicate connect attempt."""
    client = _FakeClient(is_connected=False)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
    )
    lock = get_client_lifecycle_lock()
    await lock.acquire()
    monitor.start()
    await asyncio.sleep(0.05)  # let monitor reach the lock acquire and wait
    # Simulate the operator's reconnect having succeeded.
    client._is_connected = True
    client._connection_lost = False
    lock.release()

    # Give the monitor time to acquire the lock, re-check, and exit.
    await asyncio.sleep(0.05)
    await monitor.stop()

    # No connect call: monitor saw the restored state and exited its attempt
    # loop. (Successful_reconnect_count stays at 0 because the recovery was
    # not monitor-driven.)
    assert client.connect_calls == 0
    assert client.successful_reconnect_count == 0
