"""Read-only IBKR status proof for Alpaca execution lanes.

IBKR tick 49 reports halts; tick 48 (generic 233, RTVolume) supplies live
trade timestamps. Missing initial halt ticks are normal in Gateway. Silence
alone never proves tradability: a symbol needs a fresh real-time trade and no
latched halt, or an explicit not-halted tick on the current connection.
See docs/runbooks/add-an-alpaca-account.md for the retained-provider decision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.broker.ibkr.client import IbkrClient, get_client
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.models import _coerce_quote, _coerce_size
from app.schemas.market_liveness import (
    MarketStatusSnapshot,
    SymbolTradingStatusEvidence,
    TopOfBookQuote,
)
from app.utils.timestamps import Clock, now_ms_utc

_SOURCE = "ibkr.market_data.status"
_TRADE_MAX_AGE_MS = 5_000


class IbkrMarketStatusSource:
    """Own bounded, demand-driven subscriptions on the retained IBKR client."""

    def __init__(
        self, *, symbols: Callable[[], tuple[str, ...]],
        client: Callable[[], IbkrClient | None] = get_client,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._symbols = symbols
        self._client = client
        self._clock = clock
        self._generation: tuple[int, int, int, int] | None = None
        self._owner: IbkrClient | None = None
        self._tickers: dict[str, Any] = {}
        self._halted: dict[str, SymbolTradingStatusEvidence] = {}
        self._connection_changed_at_ms = 0

    async def close(self) -> None:
        """Release only this consumer's market-data requests."""
        if self._owner is not None and self._owner.is_connected():
            for ticker in self._tickers.values():
                self._owner.ib.cancelMktData(ticker.contract)
        self._tickers.clear()
        self._generation = None
        self._owner = None

    async def __call__(self) -> MarketStatusSnapshot:
        client = self._client()
        now = self._clock()
        # An open API socket does not prove that market-data farms and their
        # subscriptions are usable. Discard cached ticks on any degraded state.
        if client is None or client.connection_state != "connected":
            await self.close()
            self._connection_changed_at_ms = now
            return self._snapshot(now, connected=False)
        # Farm down/up may finish between polls without changing the socket
        # generation. Its transition stamp still invalidates cached status.
        generation = (
            id(client), client.connection_generation, client.connectivity_lost_count, client.last_event_ms,
        )
        if generation != self._generation:
            await self.close()
            self._owner = client
            self._generation = generation
            self._connection_changed_at_ms = now
        symbols = self._symbols()
        for symbol in tuple(self._tickers):
            if symbol not in symbols:
                client.ib.cancelMktData(self._tickers.pop(symbol).contract)
        for symbol in symbols:
            if symbol not in self._tickers:
                contract = await asyncio.wait_for(qualify_underlying(client, symbol), timeout=3)
                self._tickers[symbol] = client.ib.reqMktData(contract, "233", False, False)
        # Qualification is asynchronous: never publish observations across an
        # intervening reconnect or data-farm outage.
        if (
            client.connection_state != "connected"
            or generation != (
                id(client), client.connection_generation, client.connectivity_lost_count, client.last_event_ms,
            )
        ):
            await self.close()
            self._connection_changed_at_ms = self._clock()
            return self._snapshot(self._connection_changed_at_ms, connected=False)
        return self._snapshot(self._clock(), connected=True)

    def _snapshot(self, now: int, *, connected: bool) -> MarketStatusSnapshot:
        symbols = self._symbols()
        return MarketStatusSnapshot(
            source=_SOURCE, connected=connected, observed_at_ms=now,
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_statuses=tuple(self._evidence(symbol, now) for symbol in symbols),
            quotes=(
                tuple(quote for symbol in symbols if (quote := self._quote(symbol, now)) is not None)
                if connected else ()
            ),
        )

    def _quote(self, symbol: str, now: int) -> TopOfBookQuote | None:
        """The live best bid and ask, or nothing when either side is absent.

        Only a live (``marketDataType == 1``) book prices a limit: a delayed or
        frozen quote is a number nobody can trade against now (#2007).
        """
        ticker = self._tickers.get(symbol)
        if ticker is None or ticker.marketDataType != 1:
            return None
        bid = _coerce_quote(getattr(ticker, "bid", None))
        ask = _coerce_quote(getattr(ticker, "ask", None))
        if not bid or not ask:
            return None
        return TopOfBookQuote(
            symbol=symbol, bid=bid, ask=ask,
            bid_size=_coerce_size(getattr(ticker, "bidSize", None)),
            ask_size=_coerce_size(getattr(ticker, "askSize", None)),
            source=_SOURCE, observed_at_ms=now,
        )

    def _evidence(self, symbol: str, now: int) -> SymbolTradingStatusEvidence:
        ticker = self._tickers.get(symbol)
        halted = ticker.halted if ticker is not None else None
        live = ticker is not None and ticker.marketDataType == 1
        if live and halted in (1, 2):
            if symbol not in self._halted:
                self._halted[symbol] = SymbolTradingStatusEvidence(
                    symbol=symbol, state="HALTED", source=_SOURCE, observed_at_ms=now,
                    reason_code="IBKR_SYMBOL_HALTED", reason="IBKR reports this symbol halted.",
                )
        elif live and halted == 0:
            self._halted.pop(symbol, None)
        if symbol in self._halted:
            return self._halted[symbol]
        trade_ms = (
            int(ticker.rtTime.timestamp() * 1000)
            if live and ticker.rtTime is not None else None
        )
        fresh_trade = trade_ms is not None and 0 <= now - trade_ms <= _TRADE_MAX_AGE_MS
        tradable = live and halted != -1 and (halted == 0 or fresh_trade)
        return SymbolTradingStatusEvidence(
            symbol=symbol, state="TRADABLE" if tradable else "UNKNOWN", source=_SOURCE,
            observed_at_ms=now, source_timestamp_ms=trade_ms if fresh_trade else None,
            reason_code="IBKR_SYMBOL_TRADABLE" if tradable else "IBKR_SYMBOL_STATUS_UNAVAILABLE",
            reason=(
                "IBKR reports no halt or supplies a fresh live trade with no reported halt."
                if tradable else
                "Waiting for IBKR live trading-status evidence for this symbol."
            ),
        )
