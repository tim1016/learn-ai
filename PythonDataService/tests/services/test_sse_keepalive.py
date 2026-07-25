"""Behavior tests for quiet-stream SSE control framing."""

from __future__ import annotations

import asyncio

import pytest

from app.services.sse_keepalive import SSE_FORWARD_QUEUE_MAXSIZE, stream_sse_with_keepalive


@pytest.mark.asyncio
async def test_stream_sse_with_keepalive_marks_a_quiet_stream_ready_then_alive() -> None:
    release = asyncio.Event()

    async def quiet_frames():
        await release.wait()
        if False:  # pragma: no cover - makes this an async generator for typing.
            yield ""

    stream = stream_sse_with_keepalive(
        quiet_frames(), heartbeat_interval_s=0.001, clock=lambda: 1_700_000_000_000
    )
    try:
        assert await anext(stream) == 'event: ready\ndata: {"ts_ms": 1700000000000}\n\n'
        assert await anext(stream) == 'event: heartbeat\ndata: {"ts_ms": 1700000000000}\n\n'
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_sse_with_keepalive_preserves_domain_frames() -> None:
    async def frames():
        yield "event: order\ndata: {\"order_id\": 42}\n\n"

    stream = stream_sse_with_keepalive(frames(), clock=lambda: 1)

    assert await anext(stream) == 'event: ready\ndata: {"ts_ms": 1}\n\n'
    assert await anext(stream) == 'event: order\ndata: {"order_id": 42}\n\n'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_stream_sse_with_keepalive_rejects_non_positive_heartbeat_interval() -> None:
    async def frames():
        if False:  # pragma: no cover - makes this an async generator for typing.
            yield ""

    stream = stream_sse_with_keepalive(frames(), heartbeat_interval_s=0)

    with pytest.raises(ValueError, match="heartbeat_interval_s must be positive"):
        await anext(stream)


@pytest.mark.asyncio
async def test_stream_sse_with_keepalive_backpressures_a_slow_consumer() -> None:
    attempted_overflow = asyncio.Event()
    advanced_past_overflow = asyncio.Event()

    async def burst_frames():
        for index in range(SSE_FORWARD_QUEUE_MAXSIZE + 1):
            if index == SSE_FORWARD_QUEUE_MAXSIZE:
                attempted_overflow.set()
            yield f"event: order\ndata: {index}\n\n"
            if index == SSE_FORWARD_QUEUE_MAXSIZE:
                advanced_past_overflow.set()

    stream = stream_sse_with_keepalive(burst_frames())
    try:
        assert (await anext(stream)).startswith("event: ready")
        await asyncio.wait_for(attempted_overflow.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not advanced_past_overflow.is_set()
    finally:
        await stream.aclose()
