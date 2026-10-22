"""The live-data seam. Selection belongs only to IbkrClient construction."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Protocol

from app.broker.ibkr.live.quotes import QuoteLease


@dataclass(frozen=True)
class RawBarReceipt:
    raw: object
    venue: str | None
    generation: int
    received_at_ms: int


@dataclass(frozen=True)
class BarRequest:
    use_rth: bool
    stall_timeout_s: float
    consumer_label: str
    no_bar_message: str
    first_bar_message: str
    last_source_ms: int | None = None
    on_source_bar: Callable[[int, int], None] | None = None
    request_deadline_ms: int | None = None
    stall_deferred: Callable[[], bool] | None = None


class LiveSource(Protocol):
    @property
    def connection_generation(self) -> int: ...
    @property
    def status_generation(self) -> Hashable: ...
    def maintains(self, previous: Hashable) -> bool: ...
    def is_connected(self) -> bool: ...
    def retains_requests(self) -> bool: ...
    def ready(self, recovery_healthy: bool) -> bool: ...
    def start(self) -> None: ...
    async def close(self) -> None: ...
    async def history_recovered(self, callback: Callable[[], Awaitable[None]]) -> None: ...
    def quote(self, contract: Any) -> QuoteLease: ...
    def raw_bars(self, symbol: str, request: BarRequest) -> AsyncIterator[RawBarReceipt | None]: ...
