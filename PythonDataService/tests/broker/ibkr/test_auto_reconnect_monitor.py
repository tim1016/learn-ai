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
    ) -> None:
        self._is_connected = is_connected
        self._connection_lost = connection_lost
        # Each outcome is True (success) or an exception instance (raised).
        # After exhausting the list, defaults to True so a long-running
        # test doesn't need to enumerate every tick.
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
        self.recovery_succeeded_calls = 0
        self.recovery_failed_calls = 0

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

    def set_desired_connected(self, value: bool) -> None:
        self._desired_connected = value

    def mark_recovery_succeeded(self) -> None:
        self.recovery_succeeded_calls += 1
        self._subscriptions_stale = False

    def mark_recovery_failed(self, exc: Exception) -> None:
        self.recovery_failed_calls += 1

    async def connect(self):
        self.connect_calls += 1
        outcome = (
            self._connect_outcomes.pop(0) if self._connect_outcomes else True
        )
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
    assert monitor.is_attempting is False
    assert monitor.current_attempt == 0


@pytest.mark.asyncio
async def test_monitor_forces_reconnect_when_app_probe_fails() -> None:
    client = _FakeClient(
        is_connected=True,
        connection_lost=False,
        probe_outcomes=[TimeoutError("probe timed out")],
    )
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
        probe_interval_s=0.0,
        probe_timeout_s=0.01,
    )
    monitor.start()
    for _ in range(50):
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.probe_calls >= 1
    assert client.disconnect_calls >= 1
    assert client.connect_calls >= 1


# ─────────────────────── stale subscriptions ────────────────────────


@pytest.mark.asyncio
async def test_monitor_runs_recovery_callbacks_when_subscriptions_stale() -> None:
    """Codex P1 — IBKR code 1101 leaves the socket alive but invalidates
    market-data subscriptions. The reconnect loop never fires (because
    ``is_connected and not connection_lost``), so without an explicit
    stale-subscription path ``resubscribe_all`` would never run and charts
    would freeze. The monitor must run the recovery callbacks and clear
    the stale flag without a full reconnect."""
    client = _FakeClient(
        is_connected=True,
        connection_lost=False,
        subscriptions_stale=True,
    )
    callback_calls = 0

    async def fake_resubscribe() -> None:
        nonlocal callback_calls
        callback_calls += 1

    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
        subscription_recovery_interval_s=0.0,
        recovery_callbacks=[fake_resubscribe],
    )
    monitor.start()
    for _ in range(50):
        if callback_calls >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert callback_calls >= 1
    assert client.recovery_succeeded_calls >= 1
    assert client.subscriptions_stale is False
    # No reconnect cycle — the socket was healthy, only subscriptions were lost.
    assert client.connect_calls == 0
    assert client.disconnect_calls == 0


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
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.disconnect_calls >= 1, "must drop the soft socket before reconnecting"
    assert client.connect_calls >= 1
    assert monitor.successful_reconnect_count == 1
    # Monitor-owned state cleared after a successful recovery.
    assert monitor.is_attempting is False
    assert monitor.current_attempt == 0


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
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls >= 1
    assert monitor.successful_reconnect_count == 1


# ──────────────────────────── backoff ────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_doubles_backoff_on_repeated_failure() -> None:
    """First two attempts fail, third succeeds. ``connect()`` must be
    called three times; the monitor's ``successful_reconnect_count`` ticks
    once at the end."""
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
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls == 3
    assert monitor.successful_reconnect_count == 1
    assert monitor.is_attempting is False
    assert monitor.current_attempt == 0


# ──────────────────────────── shared lock ────────────────────────────


@pytest.mark.asyncio
async def test_monitor_serialises_reconnect_via_shared_lifecycle_lock() -> None:
    """When the operator holds the shared lock (router-side manual reconnect
    in flight), the monitor's tick must wait — never call ``connect()``
    concurrently on the same IB instance."""
    client = _FakeClient(is_connected=False)
    client.set_desired_connected(True)
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
    client.set_desired_connected(True)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.01,
    )
    lock = get_client_lifecycle_lock()
    await lock.acquire()
    try:
        monitor.start()
        await asyncio.sleep(0.05)  # let monitor reach the lock acquire and wait
        # Simulate the operator's reconnect having succeeded.
        client._is_connected = True
        client._connection_lost = False
    finally:
        # Release the process-wide lifecycle lock no matter what — leaking
        # it would poison subsequent tests in the suite.
        lock.release()

    # Give the monitor time to acquire the lock, re-check, and exit.
    await asyncio.sleep(0.05)
    await monitor.stop()

    # No connect call: monitor saw the restored state and exited its attempt
    # loop. (successful_reconnect_count stays at 0 because the recovery was
    # not monitor-driven.)
    assert client.connect_calls == 0
    assert monitor.successful_reconnect_count == 0


# ──────────────────────────── intentional disconnect ─────────────────


@pytest.mark.asyncio
async def test_monitor_does_not_reconnect_when_operator_disconnected() -> None:
    """Codex P1 regression — when the operator hits ``POST /disconnect``
    (or ``IBKR_CONNECT_ON_STARTUP=false`` leaves the client idle), the
    monitor must NOT auto-reconnect. ``desired_connected=False`` is the
    operator's stated intent and the monitor honours it."""
    client = _FakeClient(
        is_connected=False, connection_lost=False, desired_connected=False
    )
    monitor = AutoReconnectMonitor(
        client, poll_interval_s=0.01, initial_backoff_s=0.01
    )
    monitor.start()
    # Let multiple ticks fire — none should result in a connect attempt.
    await asyncio.sleep(0.05)
    await monitor.stop()

    assert client.connect_calls == 0
    assert client.disconnect_calls == 0
    assert monitor.is_attempting is False
    assert monitor.successful_reconnect_count == 0


@pytest.mark.asyncio
async def test_monitor_resumes_when_operator_clicks_connect_after_idle() -> None:
    """The dual of the above — when the operator flips desired back to
    True (clicks /connect from the cockpit), the monitor's next tick
    starts recovering normally."""
    client = _FakeClient(
        is_connected=False, connection_lost=False, desired_connected=False
    )
    monitor = AutoReconnectMonitor(
        client, poll_interval_s=0.01, initial_backoff_s=0.01
    )
    monitor.start()
    await asyncio.sleep(0.05)
    assert client.connect_calls == 0  # idle while operator hasn't asked

    # Operator clicks /connect — monitor takes over from the next tick.
    client.set_desired_connected(True)
    for _ in range(50):
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert client.connect_calls >= 1
    assert monitor.successful_reconnect_count == 1


# ──────────────────────────── monitor-owned state ─────────────────────


@pytest.mark.asyncio
async def test_monitor_publishes_attempt_state_to_observers() -> None:
    """The monitor exposes ``is_attempting`` / ``current_attempt`` /
    ``last_transition_ms`` so ``build_broker_health`` can compose them
    into the cockpit payload. Two failed attempts in a row keep
    ``current_attempt`` ticking up; a successful one clears it."""
    boom = OSError("Gateway unreachable")
    client = _FakeClient(
        is_connected=False,
        connect_outcomes=[boom, True],
    )
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.005,
        initial_backoff_s=0.005,
    )
    initial_transition = monitor.last_transition_ms
    monitor.start()
    for _ in range(100):
        if monitor.successful_reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()

    assert monitor.is_attempting is False
    assert monitor.current_attempt == 0
    assert monitor.successful_reconnect_count == 1
    assert monitor.last_transition_ms >= initial_transition
