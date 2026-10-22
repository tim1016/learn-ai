from __future__ import annotations

import asyncio
from contextlib import aclosing

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.broker.ibkr.config import IbkrSettings
from app.marketdata.shared.connection import SharedConnection
from app.marketdata.shared.protocol import FeedUnavailable, SourceHealth
from app.utils.timestamps import now_ms_utc
from tests.marketdata.shared.test_consumers import EPOCH, quote_update
from tests.marketdata.shared.test_hub import subscription


def connection() -> SharedConnection:
    return SharedConnection(
        IbkrSettings(_env_file=None, shared_feed_url="http://feed", shared_feed_token="test-secret")
    )


async def test_source_health_is_authenticated_and_independent_of_contract_refusals() -> None:
    ready = asyncio.Event()
    health = SourceHealth(connected=True, epoch=EPOCH, observed_at_ms=now_ms_utc())

    def read(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        ready.set()
        return httpx.Response(200, content=health.model_dump_json())

    shared = connection()
    with respx.mock:
        respx.get("http://feed/source-health").mock(side_effect=read)
        shared.start()
        try:
            await asyncio.wait_for(ready.wait(), 1)
            await asyncio.sleep(0)
            assert shared.is_connected()
            assert shared.connection_generation == 1
        finally:
            await shared.close()
    assert not shared.is_connected()


async def test_old_quote_epoch_is_rejected_but_known_pre_disconnect_bar_epoch_keeps_its_identity() -> None:
    shared = connection()
    new_epoch = EPOCH.replace(":1:0:0", ":2:0:0")
    frames = [
        quote_update((1, 2, 1)),
        quote_update((1, 3, 1)).model_copy(update={"epoch": new_epoch}),
        quote_update((1, 2, 1)),
    ]
    with respx.mock:
        respx.post("http://feed/stream").mock(
            return_value=httpx.Response(200, content="\n".join(f.model_dump_json() for f in frames) + "\n")
        )
        async with aclosing(shared.stream(subscription())) as source:
            await anext(source)
            old_generation = shared.connection_generation
            await anext(source)
            assert shared.connection_generation == old_generation + 1
            assert shared.bar_generation(EPOCH) == old_generation
            with pytest.raises(FeedUnavailable, match="retired generation"):
                await anext(source)


def test_configured_shared_source_requires_an_explicit_secret() -> None:
    with pytest.raises(ValidationError, match="TOKEN"):
        IbkrSettings(_env_file=None, shared_feed_url="http://feed", shared_feed_token=None)
