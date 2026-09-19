"""Delivery-D runtime isolation and compatibility-read evidence tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.fleet import lane_runtime
from app.broker.fleet.compatibility_retirement import (
    CompatibilityRouteState,
    write_route_state,
)
from app.broker.fleet.lane_runtime import (
    CompatibilityEvidenceFlushError,
    CompatibilityReadEvidence,
    FleetLaneRuntimeMiddleware,
    LaneRuntimeConfig,
    compatibility_route_family,
)

AsgiMessage = dict[str, Any]
Receive = Callable[[], Awaitable[AsgiMessage]]
Send = Callable[[AsgiMessage], Awaitable[None]]
AsgiApp = Callable[[dict[str, Any], Receive, Send], Awaitable[None]]

#: How long the injected slow export blocks. Must-timeout side of the budget.
_SLOW_EXPORT_SECONDS = 1.0
#: The response must return well inside this. Must-succeed side of the budget.
#: The invariant, not a ratio: the sleep must exceed the budget (a real
#: on-loop regression makes elapsed time track the sleep), and the budget
#: must exceed worst-case scheduling jitter on a loaded runner.
_RESPONSE_BUDGET_SECONDS = 0.5


def _config(
    *, requests: int = 1, streams: int = 1, commands: int = 1, queue: int = 0, timeout_ms: int = 0
) -> LaneRuntimeConfig:
    """Build a small explicit lane sizing for one isolated test."""
    return LaneRuntimeConfig(
        max_inflight_requests=requests,
        max_inflight_streams=streams,
        max_inflight_commands=commands,
        request_queue_limit=queue,
        request_queue_timeout_ms=timeout_ms,
    )


async def _invoke(
    app: AsgiApp,
    *,
    method: str = "GET",
    path: str = "/api/brokers/alpaca/bots",
    headers: list[tuple[bytes, bytes]] | None = None,
    app_state: object | None = None,
) -> list[dict[str, Any]]:
    """Call a small ASGI app without HTTPX's intentionally buffering SSE transport."""
    messages: list[AsgiMessage] = []

    async def receive() -> AsgiMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: AsgiMessage) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
            "app": app_state,
        },
        receive,
        send,
    )
    return messages


def _status(messages: list[AsgiMessage]) -> int:
    """Return the one response status emitted by an ASGI invocation."""
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def _scoped_os(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Swap the ``os`` *binding* inside ``lane_runtime`` only.

    ``monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", …)``
    reads through to the process-global ``os`` module and mutates it for
    every thread and every other test in the session. Rebinding the module
    attribute cannot leak: only ``lane_runtime``'s own lookups see the shim.
    """
    shim = SimpleNamespace(
        fsync=os.fsync, replace=os.replace, open=os.open, close=os.close, O_RDONLY=os.O_RDONLY
    )
    for name, value in overrides.items():
        setattr(shim, name, value)
    monkeypatch.setattr(lane_runtime, "os", shim)


async def test_request_capacity_refuses_then_recovers_without_running_refused_handler(
    tmp_path: Path,
) -> None:
    """A full request pool returns the typed 503 and recovers after release."""
    entered = asyncio.Event()
    release = asyncio.Event()
    handler_calls = 0

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal handler_calls
        handler_calls += 1
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app, config=_config(), evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    )
    first = asyncio.create_task(_invoke(runtime))
    await entered.wait()

    # A second READ contends for the same (exhausted) request pool -- unlike
    # a mutating command, which now has its own independent pool (issue
    # #2204 gate F1; see test_read_capacity_exhaustion_never_blocks_a_command
    # below for that half of the invariant).
    refused = await _invoke(runtime, method="GET")
    assert _status(refused) == 503
    assert json.loads(refused[-1]["body"])["reason"] == "fleet_lane_capacity_exhausted"
    assert handler_calls == 1, "capacity refusal must occur before a second read handler"

    release.set()
    assert _status(await first) == 200
    assert _status(await _invoke(runtime)) == 200


async def test_read_capacity_exhaustion_never_blocks_a_command(tmp_path: Path) -> None:
    """F1 regression #1: a full read pool must not delay or refuse a command.

    Before the fix, a slow read (a held bot-chart-history request) and a
    mutating command (Stop) shared one pool: with the read pool's only slot
    held, a POST command was refused outright (queue=0) or forced to wait
    out the queue deadline. This pins the new invariant directly: with the
    read pool's one slot held and its queue disabled (an immediate refusal
    if commands shared it), a POST command is admitted and completes.
    """
    read_entered = asyncio.Event()
    read_release = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["method"] == "GET":
            read_entered.set()
            await read_release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app,
        config=_config(requests=1, commands=1, queue=0, timeout_ms=0),
        evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1),
    )
    slow_read = asyncio.create_task(_invoke(runtime, method="GET"))
    await read_entered.wait()

    command = await asyncio.wait_for(_invoke(runtime, method="POST"), timeout=2.0)
    assert _status(command) == 200

    read_release.set()
    assert _status(await slow_read) == 200


async def test_every_read_slot_held_a_command_is_admitted_without_the_queue_deadline(
    tmp_path: Path,
) -> None:
    """F1 regression #2: production-like read sizing never gates a command.

    Mirrors the production defaults' shape (many read slots, a positive queue
    deadline) rather than the qualification single-slot sizing: with every
    read slot held, a command must be admitted immediately, not after
    waiting out the read pool's queue deadline (which the old shared-pool
    behavior forced).
    """
    read_entered = [asyncio.Event() for _ in range(4)]
    read_release = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["method"] == "GET":
            read_entered[int(scope["path"].rsplit("/", 1)[-1])].set()
            await read_release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app,
        # Production shape: several read slots, a real queue and a positive
        # deadline -- long enough that this test would time out waiting for
        # it if a command still shared the read pool's queue.
        config=_config(requests=4, commands=1, queue=64, timeout_ms=5_000),
        evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1),
    )
    slow_reads = [
        asyncio.create_task(_invoke(runtime, method="GET", path=f"/reads/{i}"))
        for i in range(4)
    ]
    await asyncio.gather(*(event.wait() for event in read_entered))

    started = time.monotonic()
    command = await asyncio.wait_for(_invoke(runtime, method="POST"), timeout=1.0)
    elapsed = time.monotonic() - started
    assert _status(command) == 200
    assert elapsed < 1.0, "a command must not wait out the read pool's queue deadline"

    read_release.set()
    for status in (await asyncio.gather(*slow_reads)):
        assert _status(status) == 200


async def test_internal_history_batch_read_draws_read_not_command_capacity(
    tmp_path: Path,
) -> None:
    """The coordinator's history-batch POST is a read for capacity purposes.

    Reached through this middleware whenever a ``combined``-role process
    mounts both the coordinator's internal surface and the clerk lane
    runtime (issue #2204). It must not compete with real commands for the
    (typically much smaller) command pool merely because it is spelled POST.
    """
    from app.broker.fleet.history_batch import INTERNAL_HISTORY_BATCH_PATH

    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app,
        config=_config(requests=1, commands=1, queue=0, timeout_ms=0),
        evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1),
    )
    held = asyncio.create_task(
        _invoke(runtime, method="POST", path=INTERNAL_HISTORY_BATCH_PATH)
    )
    await entered.wait()

    # The one read slot is held by the internal history-batch call above, so
    # a second one of the same shape must be refused -- proving it drew from
    # the read pool, not the untouched command pool.
    refused = await _invoke(runtime, method="POST", path=INTERNAL_HISTORY_BATCH_PATH)
    assert _status(refused) == 503

    release.set()
    assert _status(await held) == 200


async def test_bounded_request_queue_waits_then_admits_after_release(tmp_path: Path) -> None:
    """One bounded waiter is admitted; this is not an unbounded backlog."""
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app,
        config=_config(queue=1, timeout_ms=500),
        evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1),
    )
    first = asyncio.create_task(_invoke(runtime))
    await entered.wait()
    waiting = asyncio.create_task(_invoke(runtime))
    await asyncio.sleep(0)
    release.set()
    assert _status(await first) == 200
    assert _status(await waiting) == 200
    assert calls == 2


async def test_stream_capacity_is_separate_from_ordinary_requests_and_recovers(
    tmp_path: Path,
) -> None:
    """A held SSE consumes only its stream budget, never the request pool."""
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["path"] == "/stream":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            stream_started.set()
            await release_stream.wait()
            await send({"type": "http.response.body", "body": b"data: done\n\n", "more_body": False})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app, config=_config(), evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    )
    first_stream = asyncio.create_task(_invoke(runtime, path="/stream"))
    await stream_started.wait()

    assert _status(await _invoke(runtime, path="/ordinary")) == 200
    refused_stream = await _invoke(runtime, path="/stream")
    assert _status(refused_stream) == 503
    assert json.loads(refused_stream[-1]["body"])["reason"] == "fleet_lane_capacity_exhausted"

    release_stream.set()
    assert _status(await first_stream) == 200
    assert _status(await _invoke(runtime, path="/stream")) == 200


async def test_stream_queue_wait_releases_request_capacity_for_ordinary_reads(
    tmp_path: Path,
) -> None:
    """A queued stream never starves the ordinary request admission pool."""
    first_stream_started = asyncio.Event()
    second_stream_waiting = asyncio.Event()
    release_first_stream = asyncio.Event()
    stream_calls = 0

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal stream_calls
        if scope["path"] == "/stream":
            stream_calls += 1
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            if stream_calls == 1:
                first_stream_started.set()
                await release_first_stream.wait()
            else:
                second_stream_waiting.set()
            await send({"type": "http.response.body", "body": b"data: done\n\n", "more_body": False})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(
        app,
        config=_config(queue=1, timeout_ms=500),
        evidence=CompatibilityReadEvidence(tmp_path, clock=lambda: 1),
    )
    first_stream = asyncio.create_task(_invoke(runtime, path="/stream"))
    await first_stream_started.wait()
    queued_stream = asyncio.create_task(_invoke(runtime, path="/stream"))
    await asyncio.sleep(0)

    # The second stream is waiting on the stream pool, not holding the only
    # ordinary request slot while it waits.
    assert _status(await _invoke(runtime, path="/ordinary")) == 200
    release_first_stream.set()
    assert _status(await first_stream) == 200
    assert _status(await queued_stream) == 200
    assert second_stream_waiting.is_set()


def test_compatibility_inventory_is_fixed_and_excludes_canonical_internal_and_mutations() -> None:
    """Only retained, unpinned read families may enter the D measurement."""
    assert compatibility_route_family("GET", "/api/brokers/alpaca/assets") == "brokers_lane_extras"
    assert compatibility_route_family(
        "HEAD", "/api/brokers/alpaca/configuration/selection"
    ) == "broker_configuration"
    assert compatibility_route_family(
        "GET", "/api/brokers/alpaca/bots/sid-1/runs/run-1/replay-receipt"
    ) == "run_replay"
    assert compatibility_route_family(
        "GET", "/api/brokers/alpaca/clerks/clrk_abc/account"
    ) is None
    assert compatibility_route_family("GET", "/internal/fleet/sessions") is None
    assert compatibility_route_family("POST", "/api/brokers/alpaca/bots") is None


async def test_measurement_persists_only_safe_aggregate_and_never_mutations(
    tmp_path: Path,
) -> None:
    """No account, strategy, query, body, header, or secret reaches lane evidence."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1_789_000_000_123)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=_config(), evidence=evidence)
    path = "/api/brokers/alpaca/bots/account-secret/sid-secret?token=secret"
    await _invoke(runtime, path=path, headers=[(b"authorization", b"Bearer secret")])
    await _invoke(runtime, method="POST", path=path)
    await _invoke(runtime, path=path, headers=[(b"x-fleet-clerk-id", b"clrk_pinned")])
    await evidence.flush()

    payload = evidence.snapshot()
    assert payload["route_hits"] == [
        {
            "route_family": "broker_bots",
            "response_class": "2xx",
            "count": 1,
            "first_observed_at_ms": 1_789_000_000_123,
            "last_observed_at_ms": 1_789_000_000_123,
        }
    ]
    serialized = evidence.path.read_text(encoding="utf-8")
    for forbidden in ("account-secret", "sid-secret", "token", "authorization", "Bearer"):
        assert forbidden not in serialized


async def test_capacity_refusal_is_recorded_as_a_compatibility_5xx(tmp_path: Path) -> None:
    """Overloaded retained reads remain visible to retirement evidence."""
    entered = asyncio.Event()
    release = asyncio.Event()
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=_config(), evidence=evidence)
    admitted = asyncio.create_task(_invoke(runtime, path="/api/brokers/alpaca/bots"))
    await entered.wait()
    assert _status(await _invoke(runtime, path="/api/brokers/alpaca/bots")) == 503
    release.set()
    await admitted
    await evidence.flush()
    assert evidence.snapshot()["route_hits"] == [
        {
            "route_family": "broker_bots",
            "response_class": "2xx",
            "count": 1,
            "first_observed_at_ms": 1,
            "last_observed_at_ms": 1,
        },
        {
            "route_family": "broker_bots",
            "response_class": "5xx",
            "count": 1,
            "first_observed_at_ms": 1,
            "last_observed_at_ms": 1,
        },
    ]


async def test_compatibility_evidence_separates_failed_probes_from_successful_reads(
    tmp_path: Path,
) -> None:
    """Failed probes cannot be mistaken for successfully migrated consumers."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1_789_000_000_123)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        headers = {name: value for name, value in scope["headers"]}
        status = 404 if scope["path"].endswith("/missing") else 200
        if scope["path"].endswith("/assets") and b"x-test-auth" not in headers:
            status = 403
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"result", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=_config(), evidence=evidence)
    await _invoke(runtime, path="/api/brokers/alpaca/assets")
    await _invoke(
        runtime,
        path="/api/brokers/alpaca/assets",
        headers=[(b"x-test-auth", b"accepted")],
    )
    await _invoke(runtime, path="/api/brokers/alpaca/activities/missing")
    await evidence.flush()

    assert evidence.snapshot()["route_hits"] == [
        {
            "route_family": "brokers_lane_extras",
            "response_class": "2xx",
            "count": 1,
            "first_observed_at_ms": 1_789_000_000_123,
            "last_observed_at_ms": 1_789_000_000_123,
        },
        {
            "route_family": "brokers_lane_extras",
            "response_class": "4xx",
            "count": 2,
            "first_observed_at_ms": 1_789_000_000_123,
            "last_observed_at_ms": 1_789_000_000_123,
        },
    ]


async def test_compatibility_evidence_removes_temporary_file_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed durable export does not leave a request-shaped temporary file behind."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    def refuse_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("replace refused")

    _scoped_os(monkeypatch, replace=refuse_replace)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    assert _status(await _invoke(runtime)) == 200
    with pytest.raises(CompatibilityEvidenceFlushError, match="could not be flushed"):
        await evidence.flush()
    assert not list(evidence.path.parent.glob(".route_hits.json.*"))
    assert "Compatibility route-hit export failed." in caplog.text
    assert evidence._pending_updates == {("broker_bots", "2xx"): 1}

    _scoped_os(monkeypatch)
    await evidence.flush()
    assert evidence.snapshot()["route_hits"][0]["count"] == 1


async def test_background_evidence_export_does_not_delay_an_authorized_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow evidence persistence runs off-loop after the handler's response start."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    def slow_record(updates: Mapping[tuple[str, str], int]) -> None:
        del updates
        time.sleep(_SLOW_EXPORT_SECONDS)

    monkeypatch.setattr(evidence, "_record_batch", slow_record)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    started_at = time.monotonic()
    assert _status(await _invoke(runtime)) == 200
    assert time.monotonic() - started_at < _RESPONSE_BUDGET_SECONDS
    await evidence.flush()


async def test_failed_evidence_batch_is_retried_without_losing_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient export failure is retried by one bounded flush."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    record_batch = evidence._record_batch
    attempts = 0

    def fail_once(updates: Mapping[tuple[str, str], int]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient export failure")
        record_batch(updates)

    monkeypatch.setattr(evidence, "_record_batch", fail_once)
    evidence.schedule(route_family="broker_bots", response_class="2xx")
    await evidence.flush()

    assert attempts == 2
    assert evidence.snapshot()["route_hits"][0]["count"] == 1


async def test_combined_observation_uses_no_capacity_pool(tmp_path: Path) -> None:
    """Combined pre-cutover mode records browser reads without fleet sizing."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    assert _status(await _invoke(runtime)) == 200
    await evidence.flush()
    assert evidence.snapshot()["route_hits"][0]["route_family"] == "broker_bots"


def _eligible_receipt() -> dict[str, Any]:
    """A minimal retirement receipt that passes eligibility validation."""
    return {
        "schema_version": 1,
        "decision": "eligible",
        "measurement_window": {"start_ms": 1, "end_ms": 2},
        "operator_receipt_id": "receipt-1",
        "scoped_route_evidence": {"unresolved_scoped_route_failures": 0},
        "consumer_inventory": {
            "consumers": ["alpaca-desk"],
            "attested_route_families": [
                "broker_bots",
                "broker_configuration",
                "broker_v2_panel",
                "brokers_lane_extras",
                "run_replay",
            ],
        },
        "route_deltas": [],
    }


async def test_retired_state_refuses_only_retained_unscoped_reads(tmp_path: Path) -> None:
    """A retirement receipt cannot affect canonical scoped reads or mutations."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    write_route_state(
        state_path=evidence.route_state_path,
        state=CompatibilityRouteState.RETIRED,
        retirement_receipt=_eligible_receipt(),
    )
    calls = 0

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    retired = await _invoke(runtime, path="/api/brokers/alpaca/bots")
    assert _status(retired) == 410
    assert json.loads(retired[-1]["body"])["reason"] == "compatibility_read_retired"
    arbitrary_header = await _invoke(
        runtime,
        path="/api/brokers/alpaca/bots",
        headers=[(b"x-fleet-clerk-id", b"clrk_arbitrary")],
    )
    assert _status(arbitrary_header) == 410

    from app.broker.fleet.agent_identity import (
        SERVED_IDENTITY_STATE_KEY,
        FleetIdentityMiddleware,
    )

    identity = {
        "broker": "alpaca",
        "clerk_id": "clrk_serving",
        "routing_epoch": 4,
        "binding_generation": 9,
    }
    app_state = SimpleNamespace(state=SimpleNamespace())
    setattr(app_state.state, SERVED_IDENTITY_STATE_KEY, lambda: identity)
    matching_header = await _invoke(
        FleetIdentityMiddleware(runtime),
        path="/api/brokers/alpaca/bots",
        headers=[
            (b"x-fleet-broker", b"alpaca"),
            (b"x-fleet-clerk-id", b"clrk_serving"),
        ],
        app_state=app_state,
    )
    assert _status(matching_header) == 410
    assert calls == 0

    canonical = await _invoke(runtime, path="/api/brokers/alpaca/clerks/clrk_paper/bots")
    mutation = await _invoke(runtime, method="POST", path="/api/brokers/alpaca/bots")
    assert _status(canonical) == 200
    assert _status(mutation) == 200
    assert calls == 2


async def test_corrupt_route_state_refuses_compatibility_alias_without_blocking_canonical_route(
    tmp_path: Path,
) -> None:
    """An existing corrupt state file does not silently re-enable an alias."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    evidence.route_state_path.parent.mkdir(parents=True)
    evidence.route_state_path.write_text("{not-json", encoding="utf-8")
    calls = 0

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    refused = await _invoke(runtime, path="/api/brokers/alpaca/bots")
    canonical = await _invoke(runtime, path="/api/brokers/alpaca/clerks/clrk_paper/bots")
    assert _status(refused) == 503
    assert json.loads(refused[-1]["body"])["reason"] == "compatibility_retirement_state_invalid"
    assert _status(canonical) == 200
    assert calls == 1


async def test_parent_directory_fsync_failure_makes_flush_fail_without_replaying_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-replace sync failure remains explicit and retries only the sync."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)
    original_fsync = os.fsync
    fsync_calls = 0

    def refuse_parent_sync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls >= 2:
            raise OSError("parent fsync refused")
        original_fsync(file_descriptor)

    _scoped_os(monkeypatch, fsync=refuse_parent_sync)
    evidence.schedule(route_family="broker_bots", response_class="2xx")
    with pytest.raises(CompatibilityEvidenceFlushError, match="could not be flushed"):
        await evidence.flush()
    assert evidence.path.exists()
    assert not list(evidence.path.parent.glob(".route_hits.json.*"))
    assert evidence._pending_updates == {}
    assert evidence._directory_sync_required

    _scoped_os(monkeypatch)
    await evidence.flush()
    assert evidence.snapshot()["route_hits"][0]["count"] == 1


async def test_lifespan_shutdown_does_not_complete_when_evidence_flush_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown receives the same bounded durability refusal as explicit flush."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    def refuse_record(updates: Mapping[tuple[str, str], int]) -> None:
        del updates
        raise OSError("export unavailable")

    monkeypatch.setattr(evidence, "_record_batch", refuse_record)
    evidence.schedule(route_family="broker_bots", response_class="2xx")
    inner_received_shutdown = False
    inner_teardown_complete = False
    messages: list[AsgiMessage] = []

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal inner_received_shutdown, inner_teardown_complete
        message = await receive()
        inner_received_shutdown = message["type"] == "lifespan.shutdown"
        # This represents the coordinator and lane-owned writer teardown that
        # must complete before the middleware attempts its final evidence sync.
        inner_teardown_complete = True
        await send({"type": "lifespan.shutdown.complete"})

    async def receive() -> AsgiMessage:
        return {"type": "lifespan.shutdown"}

    async def send(message: AsgiMessage) -> None:
        messages.append(message)

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    with pytest.raises(CompatibilityEvidenceFlushError, match="could not be flushed"):
        await runtime({"type": "lifespan"}, receive, send)
    assert inner_received_shutdown
    assert inner_teardown_complete
    assert messages == []


async def test_identity_validation_precedes_capacity_and_pinned_reads_are_not_measured(
    tmp_path: Path,
) -> None:
    """The D middleware neither bypasses pins nor measures coordinator traffic."""
    from app.broker.fleet.agent_identity import (
        SERVED_IDENTITY_STATE_KEY,
        FleetIdentityMiddleware,
    )

    calls = 0
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    async def handler(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    identity = {
        "broker": "alpaca",
        "clerk_id": "clrk_serving",
        "routing_epoch": 4,
        "binding_generation": 9,
    }
    app_state = SimpleNamespace(state=SimpleNamespace())
    setattr(app_state.state, SERVED_IDENTITY_STATE_KEY, lambda: identity)
    runtime = FleetIdentityMiddleware(
        FleetLaneRuntimeMiddleware(handler, config=_config(), evidence=evidence)
    )

    wrong_lane = await _invoke(
        runtime,
        headers=[
            (b"x-fleet-broker", b"alpaca"),
            (b"x-fleet-clerk-id", b"clrk_wrong"),
        ],
        app_state=app_state,
    )
    assert _status(wrong_lane) == 409
    assert calls == 0
    assert evidence.snapshot()["route_hits"] == []

    pinned_read = await _invoke(
        runtime,
        headers=[
            (b"x-fleet-broker", b"alpaca"),
            (b"x-fleet-clerk-id", b"clrk_serving"),
        ],
        app_state=app_state,
    )
    assert _status(pinned_read) == 200
    assert calls == 1
    await evidence.flush()
    assert evidence.snapshot()["route_hits"] == []
    start = next(message for message in pinned_read if message["type"] == "http.response.start")
    assert (b"x-fleet-clerk-id", b"clrk_serving") in start["headers"]


def test_lane_runtime_config_rejects_partial_or_negative_sizing() -> None:
    """A clerk deployment cannot silently turn bounded resources off."""
    class PartialSettings:
        MAX_INFLIGHT_REQUESTS = 1
        MAX_INFLIGHT_STREAMS = 0
        MAX_INFLIGHT_COMMANDS = 1
        REQUEST_QUEUE_LIMIT = 0
        REQUEST_QUEUE_TIMEOUT_MS = 0

    with pytest.raises(ValueError, match="must all be positive"):
        LaneRuntimeConfig.from_settings(PartialSettings())


def test_lane_runtime_config_rejects_a_zeroed_command_pool() -> None:
    """The command pool is validated exactly like the other two (issue #2204)."""
    class ZeroedCommandSettings:
        MAX_INFLIGHT_REQUESTS = 1
        MAX_INFLIGHT_STREAMS = 1
        MAX_INFLIGHT_COMMANDS = 0
        REQUEST_QUEUE_LIMIT = 0
        REQUEST_QUEUE_TIMEOUT_MS = 0

    with pytest.raises(ValueError, match="must all be positive"):
        LaneRuntimeConfig.from_settings(ZeroedCommandSettings())


async def test_a_retired_lane_still_serves_an_authenticated_coordinator_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery E retires the browser alias, never the fleet's own transport."""
    from app.broker.fleet.delivery import COORDINATOR_TOKEN_HEADER
    from app.config import fleet_settings

    evidence = CompatibilityReadEvidence(tmp_path)
    write_route_state(
        state_path=tmp_path / "compatibility" / "route_state.json",
        state=CompatibilityRouteState.RETIRED,
        retirement_receipt=_eligible_receipt(),
    )
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", "svct_" + "a" * 32)

    inner = FastAPI()

    @inner.get("/api/brokers/alpaca/clerk/status")
    async def _status() -> dict[str, str]:
        return {"state": "ready"}

    inner.add_middleware(FleetLaneRuntimeMiddleware, config=None, evidence=evidence)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=inner), base_url="http://test"
    ) as client:
        browser = await client.get("/api/brokers/alpaca/clerk/status")
        forwarded = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={
                COORDINATOR_TOKEN_HEADER: "svct_" + "a" * 32,
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": "clk_test",
            },
        )
        spoofed = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": "clk_test"},
        )

    assert browser.status_code == 410
    assert browser.json()["reason"] == "compatibility_read_retired"
    assert forwarded.status_code == 200
    assert spoofed.status_code == 410

    # An unconfigured coordinator token means nothing is exempt, even a
    # request carrying the full forward header set.
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", None)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=inner), base_url="http://test"
    ) as client:
        unconfigured_token = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={
                COORDINATOR_TOKEN_HEADER: "svct_" + "a" * 32,
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": "clk_test",
            },
        )
    assert unconfigured_token.status_code == 410

    # The exemption is checked before route state is ever read, so a proven
    # forward is served even while the retirement state file is invalid —
    # the 503 that an invalid state would otherwise force never applies to
    # the fleet's own transport.
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", "svct_" + "a" * 32)
    evidence.route_state_path.write_text("{not-json", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=inner), base_url="http://test"
    ) as client:
        forwarded_despite_invalid_state = await client.get(
            "/api/brokers/alpaca/clerk/status",
            headers={
                COORDINATOR_TOKEN_HEADER: "svct_" + "a" * 32,
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": "clk_test",
            },
        )
    assert forwarded_despite_invalid_state.status_code == 200
