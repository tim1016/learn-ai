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
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.live.connection import SharedConnection
from app.broker.ibkr.live.protocol import (
    Frame,
    Instrument,
    QuoteUpdate,
    SourceBar,
    SourceEpoch,
    Subscription,
    Tick,
)
from app.broker.ibkr.market_data import _quote_to_model
from app.broker.ibkr.market_subscription import MarketSubscription
from app.broker.ibkr.minute_assembler import MinuteAssembler
from app.broker.ibkr.option_quotes import OptionLine
from tests.marketdata.shared.test_native import Client

EPOCH = SourceEpoch(started_at_ms=1792500000000, owner="boot", generation=1)
RTH = 1792503000000


class Delivery(SharedConnection):
    connection_generation = 1

    def __init__(self) -> None:
        super().__init__(
            IbkrSettings(_env_file=None, shared_feed_url="http://feed", shared_feed_token="test-secret"),
            qualify=self._qualify_test_underlying,
        )
        self.inputs: dict[str, list[asyncio.Queue[Frame]]] = {}

        self.requests: list[Subscription] = []

    async def _qualify_test_underlying(self, symbol: str) -> Instrument:
        from app.broker.ibkr.contracts import qualify_underlying

        contract = await qualify_underlying(None, symbol)
        return Instrument(
            con_id=contract.conId, exchange=contract.exchange, currency=contract.currency, sec_type=contract.secType
        )

    def is_connected(self) -> bool:
        return True

    def bar_generation(self, epoch: SourceEpoch) -> int:
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
    client = SimpleNamespace(live_data=delivery, ib=SimpleNamespace(errorEvent=Event()), connection_state="connected")
    return client


def quote_update(*ticks: tuple[int, float, float], received: int = 1000, kind: int = 1) -> Frame:
    return Frame(
        kind="quote",
        epoch=EPOCH,
        received_at_ms=received,
        quote=QuoteUpdate(
            received_at_ms=received,
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
    lease = client.live_data.quote(contract)
    line = OptionLine(contract=None, incomplete_since_ms=0, lease=lease)
    observed: asyncio.Queue[None] = asyncio.Queue()

    def receive(ticker: Any) -> None:
        line.ingest(ticker)
        observed.put_nowait(None)

    lease.updates += receive
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
        assert _quote_to_model(lease.latest, "QQQ", 1792526400000, 500, "C").ts_ms == 3000
        assert delivery.requests[0].instrument.con_id == 987654
    finally:
        lease.release()
        await asyncio.sleep(0)
    assert not delivery.inputs["quotes"]


async def test_data_mode_change_and_transport_loss_cannot_leave_a_live_option_quote() -> None:
    delivery = Delivery()
    lease = client_for(delivery).live_data.quote(Option(conId=10, exchange="SMART", currency="USD"))
    line = OptionLine(contract=None, incomplete_since_ms=0)
    lease.updates += lambda ticker: line.ingest(ticker)

    def failed(_failure):
        line.bid = line.ask = None
        line.market_data_type = None

    lease.failures += failed
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
        assert line.market_data_type is None
    finally:
        lease.release()
        await asyncio.sleep(0)


async def test_size_only_callbacks_cannot_seed_a_late_joiners_cached_book() -> None:
    delivery = Delivery()
    lease = client_for(delivery).live_data.quote(Stock("QQQ", "SMART", "USD", conId=10))
    book = MarketSubscription(symbol="QQQ", started_at_ms=0, lease=lease)
    lease.updates += lambda ticker: book.ingest(ticker)
    try:
        await asyncio.sleep(0)
        await delivery.publish(quote_update((0, 2, 1), (3, 3, 1)))
        assert book.quote(1000) is None
        assert book.held(1) is None
        assert book.evidence(1000).state != "READY"
    finally:
        lease.release()
        await asyncio.sleep(0)


async def test_native_fallback_has_one_line_and_only_last_lease_cancels() -> None:
    client = Client()
    contract = Option(conId=34, exchange="SMART", currency="USD")
    one, two = client.live_data.quote(contract), client.live_data.quote(contract)
    assert one is not two and len(client.ib.opened) == 1
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

    monkeypatch.setattr("app.broker.ibkr.contracts.qualify_underlying", qualify)
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

        def bar_generation(self, epoch: SourceEpoch) -> int:
            return 1 if epoch == EPOCH else 2

    delivery = Generations()
    delivery._underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
    minute = stream_minute_bars(client_for(delivery), "IWM", assembler=MinuteAssembler())
    pending = asyncio.create_task(anext(minute))
    try:
        await asyncio.sleep(0)
        for offset in range(12):
            await delivery.publish(
                Frame(
                    kind="bar",
                    epoch=EPOCH if offset < 6 else EPOCH.model_copy(update={"generation": 2}),
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
    retired = client.live_data.quote(contract)
    client.data_loss_epoch += 1
    replacement = client.live_data.quote(contract)
    assert len(client.ib.opened) == 2
    retired.release()
    assert not client.ib.closed
    replacement.release()
    assert len(client.ib.closed) == 1


async def test_shared_stall_allows_sparse_minute_emit_then_raises_retryable_stall(monkeypatch: Any) -> None:
    from app.broker.ibkr.bars import IBKRBarSubscriptionStalled

    delivery = Delivery()
    delivery._underlyings["IWM"] = Instrument(con_id=22, exchange="SMART", currency="USD", sec_type="STK")
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
    lease = client_for(delivery).live_data.quote(Option(conId=10, exchange="SMART", currency="USD"))
    try:
        assert lease.latest is None
        assert _quote_to_model(lease.latest, "QQQ", 1792526400000, 500, "C").ts_ms == 1792503057393
    finally:
        lease.release()
        await asyncio.sleep(0)


async def test_quote_greeks_cannot_survive_source_generation_replacement() -> None:
    delivery = Delivery()
    lease = client_for(delivery).live_data.quote(Option(conId=10, exchange="SMART", currency="USD"))
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
        assert _quote_to_model(lease.latest, "QQQ", 1792526400000, 500, "C").iv == 0.2
        await delivery.publish(frame.model_copy(update={"epoch": EPOCH.model_copy(update={"generation": 2})}))
        snapshot = _quote_to_model(lease.latest, "QQQ", 1792526400000, 500, "C")
        assert snapshot.iv is None and snapshot.delta is None
    finally:
        lease.release()
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    "phase",
    [
        "qualification",
        "qualification_empty",
        "qualification_failure",
        "acquisition",
        "acquisition_failure",
        "disconnected",
    ],
)
async def test_source_acquisition_races_reach_the_bar_consumer_as_retryable_interruptions(phase: str) -> None:
    from app.broker.ibkr.bars import IBKRBarInterrupted
    from app.broker.ibkr.client import NotConnectedError
    from app.broker.ibkr.live.hub import FeedHub
    from app.broker.ibkr.live.native import IbkrSource
    from tests.marketdata.shared.test_review_regressions import BarBroker

    native = Client()
    native.ib = BarBroker()
    source = IbkrSource(native)
    if phase.startswith("qualification"):
        qualify = native.ib.qualifyContractsAsync

        async def changed(contract: Any) -> Any:
            result = await qualify(contract)
            native.connection_generation += 1
            if phase == "qualification_empty":
                return []
            if phase == "qualification_failure":
                raise ConnectionError("Socket disconnected while qualifying")
            return result

        native.ib.qualifyContractsAsync = changed
    elif phase.startswith("acquisition"):
        acquire = source._bar_registry.acquire

        async def changed_acquire(*args: Any, **kwargs: Any) -> Any:
            native.connection_generation += 1
            if phase == "acquisition_failure":
                raise ConnectionError("Socket disconnected on the pacer")
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
    shared._underlyings["QQQ"] = Instrument(con_id=987654, exchange="SMART", currency="USD", sec_type="OPT")
    stream = stream_raw_5s_bars(client_for(shared), "QQQ")
    try:
        with pytest.raises(IBKRBarInterrupted) as failure:
            await asyncio.wait_for(anext(stream), 1)
        assert failure.value.cause == ("socket_down" if phase == "disconnected" else "generation_changed")
    finally:
        await stream.aclose()
        await hub.close()
