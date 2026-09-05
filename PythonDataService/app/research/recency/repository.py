"""asyncpg repository for the Recency Chart's launches, runs, trades and memberships (PRD #1927).

Python owns these four tables (ADR 0057). The semantics are the ones the
.NET ``RecencyPersistenceService`` / ``RecencyQuery`` established, kept
exactly: a snapshot for a tombstoned launch is a deliberate no-op; a trade
is written once per fingerprint and every run that produces it records a
membership, so deleting one run never hides evidence another live run still
vouches for; the chart reads trades that *overlap* the window while the hero
reads trades that *entered* inside it; the representative membership is the
newest live run (created-at, then id) matching the filters. One rule is new
and deliberate: a snapshot redelivered for a cell the launch already holds
(same launch, symbol, strategy, parameters) returns the existing run instead
of creating a second one — the cutover's "unknown commit outcome" question,
answered by identity rather than by a retry policy.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1927 revision 2;
  docs/superpowers/specs/2026-08-16-recency-chart-design.md D14, D16, D17, D20.
Canonical implementation: this file.
Validated against: tests/research/recency/test_repository.py.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg

from app.research.recency.runner import RecencyRunSnapshot
from app.research.recency.stats import HeroCandidate, HeroTrade
from app.utils.timestamps import now_ms_utc

TERMINAL_LAUNCH_STATUSES: frozenset[str] = frozenset({"COMPLETED", "CANCELLED", "FAILED"})
_FINGERPRINT_RETRIES = 3


class LaunchNotFoundError(LookupError):
    """A snapshot arrived for a launch that was never persisted — the launch must exist before dispatch (D20)."""


class LaunchAccountingError(ValueError):
    """A terminal status whose run counts do not reconcile with the launch."""


@dataclass(frozen=True)
class PersistOutcome:
    recency_run_id: int | None
    # The launch was tombstoned: nothing was written, and that is a successful no-op.
    skipped: bool = False
    # The launch already held this cell: the existing run is returned, nothing is written or counted.
    redelivered: bool = False


@dataclass(frozen=True)
class MembershipView:
    recency_run_id: int
    study_id: int | None
    created_at_ms: int


@dataclass(frozen=True)
class TradeView:
    """One trade as the chart reads it: the representative run's identity plus every live membership."""

    symbol: str
    strategy_key: str
    params_hash: str
    params_json: str
    fingerprint: str
    entry_ms: int
    exit_ms: int
    pnl_pts: float
    pnl_pct: float
    quantity: float
    pnl: float
    holding_sessions: int
    sharpe: float | None
    study_id: int | None
    recency_run_id: int
    is_synthetic_exit: bool
    signal_reason: str
    memberships: list[MembershipView] = field(default_factory=list)


def _num(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


# ── Launch lifecycle ─────────────────────────────────────────────────────


async def create_launch(conn: asyncpg.Connection, *, launch_id: str, config_json: str, expected_runs: int) -> None:
    """The durable launch, written before dispatch; a retried dispatch neither duplicates nor resets it."""
    if expected_runs <= 0:
        raise ValueError("expected_runs must be positive")
    await conn.execute(
        """
        INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
        VALUES ($1, $2::jsonb, $3, 0, 0, 'RUNNING', $4)
        ON CONFLICT ("Id") DO NOTHING
        """,
        launch_id,
        config_json,
        expected_runs,
        now_ms_utc(),
    )


async def set_terminal_status(
    conn: asyncpg.Connection,
    launch_id: str,
    *,
    status: str,
    succeeded_runs: int | None = None,
    failed_runs: int | None = None,
) -> bool:
    """Move a launch to COMPLETED / CANCELLED / FAILED; only COMPLETED demands full accounting."""
    if status not in TERMINAL_LAUNCH_STATUSES:
        raise LaunchAccountingError("status must be COMPLETED, CANCELLED, or FAILED")
    if (succeeded_runs is not None and succeeded_runs < 0) or (failed_runs is not None and failed_runs < 0):
        raise LaunchAccountingError("run counts cannot be negative")
    async with conn.transaction():
        row = await conn.fetchrow(
            'SELECT "ExpectedRuns", "SucceededRuns", "FailedRuns" FROM "RecencyLaunches" WHERE "Id" = $1 FOR UPDATE', launch_id
        )
        if row is None:
            return False
        succeeded = row["SucceededRuns"] if succeeded_runs is None else succeeded_runs
        failed = row["FailedRuns"] if failed_runs is None else failed_runs
        if succeeded + failed > row["ExpectedRuns"]:
            raise LaunchAccountingError("succeeded plus failed runs cannot exceed expected runs")
        if status == "COMPLETED" and succeeded + failed != row["ExpectedRuns"]:
            raise LaunchAccountingError("completed launches must account for every expected run")
        await conn.execute(
            """
            UPDATE "RecencyLaunches"
               SET "Status" = $2, "SucceededRuns" = $3, "FailedRuns" = $4, "CompletedAtMs" = $5
             WHERE "Id" = $1
            """,
            launch_id,
            status,
            succeeded,
            failed,
            now_ms_utc(),
        )
    return True


# ── Snapshot persistence ─────────────────────────────────────────────────


async def persist_snapshot(conn: asyncpg.Connection, snapshot: RecencyRunSnapshot) -> PersistOutcome:
    """Write one run and its trades atomically, honouring the launch tombstone and cell identity.

    The launch row is locked for the whole transaction, so a concurrent
    soft-delete serializes against this insert (never a "deleted" launch
    that keeps gaining children) and two deliveries of the same cell cannot
    both pass the identity check. A fingerprint race with another launch's
    concurrent insert of the same trade is retried a bounded number of times.
    """
    for attempt in range(_FINGERPRINT_RETRIES + 1):
        try:
            return await _persist_once(conn, snapshot)
        except asyncpg.UniqueViolationError:
            if attempt == _FINGERPRINT_RETRIES:
                raise
    raise AssertionError("unreachable")


async def _persist_once(conn: asyncpg.Connection, snapshot: RecencyRunSnapshot) -> PersistOutcome:
    async with conn.transaction():
        launch = await conn.fetchrow('SELECT "DeletedAtMs" FROM "RecencyLaunches" WHERE "Id" = $1 FOR UPDATE', snapshot.launch_id)
        if launch is None:
            raise LaunchNotFoundError(
                f"RecencyLaunch '{snapshot.launch_id}' does not exist; launches are persisted before dispatch (design spec D20)"
            )
        if launch["DeletedAtMs"] is not None:
            return PersistOutcome(recency_run_id=None, skipped=True)
        existing_run = await conn.fetchval(
            """
            SELECT "Id" FROM "RecencyRuns"
             WHERE "RecencyLaunchId" = $1 AND "Symbol" = $2 AND "StrategyKey" = $3 AND "ParamsHash" = $4
             ORDER BY "Id" LIMIT 1
            """,
            snapshot.launch_id,
            snapshot.symbol,
            snapshot.strategy_key,
            snapshot.params_hash,
        )
        if existing_run is not None:
            return PersistOutcome(recency_run_id=int(existing_run), redelivered=True)

        run_id = await conn.fetchval(
            """
            INSERT INTO "RecencyRuns" ("RecencyLaunchId", "Symbol", "StrategyKey", "ParamsJson", "ParamsHash", "StudyId", "TotalPnl", "Sharpe", "CreatedAtMs")
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
            RETURNING "Id"
            """,
            snapshot.launch_id,
            snapshot.symbol,
            snapshot.strategy_key,
            json.dumps(snapshot.params, sort_keys=True),
            snapshot.params_hash,
            snapshot.study_id,
            _dec(snapshot.total_pnl),
            _dec(snapshot.sharpe),
            now_ms_utc(),
        )
        fingerprints = [trade.fingerprint for trade in snapshot.trades]
        existing = {
            row["Fingerprint"]: row["Id"]
            for row in await conn.fetch('SELECT "Id", "Fingerprint" FROM "RecencyTrades" WHERE "Fingerprint" = ANY($1::text[])', fingerprints)
        }
        for trade in snapshot.trades:
            trade_id = existing.get(trade.fingerprint)
            if trade_id is None:
                trade_id = await conn.fetchval(
                    """
                    INSERT INTO "RecencyTrades" ("RecencyRunId", "Fingerprint", "EntryMs", "ExitMs", "PnlPts", "PnlPct", "Quantity", "Pnl", "HoldingSessions", "IsSyntheticExit", "SignalReason")
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING "Id"
                    """,
                    run_id,
                    trade.fingerprint,
                    trade.entry_ms,
                    trade.exit_ms,
                    _dec(trade.pnl_pts),
                    _dec(trade.pnl_pct),
                    Decimal(trade.quantity),
                    _dec(trade.pnl),
                    trade.holding_sessions,
                    trade.is_synthetic_exit,
                    trade.signal_reason,
                )
            # Identical evidence already persisted under another run still gets this run's membership claim.
            await conn.execute(
                'INSERT INTO "RecencyTradeMemberships" ("RecencyTradeId", "RecencyRunId") VALUES ($1, $2) ON CONFLICT DO NOTHING',
                trade_id,
                run_id,
            )
        await conn.execute('UPDATE "RecencyLaunches" SET "SucceededRuns" = "SucceededRuns" + 1 WHERE "Id" = $1', snapshot.launch_id)
        return PersistOutcome(recency_run_id=int(run_id))


# ── Reads ────────────────────────────────────────────────────────────────

_LIVE = 'r."DeletedAtMs" IS NULL AND l."DeletedAtMs" IS NULL'
_FILTERS = '($2::text[] IS NULL OR r."Symbol" = ANY($2::text[])) AND ($3::text[] IS NULL OR r."StrategyKey" = ANY($3::text[]))'


def _or_none(values: Sequence[str] | None) -> list[str] | None:
    return list(values) if values else None


async def list_trades(
    conn: asyncpg.Connection, *, from_ms: int, to_ms: int, symbols: Sequence[str] | None = None, strategies: Sequence[str] | None = None
) -> list[TradeView]:
    """Trades overlapping ``[from_ms, to_ms]`` with at least one live membership matching the filters."""
    symbol_list, strategy_list = _or_none(symbols), _or_none(strategies)
    trades = await conn.fetch(
        f"""
        SELECT t."Id", t."Fingerprint", t."EntryMs", t."ExitMs", t."PnlPts", t."PnlPct", t."Quantity", t."Pnl",
               t."HoldingSessions", t."IsSyntheticExit", t."SignalReason"
          FROM "RecencyTrades" t
         WHERE t."EntryMs" <= $4 AND t."ExitMs" >= $1
           AND EXISTS (
               SELECT 1 FROM "RecencyTradeMemberships" m
                 JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
                 JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
                WHERE m."RecencyTradeId" = t."Id" AND {_LIVE} AND {_FILTERS}
           )
         ORDER BY t."Id"
        """,
        from_ms,
        symbol_list,
        strategy_list,
        to_ms,
    )
    if not trades:
        return []
    memberships = await conn.fetch(
        f"""
        SELECT m."RecencyTradeId", m."RecencyRunId", r."Symbol", r."StrategyKey", r."ParamsHash", r."ParamsJson"::text AS params_json,
               r."Sharpe", r."StudyId", r."CreatedAtMs"
          FROM "RecencyTradeMemberships" m
          JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
          JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
         WHERE m."RecencyTradeId" = ANY($1::int[]) AND {_LIVE}
         ORDER BY r."CreatedAtMs" DESC, m."RecencyRunId" DESC
        """,
        [row["Id"] for row in trades],
    )
    live_by_trade: dict[int, list[asyncpg.Record]] = {}
    for row in memberships:
        live_by_trade.setdefault(row["RecencyTradeId"], []).append(row)
    views: list[TradeView] = []
    for trade in trades:
        live = live_by_trade.get(trade["Id"], [])
        representative = next(
            row
            for row in live
            if (symbol_list is None or row["Symbol"] in symbol_list) and (strategy_list is None or row["StrategyKey"] in strategy_list)
        )
        views.append(
            TradeView(
                symbol=representative["Symbol"],
                strategy_key=representative["StrategyKey"],
                params_hash=representative["ParamsHash"],
                params_json=representative["params_json"],
                fingerprint=trade["Fingerprint"],
                entry_ms=trade["EntryMs"],
                exit_ms=trade["ExitMs"],
                pnl_pts=float(trade["PnlPts"]),
                pnl_pct=float(trade["PnlPct"]),
                quantity=float(trade["Quantity"]),
                pnl=float(trade["Pnl"]),
                holding_sessions=trade["HoldingSessions"],
                sharpe=_num(representative["Sharpe"]),
                study_id=representative["StudyId"],
                recency_run_id=representative["RecencyRunId"],
                is_synthetic_exit=trade["IsSyntheticExit"],
                signal_reason=trade["SignalReason"],
                memberships=[MembershipView(recency_run_id=row["RecencyRunId"], study_id=row["StudyId"], created_at_ms=row["CreatedAtMs"]) for row in live],
            )
        )
    return views


async def hero_candidates(
    conn: asyncpg.Connection, *, from_ms: int, to_ms: int, symbols: Sequence[str] | None = None, strategies: Sequence[str] | None = None
) -> list[HeroCandidate]:
    """Every live (run, trade) pair whose trade *entered* inside the window, grouped per run for the hero rule."""
    rows = await conn.fetch(
        f"""
        SELECT m."RecencyRunId", r."Symbol", r."StrategyKey", r."ParamsHash", t."EntryMs", t."Pnl"
          FROM "RecencyTradeMemberships" m
          JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
          JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
          JOIN "RecencyTrades" t ON t."Id" = m."RecencyTradeId"
         WHERE {_LIVE} AND {_FILTERS} AND t."EntryMs" >= $1 AND t."EntryMs" <= $4
         ORDER BY m."RecencyRunId", t."EntryMs", t."Id"
        """,
        from_ms,
        _or_none(symbols),
        _or_none(strategies),
        to_ms,
    )
    grouped: dict[tuple[int, str, str, str], list[HeroTrade]] = {}
    for row in rows:
        key = (row["RecencyRunId"], row["Symbol"], row["StrategyKey"], row["ParamsHash"])
        grouped.setdefault(key, []).append(HeroTrade(entry_ms=row["EntryMs"], pnl=float(row["Pnl"])))
    return [
        HeroCandidate(recency_run_id=run_id, symbol=symbol, strategy_key=strategy, params_hash=params_hash, trades=tuple(trades))
        for (run_id, symbol, strategy, params_hash), trades in grouped.items()
    ]


# ── Soft delete / restore ────────────────────────────────────────────────


async def set_run_deleted(conn: asyncpg.Connection, run_id: int, *, deleted: bool) -> bool:
    result = await conn.execute('UPDATE "RecencyRuns" SET "DeletedAtMs" = $2 WHERE "Id" = $1', run_id, now_ms_utc() if deleted else None)
    return result.endswith(" 1")


async def set_launch_deleted(conn: asyncpg.Connection, launch_id: str, *, deleted: bool) -> bool:
    result = await conn.execute('UPDATE "RecencyLaunches" SET "DeletedAtMs" = $2 WHERE "Id" = $1', launch_id, now_ms_utc() if deleted else None)
    return result.endswith(" 1")
