from __future__ import annotations

from contextlib import aclosing

import httpx
import pytest
import respx

from app.broker.ibkr.live.hub import FeedHub
from app.broker.ibkr.live.protocol import FeedUnavailable, SourceEpoch, SourceHealth
from app.broker.ibkr.live.server import create_app
from app.broker.ibkr.live.transport import MAX_FRAME_BYTES, FeedTransport
from tests.marketdata.shared.test_hub import Source, quote, subscription


async def test_authentication_refuses_before_opening_a_source() -> None:
    source = Source()
    app = create_app(
        FeedHub(source),
        "secret",
        lambda: SourceHealth(connected=True, epoch=SourceEpoch(started_at_ms=0, owner="test"), observed_at_ms=1000),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/stream", json=subscription().model_dump())
    assert response.status_code == 403 and not source.opened


@pytest.mark.parametrize("payload", [b"bad json\n", b"x" * (MAX_FRAME_BYTES + 1)])
async def test_invalid_or_unbounded_frame_fails_closed(payload: bytes) -> None:
    with respx.mock:
        respx.post("http://feed/stream").mock(return_value=httpx.Response(200, content=payload))
        async with aclosing(FeedTransport("http://feed", "secret").stream(subscription())) as frames:
            with pytest.raises(FeedUnavailable):
                await anext(frames)


async def test_receipt_survives_the_wire_unchanged() -> None:
    frame = quote(3.25, receipt=123)
    with respx.mock:
        respx.post("http://feed/stream").mock(return_value=httpx.Response(200, content=frame.model_dump_json() + "\n"))
        async with aclosing(FeedTransport("http://feed", "secret").stream(subscription())) as frames:
            assert await anext(frames) == frame
