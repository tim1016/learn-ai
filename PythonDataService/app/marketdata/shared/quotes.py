"""Reference-counted quote leases for native and shared IBKR delivery."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from weakref import WeakKeyDictionary

from ib_async import Ticker

from app.marketdata.shared.protocol import FeedUnavailable, Instrument, Subscription
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)
_REQUEST_IDS = itertools.count(-1, -1)


@dataclass(eq=False)
class SharedTicker(Ticker):
    source_received_at_ms: int = 0
    source_trade_ms: int | None = None


@dataclass(eq=False)
class QuoteLease:
    ticker: Any
    request_id: int | None
    task: asyncio.Task[None] | None = None
    references: int = 1
    _release: Callable[[], None] | None = None

    @property
    def received_at_ms(self) -> int | None:
        return getattr(self.ticker, "source_received_at_ms", None)

    @property
    def trade_ms(self) -> int | None:
        return getattr(self.ticker, "source_trade_ms", None)

    def release(self) -> None:
        if self._release is not None:
            release, self._release = self._release, None
            release()


# Native fallback serves the combined application. The fleet always selects
# the authenticated installation owner (ADR 0082).
_NATIVE: WeakKeyDictionary[Any, dict[tuple[int, int, int], QuoteLease]] = WeakKeyDictionary()


def quotes_connected(client: Any) -> bool:
    shared = getattr(client, "shared_feed", None)
    return client.connection_state == "connected" if shared is None else shared.is_connected()


def request_quote(client: Any, contract: Any, _generic_ticks: str = "") -> QuoteLease:
    """A consumer owns a lease, never the upstream ticker's cancellation."""
    shared = getattr(client, "shared_feed", None)
    if shared is None:
        return _native_quote(client, contract)
    key = Subscription(
        kind="quotes",
        instrument=Instrument(
            con_id=int(contract.conId),
            exchange=contract.exchange,
            currency=contract.currency,
            sec_type=contract.secType,
        ),
    )
    ticker = SharedTicker(contract=contract)
    lease = QuoteLease(ticker=ticker, request_id=next(_REQUEST_IDS))
    lease.task = asyncio.create_task(_receive(client, lease, shared, contract, key))
    lease._release = lease.task.cancel
    return lease


def _native_quote(client: Any, contract: Any) -> QuoteLease:
    key = client.connection_generation, client.data_loss_epoch, int(contract.conId)
    cache = _NATIVE.setdefault(client, {})
    owned = cache.get(key)
    if owned is None:
        generic_ticks = "100,101,106" if contract.secType == "OPT" else "233"
        ticker = client.ib.reqMktData(contract, generic_ticks, False, False)
        request_id = client.ib.wrapper.ticker2ReqId["mktData"].get(ticker)
        owned = cache[key] = QuoteLease(ticker=ticker, request_id=request_id, references=0)
    if owned.ticker.contract.exchange != contract.exchange:
        raise FeedUnavailable("Incompatible quote venue is already active for this contract")
    owned.references += 1

    def release() -> None:
        owned.references -= 1
        if owned.references == 0:
            cache.pop(key, None)
            if (client.connection_generation, client.data_loss_epoch) == key[:2] and client.is_connected():
                client.ib.cancelMktData(owned.ticker.contract)

    return QuoteLease(ticker=owned.ticker, request_id=owned.request_id, _release=release)


def _invalidate(lease: QuoteLease) -> None:
    ticker = lease.ticker
    ticker.marketDataType, ticker.ticks = 0, []
    for name in ("bid", "ask", "last", "bidSize", "askSize", "lastSize"):
        setattr(ticker, name, float("nan"))
    ticker.source_received_at_ms, ticker.source_trade_ms = now_ms_utc(), None
    ticker.updateEvent.emit(ticker)


async def _receive(client: Any, lease: QuoteLease, shared: Any, contract: Any, key: Subscription) -> None:
    epoch: str | None = None
    retry_s = 1
    try:
        while True:
            try:
                async with aclosing(shared.stream(key)) as frames:
                    async for frame in frames:
                        if frame.kind == "error":
                            if frame.error_code is not None:
                                client.ib.errorEvent.emit(lease.request_id, frame.error_code, frame.reason, contract)
                            raise FeedUnavailable(frame.reason)
                        if frame.epoch != epoch:
                            _invalidate(lease)
                            epoch = frame.epoch
                        if frame.quote is None:
                            continue
                        update, ticker = frame.quote, lease.ticker
                        ticker.marketDataType = update.data_type
                        ticker.ticks = [
                            SimpleNamespace(tickType=t.kind, price=t.price, size=t.size) for t in update.ticks
                        ]
                        price_ticks = {t.kind for t in update.ticks}
                        required = {"bid": {1, 66}, "ask": {2, 67}, "last": {4, 68}}
                        for name, value in update.values.items():
                            if name in required and not required[name] & price_ticks:
                                continue
                            setattr(ticker, name, float("nan") if value is None else value)
                        for block, values in update.greeks.items():
                            setattr(ticker, block, SimpleNamespace(**values))
                        ticker.source_received_at_ms, ticker.source_trade_ms = frame.received_at_ms, update.trade_ms
                        ticker.updateEvent.emit(ticker)
                        retry_s = 1
                raise FeedUnavailable("Shared quote stream ended")
            except FeedUnavailable as exc:
                _invalidate(lease)
                logger.warning(
                    "Shared quote unavailable",
                    extra={"action": "shared_quote_unavailable", "con_id": contract.conId, "reason": str(exc)},
                )
                await asyncio.sleep(retry_s)
                retry_s = min(retry_s * 2, 30)
    finally:
        _invalidate(lease)
