"""Real-socket qualification of the internal HTTP seam (audit 2026-09-13, finding 8).

These tests run a real asyncio HTTP server on a real loopback socket — not an
in-process ASGI transport — because the audit's finding is precisely about
behavior only a real transport exhibits: incremental SSE delivery, refused
redirects, one-way cancellation, and the proof that the pinned httpx 0.28.1
ASGI transport buffers a streaming response to completion and therefore must
never carry clerk-scoped streams.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from httpx._transports.asgi import ASGITransport

from app.broker.fleet.internal_http import (
    FleetStreamError,
    build_internal_client,
    iter_sse_from_response,
)


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    """Read one request head from the socket; returns method-with-path and headers."""
    request_line = (await reader.readline()).decode().strip()
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode().strip()
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return request_line, headers


class _InternalServer:
    """A minimal loopback HTTP/1.1 server speaking the fleet's internal shapes."""

    def __init__(self) -> None:
        self.first_event_seen = asyncio.Event()
        self.disconnect_seen = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        self.base_url = ""

    async def start(self) -> None:
        """Listen on an ephemeral loopback port."""
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        """Close the listener and wait for it."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request, _headers = await _read_request(reader)
            if request.startswith("GET /events"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                )
                await writer.drain()
                writer.write(b"event: snapshot\ndata: first\n\n")
                await writer.drain()
                self.first_event_seen.set()
                # Hold the stream open: a buffering transport never yields
                # even the first event before this server finishes.
                await self.first_event_seen.wait()
                await asyncio.sleep(0.05)
                writer.write(b"id: 42\nevent: update\ndata: second\ndata: cont\n\n")
                await writer.drain()
                await asyncio.sleep(10)
            elif request.startswith("GET /redirect"):
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: /events\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
            elif request.startswith("GET /huge"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                )
                await writer.drain()
                writer.write(b"data: " + b"x" * 64 + b"\n")
                await writer.drain()
                await asyncio.sleep(10)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            self.disconnect_seen.set()
        finally:
            with contextlib.suppress(ConnectionError, RuntimeError):
                writer.close()


@pytest.fixture
async def internal_server():
    """One running loopback server per test."""
    server = _InternalServer()
    await server.start()
    yield server
    await server.stop()


async def test_sse_events_arrive_incrementally_over_a_real_socket(
    internal_server: _InternalServer,
) -> None:
    """The first event is delivered while the stream is still open — the
    property the buffered ASGI transport cannot provide."""
    client = build_internal_client()
    try:
        async with client.stream("GET", f"{internal_server.base_url}/events") as response:
            events = []
            async for event in iter_sse_from_response(response):
                events.append(event)
                if len(events) == 1:
                    # Prove incrementality: the second event has not been
                    # written yet, and the response is far from complete.
                    assert internal_server.first_event_seen.is_set()
                    break
            assert events[0].event == "snapshot"
            assert events[0].data == "first"
    finally:
        await client.aclose()


async def test_multi_data_lines_join_and_comments_are_ignored(
    internal_server: _InternalServer,
) -> None:
    """Complete framing: joined data lines, ids, comments and default names."""
    client = build_internal_client()
    try:
        async with client.stream("GET", f"{internal_server.base_url}/events") as response:
            received = []
            async for event in iter_sse_from_response(response):
                received.append(event)
                if len(received) == 2:
                    break
        second = received[1]
        assert second.event == "update"
        assert second.data == "second\ncont"
        assert second.id == "42"
    finally:
        await client.aclose()


async def test_internal_clients_refuse_redirects_and_proxies() -> None:
    """The pinned client posture: no redirect following, no environment proxies."""
    client = build_internal_client()
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
    finally:
        await client.aclose()


async def test_redirects_are_not_followed_on_internal_traffic(
    internal_server: _InternalServer,
) -> None:
    """A 302 from an internal destination stays a 302 — no bounce, ever."""
    client = build_internal_client()
    try:
        response = await client.get(f"{internal_server.base_url}/redirect")
        assert response.status_code == 302
        assert response.headers["location"] == "/events"
    finally:
        await client.aclose()


async def test_an_oversized_event_refuses_instead_of_buffering(
    internal_server: _InternalServer,
) -> None:
    """The framing cap refuses an unbounded payload before it completes."""
    client = build_internal_client()
    try:
        async with client.stream("GET", f"{internal_server.base_url}/huge") as response:
            with pytest.raises(FleetStreamError, match="byte cap"):
                async for _event in iter_sse_from_response(
                    response, max_event_bytes=16
                ):
                    pass
    finally:
        await client.aclose()


async def test_cancellation_propagates_to_the_open_stream(
    internal_server: _InternalServer,
) -> None:
    """Cancelling the consumer cancels the stream and disconnects the server."""
    client = build_internal_client()

    async def consume() -> None:
        async with client.stream("GET", f"{internal_server.base_url}/events") as response:
            async for _event in iter_sse_from_response(response):
                pass  # runs until cancelled

    task = asyncio.create_task(consume())
    await asyncio.wait_for(internal_server.first_event_seen.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()


async def test_the_asgi_transport_buffers_and_must_not_carry_streams() -> None:
    """The documented negative proof (audit 2026-09-13, finding 8): httpx
    0.28.1's ASGI transport collects the response before returning it, so a
    long-lived stream never delivers incrementally through it."""

    async def never_completing_app(scope, receive, send):  # type: ignore[no-untyped-def]
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        # Stream one event, then hold the response open forever — exactly a
        # clerk-scoped stream's shape.
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=ASGITransport(app=never_completing_app)) as client:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.get("http://internal/events"), timeout=0.5)


def test_the_seam_module_imports_no_provider_surface() -> None:
    """Import-isolation corollary: the internal HTTP seam imports no provider."""
    import inspect

    from app.broker.fleet import internal_http

    source = inspect.getsource(internal_http)
    assert "alpaca" not in source.lower()


async def _chunks_of(payload: bytes, *, split_at: int = 1):
    """Re-yield a payload as byte chunks of the given size."""
    for index in range(0, len(payload), split_at):
        yield payload[index : index + split_at]


async def test_an_unterminated_line_cannot_grow_memory_unboundedly() -> None:
    """Regression (independent review): the cap must count bytes that arrive
    without a line terminator — withholding newlines is not a bypass."""
    from app.broker.fleet.internal_http import iter_sse_events

    async def poisoned():
        yield b"data: "
        for _ in range(80):
            yield b"x" * 100  # 8000 bytes, never one newline

    with pytest.raises(FleetStreamError, match="byte cap"):
        async for _event in iter_sse_events(poisoned(), max_event_bytes=1024):
            pass


async def test_multibyte_payloads_split_across_chunks_parse() -> None:
    """Regression (independent review): a UTF-8 sequence split at an arbitrary
    chunk boundary decodes instead of crashing."""
    from app.broker.fleet.internal_http import iter_sse_events

    payload = "event: update\ndata: 🚀 launch ok\n\n".encode()
    events = []
    async for event in iter_sse_events(_chunks_of(payload, split_at=1)):
        events.append(event)
    assert len(events) == 1
    assert events[0].event == "update"
    assert events[0].data == "🚀 launch ok"


async def test_a_leading_bom_is_stripped_and_truncated_utf8_refuses() -> None:
    """A leading U+FEFF is not payload; a stream ending mid-sequence refuses."""
    from app.broker.fleet.internal_http import iter_sse_events

    bommed = "﻿data: clean\n\n".encode()
    events = [event async for event in iter_sse_events(_chunks_of(bommed, split_at=3))]
    assert events == [events[0]]
    assert events[0].data == "clean"

    async def truncated():
        yield b"data: half"
        yield "🚀".encode()[:1]  # first byte of a 4-byte sequence, then end

    with pytest.raises(FleetStreamError, match="mid UTF-8 sequence"):
        async for _event in iter_sse_events(truncated()):
            pass


async def test_empty_data_events_and_bare_cr_terminators_follow_the_spec() -> None:
    """No dispatch without data; CR and CRLF are line terminators."""
    from app.broker.fleet.internal_http import SseEvent, iter_sse_events

    frames = b"event: ping\n\nevent: a\rdata: cr-line\r\revent: b\r\ndata: crlf-line\r\n\r\n"
    events = [event async for event in iter_sse_events(_chunks_of(frames, split_at=5))]
    # `event: ping` with no data buffer never dispatches (WHATWG).
    assert events == [
        SseEvent(event="a", data="cr-line", id=None),
        SseEvent(event="b", data="crlf-line", id=None),
    ]


def test_cleartext_fleet_traffic_stays_inside_the_private_boundary() -> None:
    """http:// is loopback/private only; https goes anywhere; garbage refuses.

    The agent service token and worker key ride internal calls, so a public
    cleartext destination is refused before any byte is sent (audit
    2026-09-13, finding 4). IP literals are judged directly; an unresolvable
    host name cannot be verified and refuses rather than being trusted.
    """
    from app.broker.fleet.internal_http import (
        FleetTransportRefused,
        enforce_private_http_target,
    )

    # https is accepted anywhere, http only inside the boundary.
    enforce_private_http_target("https://fleet.example.com:8443")
    enforce_private_http_target("http://127.0.0.1:9001")
    enforce_private_http_target("http://[::1]:9001")
    enforce_private_http_target("http://192.168.1.20:8000")
    enforce_private_http_target("http://10.0.0.5:8000")
    enforce_private_http_target("http://[fd00::5]:8000")
    enforce_private_http_target("http://localhost:9001")

    with pytest.raises(FleetTransportRefused, match="public address"):
        enforce_private_http_target("http://93.184.216.34:8000")
    with pytest.raises(FleetTransportRefused, match="public address"):
        enforce_private_http_target("http://[2606:2800:220:1:248:1893:25c8:1946]:80")
    with pytest.raises(FleetTransportRefused, match="must be an http"):
        enforce_private_http_target("ftp://127.0.0.1:9001")
    with pytest.raises(FleetTransportRefused, match="does not resolve"):
        enforce_private_http_target("http://no-such-fleet-host.invalid:9001")
