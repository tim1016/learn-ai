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
                    )
                event_name = "message"
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
    "SseEvent",
    "build_internal_client",
    "iter_sse_events",
    "iter_sse_from_response",
]
