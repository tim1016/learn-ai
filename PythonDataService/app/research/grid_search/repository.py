"""asyncpg repository for Grid Search records (PRD #1926 "Storage medium and ownership").

Every function takes an ``asyncpg.Connection`` from whichever pool owns the
calling loop — FastAPI's loop for request handling, the shared background
loop (``app.utils.background_loop``) for worker threads — through
``app.data_lake.catalog_client``'s per-loop pool. The writer loop and pool
owner (review F15) is therefore the background loop: eight cells persist
concurrently on one bounded pool, and FastAPI reads through its own.

**Attempt fencing (review F06).** A search row carries ``attempt``, claimed
atomically by whoever runs it. Every chunk write, terminal transition and
delete checks that generation inside its transaction, so a worker that lost
its job record but stayed alive — and looks interrupted from the outside —
cannot overwrite rows a later Finish produced, nor a terminal status, and a
completed search accepts no write at all. A deleted search's row is gone, so
the same lock finds nothing and refuses the stale writer. Redis absence is a
reason to reconcile, never proof that the worker is dead.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

import asyncpg

from app.research.grid_search.models import (
    CELL_SORT_COLUMNS,
    CellPage,
    CellResult,
    CellRow,
    NewSearch,
    SearchOwner,
    SearchRow,
    SearchStatus,
)
from app.utils.timestamps import now_ms_utc

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
# A completed search is immutable evidence; everything else may be (re)claimed.
CLAIMABLE_STATUSES: frozenset[str] = frozenset({"queued", "running", "failed", "cancelled"})


class StaleAttemptError(RuntimeError):
    """The writer's attempt generation is no longer the search's current one."""


class SearchNotFoundError(LookupError):
    pass


class SearchNotClaimableError(RuntimeError):
    pass


def _row_to_search(row: asyncpg.Record) -> SearchRow:
    return SearchRow(
        id=row["id"],
        owner=SearchOwner(
            kind=row["owner_kind"],
            owner_id=row["owner_id"],
            fold_index=row["fold_index"],
            phase=row["phase"],
        ),
        strategy_key=row["strategy_key"],
        symbol=row["symbol"],
        status=row["status"],
        attempt=row["attempt"],
        job_id=row["job_id"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
        finished_at_ms=row["finished_at_ms"],
        request=json.loads(row["request_json"]),
        receipt=json.loads(row["receipt_json"]),
        expected_cells=row["expected_cells"],
        completed_cells=row["completed_cells"],
        failed_cells=row["failed_cells"],
        leader_params_hash=row["leader_params_hash"],
        leader_params=json.loads(row["leader_params_json"]) if row["leader_params_json"] else None,
        incomplete=row["incomplete"],
        failure_reason=row["failure_reason"],
    )


def _row_to_cell(row: asyncpg.Record) -> CellRow:
    return CellRow(
        search_id=row["search_id"],
        params_hash=row["params_hash"],
        params=json.loads(row["params_json"]),
        status=row["status"],
        attempt=row["attempt"],
        total_trades=row["total_trades"],
        net_profit=row["net_profit"],
        total_return_pct=row["total_return_pct"],
        sharpe_ratio=row["sharpe_ratio"],
        max_drawdown_pct=row["max_drawdown_pct"],
        win_rate=row["win_rate"],
        bars_consumed=row["bars_consumed"],
        error=row["error"],
        exploratory=row["exploratory"],
        completed_at_ms=row["completed_at_ms"],
    )


# The ORDER BY column is always one of these literals, never the caller's string.
_CELL_SORT_SQL: dict[str, str] = {name: name for name in CELL_SORT_COLUMNS}

_SEARCH_COLUMNS = """
    id, owner_kind, owner_id, fold_index, phase, strategy_key, symbol, status, attempt, job_id,
    created_at_ms, updated_at_ms, finished_at_ms, request_json::text AS request_json,
    receipt_json::text AS receipt_json, expected_cells, completed_cells, failed_cells,
    leader_params_hash, leader_params_json::text AS leader_params_json, incomplete, failure_reason
"""
_CELL_COLUMNS = """
    search_id, params_hash, params_json::text AS params_json, status, attempt, total_trades, net_profit,
    total_return_pct, sharpe_ratio, max_drawdown_pct, win_rate, bars_consumed, error, exploratory,
    completed_at_ms
"""


async def create_search(conn: asyncpg.Connection, search: NewSearch) -> SearchRow:
    now = now_ms_utc()
    await conn.execute(
        """
        INSERT INTO research_grid_searches (
            id, owner_kind, owner_id, fold_index, phase, strategy_key, symbol, status, attempt, job_id,
            created_at_ms, updated_at_ms, request_json, receipt_json, expected_cells
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'queued', 0, $8, $9, $9, $10::jsonb, $11::jsonb, $12)
        """,
        search.id,
        search.owner.kind,
        search.owner.owner_id,
        search.owner.fold_index,
        search.owner.phase,
        search.strategy_key,
        search.symbol,
        search.job_id,
        now,
        json.dumps(search.request, sort_keys=True),
        json.dumps(search.receipt, sort_keys=True),
        search.expected_cells,
    )
    row = await get_search(conn, search.id)
    assert row is not None
    return row


async def get_search(conn: asyncpg.Connection, search_id: str) -> SearchRow | None:
    row = await conn.fetchrow(f"SELECT {_SEARCH_COLUMNS} FROM research_grid_searches WHERE id = $1", search_id)
    return _row_to_search(row) if row is not None else None


async def list_searches(
    conn: asyncpg.Connection,
    *,
    owner_kind: str = "user",
    owner_id: str | None = None,
    strategy_key: str | None = None,
    symbol: str | None = None,
    statuses: Sequence[str] | None = None,
    job_id: str | None = None,
    limit: int = 200,
) -> list[SearchRow]:
    """Newest first. ``owner_kind='user'`` is the Grid Search history: walk-forward-owned sweeps never appear."""
    rows = await conn.fetch(
        f"""
        SELECT {_SEARCH_COLUMNS} FROM research_grid_searches
        WHERE owner_kind = $1
          AND ($2::text IS NULL OR owner_id = $2)
          AND ($3::text IS NULL OR strategy_key = $3)
          AND ($4::text IS NULL OR symbol = $4)
          AND ($5::text[] IS NULL OR status = ANY($5::text[]))
          AND ($6::text IS NULL OR job_id = $6)
        ORDER BY created_at_ms DESC, id DESC
        LIMIT $7
        """,
        owner_kind,
        owner_id,
        strategy_key,
        symbol,
        list(statuses) if statuses is not None else None,
        job_id,
        limit,
    )
    return [_row_to_search(row) for row in rows]


async def claim_attempt(conn: asyncpg.Connection, search_id: str, *, job_id: str | None) -> int:
    """Atomically take the next attempt generation and mark the search running."""
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT status FROM research_grid_searches WHERE id = $1 FOR UPDATE",
            search_id,
        )
        if row is None:
            raise SearchNotFoundError(search_id)
        if row["status"] not in CLAIMABLE_STATUSES:
            raise SearchNotClaimableError(f"search {search_id} is {row['status']} and cannot be claimed")
        attempt = await conn.fetchval(
            """
            UPDATE research_grid_searches
               SET attempt = attempt + 1, status = 'running', job_id = $2, updated_at_ms = $3,
                   finished_at_ms = NULL, failure_reason = NULL, incomplete = FALSE
             WHERE id = $1
            RETURNING attempt
            """,
            search_id,
            job_id,
            now_ms_utc(),
        )
        return int(attempt)


async def _lock_current_attempt(conn: asyncpg.Connection, search_id: str, attempt: int) -> None:
    """Row-lock the search and refuse a writer that is stale, or a search that is complete."""
    row = await conn.fetchrow(
        "SELECT attempt, status FROM research_grid_searches WHERE id = $1 FOR UPDATE",
        search_id,
    )
    if row is None:
        raise StaleAttemptError(f"search {search_id} no longer exists; attempt {attempt} may not write")
    if int(row["attempt"]) != attempt:
        raise StaleAttemptError(f"search {search_id} is on attempt {row['attempt']}; attempt {attempt} may not write")
    if row["status"] == "completed":
        raise StaleAttemptError(f"search {search_id} is complete and immutable; attempt {attempt} may not write")


async def write_cells(conn: asyncpg.Connection, search_id: str, attempt: int, cells: Sequence[CellResult]) -> None:
    """Persist one chunk atomically under the attempt fence and refresh the counters."""
    if not cells:
        return
    now = now_ms_utc()
    async with conn.transaction():
        await _lock_current_attempt(conn, search_id, attempt)
        await conn.executemany(
            """
            INSERT INTO research_grid_search_cells (
                search_id, params_hash, params_json, status, attempt, total_trades, net_profit, total_return_pct,
                sharpe_ratio, max_drawdown_pct, win_rate, bars_consumed, error, exploratory, completed_at_ms
            ) VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (search_id, params_hash) DO UPDATE SET
                params_json = EXCLUDED.params_json, status = EXCLUDED.status, attempt = EXCLUDED.attempt,
                total_trades = EXCLUDED.total_trades, net_profit = EXCLUDED.net_profit,
                total_return_pct = EXCLUDED.total_return_pct, sharpe_ratio = EXCLUDED.sharpe_ratio,
                max_drawdown_pct = EXCLUDED.max_drawdown_pct, win_rate = EXCLUDED.win_rate,
                bars_consumed = EXCLUDED.bars_consumed, error = EXCLUDED.error, exploratory = EXCLUDED.exploratory,
                completed_at_ms = EXCLUDED.completed_at_ms
            """,
            [
                (
                    search_id,
                    cell.params_hash,
                    json.dumps(cell.params, sort_keys=True),
                    cell.status,
                    attempt,
                    cell.total_trades,
                    cell.net_profit,
                    cell.total_return_pct,
                    cell.sharpe_ratio,
                    cell.max_drawdown_pct,
                    cell.win_rate,
                    cell.bars_consumed,
                    cell.error,
                    cell.exploratory,
                    now,
                )
                for cell in cells
            ],
        )
        await conn.execute(
            """
            UPDATE research_grid_searches s
               SET completed_cells = c.completed, failed_cells = c.failed, updated_at_ms = $2
              FROM (
                  SELECT COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                         COUNT(*) FILTER (WHERE status = 'failed') AS failed
                    FROM research_grid_search_cells WHERE search_id = $1
              ) c
             WHERE s.id = $1
            """,
            search_id,
            now,
        )


async def finish_search(
    conn: asyncpg.Connection,
    search_id: str,
    attempt: int,
    *,
    status: SearchStatus,
    leader_params_hash: str | None,
    leader_params: dict[str, Any] | None,
    incomplete: bool,
    failure_reason: str | None,
) -> None:
    """Terminal transition under the attempt fence; the leader's parameters are stored with the row."""
    async with conn.transaction():
        await _lock_current_attempt(conn, search_id, attempt)
        await conn.execute(
            """
            UPDATE research_grid_searches
               SET status = $2, leader_params_hash = $3, leader_params_json = $4::jsonb, incomplete = $5,
                   failure_reason = $6, finished_at_ms = $7, updated_at_ms = $7
             WHERE id = $1
            """,
            search_id,
            status,
            leader_params_hash,
            json.dumps(leader_params, sort_keys=True) if leader_params is not None else None,
            incomplete,
            failure_reason,
            now_ms_utc(),
        )


async def mark_exploratory(conn: asyncpg.Connection, search_id: str, *, evidence_params_hash: str) -> None:
    """Label every cell but the fold winner's as exploratory (PRD #1925: not selection evidence)."""
    await conn.execute(
        "UPDATE research_grid_search_cells SET exploratory = (params_hash <> $2) WHERE search_id = $1",
        search_id,
        evidence_params_hash,
    )


async def delete_search(conn: asyncpg.Connection, search_id: str) -> bool:
    result = await conn.execute("DELETE FROM research_grid_searches WHERE id = $1", search_id)
    return result.endswith(" 1")


async def existing_params_hashes(conn: asyncpg.Connection, search_id: str) -> set[str]:
    rows = await conn.fetch("SELECT params_hash FROM research_grid_search_cells WHERE search_id = $1", search_id)
    return {row["params_hash"] for row in rows}


async def list_all_cells(conn: asyncpg.Connection, search_id: str) -> list[CellRow]:
    rows = await conn.fetch(
        f"SELECT {_CELL_COLUMNS} FROM research_grid_search_cells WHERE search_id = $1 ORDER BY params_hash",
        search_id,
    )
    return [_row_to_cell(row) for row in rows]


async def list_cells(
    conn: asyncpg.Connection,
    search_id: str,
    *,
    sort_by: str = "sharpe_ratio",
    direction: Literal["asc", "desc"] = "desc",
    page: int = 1,
    page_size: int = 50,
) -> CellPage:
    """Server-side sorted, paged cells. Nulls (failed / zero-trade measures) always sort last."""
    column = _CELL_SORT_SQL.get(sort_by)
    if column is None:
        raise ValueError(f"unknown sort column {sort_by!r}; allowed: {CELL_SORT_COLUMNS}")
    if direction not in ("asc", "desc"):
        raise ValueError("direction must be 'asc' or 'desc'")
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    order = "ASC NULLS LAST" if direction == "asc" else "DESC NULLS LAST"
    total = await conn.fetchval("SELECT COUNT(*) FROM research_grid_search_cells WHERE search_id = $1", search_id)
    rows = await conn.fetch(
        f"""
        SELECT {_CELL_COLUMNS} FROM research_grid_search_cells
         WHERE search_id = $1
         ORDER BY {column} {order}, params_hash ASC
         LIMIT $2 OFFSET $3
        """,
        search_id,
        page_size,
        (page - 1) * page_size,
    )
    return CellPage(total=int(total or 0), page=page, page_size=page_size, cells=[_row_to_cell(row) for row in rows])

