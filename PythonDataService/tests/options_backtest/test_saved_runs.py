"""Saved Options Backtest runs on a real database (#3030).

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one, like
the saved setups beside them. Every run a test makes has a job id ending in the test's tag, and is removed afterwards.

The result saved is a real replay (``_a_run.py``): five sessions, four legs, with stops, re-entries, a square-off,
legs marked and not sold, a skipped half day and a session the underlying did not trade in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import fields, is_dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

import asyncpg
import pytest
from pydantic import BaseModel

from app.options_backtest.results import Fill, LegTrade
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import (
    DeleteOutcome,
    DuplicateJob,
    FailureItem,
    NewRun,
    RunDoesNotMatch,
    RunFailure,
    RunNotRunning,
    cancel_run,
    complete_run,
    create_run,
    delete_run,
    fail_run,
    get_run,
    list_runs,
    read_result,
)
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal
FINISHED_AT_MS = 1_791_828_000_000
ALIVE: Callable[[str | None], bool | None] = lambda _job_id: True  # noqa: E731
GONE: Callable[[str | None], bool | None] = lambda _job_id: False  # noqa: E731
CANNOT_TELL: Callable[[str | None], bool | None] = lambda _job_id: None  # noqa: E731


@pytest.fixture
async def tag(conn: asyncpg.Connection, unique: str) -> AsyncIterator[str]:
    yield unique
    await conn.execute("DELETE FROM research_options_backtest_runs WHERE job_id LIKE $1", f"%{unique}")


@pytest.fixture(scope="module")
def result() -> RunResult:
    return a_busy_run()


def _new(result: RunResult, tag: str, name: str = "job", *, created_at_ms: int = 1_791_800_000_000) -> NewRun:
    return NewRun(
        job_id=f"{name}-{tag}",
        created_at_ms=created_at_ms,
        setup=result.setup,
        setup_name="the busy setup",
        first_session_open_ms=result.sessions[0].session_open_ms,
        last_session_open_ms=result.sessions[-1].session_open_ms,
        sessions_requested=len(result.sessions),
    )


async def _complete(conn: asyncpg.Connection, result: RunResult, tag: str, name: str = "job", **kwargs: Any) -> int:
    run_id = await create_run(conn, _new(result, tag, name, **kwargs))
    await complete_run(conn, run_id, result, finished_at_ms=FINISHED_AT_MS)
    return run_id


async def _rows_of(conn: asyncpg.Connection, table: str, run_id: int) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM {table} WHERE run_id = $1", run_id)


def _the_same_all_the_way_down(saved: Any, original: Any, path: str = "result") -> None:
    """Equal in kind and in value, a Decimal for a Decimal, and no float anywhere (a float equals a Decimal of its digits)."""
    assert type(saved) is type(original), f"{path}: {type(saved).__name__} is not {type(original).__name__}"
    assert not isinstance(saved, float), f"{path}: a float"
    if isinstance(original, Decimal):
        assert saved == original, f"{path}: {saved} is not {original}"
    elif is_dataclass(original):
        for field in fields(original):
            _the_same_all_the_way_down(
                getattr(saved, field.name), getattr(original, field.name), f"{path}.{field.name}"
            )
    elif isinstance(original, BaseModel):
        _the_same_all_the_way_down(saved.model_dump(), original.model_dump(), path)
    elif isinstance(original, tuple | list):
        assert len(saved) == len(original), f"{path}: {len(saved)} items, not {len(original)}"
        for at, (kept, was) in enumerate(zip(saved, original, strict=True)):
            _the_same_all_the_way_down(kept, was, f"{path}[{at}]")
    elif isinstance(original, dict):
        assert saved.keys() == original.keys(), path
        for key in original:
            _the_same_all_the_way_down(saved[key], original[key], f"{path}.{key}")
    else:
        assert saved == original, f"{path}: {saved!r} is not {original!r}"
        assert isinstance(original, Enum | str | int | bool | date | type(None)), (
            f"{path}: unexpected {type(original).__name__}"
        )


# ── The exact round trip ─────────────────────────────────────────────────


async def test_a_run_saved_and_read_back_is_the_run_field_for_field_and_decimal_for_decimal(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)

    read = await read_result(conn, run_id)

    assert read == result
    assert read is not None
    _the_same_all_the_way_down(read, result)
    # The busy run has trades with fills and with marks, or this proves nothing about either.
    trades = [trade for session in read.sessions for trade in session.outcome.trades]
    assert any(isinstance(trade.exit, Fill) for trade in trades) and any(
        not isinstance(trade.exit, Fill) for trade in trades
    )


async def test_money_is_kept_to_the_last_digit_where_a_float_would_lose_it(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # 12345678901234567890.123456789 has 29 significant digits; a float holds about 16.
    long_cash = D("12345678901234567890.123456789")
    first, *rest = result.sessions
    long_run = replace(result, sessions=(replace(first, cash_before_usd=long_cash), *rest))
    run_id = await _complete(conn, long_run, tag)

    read = await read_result(conn, run_id)

    assert read is not None
    assert read.sessions[0].cash_before_usd == long_cash
    assert str(read.sessions[0].cash_before_usd) == "12345678901234567890.123456789"


async def test_the_run_opens_with_its_setup_headline_and_summary(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)

    stored = await get_run(conn, run_id, is_live=GONE)

    assert stored is not None
    assert stored.setup == result.setup
    assert stored.figures is not None and stored.figures.summary == result.summary
    assert stored.figures.slippage == result.slippage and stored.figures.slippage_tolerance == result.slippage_tolerance
    record = stored.record
    assert (record.status, record.setup_name, record.underlying, record.finished_at_ms) == (
        "complete",
        "the busy setup",
        "SPY",
        FINISHED_AT_MS,
    )
    assert record.sessions_requested == 5
    assert record.fee_policy_text == "Fees are $0.65 per contract traded."
    assert record.calendar_identity == "NYSE" and record.calendar_package_version
    assert record.headline is not None
    assert (
        record.headline.sessions_tested,
        record.headline.sessions_traded,
        record.headline.trades,
        record.headline.trades_won,
        record.headline.net_profit_usd,
        record.headline.ending_cash_usd,
    ) == (
        result.summary.sessions_tested,
        result.summary.sessions_traded,
        result.summary.trades,
        result.summary.trades_won,
        result.summary.net_profit_usd,
        result.summary.ending_cash_usd,
    )


async def test_no_such_run_is_none(conn: asyncpg.Connection) -> None:
    assert await get_run(conn, 2**62, is_live=GONE) is None
    assert await read_result(conn, 2**62) is None


# ── The history ──────────────────────────────────────────────────────────


async def test_the_history_is_newest_first_and_each_run_carries_what_became_of_it(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    complete = await _complete(conn, result, tag, "complete", created_at_ms=1_791_800_000_001)
    failed = await create_run(conn, _new(result, tag, "failed", created_at_ms=1_791_800_000_002))
    await fail_run(
        conn,
        failed,
        RunFailure(
            "DOWNLOAD_FAILED",
            "the data could not be fetched",
            (FailureItem(result.sessions[1].session_open_ms, "provider_rate_limited", "slow down"),),
        ),
        finished_at_ms=FINISHED_AT_MS,
    )
    cancelled = await create_run(conn, _new(result, tag, "cancelled", created_at_ms=1_791_800_000_003))
    await cancel_run(conn, cancelled, finished_at_ms=FINISHED_AT_MS)
    running = await create_run(conn, _new(result, tag, "running", created_at_ms=1_791_800_000_004))

    mine = [record for record in await list_runs(conn, is_live=ALIVE, limit=500) if record.job_id.endswith(tag)]

    assert [(record.id, record.status) for record in mine] == [
        (running, "running"),
        (cancelled, "cancelled"),
        (failed, "failed"),
        (complete, "complete"),
    ]
    by_id = {record.id: record for record in mine}
    assert by_id[complete].headline is not None and by_id[complete].failure is None
    for not_a_result in (running, cancelled, failed):
        assert by_id[not_a_result].headline is None
    assert by_id[failed].failure == RunFailure(
        "DOWNLOAD_FAILED",
        "the data could not be fetched",
        (FailureItem(result.sessions[1].session_open_ms, "provider_rate_limited", "slow down"),),
    )


async def test_the_history_can_be_narrowed_to_the_run_of_one_job_and_cut_to_a_limit(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    first = await create_run(conn, _new(result, tag, "a", created_at_ms=1_791_800_000_001))
    await create_run(conn, _new(result, tag, "b", created_at_ms=1_791_800_000_002))

    only_a = await list_runs(conn, is_live=ALIVE, job_id=f"a-{tag}")
    newest = await list_runs(conn, is_live=ALIVE, limit=1)

    assert [record.id for record in only_a] == [first]
    assert len(newest) == 1


async def test_a_running_row_whose_job_is_gone_reads_interrupted_and_one_that_cannot_be_told_stays_running(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))

    statuses = {}
    for name, is_live in (("alive", ALIVE), ("gone", GONE), ("cannot tell", CANNOT_TELL)):
        listed = [record for record in await list_runs(conn, is_live=is_live, limit=500) if record.id == run_id]
        opened = await get_run(conn, run_id, is_live=is_live)
        assert opened is not None
        statuses[name] = (listed[0].status, opened.record.status)

    assert statuses == {
        "alive": ("running", "running"),
        "gone": ("interrupted", "interrupted"),
        "cannot tell": ("running", "running"),
    }


async def test_a_run_that_is_not_running_is_never_asked_whether_its_job_is_alive(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)
    asked: list[str | None] = []

    def is_live(job_id: str | None) -> bool | None:
        asked.append(job_id)
        return False

    opened = await get_run(conn, run_id, is_live=is_live)

    assert opened is not None and opened.record.status == "complete"
    assert asked == []


# ── A part of a run is never a result ────────────────────────────────────


async def test_a_failed_or_cancelled_run_has_no_figures_no_sessions_and_no_trades(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    failed = await create_run(conn, _new(result, tag, "failed"))
    assert await fail_run(conn, failed, RunFailure("DATA_MISSING", "not captured"), finished_at_ms=FINISHED_AT_MS)
    cancelled = await create_run(conn, _new(result, tag, "cancelled"))
    assert await cancel_run(conn, cancelled, finished_at_ms=FINISHED_AT_MS)

    for run_id in (failed, cancelled):
        opened = await get_run(conn, run_id, is_live=GONE)
        assert opened is not None and opened.figures is None
        assert await read_result(conn, run_id) is None
        assert await _rows_of(conn, "research_options_backtest_run_sessions", run_id) == 0
        assert await _rows_of(conn, "research_options_backtest_run_trades", run_id) == 0


async def test_a_run_ends_only_once_and_only_from_running(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)

    assert not await fail_run(conn, run_id, RunFailure("X", "late"), finished_at_ms=FINISHED_AT_MS)
    assert not await cancel_run(conn, run_id, finished_at_ms=FINISHED_AT_MS)
    with pytest.raises(RunNotRunning):
        await complete_run(conn, run_id, result, finished_at_ms=FINISHED_AT_MS)
    cancelled = await create_run(conn, _new(result, tag, "cancelled"))
    await cancel_run(conn, cancelled, finished_at_ms=FINISHED_AT_MS)
    with pytest.raises(RunNotRunning):
        await complete_run(conn, cancelled, result, finished_at_ms=FINISHED_AT_MS)
    assert await _rows_of(conn, "research_options_backtest_run_sessions", cancelled) == 0
    assert (await get_run(conn, run_id, is_live=GONE)).record.status == "complete"  # type: ignore[union-attr]


async def test_a_save_that_fails_part_way_leaves_the_run_running_with_nothing_written(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # A fill that does not describe its own trade cannot be saved. The sessions are inserted before the trades, so
    # the refusal comes after the figures and the sessions are in the transaction: all of it must be undone.
    first_with_trades = next(session for session in result.sessions if session.outcome.trades)
    trade: LegTrade = first_with_trades.outcome.trades[0]
    wrong_leg = replace(trade, entry=replace(trade.entry, leg_id="not-this-leg"))
    broken_session = replace(
        first_with_trades,
        outcome=replace(first_with_trades.outcome, trades=(wrong_leg, *first_with_trades.outcome.trades[1:])),
    )
    broken = replace(result, sessions=tuple(broken_session if s is first_with_trades else s for s in result.sessions))
    run_id = await create_run(conn, _new(result, tag))

    with pytest.raises(ValueError, match="does not describe the trade"):
        await complete_run(conn, run_id, broken, finished_at_ms=FINISHED_AT_MS)

    opened = await get_run(conn, run_id, is_live=ALIVE)
    assert opened is not None and opened.record.status == "running" and opened.figures is None
    assert await _rows_of(conn, "research_options_backtest_run_sessions", run_id) == 0
    assert await _rows_of(conn, "research_options_backtest_run_trades", run_id) == 0


async def test_a_result_that_is_not_the_runs_own_is_refused(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    other_setup = result.setup.model_copy(update={"slippage_percent": D("1.5")})
    one_session_short = replace(result, sessions=result.sessions[:-1])

    with pytest.raises(RunDoesNotMatch, match="another setup"):
        await complete_run(conn, run_id, replace(result, setup=other_setup), finished_at_ms=FINISHED_AT_MS)
    with pytest.raises(RunDoesNotMatch, match="sessions"):
        await complete_run(conn, run_id, one_session_short, finished_at_ms=FINISHED_AT_MS)

    assert (await get_run(conn, run_id, is_live=ALIVE)).record.status == "running"  # type: ignore[union-attr]


async def test_the_database_itself_refuses_figures_on_a_run_that_is_not_complete(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE research_options_backtest_runs SET trades = 3 WHERE id = $1", run_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE research_options_backtest_runs SET status = 'complete', finished_at_ms = 1 WHERE id = $1", run_id
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE research_options_backtest_runs SET status = 'failed', finished_at_ms = 1 WHERE id = $1", run_id
        )


async def test_the_database_itself_refuses_a_running_run_with_an_end_time_and_an_ended_run_without_one(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE research_options_backtest_runs SET finished_at_ms = 5 WHERE id = $1", run_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE research_options_backtest_runs SET status = 'cancelled' WHERE id = $1", run_id)


async def test_one_job_has_one_run(conn: asyncpg.Connection, tag: str, result: RunResult) -> None:
    await create_run(conn, _new(result, tag))

    with pytest.raises(DuplicateJob):
        await create_run(conn, _new(result, tag))


# ── Delete ───────────────────────────────────────────────────────────────


async def test_deleting_a_run_takes_its_sessions_and_trades_with_it(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)
    assert await _rows_of(conn, "research_options_backtest_run_trades", run_id) > 0

    outcome = await delete_run(conn, run_id, is_live=GONE)

    assert outcome is DeleteOutcome.DELETED
    assert await get_run(conn, run_id, is_live=GONE) is None
    assert await _rows_of(conn, "research_options_backtest_run_sessions", run_id) == 0
    assert await _rows_of(conn, "research_options_backtest_run_trades", run_id) == 0


async def test_deleting_what_is_not_there_says_so(conn: asyncpg.Connection) -> None:
    assert await delete_run(conn, 2**62, is_live=GONE) is DeleteOutcome.NOT_FOUND


async def test_a_run_still_running_is_not_deleted_unless_its_job_is_gone(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))

    assert await delete_run(conn, run_id, is_live=ALIVE) is DeleteOutcome.RUNNING
    assert await delete_run(conn, run_id, is_live=CANNOT_TELL) is DeleteOutcome.RUNNING
    assert await get_run(conn, run_id, is_live=ALIVE) is not None
    assert await delete_run(conn, run_id, is_live=GONE) is DeleteOutcome.DELETED
    assert await get_run(conn, run_id, is_live=ALIVE) is None
