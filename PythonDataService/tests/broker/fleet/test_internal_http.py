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
import socket
from collections.abc import Callable

import httpx
import pytest
from httpx._transports.asgi import ASGITransport

from app.broker.fleet import internal_http
from app.broker.fleet.internal_http import (
    FleetStreamError,
    build_internal_client,
    iter_sse_from_response,
)
from app.utils.throttle import TtlCache


@pytest.fixture(autouse=True)
def _reset_private_host_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own fresh cache instance.

    ``_PRIVATE_HOST_CACHE`` is module-global state; without this, which test
    happens to run first would decide whether a given host name is already
    cached private, making the TTL/eviction/rebinding tests below order
    -dependent on tests elsewhere in this file. ``TtlCache`` has no
    ``clear()``, so a fresh instance (same TTL/bound as production) replaces
    the module global instead.
    """
    monkeypatch.setattr(
        internal_http,
        "_PRIVATE_HOST_CACHE",
        TtlCache(
            ttl_seconds=internal_http._PRIVATE_HOST_CACHE_TTL_S,
            max_size=internal_http._PRIVATE_HOST_CACHE_MAX_ENTRIES,
        ),
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
        self.hold_open = asyncio.Event()
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
        # Defensive release: wait_closed() below blocks on any still-open
        # connection handler, and a handler that reached the /events hold
        # only proceeds once hold_open is set. A test that forgets to
        # release it must fail fast here, not wedge the whole file.
        self.hold_open.set()
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
                # Hold the stream open until the test releases it: a
                # buffering transport never yields even the first event
                # before this server finishes.
                await self.hold_open.wait()
                await asyncio.sleep(0.05)
                writer.write(b": keepalive\nid: 42\nevent: update\ndata: second\ndata: cont\n\n")
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
                    # Prove incrementality: the server is still genuinely
                    # blocked before writing anything further — the second
                    # event cannot yet have been written — not merely that
                    # the first event happened to arrive.
                    assert not internal_server.hold_open.is_set()
                    internal_server.hold_open.set()
                    break
            assert events[0].event == "snapshot"
            assert events[0].data == "first"
    finally:
        await client.aclose()


async def test_multi_data_lines_join_and_comments_are_ignored(
    internal_server: _InternalServer,
) -> None:
    """Complete framing: joined data lines, ids, comments and default names."""
    internal_server.hold_open.set()
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
    """Cancelling the consumer cancels the open stream at the client."""
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
    # Release the server's connection handler so fixture teardown does not
    # wait on a stream this test intentionally never finishes reading.
    internal_server.hold_open.set()


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
    assert len(events) == 1
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


def _fake_resolution(address: str) -> list[tuple]:
    """Build a ``socket.getaddrinfo``-shaped result resolving to one address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]


def _swap_in_private_host_cache(
    monkeypatch: pytest.MonkeyPatch, *, now: Callable[[], float] = lambda: 0.0
) -> None:
    """Replace the module-global cache with a fresh instance on a controlled
    clock — ``TtlCache``'s ``now=`` parameter is the test seam; the clock is
    never controlled by patching ``time.monotonic`` itself."""
    monkeypatch.setattr(
        internal_http,
        "_PRIVATE_HOST_CACHE",
        TtlCache(
            ttl_seconds=internal_http._PRIVATE_HOST_CACHE_TTL_S,
            max_size=internal_http._PRIVATE_HOST_CACHE_MAX_ENTRIES,
            now=now,
        ),
    )


def test_a_cached_private_verdict_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private verdict is not trusted forever: once the TTL elapses, the
    next call re-resolves the host instead of trusting the stale cache entry
    (issue #2112 requirement (a))."""
    resolve_calls: list[str] = []

    def fake_getaddrinfo(host: str, port: object) -> list[tuple]:
        resolve_calls.append(host)
        return _fake_resolution("10.0.0.5")

    clock = {"now": 0.0}
    monkeypatch.setattr(internal_http.socket, "getaddrinfo", fake_getaddrinfo)
    _swap_in_private_host_cache(monkeypatch, now=lambda: clock["now"])

    internal_http.enforce_private_http_target("http://ttl-host.internal:8000")
    assert resolve_calls == ["ttl-host.internal"]

    # Still inside the TTL window: served from cache, no second resolution.
    clock["now"] += internal_http._PRIVATE_HOST_CACHE_TTL_S - 1.0
    internal_http.enforce_private_http_target("http://ttl-host.internal:8000")
    assert resolve_calls == ["ttl-host.internal"]

    # Past the TTL: the cached verdict has expired, so this re-resolves.
    clock["now"] += 2.0
    internal_http.enforce_private_http_target("http://ttl-host.internal:8000")
    assert resolve_calls == ["ttl-host.internal", "ttl-host.internal"]


def test_the_private_host_cache_is_bounded_by_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feeding more distinct private hosts than the bound evicts the oldest
    entry: proven behaviorally through the public cache API (no reaching
    into ``TtlCache`` internals) — the oldest host re-resolves once the
    bound is exceeded, while the most recent stays cached (issue #2112
    requirement (b))."""
    resolve_calls: list[str] = []

    def fake_getaddrinfo(host: str, port: object) -> list[tuple]:
        resolve_calls.append(host)
        return _fake_resolution("10.0.0.5")

    monkeypatch.setattr(internal_http.socket, "getaddrinfo", fake_getaddrinfo)
    _swap_in_private_host_cache(monkeypatch)

    bound = internal_http._PRIVATE_HOST_CACHE_MAX_ENTRIES
    hosts = [f"http://bounded-host-{index}.internal:8000" for index in range(bound)]
    for host_url in hosts:
        internal_http.enforce_private_http_target(host_url)
    assert len(resolve_calls) == bound

    # One more distinct host exceeds the bound: the oldest entry (host 0)
    # is evicted to make room.
    internal_http.enforce_private_http_target("http://bounded-host-overflow.internal:8000")
    assert len(resolve_calls) == bound + 1

    # The oldest host is no longer cached: querying it again re-resolves.
    internal_http.enforce_private_http_target(hosts[0])
    assert resolve_calls[-1] == "bounded-host-0.internal"
    assert len(resolve_calls) == bound + 2

    # The most recent host inserted before the overflow is still cached.
    internal_http.enforce_private_http_target(hosts[-1])
    assert len(resolve_calls) == bound + 2


def test_a_host_that_rebinds_public_after_ttl_expiry_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DNS-rebinding-adjacent shape the issue names: with the TTL in
    place, a host that resolves private, then resolves public after its
    cached verdict's TTL elapses, is refused on the next call rather than
    silently allowed off a stale cache hit (issue #2112 requirement (c)).

    This exercises the fixed implementation directly by swapping in a
    ``TtlCache`` on a controlled clock, so it cannot also run unmodified
    against pre-fix code (the module-global cache's type changed from a
    plain ``dict`` to a ``TtlCache`` instance). The standalone reproduction
    against unmodified master — two plain calls with the resolution changed
    between them, no test-seam dependency, showing the second is silently
    allowed — is captured in the PR description instead.
    """
    responses = iter([_fake_resolution("10.0.0.5"), _fake_resolution("93.184.216.34")])

    def fake_getaddrinfo(host: str, port: object) -> list[tuple]:
        del host, port
        return next(responses)

    clock = {"now": 0.0}
    monkeypatch.setattr(internal_http.socket, "getaddrinfo", fake_getaddrinfo)
    _swap_in_private_host_cache(monkeypatch, now=lambda: clock["now"])

    internal_http.enforce_private_http_target("http://rebind-host.internal:8000")

    clock["now"] += internal_http._PRIVATE_HOST_CACHE_TTL_S + 1.0
    with pytest.raises(internal_http.FleetTransportRefused, match="public address"):
        internal_http.enforce_private_http_target("http://rebind-host.internal:8000")


def test_a_public_verdict_is_never_cached_and_always_re_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusals must keep re-resolving on every call, never getting cached
    (issue #2112 requirement (d) — the property the TTL/eviction fix must not
    disturb)."""
    resolve_calls: list[str] = []

    def fake_getaddrinfo(host: str, port: object) -> list[tuple]:
        resolve_calls.append(host)
        return _fake_resolution("93.184.216.34")

    monkeypatch.setattr(internal_http.socket, "getaddrinfo", fake_getaddrinfo)
    _swap_in_private_host_cache(monkeypatch)

    for _ in range(3):
        with pytest.raises(internal_http.FleetTransportRefused, match="public address"):
            internal_http.enforce_private_http_target("http://public-host.internal:8000")

    assert resolve_calls == ["public-host.internal"] * 3
    assert internal_http._PRIVATE_HOST_CACHE.get("public-host.internal") is None
