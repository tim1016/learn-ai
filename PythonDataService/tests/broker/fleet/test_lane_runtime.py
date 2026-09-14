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

import pytest

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


def _config(*, requests: int = 1, streams: int = 1, queue: int = 0, timeout_ms: int = 0) -> LaneRuntimeConfig:
    """Build a small explicit lane sizing for one isolated test."""
    return LaneRuntimeConfig(
        max_inflight_requests=requests,
        max_inflight_streams=streams,
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

    refused = await _invoke(runtime, method="POST")
    assert _status(refused) == 503
    assert json.loads(refused[-1]["body"])["reason"] == "fleet_lane_capacity_exhausted"
    assert handler_calls == 1, "capacity refusal must occur before a mutation handler"

    release.set()
    assert _status(await first) == 200
    assert _status(await _invoke(runtime)) == 200


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


def test_compatibility_inventory_is_fixed_and_excludes_canonical_internal_and_mutations() -> None:
    """Only retained, unpinned read families may enter the D measurement."""
    assert compatibility_route_family("GET", "/api/brokers/alpaca/assets", pinned=False) == "brokers_lane_extras"
    assert compatibility_route_family(
        "HEAD", "/api/brokers/alpaca/configuration/selection", pinned=False
    ) == "broker_configuration"
    assert compatibility_route_family(
        "GET", "/api/brokers/alpaca/bots/sid-1/runs/run-1/replay-receipt", pinned=False
    ) == "run_replay"
    assert compatibility_route_family(
        "GET", "/api/brokers/alpaca/clerks/clrk_abc/account", pinned=False
    ) is None
    assert compatibility_route_family("GET", "/internal/fleet/sessions", pinned=False) is None
    assert compatibility_route_family("POST", "/api/brokers/alpaca/bots", pinned=False) is None
    assert compatibility_route_family("GET", "/api/brokers/alpaca/bots", pinned=True) is None


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


async def test_compatibility_evidence_separates_unauthorized_and_not_found_responses(
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

    original_replace = os.replace
    monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", refuse_replace)

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

    monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", original_replace)
    await evidence.flush()
    assert evidence.snapshot()["route_hits"][0]["count"] == 1


async def test_background_evidence_export_does_not_delay_an_authorized_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow evidence persistence runs off-loop after the handler's response start."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    def slow_record(updates: Mapping[tuple[str, str], int]) -> None:
        del updates
        time.sleep(0.2)

    monkeypatch.setattr(evidence, "_record_batch", slow_record)

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    started_at = time.monotonic()
    assert _status(await _invoke(runtime)) == 200
    assert time.monotonic() - started_at < 0.1
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

    monkeypatch.setattr("app.broker.fleet.lane_runtime.os.fsync", refuse_parent_sync)
    evidence.schedule(route_family="broker_bots", response_class="2xx")
    with pytest.raises(CompatibilityEvidenceFlushError, match="could not be flushed"):
        await evidence.flush()
    assert evidence.path.exists()
    assert not list(evidence.path.parent.glob(".route_hits.json.*"))
    assert evidence._pending_updates == {}
    assert evidence._directory_sync_required

    monkeypatch.setattr("app.broker.fleet.lane_runtime.os.fsync", original_fsync)
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
    messages: list[AsgiMessage] = []

    async def app(scope: dict[str, Any], receive: Receive, send: Send) -> None:
        nonlocal inner_received_shutdown
        message = await receive()
        inner_received_shutdown = message["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    async def receive() -> AsgiMessage:
        return {"type": "lifespan.shutdown"}

    async def send(message: AsgiMessage) -> None:
        messages.append(message)

    runtime = FleetLaneRuntimeMiddleware(app, config=None, evidence=evidence)
    with pytest.raises(CompatibilityEvidenceFlushError, match="could not be flushed"):
        await runtime({"type": "lifespan"}, receive, send)
    assert not inner_received_shutdown
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
        REQUEST_QUEUE_LIMIT = 0
        REQUEST_QUEUE_TIMEOUT_MS = 0

    with pytest.raises(ValueError, match="must both be positive"):
        LaneRuntimeConfig.from_settings(PartialSettings())
