"""The single seam for internal fleet HTTP traffic.

Every internal client — coordinator to agent, agent to coordinator — is built
here with the pinned posture (audit 2026-09-13, finding 4): redirects
refused, environment proxy inheritance refused (``trust_env=False``), bounded
timeouts. No fleet-internal call may construct its own ``httpx`` client any
other way, and no internal destination ever comes from a request.

The SSE framing utilities turn a streaming response's bytes into validated
``SseEvent`` values: per-event size caps, comment tolerance, and no partial
dispatch. They exist so clerk-scoped streams (FR-076 per-event identity is
the caller's job) share one hardened parser instead of per-surface ad-hoc
ones. This module deliberately does **not** use httpx's ASGI transport for
delivery: the pinned httpx 0.28.1 implementation buffers a response to
completion before returning it, so a long-lived stream would never deliver
incremental events (audit 2026-09-13, finding 8) — real sockets only.
"""

from __future__ import annotations

import codecs
import ipaddress
import socket
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.utils.throttle import TtlCache

DEFAULT_INTERNAL_TIMEOUT_S = 10.0
DEFAULT_MAX_EVENT_BYTES = 1_000_000


class FleetStreamError(Exception):
    """A clerk-scoped event stream violated the framing contract.

    A transport defect, not an operator refusal: it never crosses the public
    API as itself — the coordinator maps it to the lane's ``unreachable`` or
    ``outcome_unknown`` refusal depending on whether a dispatch was possible.
    """


class FleetTransportRefused(Exception):
    """An internal fleet destination is outside the private-network boundary.

    Cleartext fleet traffic (agent service tokens, worker keys) never leaves
    the private deployment network; an ``http://`` destination that names a
    public address is refused before any byte is sent.
    """


#: Hosts proven to resolve entirely to private addresses, cached through the
#: canonical ``TtlCache`` (``app/utils/throttle.py``) instead of a
#: hand-rolled cache: insertion-ordered ``OrderedDict``, per-entry TTL, and
#: bounded FIFO eviction, at the same 300s/512-entry magnitudes the
#: option-contracts cache already uses (``app/routers/broker.py``). The
#: boundary check runs where a destination is bound (presence and delivery
#: construction), and the lookup is blocking — proven verdicts are cached so
#: repeated construction against the same approved-endpoint host does not
#: repeat a DNS round trip. Only *private* verdicts are cached; a refusal
#: always re-resolves on the next call (see ``enforce_private_http_target``)
#: — cache keys come from approved-endpoint rows and compose config, never
#: from request data.
#:
#: The TTL bounds how long a host that later starts resolving to a public
#: address can keep riding a stale private verdict (DNS-rebinding-adjacent;
#: issue #2112); the size bound caps memory growth if hosts churn faster
#: than entries expire.
#:
#: Value type is ``bool`` (``True``), not ``None``: ``TtlCache.get`` returns
#: ``V | None`` to signal a cache miss, so ``V = None`` would make a hit and
#: a miss indistinguishable.
#:
#: ``TtlCache`` defaults its clock to ``time.monotonic``, which is the right
#: tool for this: the expiry deadline is local, in-process arithmetic that
#: is never stored, put on the wire, or serialized, so it sits outside this
#: repo's temporal authority (``.claude/rules/temporal-rigor.md`` governs
#: representation and scheduled session structure, neither of which this
#: is) — and a wall clock could step backwards under NTP correction or a
#: DST transition, which would let a TTL stall (never expire) or fire
#: early. ``TtlCache``'s ``now=`` constructor parameter is also the test
#: seam: tests substitute a fake clock by constructing a fresh instance and
#: assigning it over this module global, never by patching
#: ``time.monotonic`` itself.
_PRIVATE_HOST_CACHE_TTL_S = 300.0
_PRIVATE_HOST_CACHE_MAX_ENTRIES = 512
_PRIVATE_HOST_CACHE: TtlCache[str, bool] = TtlCache(
    ttl_seconds=_PRIVATE_HOST_CACHE_TTL_S,
    max_size=_PRIVATE_HOST_CACHE_MAX_ENTRIES,
)


def address_is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether one resolved address sits inside the private boundary."""
    return address.is_loopback or address.is_private or address.is_link_local


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a host as an IP literal (zone ids stripped), else ``None``."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def enforce_private_http_target(url: str) -> None:
    """Refuse a cleartext destination outside the private network boundary.

    Fleet topology places coordinators and agents on a private compose
    network, so ``http://`` is acceptable only for loopback, link-local and
    private (RFC 1918 / unique-local) destinations — the service tokens and
    worker keys that ride internal calls must never transit a public hop
    (audit 2026-09-13, finding 4). ``https://`` is accepted anywhere. A host
    name is resolved and every address it returns must be private; an
    unresolvable host cannot be verified, so it is refused rather than
    trusted. Private verdicts are cached per host for
    ``_PRIVATE_HOST_CACHE_TTL_S`` seconds; a refusal is never cached and
    always re-resolves.
    """
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http" or not parsed.hostname:
        raise FleetTransportRefused(
            f"the internal fleet destination {url!r} must be an http(s) URL"
        )
    host = parsed.hostname
    if _PRIVATE_HOST_CACHE.get(host):
        return
    literal = _parse_ip_literal(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            resolved = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise FleetTransportRefused(
                f"the internal fleet destination {url!r} does not resolve: {exc}"
            ) from exc
        addresses = [
            address
            for info in resolved
            if (address := _parse_ip_literal(info[4][0])) is not None
        ]
    for address in addresses:
        if not address_is_private(address):
            raise FleetTransportRefused(
                f"the internal fleet destination {url!r} resolves to the public "
                f"address {address}; cleartext fleet traffic never leaves the "
                "private network — use https or a private address"
            )
    _PRIVATE_HOST_CACHE.set(host, True)


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One complete server-sent event frame.

    ``identity`` carries the per-event provenance fields a serving agent
    injects as ``x-fleet-*`` field lines (FR-076): broker, clerk, routing
    epoch, binding generation. Unknown fields stay ignored per the WHATWG
    framing; these are collected so the delivery layer can validate every
    event against the pinned attempt, not just the response headers.
    """

    event: str
    data: str
    id: str | None
    identity: Mapping[str, str] = field(default_factory=dict)


def build_internal_client(
    *,
    timeout_s: float = DEFAULT_INTERNAL_TIMEOUT_S,
    limits: httpx.Limits | None = None,
    read_timeout_s: float | None = DEFAULT_INTERNAL_TIMEOUT_S,
) -> httpx.AsyncClient:
    """Build the only acceptable internal client.

    ``follow_redirects=False`` so an internal destination can never bounce a
    call elsewhere; ``trust_env=False`` so no ambient proxy environment can
    interpose on fleet traffic. ``read_timeout_s=None`` is for long-lived
    streams: connect, write and pool timeouts stay bounded while the read
    timeout is lifted — the framing layer owns stream liveness, not the
    socket timer.
    """
    timeout = httpx.Timeout(timeout_s, read=read_timeout_s)
    if limits is None:
        return httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        )
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=limits,
    )


def _split_sse_line(buffer: str) -> tuple[str | None, str]:
    """Split one complete WHATWG SSE line off the buffer.

    Lines end with CRLF, LF or a bare CR. A CR at the very end of the buffer
    is held back: it may be the first half of a CRLF split across chunks.
    Returns ``(None, buffer)`` when no complete line has arrived yet.
    """
    lf = buffer.find("\n")
    cr = buffer.find("\r")
    if lf != -1 and (cr == -1 or lf < cr):
        return buffer[:lf], buffer[lf + 1 :]
    if cr != -1:
        if cr == len(buffer) - 1:
            return None, buffer
        if buffer[cr + 1] == "\n":
            return buffer[:cr], buffer[cr + 2 :]
        return buffer[:cr], buffer[cr + 1 :]
    return None, buffer


async def iter_sse_events(
    byte_chunks: AsyncIterator[bytes],
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> AsyncIterator[SseEvent]:
    """Yield complete SSE events from a raw byte-chunk iterator.

    Follows the WHATWG server-sent events framing: ``event:``/``data:``/``id:``
    fields, ``:``-prefixed comments ignored, multi-``data`` lines joined with
    newlines, dispatch on a blank line, and no dispatch when the data buffer
    is empty. A partial event — including a line with no terminator yet —
    counts against ``max_event_bytes`` as it arrives, so a poisoned lane
    cannot grow the coordinator's memory by withholding newlines. Multibyte
    UTF-8 sequences split across chunk boundaries decode through an
    incremental decoder instead of crashing.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    event_name = "message"
    last_event_id: str | None = None
    data_lines: list[str] = []
    identity_fields: dict[str, str] = {}
    buffered = 0
    buffer = ""
    async for chunk in byte_chunks:
        try:
            buffer += decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            raise FleetStreamError(f"the event stream is not valid UTF-8: {exc}") from exc
        # The cap covers the unterminated tail: a chunk without a line
        # terminator counts against the event budget immediately.
        if buffered + len(buffer) > max_event_bytes:
            raise FleetStreamError(
                f"a server-sent event exceeded the {max_event_bytes}-byte cap "
                "before completing"
            )
        while True:
            line, buffer = _split_sse_line(buffer)
            if line is None:
                break
            buffered += len(line)
            if line == "":
                if data_lines:
                    yield SseEvent(
                        event=event_name,
                        data="\n".join(data_lines),
                        id=last_event_id,
                        identity=dict(identity_fields),
                    )
                event_name = "message"
                data_lines = []
                identity_fields = {}
                buffered = 0
                continue
            if line.startswith(":"):
                continue
            field, _separator, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
            elif field.startswith("x-fleet-"):
                identity_fields[field] = value
            elif field == "id" and "\x00" not in value:
                # The last-event-id buffer persists across events, per the
                # WHATWG framing: a following event without an id field still
                # carries it, which is what reconnect cursors depend on.
                last_event_id = value
            if buffered > max_event_bytes:
                raise FleetStreamError(
                    f"a server-sent event exceeded the {max_event_bytes}-byte cap "
                    "before completing"
                )
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise FleetStreamError(
            f"the event stream ended mid UTF-8 sequence: {exc}"
        ) from exc
    # An event still unterminated when the stream ends is incomplete and is
    # dropped, per the WHATWG framing.


async def iter_sse_from_response(
    response: httpx.Response,
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> AsyncIterator[SseEvent]:
    """Stream parsed SSE events from one open streaming response."""
    async for event in iter_sse_events(
        response.aiter_bytes(), max_event_bytes=max_event_bytes
    ):
        yield event


__all__ = [
    "DEFAULT_INTERNAL_TIMEOUT_S",
    "DEFAULT_MAX_EVENT_BYTES",
    "FleetStreamError",
    "FleetTransportRefused",
    "SseEvent",
    "address_is_private",
    "build_internal_client",
    "enforce_private_http_target",
    "iter_sse_events",
    "iter_sse_from_response",
]
