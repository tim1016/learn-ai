"""SSE control frames for quiet but healthy live streams.

The broker order stream is allowed to be idle for long periods.  A ready frame
and periodic heartbeat distinguish that healthy silence from a proxy-buffered
or stalled stream without manufacturing a broker order event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable

from app.utils.timestamps import now_ms_utc

DEFAULT_SSE_HEARTBEAT_INTERVAL_S = 15.0
SSE_FORWARD_QUEUE_MAXSIZE = 64


def _control_frame(event_name: str, ts_ms: int) -> str:
    """Serialize one non-domain SSE control frame with a UTC-ms timestamp."""

    return f"event: {event_name}\ndata: {json.dumps({'ts_ms': ts_ms})}\n\n"


async def stream_sse_with_keepalive(
    frames: AsyncIterator[str],
    *,
    heartbeat_interval_s: float = DEFAULT_SSE_HEARTBEAT_INTERVAL_S,
    clock: Callable[[], int] = now_ms_utc,
) -> AsyncIterator[str]:
    """Yield an SSE ready frame, domain frames, and idle heartbeats.

    ``asyncio.wait_for`` cannot be used here: its timeout cancels the pending
    ``anext`` call and would therefore terminate the broker event iterator.
    A small producer task leaves that iterator alive while the consumer emits
    heartbeats during otherwise healthy inactivity.
    """

    if heartbeat_interval_s <= 0:
        raise ValueError("heartbeat_interval_s must be positive")

    end_of_stream = object()
    queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=SSE_FORWARD_QUEUE_MAXSIZE)

    async def forward_frames() -> None:
        cancelled = False
        try:
            async for frame in frames:
                await queue.put(frame)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                await queue.put(end_of_stream)

    producer = asyncio.create_task(forward_frames(), name="sse-keepalive-forwarder")
    try:
        yield _control_frame("ready", clock())
        while True:
            try:
                frame = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_interval_s
                )
            except TimeoutError:
                yield _control_frame("heartbeat", clock())
                continue

            if frame is end_of_stream:
                await producer
                return
            yield frame
    finally:
        if not producer.done():
            producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
