"""Failures reproduced by the independent first thermo review of #3388."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import IB, Contract
from ib_async.objects import RealTimeBarList, TickData

from app.broker.ibkr.option_quotes import OptionLine
from app.marketdata.shared.hub import FeedHub
from app.marketdata.shared.native import IbkrSource
from app.marketdata.shared.protocol import Frame, SourceBar
from tests.marketdata.shared.test_hub import Source, quote, started, subscription
from tests.marketdata.shared.test_native import Broker, Client


@pytest.mark.parametrize("side,price_kind,size_kind", [("bid", 1, 0), ("ask", 2, 3)])
async def test_late_option_join_does_not_reconstruct_price_across_a_destructive_size_tick(
    side: str,
    price_kind: int,
    size_kind: int,
) -> None:
    source = Source()
    hub = FeedHub(source)
    original, read = await started(hub, subscription())
    line = OptionLine(contract=None, incomplete_since_ms=0)
    for frame in (quote(2, 1000, tick=price_kind), quote(0, 2000, tick=size_kind), quote(1, 3000, tick=size_kind)):
        await source.input.put(frame)
        delivered = await read
        tick = delivered.quote.ticks[0]
        line.ingest(
            SimpleNamespace(
                marketDataType=1, ticks=[TickData(None, tick.kind, tick.price, tick.price)], modelGreeks=None
            ),
            delivered.received_at_ms,
        )
        if frame.received_at_ms != 3000:
            read = asyncio.create_task(anext(original))
    assert getattr(line, side) is None
    late, pending = await started(hub, subscription())
    # Conservative join: a past callback is never replayed as a new receipt.
    assert not pending.done()
    await source.input.put(quote(4, 4000))
    assert (await pending).received_at_ms == 4000
    await original.aclose()
    await late.aclose()


async def test_terminal_source_frame_releases_owner_before_another_join() -> None:
    source = Source()
    hub = FeedHub(source)
    old, read = await started(hub, subscription())
    await source.input.put(Frame(kind="error", epoch="boot:1", received_at_ms=1, reason="refused"))
    assert (await read).kind == "error"
    fresh, pending = await started(hub, subscription())
    assert len(source.opened) == 2
    await source.input.put(quote(4))
    await pending
    await old.aclose()
    await fresh.aclose()


async def test_quote_received_while_awaiting_a_frame_is_fenced_when_connection_is_lost() -> None:
    client = Client()
    source = IbkrSource(client)
    stream = source.stream(subscription())
    pending = asyncio.create_task(anext(stream))
    for _ in range(20):
        if client.ib.ticker is not None:
            break
        await asyncio.sleep(0)
    ticker = client.ib.ticker
    assert ticker is not None
    ticker.ticks = [TickData(None, 1, 2, 1)]
    ticker.updateEvent.emit(ticker)
    client.connection_lost = True
    from app.marketdata.shared.protocol import FeedUnavailable

    try:
        with pytest.raises(FeedUnavailable):
            await asyncio.wait_for(pending, 2)
    finally:
        await stream.aclose()


class RealWrapperBroker(Broker):
    def __init__(self) -> None:
        super().__init__()
        self.native = IB()
        self.wrapper = self.native.wrapper

    def reqMktData(self, contract: Contract, ticks: str, snapshot: bool, regulatory: bool) -> Any:
        self.opened.append((contract, ticks))
        self.ticker = self.wrapper.startTicker(len(self.opened), contract, "mktData")
        return self.ticker


async def test_incompatible_quote_venues_are_refused_before_ib_async_can_alias_the_ticker() -> None:
    client = Client()
    client.ib = RealWrapperBroker()
    hub = FeedHub(IbkrSource(client))
    key = subscription()
    first, pending = await started(hub, key)
    for _ in range(20):
        if client.ib.ticker is not None:
            break
        await asyncio.sleep(0)
    other_key = key.model_copy(update={"instrument": key.instrument.model_copy(update={"exchange": "CBOE"})})
    other = hub.stream(other_key)
    # It must refuse immediately; do not silently wait on the first venue's quote.
    error = await asyncio.wait_for(anext(other), 0.3)
    assert error.kind == "error"
    assert len(client.ib.opened) == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await first.aclose()
    await other.aclose()


def test_frame_payload_must_match_its_tag() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Frame(kind="quote", epoch="a", received_at_ms=1)
    with pytest.raises(ValidationError):
        Frame(
            kind="idle",
            epoch="a",
            received_at_ms=1,
            bar=SourceBar(start_ms=1, open="1", high="1", low="1", close="1", volume="1"),
        )


class BarBroker(Broker):
    def __init__(self) -> None:
        super().__init__()
        self.raw_list: RealTimeBarList | None = None
        self.cancelled = 0

    def reqRealTimeBars(self, contract: Contract, size: int, what: str, *, useRTH: bool) -> RealTimeBarList:
        self.raw_list = RealTimeBarList()
        self.raw_list.reqId = 9
        return self.raw_list

    def cancelRealTimeBars(self, bars: RealTimeBarList) -> None:
        self.cancelled += 1


async def test_raw_bar_ingress_is_bounded_even_when_producer_cannot_drain_a_burst() -> None:
    client = Client()
    client.ib = BarBroker()
    source = IbkrSource(client)
    stream = source.stream(subscription(kind="bars"))
    pending = asyncio.create_task(anext(stream))
    for _ in range(30):
        if client.ib.raw_list is not None:
            break
        await asyncio.sleep(0)
    bars = client.ib.raw_list
    assert bars is not None
    try:
        for index in range(1000):
            bars.append(SimpleNamespace(time=1792503000000 + index * 5000, open_=2, high=2, low=2, close=2, volume=1))
            bars.updateEvent.emit(bars, True)
        assert len(bars) <= 1
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def test_pre_disconnect_bars_reach_consumer_before_the_interruption() -> None:
    class Burst:
        async def stream(self, key: Any) -> Any:
            for start in (1000, 6000):
                yield Frame(
                    kind="bar",
                    epoch="a",
                    received_at_ms=start,
                    bar=SourceBar(start_ms=start, open="2", high="2", low="2", close="2", volume="1"),
                )
            yield Frame(kind="error", epoch="a", received_at_ms=11000, cause="socket_down", reason="lost")

    hub = FeedHub(Burst())
    stream = hub.stream(subscription(kind="bars"))
    assert (await anext(stream)).bar.start_ms == 1000
    assert (await anext(stream)).bar.start_ms == 6000
    assert (await anext(stream)).cause == "socket_down"
    await stream.aclose()


async def test_reconnect_during_bar_acquisition_never_labels_new_data_with_old_epoch() -> None:
    from app.marketdata.shared.protocol import FeedUnavailable

    client = Client()
    client.ib = BarBroker()
    source = IbkrSource(client)
    original = source._bar_registry.acquire

    async def paced_acquire(*args: Any, **kwargs: Any) -> Any:
        client.connection_generation += 1
        return await original(*args, **kwargs)

    source._bar_registry.acquire = paced_acquire
    stream = source.stream(subscription(kind="bars"))
    try:
        with pytest.raises(FeedUnavailable, match="changed"):
            await asyncio.wait_for(anext(stream), timeout=1)
        assert client.ib.cancelled == 1
    finally:
        await stream.aclose()


async def test_fast_1100_1102_recovery_terminates_the_old_bar_epoch() -> None:
    client = Client()
    client.ib = BarBroker()
    source = IbkrSource(client)
    stream = source.stream(subscription(kind="bars"))
    first = await asyncio.wait_for(anext(stream), 1)
    assert first.kind == "idle"
    # The socket and generation remain unchanged, but a real soft-loss episode
    # happened between observations. Subsequent callbacks cannot use that epoch.
    client.connectivity_lost_count += 1
    bars = client.ib.raw_list
    bars.append(SimpleNamespace(time=1792503000000, open_=2, high=2, low=2, close=2, volume=1))
    bars.updateEvent.emit(bars, True)
    try:
        frame = await asyncio.wait_for(anext(stream), 1)
        assert frame.kind == "error" and frame.cause == "soft_loss_1100"
    finally:
        await stream.aclose()


async def test_qualification_cannot_substitute_another_exchange() -> None:
    client = Client()
    original = client.ib.qualifyContractsAsync

    async def qualify(contract: Any) -> Any:
        result = await original(contract)
        result[0].exchange = "CBOE"
        return result

    client.ib.qualifyContractsAsync = qualify
    hub = FeedHub(IbkrSource(client))
    stream = hub.stream(subscription())
    try:
        frame = await asyncio.wait_for(anext(stream), 0.3)
        assert frame.kind == "error" and "identity differs" in frame.reason
        assert not client.ib.opened
    finally:
        await stream.aclose()
