"""The worker adapters preserve actual quote-side and bar evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from eventkit import Event
from ib_async import Option, Stock

from app.broker.ibkr.bars import stream_minute_bars, stream_raw_5s_bars
from app.broker.ibkr.market_data import _ticker_to_quote
from app.broker.ibkr.minute_assembler import MinuteAssembler
from app.broker.ibkr.option_quotes import OptionLine
from app.marketdata.shared.protocol import (
    Frame,
    Instrument,
    QuoteUpdate,
    SourceBar,
    Subscription,
    Tick,
)
from app.marketdata.shared.quotes import request_quote
from tests.marketdata.shared.test_native import Client

EPOCH = "1792500000000-boot:1:0:0"
RTH = 1792503000000


class Delivery:
    connection_generation = 1

    def __init__(self) -> None:
        self.inputs: dict[str, list[asyncio.Queue[Frame]]] = {}
        self.underlyings: dict[str, Instrument] = {}
        self.bar_receipts: dict[str, int] = {}
        self.requests: list[Subscription] = []

    def is_connected(self) -> bool:
        return True

    def bar_generation(self, epoch: str) -> int:
        return self.connection_generation

    async def stream(self, key: Subscription) -> AsyncIterator[Frame]:
        self.requests.append(key)
        queue: asyncio.Queue[Frame] = asyncio.Queue()
        self.inputs.setdefault(key.kind, []).append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self.inputs[key.kind].remove(queue)

    async def publish(self, frame: Frame, kind: str = "quotes") -> None:
        for queue in self.inputs[kind]:
            queue.put_nowait(frame)
        await asyncio.sleep(0)


def client_for(delivery: Delivery) -> Any:
    return SimpleNamespace(shared_feed=delivery, ib=SimpleNamespace(errorEvent=Event()), connection_state="connected")


def quote_update(*ticks: tuple[int, float, float], received: int = 1000, kind: int = 1) -> Frame:
    return Frame(
        kind="quote",
        epoch=EPOCH,
        received_at_ms=received,
        quote=QuoteUpdate(
            data_type=kind,
            ticks=tuple(Tick(kind=t, price=p, size=s) for t, p, s in ticks),
            values={"bid": 2, "ask": 3, "bidSize": 1, "askSize": 1},
            greeks={},
        ),
    )


@pytest.mark.parametrize("mode", ["paper", "live_shadow", "live", "dry_run"])
async def test_any_option_quote_uses_shared_delivery_and_original_side_receipts(mode: str) -> None:
    delivery = Delivery()
    client = client_for(delivery)
    client.mode = mode
    contract = Option(conId=987654, symbol="QQQ", exchange="SMART", currency="USD")
    lease = request_quote(client, contract)
    line = OptionLine(contract=None, incomplete_since_ms=0, ticker=lease.ticker, lease=lease)
    observed: asyncio.Queue[None] = asyncio.Queue()

    def receive(ticker: Any) -> None:
        line.ingest(ticker, lease.received_at_ms)
        observed.put_nowait(None)

    lease.ticker.updateEvent += receive
    try:
        await asyncio.sleep(0)
        await delivery.publish(quote_update((1, 2, 1), (2, 3, 1), received=1000))
        while not line.has_a_whole_quote:
            await asyncio.wait_for(observed.get(), 1)
        assert (line.bid.price, line.ask.price) == (Decimal(2), Decimal(3))
        assert line.bid.received_ms == line.ask.received_ms == 1000
        await delivery.publish(quote_update((0, 0, 0), received=2000))
        assert line.bid.price == 0
        await delivery.publish(quote_update((0, 0, 1), received=3000))
        assert line.bid is None
        assert _ticker_to_quote(lease.ticker, "QQQ", 1792526400000, 500, "C").ts_ms == 3000
        assert delivery.requests[0].instrument.con_id == 987654
    finally:
        lease.release()
        await asyncio.gather(lease.task, return_exceptions=True)
    assert not delivery.inputs["quotes"]


async def test_data_mode_change_and_transport_loss_cannot_leave_a_live_option_quote() -> None:
    delivery = Delivery()
    lease = request_quote(client_for(delivery), Option(conId=10, exchange="SMART", currency="USD"))
    line = OptionLine(contract=None, incomplete_since_ms=0)
    lease.ticker.updateEvent += lambda ticker: line.ingest(ticker, lease.received_at_ms)
    try:
        await asyncio.sleep(0)
        await delivery.publish(quote_update((1, 2, 1), (2, 3, 1)))
        assert line.has_a_whole_quote
        await delivery.publish(quote_update(received=2000, kind=3))
        assert line.bid is None and line.ask is None
        assert line.market_data_type == 3
        await delivery.publish(quote_update((1, 2, 1), (2, 3, 1), received=3000))
        assert line.has_a_whole_quote
        await delivery.publish(Frame(kind="error", epoch=EPOCH, received_at_ms=4000, reason="disconnected"))
        assert line.bid is None and line.ask is None
        assert line.market_data_type == 0
    finally:
        lease.release()
        await asyncio.gather(lease.task, return_exceptions=True)


async def test_native_fallback_has_one_line_and_only_last_lease_cancels() -> None:
    client = Client()
    contract = Option(conId=34, exchange="SMART", currency="USD")
    one, two = request_quote(client, contract), request_quote(client, contract)
    assert one.ticker is two.ticker and len(client.ib.opened) == 1
    one.release()
    one.release()
    assert not client.ib.closed
    two.release()
    assert len(client.ib.closed) == 1


async def test_shared_five_second_and_minute_consumers_keep_exact_calendar_filtered_bars(monkeypatch: Any) -> None:
    delivery = Delivery()
    client = client_for(delivery)

    async def qualify(*args: Any) -> Stock:
        return Stock("IWM", "SMART", "USD", conId=22)

    monkeypatch.setattr("app.marketdata.shared.bars.qualify_underlying", qualify)
    raw = stream_raw_5s_bars(client, "IWM", use_rth=False)
    minute = stream_minute_bars(client, "IWM", use_rth=True, assembler=MinuteAssembler())
    first = asyncio.create_task(anext(raw))
    closed = asyncio.create_task(anext(minute))
    try:
        await asyncio.sleep(0)
        # 08:00 ET is outside RTH. The RTH reader must wait, while the 5s chart receives it.
        pre = RTH - 90 * 60_000
        await delivery.publish(
            Frame(
                kind="bar",
                epoch=EPOCH,
                received_at_ms=pre + 5000,
                bar=SourceBar(start_ms=pre, open="2", high="2", low="2", close="2", volume="1"),
            ),
            "bars",
        )
        assert (await first).start_ms == pre
        assert not closed.done()
        for offset in range(12):
            start = RTH + offset * 5000
            await delivery.publish(
                Frame(
                    kind="bar",
                    epoch=EPOCH,
                    received_at_ms=start + 5000,
                    bar=SourceBar(start_ms=start, open="2.1", high="2.2", low="2", close="2.1", volume="3"),
                ),
                "bars",
            )
        bar = await asyncio.wait_for(closed, 1)
        assert bar.start_ms == RTH and bar.end_ms == RTH + 60000
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
            Decimal("2.1"),
            Decimal("2.2"),
            Decimal(2),
            Decimal("2.1"),
            36,
        )
        assert bar.contribution_count == 12
    finally:
        await raw.aclose()
        await minute.aclose()


async def test_drained_old_bars_and_replacement_source_make_the_minute_span_an_interruption(monkeypatch: Any) -> None:
    class Generations(Delivery):
        connection_generation = 2

        def bar_generation(self, epoch: str) -> int:
            return 1 if epoch == EPOCH else 2

    delivery = Generations()
    delivery.underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
    minute = stream_minute_bars(client_for(delivery), "IWM", assembler=MinuteAssembler())
    pending = asyncio.create_task(anext(minute))
    try:
        await asyncio.sleep(0)
        for offset in range(12):
            await delivery.publish(
                Frame(
                    kind="bar",
                    epoch=EPOCH if offset < 6 else EPOCH.replace(":1:0:0", ":2:0:0"),
                    received_at_ms=RTH + offset * 5000 + 5000,
                    bar=SourceBar(start_ms=RTH + offset * 5000, open="2", high="2", low="2", close="2", volume="1"),
                ),
                "bars",
            )
        assert (await asyncio.wait_for(pending, 1)).spans_interruption
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await minute.aclose()


async def test_native_quote_replacement_after_1101_survives_a_retired_holders_release() -> None:
    from tests.marketdata.shared.test_review_regressions import RealWrapperBroker

    client = Client()
    client.ib = RealWrapperBroker()
    contract = Option(conId=34, exchange="SMART", currency="USD")
    retired = request_quote(client, contract)
    client.data_loss_epoch += 1
    replacement = request_quote(client, contract)
    assert len(client.ib.opened) == 2
    assert retired.request_id != replacement.request_id
    retired.release()
    assert not client.ib.closed
    replacement.release()
    assert len(client.ib.closed) == 1


async def test_shared_stall_allows_sparse_minute_emit_then_raises_retryable_stall(monkeypatch: Any) -> None:
    from app.broker.ibkr.bars import IBKRBarSubscriptionStalled

    delivery = Delivery()
    delivery.underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
    start = RTH - 90 * 60_000
    clock = [start + 60_000]
    monkeypatch.setattr("app.broker.ibkr.bars.now_ms_utc", lambda: clock[0])
    minute = stream_minute_bars(client_for(delivery), "IWM", use_rth=False, assembler=MinuteAssembler())
    pending = asyncio.create_task(anext(minute))
    try:
        await asyncio.sleep(0)
        await delivery.publish(
            Frame(
                kind="bar",
                epoch=EPOCH,
                received_at_ms=start + 5000,
                bar=SourceBar(start_ms=start, open="2", high="2", low="2", close="2", volume="1"),
            ),
            "bars",
        )
        await delivery.publish(
            Frame(kind="error", epoch=EPOCH, received_at_ms=clock[0], cause="stalled", reason="stalled"), "bars"
        )
        assert not pending.done()
        clock[0] = start + 68_000
        bar = await asyncio.wait_for(pending, 1)
        assert bar.start_ms == start and bar.contribution_count == 1
        with pytest.raises(IBKRBarSubscriptionStalled):
            await anext(minute)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await minute.aclose()


async def test_pre_delivery_snapshot_has_no_fabricated_epoch_receipt(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.broker.ibkr.market_data.now_ms_utc", lambda: 1792503057393)
    delivery = Delivery()
    lease = request_quote(client_for(delivery), Option(conId=10, exchange="SMART", currency="USD"))
    try:
        assert lease.received_at_ms is None
        assert _ticker_to_quote(lease.ticker, "QQQ", 1792526400000, 500, "C").ts_ms == 1792503057393
    finally:
        lease.release()
        await asyncio.gather(lease.task, return_exceptions=True)


async def test_quote_greeks_cannot_survive_source_generation_replacement() -> None:
    delivery = Delivery()
    lease = request_quote(client_for(delivery), Option(conId=10, exchange="SMART", currency="USD"))
    try:
        await asyncio.sleep(0)
        frame = quote_update((1, 2, 1), (2, 3, 1))
        await delivery.publish(
            frame.model_copy(
                update={
                    "quote": frame.quote.model_copy(
                        update={"greeks": {"modelGreeks": {"impliedVol": 0.2, "delta": 0.5}}}
                    )
                }
            )
        )
        assert _ticker_to_quote(lease.ticker, "QQQ", 1792526400000, 500, "C").iv == 0.2
        await delivery.publish(frame.model_copy(update={"epoch": EPOCH.replace(":1:0:0", ":2:0:0")}))
        snapshot = _ticker_to_quote(lease.ticker, "QQQ", 1792526400000, 500, "C")
        assert snapshot.iv is None and snapshot.delta is None
    finally:
        lease.release()
        await asyncio.gather(lease.task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["qualification", "acquisition", "disconnected"])
async def test_source_acquisition_races_reach_the_bar_consumer_as_retryable_interruptions(phase: str) -> None:
    from app.broker.ibkr.bars import IBKRBarInterrupted
    from app.broker.ibkr.client import NotConnectedError
    from app.marketdata.shared.hub import FeedHub
    from app.marketdata.shared.native import IbkrSource
    from tests.marketdata.shared.test_review_regressions import BarBroker

    native = Client()
    native.ib = BarBroker()
    source = IbkrSource(native)
    if phase == "qualification":
        qualify = native.ib.qualifyContractsAsync

        async def changed(contract: Any) -> Any:
            result = await qualify(contract)
            native.connection_generation += 1
            return result

        native.ib.qualifyContractsAsync = changed
    elif phase == "acquisition":
        acquire = source._bar_registry.acquire

        async def changed_acquire(*args: Any, **kwargs: Any) -> Any:
            native.connection_generation += 1
            return await acquire(*args, **kwargs)

        source._bar_registry.acquire = changed_acquire
    else:

        def disconnected() -> None:
            raise NotConnectedError("socket unavailable")

        native.require_connected = disconnected
    hub = FeedHub(source)

    class HubDelivery(Delivery):
        def stream(self, key: Subscription) -> AsyncIterator[Frame]:
            return hub.stream(key)

    shared = HubDelivery()
    shared.underlyings["QQQ"] = Instrument(con_id=987654, exchange="SMART", currency="USD", sec_type="OPT")
    stream = stream_raw_5s_bars(client_for(shared), "QQQ")
    try:
        with pytest.raises(IBKRBarInterrupted) as failure:
            await asyncio.wait_for(anext(stream), 1)
        assert failure.value.cause == ("socket_down" if phase == "disconnected" else "generation_changed")
    finally:
        await stream.aclose()
        await hub.close()
