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
from decimal import Decimal

import asyncpg

from app.research.recency.models import MembershipView, PersistOutcome, TradeView
from app.research.recency.runner import RecencyRunSnapshot
from app.research.recency.stats import HeroCandidate, HeroTrade
from app.utils.timestamps import now_ms_utc

TERMINAL_LAUNCH_STATUSES: frozenset[str] = frozenset({"COMPLETED", "CANCELLED", "FAILED"})


class LaunchNotFoundError(LookupError):
    """A snapshot arrived for a launch that was never persisted — the launch must exist before dispatch (D20)."""


class LaunchAccountingError(ValueError):
    """A terminal status whose run counts do not reconcile with the launch."""


class LaunchConflictError(ValueError):
    """A launch id arrived again with a different configuration; running the new grid under the first record would misdescribe every run it holds."""


def _numeric(value: float) -> Decimal:
    """``numeric(18,8)`` columns take a Decimal; ``str`` keeps the float's shortest repr, not its binary expansion."""
    return Decimal(str(value))


# ── Launch lifecycle ─────────────────────────────────────────────────────


async def create_launch(conn: asyncpg.Connection, *, launch_id: str, config_json: str, expected_runs: int) -> bool:
    """The durable launch, written before dispatch; returns whether this call created it.

    A retried dispatch of the same launch is a no-op (``False``) that neither
    duplicates nor resets the record. The same id with a *different*
    configuration is refused: ``ON CONFLICT DO NOTHING`` alone would keep the
    first ``ConfigJson`` while the new grid ran under it.
    """
    if expected_runs <= 0:
        raise ValueError("expected_runs must be positive")
    inserted = await conn.fetchval(
        """
        INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
        VALUES ($1, $2::jsonb, $3, 0, 0, 'RUNNING', $4)
        ON CONFLICT ("Id") DO NOTHING
        RETURNING "Id"
        """,
        launch_id,
        config_json,
        expected_runs,
        now_ms_utc(),
    )
    if inserted is not None:
        return True
    # jsonb equality is semantic (key order, numeric scale) and codec-agnostic. Two statements
    # without a transaction is safe because launches are never hard-deleted: the row read here
    # is the one the insert collided with.
    same_config = await conn.fetchval('SELECT "ConfigJson" = $2::jsonb FROM "RecencyLaunches" WHERE "Id" = $1', launch_id, config_json)
    if not same_config:
        raise LaunchConflictError(f"launch {launch_id} already exists with a different configuration")
    return False


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
    both pass the identity check. Trades are written in one statement with
    ``ON CONFLICT ("Fingerprint") DO NOTHING`` — a fingerprint another run
    inserted first, concurrently or long ago, simply keeps that run's row —
    and memberships in a second, so a run holds the launch lock for two
    round trips rather than two per trade.
    """
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
            _numeric(snapshot.total_pnl),
            None if snapshot.sharpe is None else _numeric(snapshot.sharpe),
            now_ms_utc(),
        )
        trades = snapshot.trades
        if trades:
            fingerprints = [trade.fingerprint for trade in trades]
            await conn.execute(
                """
                INSERT INTO "RecencyTrades" ("RecencyRunId", "Fingerprint", "EntryMs", "ExitMs", "PnlPts", "PnlPct", "Quantity", "Pnl", "HoldingSessions", "IsSyntheticExit", "SignalReason")
                SELECT $1, u.* FROM unnest($2::text[], $3::bigint[], $4::bigint[], $5::numeric[], $6::numeric[], $7::numeric[], $8::numeric[], $9::integer[], $10::boolean[], $11::text[]) AS u
                ON CONFLICT ("Fingerprint") DO NOTHING
                """,
                run_id,
                fingerprints,
                [t.entry_ms for t in trades],
                [t.exit_ms for t in trades],
                [_numeric(t.pnl_pts) for t in trades],
                [_numeric(t.pnl_pct) for t in trades],
                [Decimal(t.quantity) for t in trades],
                [_numeric(t.pnl) for t in trades],
                [t.holding_sessions for t in trades],
                [t.is_synthetic_exit for t in trades],
                [t.signal_reason for t in trades],
            )
            # Every run that produced a fingerprint vouches for it, whichever run inserted the row.
            await conn.execute(
                """
                INSERT INTO "RecencyTradeMemberships" ("RecencyTradeId", "RecencyRunId")
                SELECT t."Id", $2 FROM "RecencyTrades" t WHERE t."Fingerprint" = ANY($1::text[])
                ON CONFLICT DO NOTHING
                """,
                fingerprints,
                run_id,
            )
        await conn.execute('UPDATE "RecencyLaunches" SET "SucceededRuns" = "SucceededRuns" + 1 WHERE "Id" = $1', snapshot.launch_id)
        return PersistOutcome(recency_run_id=int(run_id))


# ── Reads ────────────────────────────────────────────────────────────────

# Placeholders: $1 from_ms, $2 to_ms, $3 symbols (NULL = any), $4 strategies (NULL = any).
_LIVE = 'r."DeletedAtMs" IS NULL AND l."DeletedAtMs" IS NULL'
_MATCHES_FILTERS = '($3::text[] IS NULL OR r."Symbol" = ANY($3::text[])) AND ($4::text[] IS NULL OR r."StrategyKey" = ANY($4::text[]))'


def _or_none(values: Sequence[str] | None) -> list[str] | None:
    return list(values) if values else None


def _matches(row: dict, symbols: list[str] | None, strategies: list[str] | None) -> bool:
    return (symbols is None or row["symbol"] in symbols) and (strategies is None or row["strategy_key"] in strategies)


async def list_trades(
    conn: asyncpg.Connection, *, from_ms: int, to_ms: int, symbols: Sequence[str] | None = None, strategies: Sequence[str] | None = None
) -> list[TradeView]:
    """Trades overlapping ``[from_ms, to_ms]`` with at least one live membership matching the filters.

    One statement: the trade rows and, per trade, its live memberships as a
    JSON array ordered newest run first — so a soft-delete committing between
    two reads can never leave a trade with a membership list that contradicts
    the predicate that selected it.
    """
    symbol_list, strategy_list = _or_none(symbols), _or_none(strategies)
    rows = await conn.fetch(
        f"""
        SELECT t."Fingerprint", t."EntryMs", t."ExitMs", t."PnlPts", t."PnlPct", t."Quantity", t."Pnl",
               t."HoldingSessions", t."IsSyntheticExit", t."SignalReason",
               (SELECT json_agg(json_build_object(
                           'recency_run_id', m."RecencyRunId", 'symbol', r."Symbol", 'strategy_key', r."StrategyKey",
                           'params_hash', r."ParamsHash", 'params_json', r."ParamsJson"::text, 'sharpe', r."Sharpe",
                           'study_id', r."StudyId", 'created_at_ms', r."CreatedAtMs")
                       ORDER BY r."CreatedAtMs" DESC, m."RecencyRunId" DESC)
                  FROM "RecencyTradeMemberships" m
                  JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
                  JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
                 WHERE m."RecencyTradeId" = t."Id" AND {_LIVE}) AS live_memberships
          FROM "RecencyTrades" t
         WHERE t."EntryMs" <= $2 AND t."ExitMs" >= $1
           AND EXISTS (
               SELECT 1 FROM "RecencyTradeMemberships" m
                 JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
                 JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
                WHERE m."RecencyTradeId" = t."Id" AND {_LIVE} AND {_MATCHES_FILTERS}
           )
         ORDER BY t."Id"
        """,
        from_ms,
        to_ms,
        symbol_list,
        strategy_list,
    )
    views: list[TradeView] = []
    for trade in rows:
        live: list[dict] = json.loads(trade["live_memberships"] or "[]")
        representative = next((m for m in live if _matches(m, symbol_list, strategy_list)), None)
        if representative is None:
            continue  # nobody vouches for it any more; not drawn
        views.append(
            TradeView(
                symbol=representative["symbol"],
                strategy_key=representative["strategy_key"],
                params_hash=representative["params_hash"],
                params_json=representative["params_json"],
                fingerprint=trade["Fingerprint"],
                entry_ms=trade["EntryMs"],
                exit_ms=trade["ExitMs"],
                pnl_pts=float(trade["PnlPts"]),
                pnl_pct=float(trade["PnlPct"]),
                quantity=float(trade["Quantity"]),
                pnl=float(trade["Pnl"]),
                holding_sessions=trade["HoldingSessions"],
                sharpe=None if representative["sharpe"] is None else float(representative["sharpe"]),
                study_id=representative["study_id"],
                recency_run_id=representative["recency_run_id"],
                is_synthetic_exit=trade["IsSyntheticExit"],
                signal_reason=trade["SignalReason"],
                memberships=[MembershipView(recency_run_id=m["recency_run_id"], study_id=m["study_id"], created_at_ms=m["created_at_ms"]) for m in live],
            )
        )
    return views


async def hero_candidates(
    conn: asyncpg.Connection, *, from_ms: int, to_ms: int, symbols: Sequence[str] | None = None, strategies: Sequence[str] | None = None
) -> list[HeroCandidate]:
    """Every live run's trades that *entered* inside the window, grouped per run for the hero rule."""
    rows = await conn.fetch(
        f"""
        SELECT m."RecencyRunId", r."Symbol", r."StrategyKey", r."ParamsHash", t."EntryMs", t."Pnl"
          FROM "RecencyTradeMemberships" m
          JOIN "RecencyRuns" r ON r."Id" = m."RecencyRunId"
          JOIN "RecencyLaunches" l ON l."Id" = r."RecencyLaunchId"
          JOIN "RecencyTrades" t ON t."Id" = m."RecencyTradeId"
         WHERE {_LIVE} AND {_MATCHES_FILTERS} AND t."EntryMs" >= $1 AND t."EntryMs" <= $2
         ORDER BY m."RecencyRunId", t."EntryMs", t."Id"
        """,
        from_ms,
        to_ms,
        _or_none(symbols),
        _or_none(strategies),
    )
    candidates: dict[int, HeroCandidate] = {}
    for row in rows:
        run_id = row["RecencyRunId"]
        candidate = candidates.get(run_id)
        trade = HeroTrade(entry_ms=row["EntryMs"], pnl=float(row["Pnl"]))
        if candidate is None:
            candidates[run_id] = HeroCandidate(recency_run_id=run_id, symbol=row["Symbol"], strategy_key=row["StrategyKey"], params_hash=row["ParamsHash"], trades=(trade,))
        else:
            candidates[run_id] = HeroCandidate(recency_run_id=run_id, symbol=candidate.symbol, strategy_key=candidate.strategy_key, params_hash=candidate.params_hash, trades=(*candidate.trades, trade))
    return list(candidates.values())


# ── Soft delete / restore ────────────────────────────────────────────────


async def set_run_deleted(conn: asyncpg.Connection, run_id: int, *, deleted: bool) -> bool:
    result = await conn.execute('UPDATE "RecencyRuns" SET "DeletedAtMs" = $2 WHERE "Id" = $1', run_id, now_ms_utc() if deleted else None)
    return result.endswith(" 1")


async def set_launch_deleted(conn: asyncpg.Connection, launch_id: str, *, deleted: bool) -> bool:
    result = await conn.execute('UPDATE "RecencyLaunches" SET "DeletedAtMs" = $2 WHERE "Id" = $1', launch_id, now_ms_utc() if deleted else None)
    return result.endswith(" 1")
