"""Observable stream and path behavior across fleet delivery postures."""

from __future__ import annotations

import asyncio
import dataclasses
import threading

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.broker.fleet.agent_identity import _FrameInjector
from app.broker.fleet.delivery import (
    DeliveryRequest,
    HttpLaneDelivery,
    LocalLaneDelivery,
    _closing_event_iterator,
    bind_path_template,
)
from app.broker.fleet.internal_http import SseEvent
from app.broker.fleet.provider import OperationStream
from app.broker.fleet.routing import _LocalAsgiHandler
from app.routers import broker_clerks
from tests.broker.fleet.test_a2_alpaca_lane import _RealServer


async def test_idle_public_stream_keeps_its_source_and_delivers_the_next_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_clerks, "_KEEPALIVE_INTERVAL_S", 0.02)
    closed = asyncio.Event()

    async def source():
        try:
            yield SseEvent(event="update", data="one", id=None)
            await asyncio.sleep(0.08)
            yield SseEvent(event="update", data="two", id=None)
        finally:
            closed.set()

    frames = broker_clerks._sse_frames(source())
    assert b"data: one" in await frames.__anext__()
    assert await frames.__anext__() == b": keepalive\n\n"
    second = b""
    while b"data: two" not in second:
        second = await asyncio.wait_for(frames.__anext__(), timeout=1)
    assert b"data: two" in second
    await frames.aclose()
    assert closed.is_set()


async def test_local_stream_failure_before_headers_returns_the_app_error() -> None:
    async def broken_app(scope, receive, send) -> None:
        raise RuntimeError("stream setup failed")

    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", stream=OperationStream.SSE,
        agent_path_template="/broken"
    )
    request = DeliveryRequest(broker="alpaca", clerk_id="clerk", operation=operation, path_params={})
    handler = _LocalAsgiHandler(broken_app)
    with pytest.raises(RuntimeError, match="stream setup failed"):
        await asyncio.wait_for(handler(request), timeout=1)


def _pending_local_stream_tasks() -> set[asyncio.Task[object]]:
    return {
        task for task in asyncio.all_tasks()
        if not task.done() and (
            task.get_name() == "fleet-local-stream"
            or getattr(task.get_coro(), "__qualname__", "") == "Queue.get"
        )
    }


async def test_cancelling_idle_local_stream_leaves_no_queue_getter_or_app_task() -> None:
    closed = asyncio.Event()

    async def idle_app(scope, receive, send) -> None:
        try:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await receive()
            await receive()
        finally:
            closed.set()

    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", stream=OperationStream.SSE,
        agent_path_template="/idle",
    )
    request = DeliveryRequest(broker="alpaca", clerk_id="clerk", operation=operation, path_params={})
    result = await _LocalAsgiHandler(idle_app)(request)
    consumer = asyncio.create_task(result.events.__anext__())
    try:
        for _ in range(20):
            if any(getattr(task.get_coro(), "__qualname__", "") == "Queue.get" for task in _pending_local_stream_tasks()):
                break
            await asyncio.sleep(0)
        assert any(getattr(task.get_coro(), "__qualname__", "") == "Queue.get" for task in _pending_local_stream_tasks())
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await asyncio.wait_for(closed.wait(), timeout=1)
        assert not _pending_local_stream_tasks()
    finally:
        consumer.cancel()
        tasks = _pending_local_stream_tasks()
        for task in tasks:
            task.cancel()
        await asyncio.gather(consumer, *tasks, return_exceptions=True)


async def test_cancelling_local_refusal_read_leaves_no_queue_getter_or_app_task() -> None:
    closed = asyncio.Event()

    async def refusing_app(scope, receive, send) -> None:
        try:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await receive()
            await receive()
        finally:
            closed.set()

    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", stream=OperationStream.SSE,
        agent_path_template="/refused",
    )
    request = DeliveryRequest(broker="alpaca", clerk_id="clerk", operation=operation, path_params={})
    opening = asyncio.create_task(_LocalAsgiHandler(refusing_app)(request))
    try:
        for _ in range(20):
            if any(getattr(task.get_coro(), "__qualname__", "") == "Queue.get" for task in _pending_local_stream_tasks()):
                break
            await asyncio.sleep(0)
        assert any(getattr(task.get_coro(), "__qualname__", "") == "Queue.get" for task in _pending_local_stream_tasks())
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.wait_for(closed.wait(), timeout=1)
        assert not _pending_local_stream_tasks()
    finally:
        opening.cancel()
        tasks = _pending_local_stream_tasks()
        for task in tasks:
            task.cancel()
        await asyncio.gather(opening, *tasks, return_exceptions=True)


async def test_http_stream_open_is_bounded_and_releases_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.fleet import delivery

    class StalledClient:
        def __init__(self) -> None:
            self.closed = False

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    client = StalledClient()
    monkeypatch.setattr(delivery, "DEFAULT_STREAM_OPEN_TIMEOUT_S", 0.02)
    monkeypatch.setattr(delivery, "build_internal_client", lambda **kwargs: client)
    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", agent_path_template="/stream"
    )
    request = DeliveryRequest(broker="alpaca", clerk_id="clerk", operation=operation, path_params={})
    with pytest.raises(TimeoutError):
        await HttpLaneDelivery(
            base_url="http://127.0.0.1", coordinator_service_token="test-token"
        ).stream(request)
    assert client.closed


async def test_stream_closes_client_when_response_close_fails() -> None:
    class BrokenResponse:
        async def aclose(self) -> None:
            raise OSError("response close failed")

    class TrackedClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    client = TrackedClient()
    stream = _closing_event_iterator(BrokenResponse(), client)
    with pytest.raises(OSError, match="response close failed"):
        await stream.__anext__()
    assert client.closed


async def test_stream_open_failure_closes_client_when_response_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.fleet import delivery

    class BrokenResponse:
        status_code = 503
        headers: dict[str, str] = {}

        async def aread(self) -> bytes:
            return b"lane unavailable"

        async def aclose(self) -> None:
            raise OSError("response close failed")

    class TrackedClient:
        def __init__(self) -> None:
            self.closed = False

        def build_request(self, *args: object, **kwargs: object) -> object:
            return object()

        async def send(self, *args: object, **kwargs: object) -> BrokenResponse:
            return BrokenResponse()

        async def aclose(self) -> None:
            self.closed = True

    client = TrackedClient()
    monkeypatch.setattr(delivery, "build_internal_client", lambda **kwargs: client)
    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", agent_path_template="/stream"
    )
    request = DeliveryRequest(broker="alpaca", clerk_id="clerk", operation=operation, path_params={})
    with pytest.raises(OSError, match="response close failed"):
        await HttpLaneDelivery(
            base_url="http://127.0.0.1", coordinator_service_token="test-token"
        ).stream(request)
    assert client.closed


async def test_private_target_resolution_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.fleet import delivery

    entered = threading.Event()
    release = threading.Event()

    def slow_resolution(url: str) -> None:
        entered.set()
        assert release.wait(timeout=2), url

    monkeypatch.setattr(delivery, "enforce_private_http_target", slow_resolution)
    target = HttpLaneDelivery(base_url="http://fleet.internal", coordinator_service_token="token")
    task = asyncio.create_task(target.validate_target())
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
    finally:
        release.set()
        await task


@pytest.mark.parametrize("value", ["a?query=1", "a#fragment", "a%2fb"])
async def test_path_parameters_reach_the_same_agent_route_locally_and_over_http(value: str) -> None:
    app = FastAPI()

    @app.get("/probe/{value}")
    async def probe(value: str, request: Request) -> JSONResponse:
        return JSONResponse(
            {"value": value, "query": request.query_params.multi_items()},
            headers={"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": "clerk"},
        )

    operation = dataclasses.replace(
        next(iter(ALPACA_OPERATIONS)), method="GET", agent_path_template="/probe/{value}",
        stream=OperationStream.NONE,
    )
    delivery_request = DeliveryRequest(
        broker="alpaca", clerk_id="clerk", operation=operation, path_params={"value": value},
        query=[("strikes", "100"), ("strikes", "105"), ("expiry_ms", "1"), ("expiry_ms", "2")],
    )
    local = await LocalLaneDelivery(_LocalAsgiHandler(app)).deliver(delivery_request)
    server = _RealServer(app)
    server.start()
    try:
        remote = await HttpLaneDelivery(
            base_url=server.base_url, coordinator_service_token="test-token"
        ).deliver(delivery_request)
    finally:
        server.stop()
    assert local.status_code == remote.status_code == 200
    assert local.body == remote.body
    assert b'"query":[["strikes","100"],["strikes","105"],["expiry_ms","1"],["expiry_ms","2"]]' in local.body


@pytest.mark.parametrize(
    "value", ["", ".", "..", "a//b", "../other", "%2e%2e/other", "a/%252e%252e/b", "%" + "25" * 9 + "2e"]
)
def test_path_binding_refuses_empty_and_encoded_traversal_segments(value: str) -> None:
    with pytest.raises(ValueError, match="invalid path parameter"):
        bind_path_template("/orders/{order_ref:path}/cancel", {"order_ref": value})


def test_path_converter_preserves_safe_subpaths_and_quotes_fragment() -> None:
    assert bind_path_template("/orders/{order_ref:path}/cancel", {"order_ref": "a/b#c"}) == (
        "/orders/a/b%23c/cancel"
    )


def test_missing_lane_identity_closes_the_frame_injector_deliberately() -> None:
    with pytest.raises(RuntimeError, match="lane identity disappeared"):
        _FrameInjector(lambda: None).push(b"event: update\ndata: x\n\n")
