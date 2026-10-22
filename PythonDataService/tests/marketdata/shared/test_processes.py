"""Real TCP delivery to separate Paper/Live-shaped worker processes, no Gateway."""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import socket
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from urllib.request import urlopen

import pytest

from app.marketdata.shared.hub import FeedHub
from app.marketdata.shared.protocol import Frame, Subscription
from app.marketdata.shared.server import create_app
from app.marketdata.shared.transport import FeedTransport
from tests.marketdata.shared.test_hub import quote, subscription


def serve(listener: socket.socket, events: Any) -> None:
    import uvicorn

    class Source:
        async def stream(self, key: Subscription) -> AsyncIterator[Frame]:
            events.put(("opened", key.instrument.con_id))
            try:
                while True:
                    yield quote(12, receipt=1000)
                    await asyncio.sleep(0.05)
            finally:
                events.put(("closed", key.instrument.con_id))

    uvicorn.Server(uvicorn.Config(create_app(FeedHub(Source()), "test-secret"), log_level="error")).run(
        sockets=[listener]
    )


def consume(url: str, con_id: int, events: Any, release: Any) -> None:
    async def run() -> None:
        async with aclosing(FeedTransport(url, "test-secret").stream(subscription(con_id))) as frames:
            frame = await anext(frames)
            events.put(("received", con_id, frame.received_at_ms, frame.quote.values["bid"]))
            await asyncio.to_thread(release.wait, 15)

    asyncio.run(run())


@pytest.fixture
def server() -> Any:
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    process = context.Process(target=serve, args=(listener, events))
    process.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(url + "/health", timeout=0.2) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("test feed did not start") from None
                time.sleep(0.05)
        yield context, url, events
    finally:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
        listener.close()
        events.close()


@pytest.mark.parametrize("con_id", [101, 987654])
def test_two_workers_share_any_contract_and_one_worker_stopping_does_not_cancel_peer(server: Any, con_id: int) -> None:
    context, url, events = server
    release = [context.Event(), context.Event()]
    workers = [context.Process(target=consume, args=(url, con_id, events, signal)) for signal in release]
    for worker in workers:
        worker.start()
    seen = []
    try:
        while sum(item[0] == "received" for item in seen) < 2:
            seen.append(events.get(timeout=15))
        assert [item for item in seen if item[0] == "opened"] == [("opened", con_id)]
        assert [item[2:] for item in seen if item[0] == "received"] == [(1000, 12), (1000, 12)]
        release[0].set()
        workers[0].join(5)
        assert workers[0].exitcode == 0
        with pytest.raises(queue.Empty):
            events.get(timeout=0.3)
        release[1].set()
        workers[1].join(5)
        assert workers[1].exitcode == 0
        assert events.get(timeout=5) == ("closed", con_id)
    finally:
        for signal in release:
            signal.set()
        for worker in workers:
            worker.join(5)
            if worker.is_alive():
                worker.kill()
                worker.join()
