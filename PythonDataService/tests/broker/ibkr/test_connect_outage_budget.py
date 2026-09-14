"""#2080 — a long outage must report one age, not the age of the last probe."""

from __future__ import annotations

import pytest

from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
from tests.broker.ibkr._support import _FakeClient, _wait_for


@pytest.mark.asyncio
async def test_open_breaker_probes_do_not_reset_the_outage_anchor() -> None:
    clock = {"now": 1_700_000_000_000}
    client = _FakeClient(is_connected=False, reachable=False)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.0,
        max_backoff_s=0.0,
        max_reconnect_attempts=1,
        open_probe_interval_s=0.0,
        now_ms=lambda: clock["now"],
    )

    monitor.start()
    await _wait_for(lambda: monitor.is_hard_down)
    anchored = monitor.unreachable_since_ms
    assert anchored == 1_700_000_000_000

    clock["now"] += 8 * 60 * 60 * 1000  # an eight-hour gateway blackout
    probes = client.connect_calls
    await _wait_for(lambda: client.connect_calls > probes)
    await monitor.stop()

    assert monitor.unreachable_since_ms == anchored


@pytest.mark.asyncio
async def test_a_successful_connect_clears_the_outage_anchor() -> None:
    client = _FakeClient(is_connected=False, reachable=False)
    monitor = AutoReconnectMonitor(client, poll_interval_s=0.01, initial_backoff_s=0.001)

    monitor.start()
    await _wait_for(lambda: monitor.unreachable_since_ms is not None)
    client.set_reachable(True)
    await _wait_for(lambda: monitor.unreachable_since_ms is None)
    await monitor.stop()


@pytest.mark.asyncio
async def test_hard_down_tick_shortcut_clears_the_outage_anchor() -> None:
    """Fix-round-1 regression — ``_tick``'s ``is_hard_down`` branch recovers
    without ever calling ``_attempt_under_lifecycle_lock`` (it never calls
    ``connect()`` at all), so it must still clear ``unreachable_since_ms``.
    Before the chokepoint fix, a manually-restored connection reported
    ``connected``/HEALTHY with a permanently stale anchor.
    """
    client = _FakeClient(is_connected=False, reachable=False)
    monitor = AutoReconnectMonitor(
        client,
        poll_interval_s=0.01,
        initial_backoff_s=0.001,
        max_backoff_s=0.001,
        max_reconnect_attempts=1,
        # Keep the open-breaker probe out of the way so only the tick
        # shortcut (not a probe re-running _attempt_under_lifecycle_lock)
        # can be responsible for clearing the anchor below.
        open_probe_interval_s=1000.0,
    )

    monitor.start()
    await _wait_for(lambda: monitor.is_hard_down)
    assert monitor.unreachable_since_ms is not None
    connect_calls_before = client.connect_calls

    # Simulate an externally-restored socket (operator's own /connect)
    # without going through the monitor's own connect() -- the exact
    # shortcut `_tick`'s `is_hard_down` branch observes.
    client._is_connected = True
    client._connection_lost = False

    await _wait_for(lambda: monitor.unreachable_since_ms is None)
    await monitor.stop()

    assert monitor.recovery_state == "HEALTHY"
    assert client.connect_calls == connect_calls_before
