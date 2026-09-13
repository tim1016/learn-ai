"""Delivery-D runtime isolation and compatibility-read evidence tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.fleet.lane_runtime import (
    CompatibilityReadEvidence,
    FleetLaneRuntimeMiddleware,
    LaneRuntimeConfig,
    compatibility_route_family,
)


def _config(*, requests: int = 1, streams: int = 1, queue: int = 0, timeout_ms: int = 0) -> LaneRuntimeConfig:
    """Build a small explicit lane sizing for one isolated test."""
    return LaneRuntimeConfig(
        max_inflight_requests=requests,
        max_inflight_streams=streams,
        request_queue_limit=queue,
        request_queue_timeout_ms=timeout_ms,
    )


async def _invoke(
    app: Callable[..., Awaitable[None]],
    *,
    method: str = "GET",
    path: str = "/api/brokers/alpaca/bots",
    headers: list[tuple[bytes, bytes]] | None = None,
    app_state: object | None = None,
) -> list[dict[str, Any]]:
    """Call a small ASGI app without HTTPX's intentionally buffering SSE transport."""
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
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


def _status(messages: list[dict[str, Any]]) -> int:
    """Return the one response status emitted by an ASGI invocation."""
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


async def test_request_capacity_refuses_then_recovers_without_running_refused_handler(
    tmp_path: Path,
) -> None:
    """A full request pool returns the typed 503 and recovers after release."""
    entered = asyncio.Event()
    release = asyncio.Event()
    handler_calls = 0

    async def app(scope, receive, send) -> None:
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

    async def app(scope, receive, send) -> None:
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

    async def app(scope, receive, send) -> None:
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

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    runtime = FleetLaneRuntimeMiddleware(app, config=_config(), evidence=evidence)
    path = "/api/brokers/alpaca/bots/account-secret/sid-secret?token=secret"
    await _invoke(runtime, path=path, headers=[(b"authorization", b"Bearer secret")])
    await _invoke(runtime, method="POST", path=path)
    await _invoke(runtime, path=path, headers=[(b"x-fleet-clerk-id", b"clrk_pinned")])

    payload = evidence.snapshot()
    assert payload["route_hits"] == [
        {
            "method": "GET",
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

    async def app(scope, receive, send) -> None:
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

    assert evidence.snapshot()["route_hits"] == [
        {
            "method": "GET",
            "route_family": "brokers_lane_extras",
            "response_class": "2xx",
            "count": 1,
            "first_observed_at_ms": 1_789_000_000_123,
            "last_observed_at_ms": 1_789_000_000_123,
        },
        {
            "method": "GET",
            "route_family": "brokers_lane_extras",
            "response_class": "4xx",
            "count": 2,
            "first_observed_at_ms": 1_789_000_000_123,
            "last_observed_at_ms": 1_789_000_000_123,
        },
    ]


def test_compatibility_evidence_removes_temporary_file_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed durable export does not leave a request-shaped temporary file behind."""
    evidence = CompatibilityReadEvidence(tmp_path, clock=lambda: 1)

    def refuse_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("replace refused")

    monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", refuse_replace)
    with pytest.raises(OSError, match="replace refused"):
        evidence.record(
            method="GET", route_family="broker_bots", response_class="2xx"
        )
    assert not list(evidence.path.parent.glob(".route_hits.json.*"))


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

    async def handler(scope, receive, send) -> None:
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
