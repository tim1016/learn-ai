"""The scoped-health projection over the four existing health sources (PRD #3490 R30, #3500).

Each source is read through the endpoint or service that already answers for
it: the lake's run-readiness, the option archive's health, and this process's
IBKR session health. This module reads them, bounds each read, and hands the
readings to ``scoped_health_items`` to be said as items. It decides nothing
about a source's own state.

* A read that fails, or runs past its bound, is an ``unknown`` reading. Nothing
  in a failure path produces a healthy item.
* Only the sources a request can be about are read, so a page asking about the
  live chain never touches the lake catalog.
* The lake read is the costly one (a catalog query and a snapshot read per
  symbol), so a successful reading is reused for ``LAKE_TTL_MS`` and
  concurrent requests share one read.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Final

from fastapi import FastAPI, Request

from app.broker.ibkr.health import current_broker_health
from app.broker.ibkr.models import IbkrConnectionHealth
from app.config import fleet_settings
from app.data_lake import catalog_client
from app.data_lake.polygon_fetcher import polygon_history_floor
from app.data_lake.run_readiness import read_run_readiness
from app.data_lake.types import RunReadinessResponse, trading_date_at_ms
from app.schemas.option_archive import OptionArchiveHealth
from app.schemas.scoped_health import AffectedOperation, ScopedHealthResponse
from app.services.scoped_health_items import (
    LAKE_DATA_TYPE,
    LAKE_MODE,
    Reading,
    archive_items,
    backup_items,
    concerns,
    ibkr_items,
    kinds_for,
    lake_items,
    sources_for,
    worst_first,
)
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

#: A source that has not answered by now is unknown: a hung catalog or archive must not hold a page up.
SOURCE_TIMEOUT_S: Final = 5.0
#: A successful lake reading is reused this long; a failed one only briefly, so recovery shows soon.
LAKE_TTL_MS: Final = 30_000
LAKE_FAILURE_TTL_MS: Final = 5_000
#: The most per-symbol lake items an unfiltered request returns before folding the rest into one.
MAX_LAKE_ITEMS: Final = 20

type Read[T] = Callable[[], Awaitable[T]]


class SourceUnreadable(Exception):
    """A source cannot answer, and says why in a sentence fit for a page."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HealthReaders:
    """How each source is read. Tests substitute these; the app builds the real ones in ``get_scoped_health_service``."""

    lake: Read[RunReadinessResponse]
    archive: Read[OptionArchiveHealth]
    ibkr: Read[IbkrConnectionHealth]


class ScopedHealthService:
    def __init__(self, readers: HealthReaders, *, clock: Clock = now_ms_utc) -> None:
        self._readers = readers
        self._clock = clock
        self._lake_lock = asyncio.Lock()
        self._lake_reading: Reading[RunReadinessResponse] | None = None

    async def project(
        self, *, operations: frozenset[AffectedOperation] = frozenset(), instrument: str | None = None
    ) -> ScopedHealthResponse:
        """The items that bear on ``operations`` for ``instrument``; all of them when neither is given."""
        sources = sources_for(kinds_for(operations))
        async with asyncio.TaskGroup() as reads:
            lake = reads.create_task(self._lake()) if "lake_catalog" in sources else None
            archive = (
                reads.create_task(self._attempt("option_archive", self._readers.archive))
                if sources & {"option_archive", "backups"}
                else None
            )
            ibkr = reads.create_task(self._attempt("ibkr_feed", self._readers.ibkr)) if "ibkr_feed" in sources else None
        items = [
            *(lake_items(lake.result(), instrument=instrument, limit=MAX_LAKE_ITEMS) if lake else []),
            *(archive_items(archive.result()) if archive else []),
            *(backup_items(archive.result()) if archive else []),
            *(ibkr_items(ibkr.result()) if ibkr else []),
        ]
        kept = [item for item in items if concerns(item, operations, instrument)]
        return ScopedHealthResponse(generated_ms=self._clock(), items=worst_first(kept))

    async def _lake(self) -> Reading[RunReadinessResponse]:
        async with self._lake_lock:
            cached = self._lake_reading
            if cached is not None:
                held = cached.value is not None
                if self._clock() - cached.read_ms < (LAKE_TTL_MS if held else LAKE_FAILURE_TTL_MS):
                    return replace(cached, freshness="cached") if held else cached
            self._lake_reading = await self._attempt("lake_catalog", self._readers.lake)
            return self._lake_reading

    async def _attempt[T](self, source: str, read: Read[T]) -> Reading[T]:
        """One bounded read. Whatever goes wrong, the answer is an unknown reading, and the cause is logged."""
        try:
            async with asyncio.timeout(SOURCE_TIMEOUT_S):
                value = await read()
        except SourceUnreadable as unreadable:
            logger.info(
                "Health source cannot be read",
                extra={"action": "scoped_health_source_unreadable", "source": source, "reason": unreadable.reason},
            )
            return Reading(read_ms=self._clock(), freshness="unknown", reason=unreadable.reason)
        except Exception:
            # A health projection exists to survive its sources failing: any failure is "unknown", never a 500 or a pass.
            logger.exception("Health source failed", extra={"action": "scoped_health_source_failed", "source": source})
            return Reading(read_ms=self._clock(), freshness="unknown")
        return Reading(read_ms=self._clock(), freshness="fresh", value=value)


# ── The default readers: the sources as the data plane already answers for them ──


async def _read_lake() -> RunReadinessResponse:
    # The route's own dependency: a fresh process has no catalog pool until something asks for one.
    await catalog_client.init_pool()
    floor = polygon_history_floor(trading_date_at_ms(now_ms_utc()))
    return await read_run_readiness(market="usa", data_type=LAKE_DATA_TYPE, price_adjustment_mode=LAKE_MODE, floor=floor)


def _archive_reader(app: FastAPI) -> Read[OptionArchiveHealth]:
    async def read() -> OptionArchiveHealth:
        service = getattr(app.state, "option_archive_service", None)
        if service is None:
            raise SourceUnreadable("The option archive is not running in this data service, so its status cannot be read.")
        return await asyncio.to_thread(service.health)

    return read


async def _read_ibkr() -> IbkrConnectionHealth:
    if fleet_settings.ROLE == "fleet_coordinator":
        # The coordinator opens no Gateway session: each clerk lane holds its own, and answers for it.
        raise SourceUnreadable(
            "This data service holds no IBKR session, so it cannot say whether live option chains and bot feeds can stream."
        )
    return current_broker_health()


async def get_scoped_health_service(request: Request) -> ScopedHealthService:
    """The process's one projection, built on first use so it can see the running app's archive service.

    ``async`` so the check-and-build runs on the event loop, never on two pool threads at once.
    """
    app = request.app
    service = getattr(app.state, "scoped_health_service", None)
    if service is None:
        service = ScopedHealthService(HealthReaders(lake=_read_lake, archive=_archive_reader(app), ibkr=_read_ibkr))
        app.state.scoped_health_service = service
    return service
