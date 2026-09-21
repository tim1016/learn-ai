"""The listing catalog behind the shared symbol picker (ADR 0066).

One cached Polygon reference walk serves every picker in every open tab.
The cache holds the in-flight task, not a materialized list: concurrent
misses single-flight onto one walk, and a failed walk drops its entry so a
retry reaches the vendor instead of every picker staring at a sticky error.
The TTL is long because reference data moves on listing/delisting cadence,
not per request.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

from app.schemas.ticker_catalog import SymbolCatalogEntry
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

_CATALOG_CACHE_TTL_S = 3600.0


class TickerCatalogService:
    """Caches one vendor catalog walk per process, single-flighting misses."""

    def __init__(self, client: PolygonClientService) -> None:
        self._client = client
        self._entry: tuple[float, asyncio.Task[list[SymbolCatalogEntry]]] | None = None

    async def get(self) -> list[SymbolCatalogEntry]:
        entry = self._entry
        if entry is not None:
            loaded_at, task = entry
            if not task.done() or monotonic() - loaded_at < _CATALOG_CACHE_TTL_S:
                return await self._await(entry)
        entry = (monotonic(), asyncio.ensure_future(self._load()))
        self._entry = entry
        return await self._await(entry)

    def clear_for_testing(self) -> None:
        """Drop the cached catalog so a test's stubbed client is consulted."""
        self._entry = None

    async def _load(self) -> list[SymbolCatalogEntry]:
        entries = await asyncio.to_thread(self._client.list_catalog_tickers)
        return [SymbolCatalogEntry(**entry) for entry in entries]

    async def _await(
        self,
        entry: tuple[float, asyncio.Task[list[SymbolCatalogEntry]]],
    ) -> list[SymbolCatalogEntry]:
        try:
            # Shielded: a browser disconnect cancels the requesting handler,
            # never the shared walk — concurrent callers keep coalescing
            # onto it.
            return await asyncio.shield(entry[1])
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A failed walk is not cached: the next caller re-loads instead
            # of inheriting the failure.
            if self._entry is entry:
                self._entry = None
            raise
