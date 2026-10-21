"""Saved Data Lab sessions on Postgres: the SQL behind ``/api/data-lab/sessions``.

The table is the ``"DataLabSessions"`` one the retired C# backend created. A row carries each instant twice: the
legacy ``timestamptz`` columns (``CreatedAt`` / ``UpdatedAt``) and the ``int64 ms UTC`` ones. A write sets both from
one ``int64 ms`` reading of the clock, so the legacy columns can never disagree with the ms ones. A read answers the
ms columns and, for a legacy row where one is NULL, the epoch ms of the legacy column beside it.

``EntriesJson`` / ``ChartSnapshotJson`` are ``jsonb`` columns holding documents the screen owns; asyncpg moves them as
JSON text, so this module serializes on the way in and parses on the way out. Only a read of one session parses them:
the list answers summaries whose entry count and has-chart flag the database works out, so no document leaves Postgres
for it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.schemas.data_lab_sessions import (
    DataLabSessionReceipt,
    DataLabSessionRequest,
    DataLabSessionResponse,
    DataLabSessionSummary,
    session_date_window_ms,
)
from app.services.postgres_connection import pooled_connection
from app.utils.timestamps import Clock, now_ms_utc

# A legacy row may have no ms column: its epoch ms is the legacy column's, floored (the C# writer truncated too).
_CREATED_AT_MS = 'COALESCE("CreatedMsUtc", FLOOR(EXTRACT(EPOCH FROM "CreatedAt") * 1000)::bigint)'
_UPDATED_AT_MS = 'COALESCE("UpdatedMsUtc", FLOOR(EXTRACT(EPOCH FROM "UpdatedAt") * 1000)::bigint)'


def _instant(ms_parameter: str) -> str:
    """The ``timestamptz`` of an ``int64 ms`` parameter, exact to the millisecond."""
    return f"('epoch'::timestamptz + ({ms_parameter}::bigint * interval '1 millisecond'))"


# The columns every read answers (``DataLabSessionHeader``), instants included.
_HEADER_COLUMNS = f"""
    "Id", "Name", "Ticker", "FromDate", "ToDate",
    {_CREATED_AT_MS} AS created_at_ms, {_UPDATED_AT_MS} AS updated_at_ms
"""
_COLUMNS = f"""
    {_HEADER_COLUMNS},
    "Session", "ForwardFill", "Adjusted", "EntriesJson", "ChartSnapshotJson",
    "WindowStartMsUtc" AS window_start_ms, "WindowEndMsUtc" AS window_end_ms
"""
# The list never reads a document: the database counts the entries and says whether a chart is held, so a chart
# snapshot of megabytes stays in Postgres. A row whose entries are not a JSON array fails the query, as it fails a GET.
_SUMMARY_COLUMNS = f"""
    {_HEADER_COLUMNS},
    jsonb_array_length("EntriesJson") AS entry_count, "ChartSnapshotJson" IS NOT NULL AS has_chart
"""
_SELECT_ONE = f'SELECT {_COLUMNS} FROM public."DataLabSessions" WHERE "Id" = $1'
# Newest edit first; ties fall back to the newer creation, then the id, so the order is the same on every call.
_SELECT_ALL = (
    f'SELECT {_SUMMARY_COLUMNS} FROM public."DataLabSessions" ORDER BY "UpdatedAt" DESC, "CreatedAt" DESC, "Id"'
)
_INSERT = f"""
    INSERT INTO public."DataLabSessions" (
        "Id", "Name", "Ticker", "FromDate", "ToDate", "Session", "ForwardFill", "Adjusted",
        "EntriesJson", "ChartSnapshotJson", "WindowStartMsUtc", "WindowEndMsUtc",
        "CreatedAt", "UpdatedAt", "CreatedMsUtc", "UpdatedMsUtc"
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12,
        {_instant("$13")}, {_instant("$13")}, $13::bigint, $13::bigint
    )
    RETURNING "Id", "CreatedMsUtc" AS created_at_ms, "UpdatedMsUtc" AS updated_at_ms
"""
# Full replace. The creation instant is create-only; a legacy row without an ms one gets it backfilled here.
_REPLACE = f"""
    UPDATE public."DataLabSessions" SET
        "Name" = $2, "Ticker" = $3, "FromDate" = $4, "ToDate" = $5, "Session" = $6,
        "ForwardFill" = $7, "Adjusted" = $8, "EntriesJson" = $9::jsonb, "ChartSnapshotJson" = $10::jsonb,
        "WindowStartMsUtc" = $11, "WindowEndMsUtc" = $12,
        "UpdatedAt" = {_instant("$13")}, "UpdatedMsUtc" = $13::bigint,
        "CreatedMsUtc" = {_CREATED_AT_MS}
    WHERE "Id" = $1
    RETURNING "Id", "CreatedMsUtc" AS created_at_ms, "UpdatedMsUtc" AS updated_at_ms
"""
_RENAME = f"""
    UPDATE public."DataLabSessions" SET
        "Name" = $2,
        "UpdatedAt" = {_instant("$3")}, "UpdatedMsUtc" = $3::bigint,
        "CreatedMsUtc" = {_CREATED_AT_MS}
    WHERE "Id" = $1
    RETURNING "Id", "CreatedMsUtc" AS created_at_ms, "UpdatedMsUtc" AS updated_at_ms
"""
_DELETE = 'DELETE FROM public."DataLabSessions" WHERE "Id" = $1'


def _document(value: Any) -> str | None:
    """A document as the JSON text a ``jsonb`` parameter takes; ``None`` stays SQL NULL."""
    return None if value is None else json.dumps(value, allow_nan=False)


def session_from_row(row: Mapping[str, Any]) -> DataLabSessionResponse:
    """One session from a ``_COLUMNS`` row.

    A legacy row saved before the window columns existed has none; its window is the one its date strings give, by the
    rule a save uses, and stays null where those are not dates.
    """
    snapshot = row["ChartSnapshotJson"]
    window_start = row["window_start_ms"]
    window_end = row["window_end_ms"]
    return DataLabSessionResponse(
        id=row["Id"],
        name=row["Name"],
        ticker=row["Ticker"],
        from_date=row["FromDate"],
        to_date=row["ToDate"],
        session=row["Session"],
        forward_fill=row["ForwardFill"],
        adjusted=row["Adjusted"],
        entries=json.loads(row["EntriesJson"]),
        chart_snapshot=None if snapshot is None else json.loads(snapshot),
        window_start_ms=session_date_window_ms(row["FromDate"]) if window_start is None else window_start,
        window_end_ms=session_date_window_ms(row["ToDate"]) if window_end is None else window_end,
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def summary_from_row(row: Mapping[str, Any]) -> DataLabSessionSummary:
    """One list entry from a ``_SUMMARY_COLUMNS`` row."""
    return DataLabSessionSummary(
        id=row["Id"],
        name=row["Name"],
        ticker=row["Ticker"],
        from_date=row["FromDate"],
        to_date=row["ToDate"],
        entry_count=row["entry_count"],
        has_chart=row["has_chart"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _receipt(row: Mapping[str, Any]) -> DataLabSessionReceipt:
    return DataLabSessionReceipt(id=row["Id"], created_at_ms=row["created_at_ms"], updated_at_ms=row["updated_at_ms"])


class DataLabSessionStore(Protocol):
    """What the routes need from the saved-session store; the routes inject it so tests can stand in for Postgres."""

    async def list_sessions(self) -> list[DataLabSessionSummary]: ...

    async def get_session(self, session_id: UUID) -> DataLabSessionResponse | None: ...

    async def create_session(self, request: DataLabSessionRequest) -> DataLabSessionReceipt: ...

    async def replace_session(
        self, session_id: UUID, request: DataLabSessionRequest
    ) -> DataLabSessionReceipt | None: ...

    async def rename_session(self, session_id: UUID, name: str) -> DataLabSessionReceipt | None: ...

    async def delete_session(self, session_id: UUID) -> bool: ...


class PostgresDataLabSessionStore:
    """The store over the shared Postgres. ``None`` / ``False`` answers mean no session has that id."""

    def __init__(self, clock: Clock = now_ms_utc) -> None:
        self._clock = clock

    async def list_sessions(self) -> list[DataLabSessionSummary]:
        async with pooled_connection() as conn:
            rows = await conn.fetch(_SELECT_ALL)
        return [summary_from_row(row) for row in rows]

    async def get_session(self, session_id: UUID) -> DataLabSessionResponse | None:
        async with pooled_connection() as conn:
            row = await conn.fetchrow(_SELECT_ONE, session_id)
        return None if row is None else session_from_row(row)

    async def create_session(self, request: DataLabSessionRequest) -> DataLabSessionReceipt:
        async with pooled_connection() as conn:
            row = await conn.fetchrow(_INSERT, uuid4(), *self._body(request), self._clock())
        if row is None:  # an INSERT ... RETURNING that returns nothing is a broken statement, not an absent session
            raise RuntimeError("the DataLabSessions insert returned no row")
        return _receipt(row)

    async def replace_session(self, session_id: UUID, request: DataLabSessionRequest) -> DataLabSessionReceipt | None:
        async with pooled_connection() as conn:
            row = await conn.fetchrow(_REPLACE, session_id, *self._body(request), self._clock())
        return None if row is None else _receipt(row)

    async def rename_session(self, session_id: UUID, name: str) -> DataLabSessionReceipt | None:
        async with pooled_connection() as conn:
            row = await conn.fetchrow(_RENAME, session_id, name, self._clock())
        return None if row is None else _receipt(row)

    async def delete_session(self, session_id: UUID) -> bool:
        async with pooled_connection() as conn:
            outcome = await conn.execute(_DELETE, session_id)
        return outcome == "DELETE 1"

    @staticmethod
    def _body(request: DataLabSessionRequest) -> tuple[Any, ...]:
        """The request's columns in the order ``_INSERT`` and ``_REPLACE`` take them, after the id."""
        return (
            request.name,
            request.ticker,
            request.from_date,
            request.to_date,
            request.session,
            request.forward_fill,
            request.adjusted,
            _document(request.entries),
            _document(request.chart_snapshot),
            session_date_window_ms(request.from_date),
            session_date_window_ms(request.to_date),
        )
