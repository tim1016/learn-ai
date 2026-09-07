"""asyncpg repository for backtest runs, their trades and parity verdicts (PRD #1929, ADR 0058).

Python owns the three tables (``research/persistence/schema.py`` version 5).
The semantics are the ones ``StudiesApi``, ``BacktestRunPersistenceService``,
``BacktestRunsQuery`` / ``BacktestRunDetailQuery`` and ``ParityVerdictsApi``
established, kept exactly: a LEAN run is idempotent on its ``lean_run_id``
(a redelivery returns the existing row and refuses a different
``requested_engine``), an engine run has no external key and every persist is
a new row; history reads newest-first; the detail read carries the newest
five hundred trades in entry order and says when it truncated; a run backing
a live Recency Chart run cannot be hard-deleted. A parity verdict written from
a landed companion row is never overwritten; the provisional failure the
dispatch path writes before any companion exists is superseded by one
(ADR 0058 as amended by #1977).

Rows come back as frozen dataclasses built straight from the selected
columns (the SELECT lists name exactly their fields); the views the wire
needs but the table does not store — engine identity, the ET calendar dates
of the date-anchored window, per-trade points — are properties. Dates are
date-anchored values stored as ``int64 ms UTC`` at ET midnight
(``app.utils.session_anchors``).
Canonical implementation: this file.
Validated against: tests/research/backtest_runs/test_repository.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import asyncpg

from app.research.backtest_runs.records import BacktestRunRecord, TradeRecord
from app.utils.timestamps import now_ms_utc

REPORT_TRADE_LIMIT = 500
Engine = Literal["PYTHON", "LEAN"]
_SOURCE_BY_ENGINE: dict[str, str] = {"PYTHON": "engine", "LEAN": "lean-sidecar"}
_ENGINE_BY_SOURCE: dict[str, Engine] = {"engine": "PYTHON", "lean-sidecar": "LEAN"}

PARITY_CREATE_STATUSES: frozenset[str] = frozenset({"pending", "unavailable"})
PARITY_FAILURE_STATUSES: frozenset[str] = frozenset({"run_failed", "persist_failed"})
# The dispatch path writes these before any companion row exists, so each is a
# claim about the future ("no comparable companion is coming") rather than a
# comparison. A companion row that does land falsifies the claim and supersedes
# it; a computed verdict never is superseded (ADR 0058, amended by #1977).
PARITY_SUPERSEDABLE_STATUSES: frozenset[str] = frozenset({"pending"}) | PARITY_FAILURE_STATUSES
PARITY_VERDICT_VERSION = 2

DeleteOutcome = Literal["deleted", "not_found", "recency_member"]


class RunConflictError(ValueError):
    """A LEAN run id was redelivered with a different ``requested_engine``."""


@dataclass(frozen=True, slots=True)
class InsertOutcome:
    run_id: int
    created: bool


@dataclass(frozen=True, slots=True)
class RunRow:
    """The columns both reads share, plus the derived views the wire needs."""

    id: int
    source: str
    strategy_name: str
    symbol: str
    lean_run_id: str | None
    parameters_json: str
    start_ms: int
    end_ms: int
    executed_at_ms: int
    total_trades: int
    total_pnl: float
    commission_per_order: float | None
    brokerage_policy: str | None
    notes: str | None
    data_policy_json: str | None
    verdict_grade: str | None
    verdict_signal: str | None
    parity_group_id: str | None

    @property
    def engine(self) -> Engine:
        return _ENGINE_BY_SOURCE[self.source]



@dataclass(frozen=True, slots=True)
class RunSummary(RunRow):
    """One history row."""

    has_synthetic_exit: bool


@dataclass(frozen=True, slots=True)
class TradeRow:
    id: int
    trade_number: int
    entry_ms: int
    exit_ms: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    signal_reason: str
    is_synthetic_exit: bool

    @property
    def pnl_pts(self) -> float:
        return self.exit_price - self.entry_price

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price if self.entry_price > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ParityVerdictRow:
    id: int
    parity_group_id: str
    left_run_id: int
    right_run_id: int | None
    verdict_version: int
    status: str
    verdict_json: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class RunDetail(RunRow):
    """Everything the run report reads, plus the bounded trade evidence."""

    requested_engine: str | None
    fill_mode: str
    timespan: str
    duration_ms: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    initial_cash: float
    final_equity: float
    total_fees: float
    max_drawdown: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    profit_factor: float | None
    lean_statistics_json: str | None
    lean_analysis_json: str | None
    run_verdict_json: str | None
    verdict_version: int | None
    equity_curve_json: str | None
    validation_analytics_json: str | None
    insight_summary_json: str | None
    metric_documentation_json: str | None
    trades: tuple[TradeRow, ...]
    trades_truncated: bool
    parity_verdicts: tuple[ParityVerdictRow, ...]


# ── Writes ───────────────────────────────────────────────────────────────


async def insert_run(conn: asyncpg.Connection, record: BacktestRunRecord) -> InsertOutcome:
    """Write one run and its trades atomically; a LEAN redelivery returns the existing row."""
    if record.lean_run_id is not None:
        existing = await _existing_lean_run(conn, record)
        if existing is not None:
            return existing
    try:
        async with conn.transaction():
            run_id = await conn.fetchval(
                """
                INSERT INTO research_backtest_runs (
                    source, requested_engine, lean_run_id, parity_group_id, strategy_name, symbol, parameters_json,
                    start_ms, end_ms, timespan, fill_mode, executed_at_ms, duration_ms,
                    total_trades, winning_trades, losing_trades, win_rate, total_pnl, initial_cash, final_equity, total_fees,
                    max_drawdown, sharpe_ratio, sortino_ratio, profit_factor, commission_per_order, brokerage_policy,
                    data_policy_json, lean_statistics_json, lean_analysis_json, run_verdict_json,
                    verdict_version, verdict_grade, verdict_signal, equity_curve_json, validation_analytics_json,
                    insight_summary_json, metric_documentation_json
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb,
                    $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20, $21,
                    $22, $23, $24, $25, $26, $27,
                    $28::jsonb, $29::jsonb, $30::jsonb, $31::jsonb,
                    $32, $33, $34, $35::jsonb, $36::jsonb,
                    $37::jsonb, $38::jsonb
                )
                RETURNING id
                """,
                record.source,
                record.requested_engine,
                record.lean_run_id,
                record.parity_group_id,
                record.strategy_name,
                record.symbol,
                json.dumps(record.parameters, sort_keys=True),
                record.start_ms,
                record.end_ms,
                record.timespan,
                record.fill_mode,
                now_ms_utc(),
                record.duration_ms,
                record.total_trades,
                record.winning_trades,
                record.losing_trades,
                record.win_rate,
                record.total_pnl,
                record.initial_cash,
                record.final_equity,
                record.total_fees,
                record.max_drawdown,
                record.sharpe_ratio,
                record.sortino_ratio,
                record.profit_factor,
                record.commission_per_order,
                record.brokerage_policy,
                record.data_policy_json,
                record.lean_statistics_json,
                record.lean_analysis_json,
                record.run_verdict_json,
                record.verdict_version,
                record.verdict_grade,
                record.verdict_signal,
                record.equity_curve_json,
                record.validation_analytics_json,
                record.insight_summary_json,
                record.metric_documentation_json,
            )
            await _insert_trades(conn, int(run_id), record.trades)
    except asyncpg.UniqueViolationError:
        # A concurrent persist of the same LEAN run won the race; hand back its row.
        winner = await _existing_lean_run(conn, record)
        if winner is None:
            raise
        return winner
    return InsertOutcome(run_id=int(run_id), created=True)


async def _existing_lean_run(conn: asyncpg.Connection, record: BacktestRunRecord) -> InsertOutcome | None:
    row = await conn.fetchrow(
        "SELECT id, requested_engine FROM research_backtest_runs WHERE lean_run_id = $1", record.lean_run_id
    )
    if row is None:
        return None
    existing_engine = row["requested_engine"]
    if (
        existing_engine is not None
        and record.requested_engine is not None
        and existing_engine != record.requested_engine
    ):
        raise RunConflictError(f"requested_engine conflicts with the existing run '{record.lean_run_id}'")
    return InsertOutcome(run_id=int(row["id"]), created=False)


async def _insert_trades(conn: asyncpg.Connection, run_id: int, trades: tuple[TradeRecord, ...]) -> None:
    if not trades:
        return
    ordered = sorted(trades, key=lambda trade: (trade.entry_ms, trade.trade_number))
    await conn.execute(
        """
        INSERT INTO research_backtest_run_trades
            (run_id, trade_number, entry_ms, exit_ms, entry_price, exit_price, quantity, pnl, signal_reason, is_synthetic_exit)
        SELECT $1, u.* FROM unnest(
            $2::integer[], $3::bigint[], $4::bigint[], $5::double precision[], $6::double precision[],
            $7::double precision[], $8::double precision[], $9::text[], $10::boolean[]
        ) AS u
        """,
        run_id,
        [t.trade_number for t in ordered],
        [t.entry_ms for t in ordered],
        [t.exit_ms for t in ordered],
        [t.entry_price for t in ordered],
        [t.exit_price for t in ordered],
        [t.quantity for t in ordered],
        [t.pnl for t in ordered],
        [t.signal_reason for t in ordered],
        [t.is_synthetic_exit for t in ordered],
    )


async def update_notes(conn: asyncpg.Connection, run_id: int, notes: str | None) -> bool:
    result = await conn.execute("UPDATE research_backtest_runs SET notes = $2 WHERE id = $1", run_id, notes)
    return result.endswith(" 1")


async def delete_run(conn: asyncpg.Connection, run_id: int) -> DeleteOutcome:
    """Hard-delete a run unless a live Recency Chart run still points at it.

    A study backing a live Recency run must go through Recency soft-delete
    (design spec D22, P0-4): deleting it here would break "forever until you
    soft-delete it" out from under the chart. Both tables are Python-owned
    now, so the guard is a join rather than the cross-owner read it used to be.
    """
    async with conn.transaction():
        exists = await conn.fetchval("SELECT 1 FROM research_backtest_runs WHERE id = $1 FOR UPDATE", run_id)
        if exists is None:
            return "not_found"
        if await is_recency_member(conn, run_id):
            return "recency_member"
        await conn.execute("DELETE FROM research_backtest_runs WHERE id = $1", run_id)
    return "deleted"


async def is_recency_member(conn: asyncpg.Connection, run_id: int) -> bool:
    """True iff ``run_id`` backs a non-tombstoned Recency run."""
    live = await conn.fetchval(
        'SELECT count(*) FROM "RecencyRuns" WHERE "StudyId" = $1 AND "DeletedAtMs" IS NULL', run_id
    )
    return int(live) > 0


# ── Reads ────────────────────────────────────────────────────────────────

# Column lists name exactly the dataclass fields, so a row builds its dataclass directly.
_RUN_ROW_COLUMNS = """
    r.id, r.source, r.strategy_name, r.symbol, r.lean_run_id, r.parameters_json::text AS parameters_json,
    r.start_ms, r.end_ms, r.executed_at_ms, r.total_trades, r.total_pnl, r.commission_per_order,
    r.brokerage_policy, r.notes, r.data_policy_json::text AS data_policy_json, r.verdict_grade,
    r.verdict_signal, r.parity_group_id
"""
_RUN_DETAIL_COLUMNS = f"""
    {_RUN_ROW_COLUMNS},
    r.requested_engine, r.fill_mode, r.timespan, r.duration_ms, r.winning_trades, r.losing_trades, r.win_rate,
    r.initial_cash, r.final_equity, r.total_fees, r.max_drawdown, r.sharpe_ratio, r.sortino_ratio, r.profit_factor,
    r.lean_statistics_json::text AS lean_statistics_json, r.lean_analysis_json::text AS lean_analysis_json,
    r.run_verdict_json::text AS run_verdict_json, r.verdict_version,
    r.equity_curve_json::text AS equity_curve_json, r.validation_analytics_json::text AS validation_analytics_json,
    r.insight_summary_json::text AS insight_summary_json,
    r.metric_documentation_json::text AS metric_documentation_json
"""
_TRADE_COLUMNS = (
    "id, trade_number, entry_ms, exit_ms, entry_price, exit_price, quantity, pnl, signal_reason, is_synthetic_exit"
)
_VERDICT_COLUMNS = "id, parity_group_id, left_run_id, right_run_id, verdict_version, status, verdict_json::text AS verdict_json, created_at_ms"


async def list_runs(conn: asyncpg.Connection, *, engine: Engine | None, limit: int) -> list[RunSummary]:
    """History rows newest-first, optionally one engine's."""
    rows = await conn.fetch(
        f"""
        SELECT {_RUN_ROW_COLUMNS},
               EXISTS (
                   SELECT 1 FROM research_backtest_run_trades t WHERE t.run_id = r.id AND t.is_synthetic_exit
               ) AS has_synthetic_exit
          FROM research_backtest_runs r
         WHERE $1::text IS NULL OR r.source = $1::text
         ORDER BY r.executed_at_ms DESC, r.id DESC
         LIMIT $2
        """,
        None if engine is None else _SOURCE_BY_ENGINE[engine],
        limit,
    )
    return [RunSummary(**row) for row in rows]


async def get_run(
    conn: asyncpg.Connection, run_id: int, *, trade_limit: int | None = REPORT_TRADE_LIMIT
) -> RunDetail | None:
    """One run with its newest ``trade_limit`` trades in entry order (``None`` reads them all)."""
    row = await conn.fetchrow(f"SELECT {_RUN_DETAIL_COLUMNS} FROM research_backtest_runs r WHERE r.id = $1", run_id)
    if row is None:
        return None
    trades, truncated = await _trades_for_report(conn, run_id, trade_limit)
    return RunDetail(
        **row, trades=trades, trades_truncated=truncated, parity_verdicts=await list_parity_verdicts(conn, run_id)
    )


async def _trades_for_report(
    conn: asyncpg.Connection, run_id: int, limit: int | None
) -> tuple[tuple[TradeRow, ...], bool]:
    """The newest ``limit`` trades, returned in entry order, and whether older ones were left out."""
    rows = await conn.fetch(
        f"""
        SELECT {_TRADE_COLUMNS}
          FROM research_backtest_run_trades
         WHERE run_id = $1
         ORDER BY entry_ms DESC, id DESC
         LIMIT $2
        """,
        run_id,
        None if limit is None else limit + 1,
    )
    truncated = limit is not None and len(rows) > limit
    kept = rows[:limit] if truncated else rows
    return tuple(TradeRow(**row) for row in reversed(kept)), truncated


# ── Parity verdicts ──────────────────────────────────────────────────────


async def list_parity_verdicts(conn: asyncpg.Connection, run_id: int) -> tuple[ParityVerdictRow, ...]:
    rows = await conn.fetch(
        f"SELECT {_VERDICT_COLUMNS} FROM research_parity_verdicts WHERE left_run_id = $1 OR right_run_id = $1 ORDER BY id",
        run_id,
    )
    return tuple(ParityVerdictRow(**row) for row in rows)


async def get_parity_verdict(conn: asyncpg.Connection, parity_group_id: str) -> ParityVerdictRow | None:
    row = await conn.fetchrow(
        f"SELECT {_VERDICT_COLUMNS} FROM research_parity_verdicts WHERE parity_group_id = $1", parity_group_id
    )
    return None if row is None else ParityVerdictRow(**row)


async def create_parity_verdict(
    conn: asyncpg.Connection, *, parity_group_id: str, left_run_id: int, status: str, verdict_json: str
) -> ParityVerdictRow:
    """Record the run-time disposition; idempotent per group (a racing terminal verdict is kept)."""
    if status not in PARITY_CREATE_STATUSES:
        raise ValueError(f"status must be one of {sorted(PARITY_CREATE_STATUSES)}")
    await conn.execute(
        """
        INSERT INTO research_parity_verdicts
            (parity_group_id, left_run_id, right_run_id, verdict_version, status, verdict_json, created_at_ms)
        VALUES ($1, $2, NULL, $3, $4, $5::jsonb, $6)
        ON CONFLICT (parity_group_id) DO NOTHING
        """,
        parity_group_id,
        left_run_id,
        PARITY_VERDICT_VERSION,
        status,
        verdict_json or "{}",
        now_ms_utc(),
    )
    row = await get_parity_verdict(conn, parity_group_id)
    assert row is not None  # just inserted or already present
    return row


async def mark_parity_failed(
    conn: asyncpg.Connection, parity_group_id: str, *, status: str, detail: str
) -> tuple[ParityVerdictRow | None, bool]:
    """``pending -> run_failed | persist_failed``; a verdict that already froze is never overwritten.

    Returns the group's row (``None`` when the group is unknown) and whether
    this call performed the transition.
    """
    if status not in PARITY_FAILURE_STATUSES:
        raise ValueError(f"status must be one of {sorted(PARITY_FAILURE_STATUSES)}")
    verdict_json = json.dumps(
        {
            "schema_version": 1,
            "parity_group_id": parity_group_id,
            "status": status,
            "reason": detail,
            "computed_at_ms": now_ms_utc(),
        }
    )
    # One conditional UPDATE: two concurrent failures cannot both win the transition.
    result = await conn.execute(
        """
        UPDATE research_parity_verdicts
           SET status = $2, verdict_json = $3::jsonb
         WHERE parity_group_id = $1 AND status = 'pending'
        """,
        parity_group_id,
        status,
        verdict_json,
    )
    return await get_parity_verdict(conn, parity_group_id), result.endswith(" 1")


async def freeze_parity_verdict(
    conn: asyncpg.Connection,
    *,
    parity_group_id: str,
    left_run_id: int,
    right_run_id: int,
    status: str,
    verdict_json: str,
) -> bool:
    """Freeze the computed verdict onto the group's row, inserting it if the row was lost.

    Returns whether the verdict was written. ``False`` means the group already
    carries a verdict this one may not replace: another computed comparison, or
    the ``unavailable`` disposition of a group that never dispatched a
    companion. A provisional dispatch failure (:data:`PARITY_FAILURE_STATUSES`)
    *is* replaced — the companion row in hand disproves it (#1977).
    """
    async with conn.transaction():
        existing = await conn.fetchrow(
            "SELECT status FROM research_parity_verdicts WHERE parity_group_id = $1 FOR UPDATE", parity_group_id
        )
        if existing is None:
            await conn.execute(
                """
                INSERT INTO research_parity_verdicts
                    (parity_group_id, left_run_id, right_run_id, verdict_version, status, verdict_json, created_at_ms)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                """,
                parity_group_id,
                left_run_id,
                right_run_id,
                PARITY_VERDICT_VERSION,
                status,
                verdict_json,
                now_ms_utc(),
            )
            return True
        if existing["status"] not in PARITY_SUPERSEDABLE_STATUSES:
            return False
        await conn.execute(
            """
            UPDATE research_parity_verdicts
               SET right_run_id = $2, verdict_version = $3, status = $4, verdict_json = $5::jsonb
             WHERE parity_group_id = $1
            """,
            parity_group_id,
            right_run_id,
            PARITY_VERDICT_VERSION,
            status,
            verdict_json,
        )
        return True


async def find_left_run_id(conn: asyncpg.Connection, parity_group_id: str) -> int | None:
    """The Python engine run of a parity group (the oldest, if a group somehow holds several)."""
    return await conn.fetchval(
        "SELECT id FROM research_backtest_runs WHERE parity_group_id = $1 AND source = 'engine' ORDER BY id LIMIT 1",
        parity_group_id,
    )
