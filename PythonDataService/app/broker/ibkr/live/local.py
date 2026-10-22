"""Local IBKR delivery implements the same receipt interface as the fleet."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.broker.ibkr.live.protocol import FeedUnavailable
from app.broker.ibkr.live.quote_callbacks import (
    REQUEST_FAILURE_CODES,
    generic_ticks,
    install_market_data_callbacks,
    quote_update,
    reset_greeks,
)
from app.broker.ibkr.live.quotes import QuoteFailure, QuoteLease
from app.broker.ibkr.live.source import BarRequest, RawBarReceipt

if TYPE_CHECKING:
    from app.broker.ibkr.client import IbkrClient


@dataclass(frozen=True)
class NativeGeneration:
    socket: int
    connectivity_loss: int
    event_ms: int


@dataclass
class _QuoteLine:
    ticker: Any
    receive: Callable[..., None]
    failed: Callable[..., None]
    readers: set[QuoteLease] = field(default_factory=set)


class NativeLiveSource:
    def __init__(self, client: IbkrClient) -> None:
        self._client = client
        self._quotes: dict[tuple[int, int, int], _QuoteLine] = {}

    @property
    def connection_generation(self) -> int:
        return self._client.connection_generation

    @property
    def status_generation(self) -> NativeGeneration:
        client = self._client
        return NativeGeneration(client.connection_generation, client.connectivity_lost_count, client.last_event_ms)

    def maintains(self, previous: Hashable) -> bool:
        return (
            isinstance(previous, NativeGeneration)
            and previous.socket == self.connection_generation
            and self._client.last_ibkr_code == 1102
        )

    def is_connected(self) -> bool:
        return (
            self._client.is_connected()
            and not self._client.connection_lost
            and self._client.connection_state == "connected"
        )

    def retains_requests(self) -> bool:
        return self._client.is_connected() and self._client.connection_state == "soft_lost"

    def ready(self, recovery_healthy: bool) -> bool:
        return self.is_connected() and recovery_healthy

    def start(self) -> None:
        """The application's reconnect monitor owns the local socket."""

    async def close(self) -> None:
        for line in tuple(self._quotes.values()):
            for reader in tuple(line.readers):
                reader.fail(QuoteFailure("Live source closed"))
                reader.release()

    async def history_recovered(self, callback: Callable[[], Awaitable[None]]) -> None:
        await callback()

    def raw_bars(self, symbol: str, request: BarRequest) -> AsyncIterator[RawBarReceipt | None]:
        from app.broker.ibkr.bars import _iter_native_raw_bars

        return _iter_native_raw_bars(self._client, symbol, **vars(request))

    def quote(self, contract: Any) -> QuoteLease:
        client = self._client
        key = client.connection_generation, client.data_loss_epoch, int(contract.conId)
        line = self._quotes.get(key)
        if line is not None and line.ticker.contract.exchange != contract.exchange:
            raise FeedUnavailable("Incompatible quote venue is already active for this contract")
        if line is None:
            install_market_data_callbacks(client.ib.wrapper)
            ticker = client.ib.reqMktData(contract, generic_ticks(contract), False, False)
            ticker.contract = contract
            reset_greeks(ticker)
            req_id = client.ib.wrapper.ticker2ReqId["mktData"].get(ticker)

            previous_greeks: dict[str, Any] = {}

            def receive(ticker: Any) -> None:
                if not self.is_connected():
                    return
                if (client.connection_generation, client.data_loss_epoch) != key[:2]:
                    for reader in tuple(line.readers):
                        reader.fail(QuoteFailure("IBKR quote generation is no longer current"))
                    return
                update = quote_update(ticker, previous_greeks)
                for reader in tuple(line.readers):
                    reader.publish(update)

            def failed(request_id: int, code: int, reason: str, _contract: Any) -> None:
                if request_id == req_id and code in REQUEST_FAILURE_CODES:
                    for reader in tuple(line.readers):
                        reader.fail(QuoteFailure(reason, code))

            line = self._quotes[key] = _QuoteLine(ticker, receive, failed)
            ticker.updateEvent += receive
            client.ib.errorEvent += failed

        def release() -> None:
            line.readers.discard(lease)
            if not line.readers:
                self._quotes.pop(key, None)
                line.ticker.updateEvent -= line.receive
                client.ib.errorEvent -= line.failed
                if (client.connection_generation, client.data_loss_epoch) == key[:2] and client.is_connected():
                    client.ib.cancelMktData(line.ticker.contract)

        lease = QuoteLease(release)
        line.readers.add(lease)
        return lease
