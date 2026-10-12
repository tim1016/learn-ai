"""Options Backtest runs an owner can reopen: their own tables, saved exactly (#3030).

Python owns the tables (``research/persistence/schema.py`` version 20). The functions take a connection, the way
``saved_setups.py`` does. Money and prices are ``NUMERIC`` and come back as the same ``Decimal``; what is kept as JSON
(a session's events, the summary tables) holds every ``Decimal`` as its text (``run_json.py``). No float is anywhere.

A run is made ``running`` when its job is accepted. It ends in exactly one of three ways, and only from ``running``:

* ``complete_run``: the figures, every session and every leg trade, in one transaction. A run that is complete has all
  of them, and nothing that is not complete has any: a part of a run is never a result.
* ``fail_run``: a code and a plain reason, and a list of what failed, with no figures.
* ``cancel_run``: nothing but the status.

A row that says ``running`` while its job is gone reads back as ``interrupted``; whether the job is alive is asked of
the caller's ``is_live`` (``lifecycle.job_is_live``), and ``None`` ("cannot tell") leaves it ``running``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import asyncpg

from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION
from app.options.contract import OptionContract
from app.options_backtest import run_json
from app.options_backtest.describe import fee_policy_sentence
from app.options_backtest.results import ExitReason, Fill, LegTrade, Mark, SessionOutcome, SessionStatus, SkipReason
from app.options_backtest.run import RunResult, SlippagePoint, SlippageTolerance
from app.options_backtest.session_result import SessionResult
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.statistics import RunSummary
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection, writer_loop_call
from app.utils.background_loop import run_on_background_loop
from app.utils.session_anchors import et_date_at_ms

StoredStatus = Literal["running", "complete", "failed", "cancelled"]
PresentedStatus = Literal["running", "complete", "failed", "cancelled", "interrupted"]
IsLive = Callable[[str | None], bool | None]

# How long a worker waits for the one transaction that saves a whole run.
SAVE_TIMEOUT_SECONDS = 600.0

_RUN_COLUMNS = """
    id, created_at_ms, finished_at_ms, job_id, status, setup_name, underlying, first_session_open_ms,
    last_session_open_ms, sessions_requested, fee_policy_text, calendar_identity, calendar_package_version,
    failure_code, failure_reason, failure_detail_json::text AS failure_detail_json,
    sessions_tested, sessions_traded, trades, trades_won, net_profit_usd, ending_cash_usd
"""


class DuplicateJob(ValueError):
    """A run already exists for that job id."""


class RunNotRunning(RuntimeError):
    """A run was asked to end and is not (or no longer) running: it was deleted, or it had already ended."""


class RunDoesNotMatch(ValueError):
    """A result handed to ``complete_run`` is not a result of the run it was handed to."""


@dataclass(frozen=True)
class FailureItem:
    """One thing that failed: a session (by its open) or the run as a whole (``None``), a code and plain words."""

    session_open_ms: int | None
    code: str
    message: str


@dataclass(frozen=True)
class RunFailure:
    """Why a run failed, in a code a page can branch on, a plain reason, and the list of what failed."""

    code: str
    reason: str
    items: tuple[FailureItem, ...] = ()


@dataclass(frozen=True)
class NewRun:
    """What is known when a run is accepted. ``sessions_requested`` is the number of sessions of the range."""

    job_id: str
    created_at_ms: int
    setup: OptionsBacktestSetup
    setup_name: str | None
    first_session_open_ms: int
    last_session_open_ms: int
    sessions_requested: int


@dataclass(frozen=True)
class Headline:
    """The figures the history shows beside a run; each is also in the run's summary."""

    sessions_tested: int
    sessions_traded: int
    trades: int
    trades_won: int
    net_profit_usd: Decimal
    ending_cash_usd: Decimal


@dataclass(frozen=True)
class RunRecord:
    """A stored run without its figures' tables: what the history lists."""

    id: int
    created_at_ms: int
    finished_at_ms: int | None
    job_id: str
    status: PresentedStatus
    setup_name: str | None
    underlying: str
    first_session_open_ms: int
    last_session_open_ms: int
    sessions_requested: int
    fee_policy_text: str
    calendar_identity: str
    calendar_package_version: str
    failure: RunFailure | None
    headline: Headline | None


@dataclass(frozen=True)
class RunFigures:
    summary: RunSummary
    slippage: tuple[SlippagePoint, ...]
    slippage_tolerance: SlippageTolerance


@dataclass(frozen=True)
class StoredRun:
    """One run, opened: its record, the setup it ran, and (when complete) its summary, ladder and tolerance."""

    record: RunRecord
    setup: OptionsBacktestSetup
    figures: RunFigures | None


class DeleteOutcome(StrEnum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    RUNNING = "running"


def fee_policy_text(setup: OptionsBacktestSetup) -> str:
    """The fee policy in words, as the headline of the run says it."""
    return fee_policy_sentence(setup.fees)


# ── Writes ───────────────────────────────────────────────────────────────


async def create_run(conn: asyncpg.Connection, new: NewRun) -> int:
    """Make a ``running`` run for a job and return its id; ``DuplicateJob`` when that job already has one."""
    try:
        return await conn.fetchval(
            """
            INSERT INTO research_options_backtest_runs (
                created_at_ms, job_id, status, setup_name, setup_json, underlying, first_session_open_ms,
                last_session_open_ms, sessions_requested, fee_policy_text, calendar_identity, calendar_package_version
            ) VALUES ($1, $2, 'running', $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            new.created_at_ms,
            new.job_id,
            new.setup_name,
            new.setup.model_dump_json(),
            new.setup.underlying,
            new.first_session_open_ms,
            new.last_session_open_ms,
            new.sessions_requested,
            fee_policy_text(new.setup),
            CALENDAR_IDENTITY,
            CALENDAR_PACKAGE_VERSION,
        )
    except asyncpg.UniqueViolationError as exc:
        raise DuplicateJob(f"job {new.job_id} already has a run") from exc


async def complete_run(conn: asyncpg.Connection, run_id: int, result: RunResult, *, finished_at_ms: int) -> None:
    """Save a finished run's figures, sessions and leg trades in one transaction, and mark it ``complete``.

    ``RunNotRunning`` when the run is gone or is not running; ``RunDoesNotMatch`` when the result is not one of this
    run (another setup, or another number of sessions); nothing is written in either case.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT status, setup_json::text AS setup_json, sessions_requested FROM research_options_backtest_runs "
            "WHERE id = $1 FOR UPDATE",
            run_id,
        )
        if row is None or row["status"] != "running":
            raise RunNotRunning(f"run {run_id} is not running")
        if OptionsBacktestSetup.model_validate_json(row["setup_json"]) != result.setup:
            raise RunDoesNotMatch(f"the result is of another setup than run {run_id} was accepted with")
        if len(result.sessions) != row["sessions_requested"]:
            raise RunDoesNotMatch(
                f"run {run_id} was accepted for {row['sessions_requested']} sessions and the result has {len(result.sessions)}"
            )
        summary = result.summary
        await conn.execute(
            """
            UPDATE research_options_backtest_runs
               SET status = 'complete', finished_at_ms = $2, summary_json = $3::jsonb, slippage_json = $4::jsonb,
                   tolerance_json = $5::jsonb, sessions_tested = $6, sessions_traded = $7, trades = $8,
                   trades_won = $9, net_profit_usd = $10, ending_cash_usd = $11
             WHERE id = $1
            """,
            run_id,
            finished_at_ms,
            json.dumps(run_json.summary_to_json(summary)),
            json.dumps(run_json.slippage_to_json(result.slippage)),
            json.dumps(run_json.tolerance_to_json(result.slippage_tolerance)),
            summary.sessions_tested,
            summary.sessions_traded,
            summary.trades,
            summary.trades_won,
            summary.net_profit_usd,
            summary.ending_cash_usd,
        )
        await conn.executemany(
            _INSERT_SESSION, [_session_row(run_id, at, session) for at, session in enumerate(result.sessions)]
        )
        await conn.executemany(
            _INSERT_TRADE,
            [
                _trade_row(run_id, session.session_open_ms, number, trade)
                for session in result.sessions
                for number, trade in enumerate(session.outcome.trades)
            ],
        )


_INSERT_SESSION = """
    INSERT INTO research_options_backtest_run_sessions (
        run_id, session_open_ms, position, session_close_ms, status, skip_reason, cash_before_usd, cash_after_usd,
        cash_change_usd, profit_usd, underlying_sha256, listing_sha256, option_days_json, events_json
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14::jsonb)
"""

_INSERT_TRADE = """
    INSERT INTO research_options_backtest_run_trades (
        run_id, session_open_ms, trade_number, leg_id, entry_number, contract, contracts, multiplier, starting_price,
        momentum_level, stop_level, entry_decided_at_ms, entry_side, entry_bar_close_ms, entry_seen, entry_filled,
        exit_reason, exit_group, exit_decided_at_ms, exit_kind, exit_side, exit_bar_close_ms, exit_seen, exit_price,
        entry_fee_usd, exit_fee_usd, pnl_usd
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
        $25, $26, $27
    )
"""


def _session_row(run_id: int, position: int, session: SessionResult) -> tuple[Any, ...]:
    outcome = session.outcome
    return (
        run_id,
        session.session_open_ms,
        position,
        session.session_close_ms,
        outcome.status.value,
        None if outcome.skip_reason is None else outcome.skip_reason.value,
        session.cash_before_usd,
        session.cash_after_usd,
        outcome.cash_change_usd,
        outcome.pnl_usd,
        session.pins.underlying_sha256,
        session.pins.listing_sha256,
        json.dumps(run_json.option_days_to_json(session.pins)),
        json.dumps(run_json.events_to_json(outcome.events)),
    )


def _trade_row(run_id: int, session_open_ms: int, number: int, trade: LegTrade) -> tuple[Any, ...]:
    entry, leaving = trade.entry, trade.exit
    _require_fills_of_this_trade(trade)
    return (
        run_id,
        session_open_ms,
        number,
        trade.leg_id,
        trade.entry_number,
        trade.contract.occ_symbol,
        trade.contracts,
        trade.multiplier,
        trade.starting_price,
        trade.momentum_level,
        trade.stop_level,
        trade.entry_decided_at_ms,
        entry.side,
        entry.bar_close_ms,
        entry.seen,
        entry.filled,
        trade.exit_reason.value,
        trade.exit_group,
        trade.exit_decided_at_ms,
        "fill" if isinstance(leaving, Fill) else "mark",
        leaving.side if isinstance(leaving, Fill) else None,
        leaving.bar_close_ms,
        leaving.seen if isinstance(leaving, Fill) else None,
        trade.exit_price,
        trade.entry_fee_usd,
        trade.exit_fee_usd,
        trade.pnl_usd,
    )


def _require_fills_of_this_trade(trade: LegTrade) -> None:
    """A fill is stored with its trade's leg, contract, contracts and multiplier, so they must be the fill's too."""
    for fill in (trade.entry, trade.exit):
        if isinstance(fill, Fill) and (fill.leg_id, fill.contract, fill.contracts, fill.multiplier) != (
            trade.leg_id,
            trade.contract,
            trade.contracts,
            trade.multiplier,
        ):
            raise ValueError(
                f"a fill of leg {trade.leg_id} does not describe the trade it belongs to; it cannot be saved"
            )


async def fail_run(conn: asyncpg.Connection, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> bool:
    """Mark a ``running`` run failed. ``False`` when it was not running."""
    detail = [
        {"session_open_ms": item.session_open_ms, "code": item.code, "message": item.message} for item in failure.items
    ]
    status = await conn.execute(
        """
        UPDATE research_options_backtest_runs
           SET status = 'failed', finished_at_ms = $2, failure_code = $3, failure_reason = $4,
               failure_detail_json = $5::jsonb
         WHERE id = $1 AND status = 'running'
        """,
        run_id,
        finished_at_ms,
        failure.code,
        failure.reason,
        json.dumps(detail),
    )
    return status == "UPDATE 1"


async def cancel_run(conn: asyncpg.Connection, run_id: int, *, finished_at_ms: int) -> bool:
    """Mark a ``running`` run cancelled. ``False`` when it was not running."""
    status = await conn.execute(
        "UPDATE research_options_backtest_runs SET status = 'cancelled', finished_at_ms = $2 "
        "WHERE id = $1 AND status = 'running'",
        run_id,
        finished_at_ms,
    )
    return status == "UPDATE 1"


# ── Reads ────────────────────────────────────────────────────────────────


async def list_runs(
    conn: asyncpg.Connection, *, is_live: IsLive, limit: int = 200, job_id: str | None = None
) -> tuple[RunRecord, ...]:
    """Runs newest first, each with its headline figures. ``job_id`` narrows to the run of one job."""
    rows = await conn.fetch(
        f"SELECT {_RUN_COLUMNS} FROM research_options_backtest_runs WHERE ($1::text IS NULL OR job_id = $1) "
        "ORDER BY created_at_ms DESC, id DESC LIMIT $2",
        job_id,
        limit,
    )
    return tuple(_record(row, is_live) for row in rows)


async def get_run(conn: asyncpg.Connection, run_id: int, *, is_live: IsLive) -> StoredRun | None:
    """One run with its setup and, when it is complete, its summary, ladder and tolerance; ``None`` when there is none."""
    row = await conn.fetchrow(
        f"SELECT {_RUN_COLUMNS}, setup_json::text AS setup_json, summary_json::text AS summary_json, "
        "slippage_json::text AS slippage_json, tolerance_json::text AS tolerance_json "
        "FROM research_options_backtest_runs WHERE id = $1",
        run_id,
    )
    if row is None:
        return None
    figures = None
    if row["summary_json"] is not None:
        figures = RunFigures(
            summary=run_json.summary_from_json(json.loads(row["summary_json"])),
            slippage=run_json.slippage_from_json(json.loads(row["slippage_json"])),
            slippage_tolerance=run_json.tolerance_from_json(json.loads(row["tolerance_json"])),
        )
    return StoredRun(
        record=_record(row, is_live),
        setup=OptionsBacktestSetup.model_validate_json(row["setup_json"]),
        figures=figures,
    )


async def read_result(conn: asyncpg.Connection, run_id: int) -> RunResult | None:
    """The whole result of a complete run, rebuilt field for field; ``None`` for no such run or one not complete."""
    stored = await get_run(conn, run_id, is_live=lambda _job_id: False)
    if stored is None or stored.figures is None:
        return None
    session_rows = await conn.fetch(
        "SELECT session_open_ms, session_close_ms, status, skip_reason, cash_before_usd, cash_after_usd, "
        "cash_change_usd, underlying_sha256, listing_sha256, option_days_json::text AS option_days_json, "
        "events_json::text AS events_json FROM research_options_backtest_run_sessions "
        "WHERE run_id = $1 ORDER BY position",
        run_id,
    )
    trade_rows = await conn.fetch(
        "SELECT * FROM research_options_backtest_run_trades WHERE run_id = $1 ORDER BY session_open_ms, trade_number",
        run_id,
    )
    trades_of: dict[int, list[LegTrade]] = {}
    for trade_row in trade_rows:
        trades_of.setdefault(trade_row["session_open_ms"], []).append(_trade(trade_row))
    sessions = tuple(_session(row, tuple(trades_of.get(row["session_open_ms"], ()))) for row in session_rows)
    return RunResult(
        setup=stored.setup,
        sessions=sessions,
        summary=stored.figures.summary,
        slippage=stored.figures.slippage,
        slippage_tolerance=stored.figures.slippage_tolerance,
    )


def _record(row: asyncpg.Record, is_live: IsLive) -> RunRecord:
    stored: StoredStatus = row["status"]
    live = is_live(row["job_id"]) if stored == "running" else None
    failure = None
    if row["failure_code"] is not None:
        items = json.loads(row["failure_detail_json"]) if row["failure_detail_json"] else []
        failure = RunFailure(
            code=row["failure_code"],
            reason=row["failure_reason"],
            items=tuple(FailureItem(item["session_open_ms"], item["code"], item["message"]) for item in items),
        )
    headline = None
    if stored == "complete":
        headline = Headline(
            sessions_tested=row["sessions_tested"],
            sessions_traded=row["sessions_traded"],
            trades=row["trades"],
            trades_won=row["trades_won"],
            net_profit_usd=row["net_profit_usd"],
            ending_cash_usd=row["ending_cash_usd"],
        )
    return RunRecord(
        id=row["id"],
        created_at_ms=row["created_at_ms"],
        finished_at_ms=row["finished_at_ms"],
        job_id=row["job_id"],
        status=lifecycle.presented_status_for(stored, live=live),  # type: ignore[arg-type]
        setup_name=row["setup_name"],
        underlying=row["underlying"],
        first_session_open_ms=row["first_session_open_ms"],
        last_session_open_ms=row["last_session_open_ms"],
        sessions_requested=row["sessions_requested"],
        fee_policy_text=row["fee_policy_text"],
        calendar_identity=row["calendar_identity"],
        calendar_package_version=row["calendar_package_version"],
        failure=failure,
        headline=headline,
    )


def _session(row: asyncpg.Record, trades: tuple[LegTrade, ...]) -> SessionResult:
    opened = row["session_open_ms"]
    trading_date: date = et_date_at_ms(opened)
    return SessionResult(
        trading_date=trading_date,
        session_open_ms=opened,
        session_close_ms=row["session_close_ms"],
        cash_before_usd=row["cash_before_usd"],
        outcome=SessionOutcome(
            status=SessionStatus(row["status"]),
            skip_reason=None if row["skip_reason"] is None else SkipReason(row["skip_reason"]),
            trades=trades,
            events=run_json.events_from_json(json.loads(row["events_json"])),
            cash_change_usd=row["cash_change_usd"],
        ),
        cash_after_usd=row["cash_after_usd"],
        pins=run_json.pins_from_json(
            row["underlying_sha256"], row["listing_sha256"], json.loads(row["option_days_json"])
        ),
    )


def _trade(row: asyncpg.Record) -> LegTrade:
    contract = OptionContract.from_occ_symbol(row["contract"])
    entry = Fill(
        leg_id=row["leg_id"],
        contract=contract,
        side=row["entry_side"],
        contracts=row["contracts"],
        multiplier=row["multiplier"],
        bar_close_ms=row["entry_bar_close_ms"],
        seen=row["entry_seen"],
        filled=row["entry_filled"],
    )
    leaving: Fill | Mark
    if row["exit_kind"] == "fill":
        leaving = Fill(
            leg_id=row["leg_id"],
            contract=contract,
            side=row["exit_side"],
            contracts=row["contracts"],
            multiplier=row["multiplier"],
            bar_close_ms=row["exit_bar_close_ms"],
            seen=row["exit_seen"],
            filled=row["exit_price"],
        )
    else:
        leaving = Mark(bar_close_ms=row["exit_bar_close_ms"], price=row["exit_price"])
    return LegTrade(
        leg_id=row["leg_id"],
        entry_number=row["entry_number"],
        contract=contract,
        contracts=row["contracts"],
        multiplier=row["multiplier"],
        starting_price=row["starting_price"],
        momentum_level=row["momentum_level"],
        stop_level=row["stop_level"],
        entry_decided_at_ms=row["entry_decided_at_ms"],
        entry=entry,
        exit_reason=ExitReason(row["exit_reason"]),
        exit_group=row["exit_group"],
        exit_decided_at_ms=row["exit_decided_at_ms"],
        exit=leaving,
        entry_fee_usd=row["entry_fee_usd"],
        exit_fee_usd=row["exit_fee_usd"],
        pnl_usd=row["pnl_usd"],
    )


async def delete_run(conn: asyncpg.Connection, run_id: int, *, is_live: IsLive) -> DeleteOutcome:
    """Delete a run with its sessions and trades.

    A run that is running is refused, unless its job is gone (it reads ``interrupted``), which no worker can finish.
    "Cannot tell" counts as alive. The row is locked while it is judged, so a worker finishing at the same moment
    cannot be deleted from under its own save.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT status, job_id FROM research_options_backtest_runs WHERE id = $1 FOR UPDATE", run_id
        )
        if row is None:
            return DeleteOutcome.NOT_FOUND
        if row["status"] == "running" and is_live(row["job_id"]) is not False:
            return DeleteOutcome.RUNNING
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id = $1", run_id)
        return DeleteOutcome.DELETED


# ── The store a worker thread uses ───────────────────────────────────────


class DbRunStore:
    """Saves a run to the research tables. ``create`` is awaited by the route; the others block, for a worker thread."""

    async def create(self, new: NewRun) -> int:
        return await writer_loop_call(with_connection(create_run, new))

    def complete(self, run_id: int, result: RunResult, *, finished_at_ms: int) -> None:
        run_on_background_loop(
            with_connection(complete_run, run_id, result, finished_at_ms=finished_at_ms), timeout=SAVE_TIMEOUT_SECONDS
        )

    def fail(self, run_id: int, failure: RunFailure, *, finished_at_ms: int) -> None:
        run_on_background_loop(
            with_connection(fail_run, run_id, failure, finished_at_ms=finished_at_ms), timeout=SAVE_TIMEOUT_SECONDS
        )

    def cancel(self, run_id: int, *, finished_at_ms: int) -> None:
        run_on_background_loop(
            with_connection(cancel_run, run_id, finished_at_ms=finished_at_ms), timeout=SAVE_TIMEOUT_SECONDS
        )
