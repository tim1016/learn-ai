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

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

DEFAULT_INTERNAL_TIMEOUT_S = 10.0
DEFAULT_MAX_EVENT_BYTES = 1_000_000


class FleetStreamError(Exception):
    """A clerk-scoped event stream violated the framing contract.

    A transport defect, not an operator refusal: it never crosses the public
    API as itself — the coordinator maps it to the lane's ``unreachable`` or
    ``outcome_unknown`` refusal depending on whether a dispatch was possible.
    """


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One complete server-sent event frame."""

    event: str
    data: str
    id: str | None


def build_internal_client(
    *,
    timeout_s: float = DEFAULT_INTERNAL_TIMEOUT_S,
    limits: httpx.Limits | None = None,
) -> httpx.AsyncClient:
    """Build the only acceptable internal client.

    ``follow_redirects=False`` so an internal destination can never bounce a
    call elsewhere; ``trust_env=False`` so no ambient proxy environment can
    interpose on fleet traffic.
    """
    if limits is None:
        return httpx.AsyncClient(timeout=timeout_s, follow_redirects=False, trust_env=False)
    return httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=False,
        trust_env=False,
        limits=limits,
    )


async def iter_sse_events(
    byte_chunks: AsyncIterator[bytes],
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> AsyncIterator[SseEvent]:
    """Yield complete SSE events from a raw byte-chunk iterator.

    Follows the WHATWG server-sent events framing: ``event:``/``data:``/``id:``
    fields, ``:``-prefixed comments ignored, multi-``data`` lines joined with
    newlines, dispatch on a blank line. A single event larger than
    ``max_event_bytes`` refuses — a lane cannot smuggle an unbounded payload
    through the framing layer.
    """
    event_name = "message"
    event_id: str | None = None
    data_lines: list[str] = []
    buffered = 0
    buffer = ""
    async for chunk in byte_chunks:
        buffer += chunk.decode("utf-8", errors="strict")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line == "":
                if data_lines or event_name != "message" or event_id is not None:
                    yield SseEvent(
                        event=event_name,
                        data="\n".join(data_lines),
                        id=event_id,
                    )
                event_name = "message"
                event_id = None
                data_lines = []
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
            elif field == "id" and "\x00" not in value:
                event_id = value
            buffered += len(line)
            if buffered > max_event_bytes:
                raise FleetStreamError(
                    f"a server-sent event exceeded the {max_event_bytes}-byte cap "
                    "before completing"
                )


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
    "SseEvent",
    "build_internal_client",
    "iter_sse_events",
    "iter_sse_from_response",
]
