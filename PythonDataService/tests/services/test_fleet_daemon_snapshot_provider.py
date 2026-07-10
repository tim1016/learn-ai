"""ADR-0028 Stage 3C fleet daemon cache and breaker evidence."""

from __future__ import annotations

import asyncio

import pytest

from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.host_daemon_client import HostDaemonCircuitBreaker
from app.services.fleet_daemon_snapshot_provider import (
    FleetDaemonSnapshotProvider,
)


def _fleet_payload(*, fetched_at_ms: int, state: str = "running") -> dict:
    return {
        "fetched_at_ms": fetched_at_ms,
        "instances": [
            {
                "strategy_instance_id": "bot-a",
                "run_id": "run-a",
                "run_dir": "/runs/run-a",
                "process": {
                    "state": state,
                    "run_id": "run-a",
                    "pid": 41,
                    "started_at_ms": 1_700_000_000_000,
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_concurrent_bot_reads_share_one_call_per_interval() -> None:
    monotonic = [0.0]
    calls = 0

    async def fetch_instances(_daemon_url: str):
        nonlocal calls
        calls += 1
        return DaemonResult.connected(), _fleet_payload(
            fetched_at_ms=1_700_000_000_000 + calls,
        )

    provider = FleetDaemonSnapshotProvider(
        daemon_url="http://daemon",
        fetch_instances=fetch_instances,
        poll_interval_seconds=1.0,
        monotonic=lambda: monotonic[0],
        now_ms=lambda: 1_700_000_000_100,
    )

    await provider.refresh(force=True)
    first = await asyncio.gather(
        *(provider.process_for(f"bot-{suffix}") for suffix in "abcde")
    )
    assert calls == 1
    assert first[0][1]["run_id"] == "run-a"

    monotonic[0] = 1.0
    stale = await asyncio.gather(
        *(provider.process_for(f"bot-{suffix}") for suffix in "abcde")
    )
    assert stale == first
    await provider.wait_for_idle()
    assert calls == 2


@pytest.mark.asyncio
async def test_breaker_retains_stamp_but_current_process_is_unreachable() -> None:
    monotonic = [0.0]
    calls = 0
    outcomes = [
        (DaemonResult.connected(), _fleet_payload(fetched_at_ms=1_700_000_000_001)),
        (
            DaemonResult(
                kind="UNREACHABLE",
                detail="connection refused",
                error_category="connect_error",
            ),
            None,
        ),
        (DaemonResult.connected(), _fleet_payload(fetched_at_ms=1_700_000_000_009)),
    ]

    async def fetch_instances(_daemon_url: str):
        nonlocal calls
        calls += 1
        return outcomes.pop(0)

    provider = FleetDaemonSnapshotProvider(
        daemon_url="http://daemon",
        fetch_instances=fetch_instances,
        poll_interval_seconds=1.0,
        breaker_initial_backoff_seconds=5.0,
        breaker_max_backoff_seconds=8.0,
        monotonic=lambda: monotonic[0],
        now_ms=lambda: 1_700_000_000_100 + int(monotonic[0] * 1_000),
    )

    connected = await provider.refresh(force=True)
    monotonic[0] = 1.0
    unreachable = await provider.refresh(force=True)

    assert connected.source_fetched_at_ms == 1_700_000_000_001
    assert unreachable.result.kind == "UNREACHABLE"
    assert unreachable.payload == connected.payload
    assert unreachable.source_fetched_at_ms == connected.source_fetched_at_ms
    assert unreachable.process_for("bot-a") is None
    assert provider.breaker.is_open(monotonic[0]) is True

    monotonic[0] = 2.0
    still_open = await provider.refresh(force=True)
    assert calls == 2
    assert still_open == unreachable

    monotonic[0] = 6.0
    recovered = await provider.refresh(force=True)
    assert calls == 3
    assert recovered.result.kind == "CONNECTED"
    assert recovered.source_fetched_at_ms == 1_700_000_000_009
    assert recovered.process_for("bot-a") is not None


def test_breaker_backoff_is_exponential_and_bounded() -> None:
    breaker = HostDaemonCircuitBreaker(
        initial_backoff_seconds=2.0,
        max_backoff_seconds=5.0,
    )
    failure = DaemonResult(kind="UNREACHABLE", error_category="connect_error")

    breaker.observe(failure, now=0.0)
    assert breaker.open_until == 2.0
    breaker.observe(failure, now=2.0)
    assert breaker.open_until == 6.0
    breaker.observe(failure, now=6.0)
    assert breaker.open_until == 11.0
    breaker.observe(failure, now=11.0)
    assert breaker.open_until == 16.0

    breaker.observe(DaemonResult.connected(), now=16.0)
    assert breaker.consecutive_failures == 0
    assert breaker.is_open(16.0) is False


@pytest.mark.asyncio
async def test_connected_payload_requires_int64_ms_utc_source_stamp() -> None:
    async def fetch_instances(_daemon_url: str):
        return DaemonResult.connected(), {
            "fetched_at_ms": "2026-07-10T16:00:00Z",
            "instances": [],
        }

    provider = FleetDaemonSnapshotProvider(
        daemon_url="http://daemon",
        fetch_instances=fetch_instances,
    )

    observation = await provider.refresh(force=True)

    assert observation.result.kind == "INCOMPATIBLE_CONTRACT"
    assert observation.payload is None
    assert observation.source_fetched_at_ms is None


@pytest.mark.asyncio
async def test_stop_cancels_poll_and_in_flight_revalidation() -> None:
    monotonic = [0.0]
    calls = 0
    refresh_started = asyncio.Event()

    async def fetch_instances(_daemon_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return DaemonResult.connected(), _fleet_payload(
                fetched_at_ms=1_700_000_000_001,
            )
        refresh_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled refresh must not complete")

    provider = FleetDaemonSnapshotProvider(
        daemon_url="http://daemon",
        fetch_instances=fetch_instances,
        poll_interval_seconds=1.0,
        monotonic=lambda: monotonic[0],
        now_ms=lambda: 1_700_000_000_100,
    )
    await provider.start()
    monotonic[0] = 1.0
    await provider.observation()
    await refresh_started.wait()

    await asyncio.wait_for(provider.stop(timeout_seconds=0.05), timeout=0.1)

    assert provider.is_running is False
    assert calls == 2
