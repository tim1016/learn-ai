"""IBKR requests are shared by qualified contract, including option quotes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from eventkit import Event
from ib_async import Contract, Ticker
from ib_async.objects import TickData

from app.broker.ibkr.live.hub import FeedHub
from app.broker.ibkr.live.native import IbkrSource
from app.broker.ibkr.live.protocol import Subscription
from tests.marketdata.shared.test_hub import started, subscription


class Broker:
    def __init__(self) -> None:
        self.opened: list[tuple[Contract, str]] = []
        self.closed: list[Contract] = []
        self.errorEvent = Event()
        self.wrapper = SimpleNamespace(
            ticker2ReqId={"mktData": {}}, tickGeneric=lambda *args: None, marketDataType=lambda *args: None
        )
        self.ticker: Ticker | None = None

    async def qualifyContractsAsync(self, contract: Contract) -> list[Contract]:
        contract.secType, contract.currency, contract.symbol = "OPT", "USD", "QQQ"
        return [contract]

    def reqMktData(self, contract: Contract, ticks: str, snapshot: bool, regulatory: bool) -> Ticker:
        self.opened.append((contract, ticks))
        self.ticker = Ticker(contract=contract)
        self.wrapper.ticker2ReqId["mktData"][self.ticker] = 5
        return self.ticker

    def cancelMktData(self, contract: Contract) -> None:
        self.closed.append(contract)


class Client:
    settings = SimpleNamespace(readonly=True)
    connection_generation = data_loss_epoch = connectivity_lost_count = 1
    connection_lost = False
    connection_state = "connected"
    last_event_ms = 0
    last_ibkr_code = None

    def __init__(self) -> None:
        self.ib = Broker()
        from app.broker.ibkr.live.local import NativeLiveSource

        self.live_data = NativeLiveSource(self)

    def require_connected(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True


async def test_native_option_source_is_one_request_and_preserves_delayed_kind_and_negative_halt() -> None:
    client = Client()
    hub = FeedHub(IbkrSource(client))
    key: Subscription = subscription(2345)
    one, first = await started(hub, key)
    two, second = await started(hub, key)
    for _ in range(20):
        if client.ib.ticker is not None:
            break
        await asyncio.sleep(0)
    ticker = client.ib.ticker
    assert ticker is not None
    ticker.marketDataType = 3
    ticker.bid, ticker.ask = 1.25, 1.30
    ticker.ticks = [TickData(None, 66, 1.25, 4), TickData(None, 49, -1, 0)]
    ticker.updateEvent.emit(ticker)
    frames = await asyncio.gather(first, second)
    assert frames[0].quote == frames[1].quote
    assert frames[0].quote.data_type == 3
    assert frames[0].quote.ticks[1].price == -1
    assert frames[0].quote.values["bid"] == 1.25
    assert len(client.ib.opened) == 1
    assert client.ib.opened[0][1] == "100,101,106"
    await one.aclose()
    assert not client.ib.closed
    await two.aclose()
    assert len(client.ib.closed) == 1


async def test_incorrect_currency_is_refused_before_subscribing() -> None:
    client = Client()
    key = subscription().model_copy(
        update={"instrument": subscription().instrument.model_copy(update={"currency": "EUR"})}
    )
    hub = FeedHub(IbkrSource(client))
    stream = hub.stream(key)
    frame = await anext(stream)
    assert frame.kind == "error" and "identity differs" in frame.reason
    assert not client.ib.opened
    await stream.aclose()


def test_source_refuses_a_writable_gateway_connection() -> None:
    client: Any = Client()
    client.settings = SimpleNamespace(readonly=False)
    with pytest.raises(ValueError, match="read-only"):
        IbkrSource(client)
