"""The shared process's read-only IBKR source. No account or execution state."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from uuid import uuid4

from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarSubscriptionStalled,
    _check_realtime_subscription_liveness,
    _contract_venue,
    _RealtimeBarSubscriptionRegistry,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.market_subscription import REQUEST_FAILURE_CODES, install_market_data_callbacks
from app.broker.ibkr.minute_assembler import _bar_time_ms, _validated_contribution
from app.marketdata.shared.protocol import (
    FeedUnavailable,
    Frame,
    QuoteUpdate,
    SourceBar,
    SourceHealth,
    Subscription,
    Tick,
)
from app.utils.timestamps import now_ms_utc

QUOTE_FIELDS = (
    "bid",
    "ask",
    "last",
    "close",
    "bidSize",
    "askSize",
    "lastSize",
    "halted",
    "callOpenInterest",
    "putOpenInterest",
    "callVolume",
    "putVolume",
)
GREEK_FIELDS = ("impliedVol", "delta", "gamma", "theta", "vega", "undPrice", "optPrice", "pvDividend")
GREEK_BLOCKS = ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks")


def _number(value: Any) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


class IbkrSource:
    def __init__(self, client: IbkrClient) -> None:
        if not client.settings.readonly:
            raise ValueError("Shared IBKR source must be read-only")
        self._client = client
        self._boot = f"{now_ms_utc()}-{uuid4().hex}"
        self._active: set[tuple[str, int]] = set()
        self._bar_registry = _RealtimeBarSubscriptionRegistry()

    def _epoch(self) -> str:
        client = self._client
        return f"{self._boot}:{client.connection_generation}:{client.data_loss_epoch}:{client.connectivity_lost_count}"

    def health(self) -> SourceHealth:
        return SourceHealth(connected=self._current(self._epoch()), epoch=self._epoch(), observed_at_ms=now_ms_utc())

    async def stream(self, subscription: Subscription) -> AsyncIterator[Frame]:
        from ib_async import Contract

        self._client.require_connected()
        epoch = self._epoch()
        identity = subscription.instrument
        contract = Contract(conId=identity.con_id, exchange=identity.exchange)
        qualified = await asyncio.wait_for(self._client.ib.qualifyContractsAsync(contract), timeout=5)
        if len(qualified) != 1 or qualified[0] is None:
            raise FeedUnavailable("Shared source could not qualify the instrument")
        if not self._current(epoch):
            raise FeedUnavailable("Shared source changed while qualifying the instrument")
        contract = qualified[0]
        if (contract.conId, contract.secType, contract.currency, contract.exchange) != (
            identity.con_id,
            identity.sec_type,
            identity.currency,
            identity.exchange,
        ):
            raise FeedUnavailable("Shared source instrument identity differs from the request")
        slot = (subscription.kind, identity.con_id)
        if slot in self._active:
            # ib_async's ticker cache and the legacy bar registry use conId
            # alone. Refuse a second venue instead of merging its semantics.
            raise FeedUnavailable("Incompatible source for this contract is already active")
        self._active.add(slot)
        try:
            stream = self._bars(contract) if subscription.kind == "bars" else self._quotes(contract)
            async with aclosing(stream) as frames:
                async for frame in frames:
                    yield frame
        finally:
            self._active.remove(slot)

    def _current(self, epoch: str) -> bool:
        return self._epoch() == epoch and self._client.is_connected() and not self._client.connection_lost

    async def _bars(self, contract: Any) -> AsyncIterator[Frame]:
        client, epoch = self._client, self._epoch()
        lease = await self._bar_registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=False)
        if not self._current(epoch):
            lease.release()
            raise FeedUnavailable("Shared source changed while acquiring bars")
        bars = lease.bars
        queue: asyncio.Queue[tuple[object, int]] = asyncio.Queue(256)
        overflow = False
        last_progress = time.monotonic()
        last_source_ms: int | None = None

        def receive(raw_bars: Any, _new: bool) -> None:
            nonlocal overflow
            if not self._current(epoch):
                raw_bars.clear()
                return
            for raw in raw_bars:
                if queue.full():
                    overflow = True
                    break
                queue.put_nowait((raw, now_ms_utc()))
            # This source is the exclusive owner of its list. Keep no vendor
            # history: reconnect history belongs to each consumer's proof.
            raw_bars.clear()

        def frame_of(raw: object, received: int) -> Frame:
            nonlocal last_progress, last_source_ms
            start = _bar_time_ms(raw)
            bar = _validated_contribution(raw, symbol=contract.symbol, source_ms=start)
            if last_source_ms is None or start > last_source_ms:
                last_progress, last_source_ms = time.monotonic(), start
            return Frame(
                kind="bar",
                epoch=epoch,
                received_at_ms=received,
                bar=SourceBar(
                    start_ms=start,
                    open=str(bar.open),
                    high=str(bar.high),
                    low=str(bar.low),
                    close=str(bar.close),
                    volume=str(bar.volume),
                    venue=_contract_venue(contract),
                ),
            )

        bars.updateEvent += receive
        try:
            while True:
                if overflow:
                    raise FeedUnavailable("IBKR bar ingress overflow; continuity is lost")
                try:
                    last_progress, _, _ = _check_realtime_subscription_liveness(
                        client=client,
                        lease=lease,
                        symbol=contract.symbol,
                        use_rth=False,
                        stall_timeout_s=60,
                        last_progress_at=last_progress,
                        last_source_ms=last_source_ms,
                        stall_deferred=not queue.empty(),
                    )
                    if not self._current(epoch):
                        raise IBKRBarInterrupted("Shared bar source epoch changed", cause="soft_loss_1100")
                except IBKRBarInterrupted:
                    # Pre-disconnect raw bars are real observations, never a
                    # claim that a quote still belongs to a live generation.
                    while not queue.empty():
                        yield frame_of(*queue.get_nowait())
                    raise
                try:
                    raw, received = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    if not self._current(epoch):
                        continue
                    yield Frame(kind="idle", epoch=epoch, received_at_ms=now_ms_utc())
                    continue
                if overflow:
                    raise FeedUnavailable("IBKR bar ingress overflow; continuity is lost")
                yield frame_of(raw, received)
        except IBKRBarInterrupted as exc:
            yield Frame(kind="error", epoch=epoch, received_at_ms=now_ms_utc(), reason=str(exc), cause=exc.cause)
        except IBKRBarSubscriptionStalled as exc:
            yield Frame(kind="error", epoch=epoch, received_at_ms=now_ms_utc(), reason=str(exc), cause="stalled")
        finally:
            bars.updateEvent -= receive
            lease.release()

    async def _quotes(self, contract: Any) -> AsyncIterator[Frame]:
        client, epoch = self._client, self._epoch()
        generation = client.connection_generation
        install_market_data_callbacks(client.ib.wrapper)
        queue: asyncio.Queue[Frame] = asyncio.Queue(256)
        overflow = False

        def publish(frame: Frame) -> None:
            nonlocal overflow
            if queue.full():
                overflow = True
            else:
                queue.put_nowait(frame)

        def receive(ticker: Any) -> None:
            trade = ticker.rtTime
            publish(
                Frame(
                    kind="quote",
                    epoch=epoch,
                    received_at_ms=now_ms_utc(),
                    quote=QuoteUpdate(
                        data_type=ticker.marketDataType,
                        ticks=tuple(
                            Tick(kind=t.tickType, price=_number(t.price), size=_number(t.size)) for t in ticker.ticks
                        ),
                        values={key: _number(getattr(ticker, key, None)) for key in QUOTE_FIELDS},
                        greeks={
                            block: {key: _number(getattr(greeks, key, None)) for key in GREEK_FIELDS}
                            for block in GREEK_BLOCKS
                            if (greeks := getattr(ticker, block, None)) is not None
                        },
                        trade_ms=None if trade is None else int(trade.timestamp() * 1000),
                    ),
                )
            )

        def failed(request_id: int, code: int, message: str, _contract: Any) -> None:
            if request_id == req_id and code in REQUEST_FAILURE_CODES:
                publish(Frame(kind="error", epoch=epoch, received_at_ms=now_ms_utc(), reason=message, error_code=code))

        # One fixed superset per security type means the chain, status watcher
        # and bots reuse the same quote. None requests a delayed entitlement.
        ticks = "100,101,106" if contract.secType == "OPT" else "233"
        ticker = client.ib.reqMktData(contract, ticks, False, False)
        req_id = client.ib.wrapper.ticker2ReqId["mktData"][ticker]
        ticker.updateEvent += receive
        client.ib.errorEvent += failed
        try:
            while True:
                if overflow:
                    raise FeedUnavailable("IBKR quote ingress overflow; receipts were lost")
                if not self._current(epoch):
                    raise FeedUnavailable("IBKR quote source interrupted; previous receipts are invalid")
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1)
                except TimeoutError:
                    frame = Frame(kind="idle", epoch=epoch, received_at_ms=now_ms_utc())
                if not self._current(epoch):
                    raise FeedUnavailable("IBKR quote source interrupted; queued receipts are invalid")
                yield frame
        finally:
            ticker.updateEvent -= receive
            client.ib.errorEvent -= failed
            if client.connection_generation == generation and client.is_connected():
                client.ib.cancelMktData(contract)
