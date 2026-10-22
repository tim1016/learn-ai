"""Installation sharing, receipts, and reader isolation through the real hub."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.marketdata.shared.hub import FeedHub
from app.marketdata.shared.protocol import Frame, Instrument, QuoteUpdate, Subscription, Tick


def subscription(con_id: int = 1, kind: str = "quotes") -> Subscription:
    return Subscription(
        instrument=Instrument(con_id=con_id, exchange="SMART", currency="USD", sec_type="OPT"), kind=kind
    )


def quote(price: float, receipt: int = 1000, tick: int = 1, data_type: int = 1) -> Frame:
    return Frame(
        kind="quote",
        epoch="boot:1",
        received_at_ms=receipt,
        quote=QuoteUpdate(
            data_type=data_type,
            ticks=(Tick(kind=tick, price=price, size=1),),
            values={"bid": price},
            greeks={},
        ),
    )


class Source:
    def __init__(self) -> None:
        self.opened: list[Subscription] = []
        self.closed: list[Subscription] = []
        self.input: asyncio.Queue[Frame | Exception] = asyncio.Queue()

    async def stream(self, key: Subscription) -> AsyncIterator[Frame]:
        self.opened.append(key)
        try:
            while True:
                frame = await self.input.get()
                if isinstance(frame, Exception):
                    raise frame
                yield frame
        finally:
            self.closed.append(key)


async def started(hub: FeedHub, key: Subscription) -> tuple[AsyncIterator[Frame], asyncio.Task[Frame]]:
    stream = hub.stream(key)
    read = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return stream, read


async def test_same_option_reuses_line_and_last_reader_releases_it() -> None:
    source = Source()
    hub = FeedHub(source)
    one, first = await started(hub, subscription())
    two, second = await started(hub, subscription())
    await source.input.put(quote(2))
    assert (await first).quote == (await second).quote
    assert len(source.opened) == 1
    await one.aclose()
    assert not source.closed
    read = asyncio.create_task(anext(two))
    await source.input.put(quote(3))
    assert (await read).quote.values["bid"] == 3
    await two.aclose()
    assert len(source.closed) == 1


async def test_late_join_waits_for_a_current_callback_without_renewing_a_receipt() -> None:
    source = Source()
    hub = FeedHub(source)
    one, read = await started(hub, subscription())
    await source.input.put(quote(2, receipt=1000))
    await read
    two, pending = await started(hub, subscription())
    assert not pending.done()
    await source.input.put(quote(5, receipt=2000, tick=2, data_type=3))
    delayed = await pending
    assert delayed.quote.data_type == 3 and delayed.received_at_ms == 2000
    await one.aclose()
    await two.aclose()


async def test_slow_reader_fails_without_losing_peer_or_restarting_source() -> None:
    source = Source()
    hub = FeedHub(source, queue_size=1)
    slow, first = await started(hub, subscription())
    peer, second = await started(hub, subscription())
    await source.input.put(quote(1))
    await first
    await second
    for price in (2, 3):
        read = asyncio.create_task(anext(peer))
        await source.input.put(quote(price))
        assert (await read).quote.values["bid"] == price
    assert (await anext(slow)).kind == "error"
    assert len(source.opened) == 1
    await slow.aclose()
    await peer.aclose()


async def test_source_failure_clears_retained_quote_for_next_generation() -> None:
    source = Source()
    hub = FeedHub(source)
    one, first = await started(hub, subscription())
    await source.input.put(quote(2))
    await first
    read = asyncio.create_task(anext(one))
    await source.input.put(RuntimeError("connection lost"))
    assert (await read).kind == "error"
    two, second = await started(hub, subscription())
    assert not second.done()
    await source.input.put(quote(8, receipt=3000))
    assert (await second).received_at_ms == 3000
    assert len(source.opened) == 2
    await one.aclose()
    await two.aclose()


async def test_different_contracts_and_exchange_semantics_do_not_merge() -> None:
    source = Source()
    hub = FeedHub(source)
    streams = []
    keys = [
        subscription(1),
        subscription(2),
        subscription(1).model_copy(
            update={"instrument": subscription(1).instrument.model_copy(update={"exchange": "CBOE"})}
        ),
    ]
    for key in keys:
        stream, pending = await started(hub, key)
        streams.append((stream, pending))
    assert source.opened == keys
    for stream, pending in streams:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()


async def test_hub_close_wakes_a_waiting_reader() -> None:
    source = Source()
    hub = FeedHub(source)
    stream, read = await started(hub, subscription())
    await hub.close()
    assert (await read).kind == "error"
    await stream.aclose()
