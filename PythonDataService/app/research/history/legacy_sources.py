"""The two experiment tables the retired .NET backend filled, as History stores.

``"ResearchExperiments"`` (feature studies) and ``"SignalExperiments"`` (signal studies) are read-only history: nothing
writes them now. Their rows hold one instant (``"CreatedAt"``), a ticker, two date strings and a headline result, and
nothing about the bars they ran on, so a row's provenance and completion time are not recorded and read that way.

A feature experiment has no stored detail beyond the headline, so its entry carries the headline and no detail page. A
signal experiment's full report is served by ``GET /api/research/signal-experiments/{id}/report``.

Ordering is by the instant floored to the millisecond, then by id: the shared order compares milliseconds, so the SQL
must too, or two rows inside one millisecond would be ordered by their microseconds in the store and by id in the merge.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

import asyncpg

from app.data_lake.catalog_client import CatalogUnavailableError
from app.research.history.contract import HistoryQuery, SourceRead, SourceUnavailableError
from app.research.runs.runner import run_date_to_ms
from app.schemas.research_history import HistoryType, ResearchHistoryEntry, ResearchHistoryWindow
from app.services.postgres_connection import pooled_connection

logger = logging.getLogger(__name__)

_CREATED_MS = 'FLOOR(EXTRACT(EPOCH FROM e."CreatedAt") * 1000)::bigint'
_FILTERS = f"""
    WHERE ($1::text IS NULL OR t."Symbol" = $1)
      AND ($2::bigint IS NULL OR {_CREATED_MS} >= $2)
      AND ($3::bigint IS NULL OR {_CREATED_MS} < $3)
"""
_TIME_ONLY = f"""
    WHERE ($1::bigint IS NULL OR {_CREATED_MS} >= $1)
      AND ($2::bigint IS NULL OR {_CREATED_MS} < $2)
"""
_STORE_FAILURES = (CatalogUnavailableError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


def _window(start: str, end: str, *, experiment_id: int) -> ResearchHistoryWindow | None:
    """The stored date strings as the ET-midnight instants a run's own window uses; ``None`` for a date that is not one."""
    try:
        return ResearchHistoryWindow(
            start_ms=run_date_to_ms(date.fromisoformat(start)), end_ms=run_date_to_ms(date.fromisoformat(end))
        )
    except ValueError:
        logger.warning(
            "a stored experiment window is not a date; shown as not recorded",
            extra={"action": "research_history_window_unreadable", "experiment_id": experiment_id},
        )
        return None


class _ExperimentTable:
    """One of the two tables: the SQL shared, the headline and the question its own."""

    type: HistoryType
    label: str
    table: str
    headline_columns: str

    def order_key(self, native_id: str) -> int:
        return int(native_id)

    def _question(self, feature: str, symbol: str) -> str:
        raise NotImplementedError

    def _headline(self, row: Mapping[str, Any]) -> str:
        raise NotImplementedError

    def _entry(self, row: Mapping[str, Any]) -> ResearchHistoryEntry:
        symbol: str = row["Symbol"]
        return ResearchHistoryEntry(
            type=self.type,
            native_id=str(row["Id"]),
            question=self._question(row["FeatureName"], symbol),
            symbols=[symbol],
            window=_window(row["StartDate"], row["EndDate"], experiment_id=row["Id"]),
            # A stored row is a finished result: a run that failed left no headline to store.
            status="completed",
            status_detail=None,
            created_at_ms=row["created_at_ms"],
            completed_at_ms=None,
            provenance=None,
            headline=self._headline(row),
            parent=None,
        )

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        listing = f"""
            SELECT e."Id", t."Symbol", e."FeatureName", e."StartDate", e."EndDate", e."BarsUsed",
                   {self.headline_columns}, {_CREATED_MS} AS created_at_ms
            FROM public."{self.table}" e
            JOIN public."Tickers" t ON t."Id" = e."TickerId"
            {_FILTERS}
            ORDER BY created_at_ms DESC, e."Id" ASC
            LIMIT $4
        """
        count = f'SELECT COUNT(*) FROM public."{self.table}" e JOIN public."Tickers" t ON t."Id" = e."TickerId" {_FILTERS}'
        symbols = (
            f'SELECT DISTINCT t."Symbol" FROM public."{self.table}" e JOIN public."Tickers" t ON t."Id" = e."TickerId" {_TIME_ONLY}'
        )
        # Every stored row is a completed result, so any other status filter matches none of them.
        admitted = query.status in (None, "completed")
        filters = (query.symbol, query.created_from_ms, query.created_before_ms)
        try:
            async with pooled_connection() as conn:
                symbol_rows = await conn.fetch(symbols, query.created_from_ms, query.created_before_ms)
                total = await conn.fetchval(count, *filters) if admitted else 0
                rows = await conn.fetch(listing, *filters, take) if admitted and total else []
        except _STORE_FAILURES as exc:
            raise SourceUnavailableError("the database could not be reached or queried") from exc
        return SourceRead(
            total=total,
            entries=tuple(self._entry(row) for row in rows),
            symbols=frozenset(row["Symbol"] for row in symbol_rows),
        )


class FeatureExperiments(_ExperimentTable):
    type: HistoryType = "feature-experiment"
    label = "saved feature experiments"
    table = "ResearchExperiments"
    headline_columns = 'e."MeanIC", e."ICTStat", e."PassedValidation"'

    def _question(self, feature: str, symbol: str) -> str:
        return f"Does {feature} predict the next move of {symbol}?"

    def _headline(self, row: Mapping[str, Any]) -> str:
        verdict = "passed" if row["PassedValidation"] else "did not pass"
        return (
            f"Mean IC {float(row['MeanIC']):.4f}, t-stat {float(row['ICTStat']):.2f}; {verdict} validation; "
            f"{row['BarsUsed']:,} bars"
        )


class SignalReports(_ExperimentTable):
    type: HistoryType = "signal-report"
    label = "saved signal reports"
    table = "SignalExperiments"
    headline_columns = 'e."OverallGrade", e."StatusLabel", e."MeanOosSharpe", e."BestThreshold"'

    def _question(self, feature: str, symbol: str) -> str:
        return f"Does a trading signal built on {feature} hold up out of sample on {symbol}?"

    def _headline(self, row: Mapping[str, Any]) -> str:
        return (
            f"Grade {row['OverallGrade']} ({row['StatusLabel']}); mean OOS Sharpe {float(row['MeanOosSharpe']):.2f}; "
            f"best threshold {float(row['BestThreshold']):g}; {row['BarsUsed']:,} bars"
        )
