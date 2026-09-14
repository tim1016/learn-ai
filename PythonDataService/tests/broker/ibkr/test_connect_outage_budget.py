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
    monitor = AutoReconnectMonitor(client, poll_interval_s=0.01, initial_backoff_s=0.0)

    monitor.start()
    await _wait_for(lambda: monitor.unreachable_since_ms is not None)
    client.set_reachable(True)
    await _wait_for(lambda: monitor.unreachable_since_ms is None, timeout_s=5.0)
    await monitor.stop()
