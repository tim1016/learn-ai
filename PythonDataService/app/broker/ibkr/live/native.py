"""The shared process's read-only IBKR source. No account or execution state."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any
from uuid import uuid4

from app.broker.ibkr.bars import (
    REALTIME_BAR_STALL_TIMEOUT_S,
    IBKRBarInterrupted,
    IBKRBarSubscriptionStalled,
    _check_realtime_subscription_liveness,
    _contract_venue,
    _RealtimeBarSubscriptionRegistry,
)
from app.broker.ibkr.client import IbkrClient, NotConnectedError
from app.broker.ibkr.live.protocol import (
    FeedUnavailable,
    Frame,
    SourceBar,
    SourceEpoch,
    SourceHealth,
    Subscription,
)
from app.broker.ibkr.live.quote_callbacks import (
    REQUEST_FAILURE_CODES,
    generic_ticks,
    install_market_data_callbacks,
    quote_update,
    reset_greeks,
)
from app.broker.ibkr.minute_assembler import IBKRImpossibleBarError, _bar_time_ms, _validated_contribution
from app.utils.timestamps import now_ms_utc


class IbkrSource:
    def __init__(self, client: IbkrClient) -> None:
        if not client.settings.readonly:
            raise ValueError("Shared IBKR source must be read-only")
        self._client = client
        self._started_at_ms, self._owner = now_ms_utc(), uuid4().hex
        self._active: set[tuple[str, int]] = set()
        self._bar_registry = _RealtimeBarSubscriptionRegistry()

    def _epoch(self) -> SourceEpoch:
        client = self._client
        return SourceEpoch(
            started_at_ms=self._started_at_ms,
            owner=self._owner,
            generation=client.connection_generation,
            data_loss=client.data_loss_epoch,
            connectivity_loss=client.connectivity_lost_count,
        )

    def health(self) -> SourceHealth:
        return SourceHealth(connected=self._current(self._epoch()), epoch=self._epoch(), observed_at_ms=now_ms_utc())

    async def stream(self, subscription: Subscription) -> AsyncIterator[Frame]:
        from ib_async import Contract

        try:
            self._client.require_connected()
        except NotConnectedError as exc:
            raise FeedUnavailable(str(exc), cause="socket_down") from exc
        epoch = self._epoch()
        identity = subscription.instrument
        contract = Contract(conId=identity.con_id, exchange=identity.exchange)
        try:
            qualified = await asyncio.wait_for(self._client.ib.qualifyContractsAsync(contract), timeout=5)
        except Exception as exc:
            if not self._current(epoch) or isinstance(exc, (ConnectionError, NotConnectedError)):
                raise FeedUnavailable(
                    "Shared source interrupted during qualification", cause="generation_changed"
                ) from exc
            raise
        if not self._current(epoch):
            raise FeedUnavailable("Shared source changed while qualifying the instrument", cause="generation_changed")
        if len(qualified) != 1 or qualified[0] is None:
            raise FeedUnavailable("Shared source could not qualify the instrument")
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

    def _current(self, epoch: SourceEpoch) -> bool:
        return self._epoch() == epoch and self._client.is_connected() and not self._client.connection_lost

    async def _bars(self, contract: Any) -> AsyncIterator[Frame]:
        client, epoch = self._client, self._epoch()
        try:
            lease = await self._bar_registry.acquire(client, contract, bar_size=5, what_to_show="TRADES", use_rth=False)
        except IBKRBarInterrupted as exc:
            raise FeedUnavailable(str(exc), cause=exc.cause) from exc
        except Exception as exc:
            if not self._current(epoch) or isinstance(exc, (ConnectionError, NotConnectedError)):
                raise FeedUnavailable(
                    "Shared source interrupted during bar acquisition", cause="generation_changed"
                ) from exc
            raise
        if not self._current(epoch):
            lease.release()
            raise FeedUnavailable("Shared source changed while acquiring bars", cause="generation_changed")
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
                        stall_timeout_s=REALTIME_BAR_STALL_TIMEOUT_S,
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
        except IBKRImpossibleBarError as exc:
            yield Frame(kind="error", epoch=epoch, received_at_ms=now_ms_utc(), reason=str(exc), cause="impossible_bar")
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

        previous_greeks: dict[str, Any] = {}

        def receive(ticker: Any) -> None:
            update = quote_update(ticker, previous_greeks)
            publish(Frame(kind="quote", epoch=epoch, received_at_ms=update.received_at_ms, quote=update))

        def failed(request_id: int, code: int, message: str, _contract: Any) -> None:
            if request_id == req_id and code in REQUEST_FAILURE_CODES:
                publish(Frame(kind="error", epoch=epoch, received_at_ms=now_ms_utc(), reason=message, error_code=code))

        # One fixed superset per security type means the chain, status watcher
        # and bots reuse the same quote. None requests a delayed entitlement.
        ticker = client.ib.reqMktData(contract, generic_ticks(contract), False, False)
        reset_greeks(ticker)
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
