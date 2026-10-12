"""Saved Options Backtest runs on a real database (#3030).

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one, like
the saved setups beside them. Every run a test makes has a job id ending in the test's tag, and is removed afterwards.

The result saved is a real replay (``_a_run.py``): five sessions, four legs, with stops, re-entries, a square-off,
legs marked and not sold, a skipped half day and a session the underlying did not trade in.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import fields, is_dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

import asyncpg
import pytest
from pydantic import BaseModel

from app.jobs.progress import JOB_LEASE_TTL_SECONDS
from app.options_backtest import saved_runs
from app.options_backtest.results import Fill, LegTrade, SessionStatus
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import (
    DeleteOutcome,
    DuplicateJob,
    FailureItem,
    NewRun,
    RunDoesNotMatch,
    RunFailure,
    RunNotRunning,
    RunSetupCannotBeRead,
    cancel_run,
    complete_run,
    create_run,
    delete_run,
    fail_run,
    get_run,
    list_runs,
    read_result,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique
from tests.research.conftest import second_conn as second_conn

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
        # Equal in value is not enough: a later piece that prints one must print the digits it was given.
        assert str(saved) == str(original), f"{path}: reads {saved!s}, written {original!s}"
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


_DATE_IN_WORDS = re.compile(r"\d{4}-\d{2}-\d{2}")
_ONLY_PLACE_FOR_A_DATE_IN_WORDS = "session.events_json[].detail"


def _walk_json(value: Any, path: str = "") -> list[tuple[str, str, Any]]:
    """Every (path, key, value) of a parsed JSON value, a list index written ``[]`` in the path."""
    found: list[tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for key, each in value.items():
            found.append((path, key, each))
            found += _walk_json(each, f"{path}.{key}")
    elif isinstance(value, list):
        for each in value:
            found += _walk_json(each, f"{path}[]")
    return found


async def test_every_time_a_run_stores_is_an_integer_of_ms_and_a_date_in_words_is_only_an_events_detail(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # The decision (#3030): an event's ``detail`` is prose the backend wrote for the owner, and the picker's sentence for
    # a refused pick names the day a contract is listed for. It is stored and served as written. Every other place
    # that holds a time holds an integer of milliseconds, and no other string anywhere names a date.
    run_id = await _complete(conn, result, tag)
    run_row = await conn.fetchrow(
        "SELECT setup_json::text AS setup_json, summary_json::text AS summary_json, slippage_json::text AS slippage_json, "
        "tolerance_json::text AS tolerance_json, setup_name, underlying, job_id, fee_policy_text, calendar_identity, "
        "calendar_package_version FROM research_options_backtest_runs WHERE id = $1",
        run_id,
    )
    session_rows = await conn.fetch(
        "SELECT status, skip_reason, underlying_sha256, listing_sha256, option_days_json::text AS option_days_json, "
        "events_json::text AS events_json FROM research_options_backtest_run_sessions WHERE run_id = $1",
        run_id,
    )
    trade_rows = await conn.fetch(
        "SELECT leg_id, contract, entry_side, exit_reason, exit_kind, exit_side FROM research_options_backtest_run_trades "
        "WHERE run_id = $1",
        run_id,
    )
    columns = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name IN ('research_options_backtest_runs', 'research_options_backtest_run_sessions', "
        "'research_options_backtest_run_trades')"
    )

    # No column is a date or a clock time, and every column that holds an instant is a 64-bit integer.
    assert {column["data_type"] for column in columns}.isdisjoint(
        {
            "date",
            "time without time zone",
            "time with time zone",
            "timestamp without time zone",
            "timestamp with time zone",
        }
    )
    assert {column["data_type"] for column in columns if column["column_name"].endswith("_ms")} == {"bigint"}

    json_places = {
        f"run.{name}": json.loads(run_row[name])
        for name in ("setup_json", "summary_json", "slippage_json", "tolerance_json")
    }
    for at, row in enumerate(session_rows):
        json_places[f"session.option_days_json#{at}"] = json.loads(row["option_days_json"])
        json_places[f"session.events_json#{at}"] = json.loads(row["events_json"])
    names_a_date: set[str] = set()
    time_keys_seen = 0
    for place, parsed in json_places.items():
        label = place.split("#")[0]
        for path, key, value in _walk_json(parsed):
            where = f"{label}{path}.{key}"
            if key.endswith("_ms"):
                time_keys_seen += 1
                assert value is None or (isinstance(value, int) and not isinstance(value, bool)), (
                    f"{where} is {value!r}"
                )
            if isinstance(value, str) and _DATE_IN_WORDS.search(value):
                names_a_date.add(where)
    for name in (
        "setup_name",
        "underlying",
        "job_id",
        "fee_policy_text",
        "calendar_identity",
        "calendar_package_version",
    ):
        assert not _DATE_IN_WORDS.search(run_row[name]), f"run.{name} names a date"
    for row in (*session_rows, *trade_rows):
        for name, value in row.items():
            if name not in ("option_days_json", "events_json") and isinstance(value, str):
                assert not _DATE_IN_WORDS.search(value), f"{name} names a date"

    assert time_keys_seen > 50, "the stored JSON of the busy run has times in it, or this proves nothing"
    assert _ONLY_PLACE_FOR_A_DATE_IN_WORDS in names_a_date, "the busy run has a refused pick whose sentence names a day"
    assert names_a_date <= {_ONLY_PLACE_FOR_A_DATE_IN_WORDS}, sorted(names_a_date)
    assert "trading_date" not in {column["column_name"] for column in columns}


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


async def test_a_session_stores_its_own_profit_and_it_differs_from_its_cash_change_where_a_leg_was_marked_not_sold(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # The view of one session and the comparison of two runs read a session's profit from its row. The profit counts a
    # leg marked, not sold, at its mark; the cash change counts only the cash that moved. Each is worked out by hand
    # from the busy run's fills and marks, a trade being (exit - entry) x 100 x contracts - its two fees.
    #
    # Session 1, every leg sold, so the two are one number: far 1 x (1.76 - 1.72) x 100 - 0.65 - 0.65 = 2.70; put
    # 2 x (3.80 - 4.30) x 100 - 1.30 - 1.30 = -102.60; the put again 2 x (3.32 - 3.90) x 100 - 1.30 - 1.30 = -118.60.
    # Together -218.50, a loss.
    #
    # Session 2, two legs marked and not sold, so the two differ. Profit: far, marked at 3.23 after 3.25, is
    # (3.23 - 3.25) x 100 - 0.65 = -2.65; the put, sold at 0.27 after 0.09, is (0.27 - 0.09) x 200 - 1.30 - 1.30 =
    # 33.40; the call, marked at 3.94 after 4.23, is (3.94 - 4.23) x 100 - 0.65 = -29.65; together 1.10. Cash change:
    # far paid out 3.25 x 100 + 0.65 = 325.65, the call 4.23 x 100 + 0.65 = 423.65, and the put netted +33.40, so
    # -325.65 - 423.65 + 33.40 = -715.90. The two marks are worth 3.23 x 100 + 3.94 x 100 = 717.00, and
    # -715.90 + 717.00 = 1.10.
    #
    # Session 3, every leg sold: 9.40 - 29.30 + 71.40 - 21.30 - 86.60 = -56.40, a loss, in both.
    # Sessions 4 and 5 are skipped and moved nothing: 0 and 0.
    assert result.sessions[1].status is SessionStatus.INCOMPLETE, "the busy run has no leg marked, or this proves less"
    run_id = await _complete(conn, result, tag)

    rows = await conn.fetch(
        "SELECT session_open_ms, profit_usd, cash_change_usd FROM research_options_backtest_run_sessions "
        "WHERE run_id = $1 ORDER BY position",
        run_id,
    )

    opens = [session.session_open_ms for session in result.sessions]
    assert [
        (row["session_open_ms"], str(saved_runs._money(row["profit_usd"])), str(saved_runs._money(row["cash_change_usd"])))
        for row in rows
    ] == [
        (opens[0], "-218.50", "-218.50"),
        (opens[1], "1.10", "-715.90"),
        (opens[2], "-56.40", "-56.40"),
        (opens[3], "0", "0"),
        (opens[4], "0", "0"),
    ]


# ── Money reads back as it was written ───────────────────────────────────


def _stored_money(result: RunResult) -> list[tuple[str, Decimal]]:
    """Every value of a result that is kept in a NUMERIC column, with its place."""
    found: list[tuple[str, Decimal]] = []
    for at, session in enumerate(result.sessions):
        found += [
            (f"sessions[{at}].cash_before_usd", session.cash_before_usd),
            (f"sessions[{at}].cash_after_usd", session.cash_after_usd),
            (f"sessions[{at}].outcome.cash_change_usd", session.outcome.cash_change_usd),
        ]
        for number, trade in enumerate(session.outcome.trades):
            where = f"sessions[{at}].trades[{number}]"
            optional = {
                "starting_price": trade.starting_price,
                "momentum_level": trade.momentum_level,
                "stop_level": trade.stop_level,
            }
            found += [(f"{where}.{name}", value) for name, value in optional.items() if value is not None]
            found += [(f"{where}.entry.seen", trade.entry.seen), (f"{where}.entry.filled", trade.entry.filled)]
            if isinstance(trade.exit, Fill):
                found += [(f"{where}.exit.seen", trade.exit.seen), (f"{where}.exit.filled", trade.exit.filled)]
            else:
                found.append((f"{where}.exit.price", trade.exit.price))
            found += [
                (f"{where}.entry_fee_usd", trade.entry_fee_usd),
                (f"{where}.exit_fee_usd", trade.exit_fee_usd),
                (f"{where}.pnl_usd", trade.pnl_usd),
            ]
    return found


def _with_every_stored_money_set_to(result: RunResult, value: Decimal) -> RunResult:
    def trade(each: LegTrade) -> LegTrade:
        leaving = (
            replace(each.exit, seen=value, filled=value)
            if isinstance(each.exit, Fill)
            else replace(each.exit, price=value)
        )
        return replace(
            each,
            starting_price=None if each.starting_price is None else value,
            momentum_level=None if each.momentum_level is None else value,
            stop_level=None if each.stop_level is None else value,
            entry=replace(each.entry, seen=value, filled=value),
            exit=leaving,
            entry_fee_usd=value,
            exit_fee_usd=value,
            pnl_usd=value,
        )

    def session(each: Any) -> Any:
        outcome = replace(each.outcome, trades=tuple(trade(t) for t in each.outcome.trades), cash_change_usd=value)
        return replace(each, cash_before_usd=value, cash_after_usd=value, outcome=outcome)

    summary = replace(result.summary, net_profit_usd=value, ending_cash_usd=value)
    return replace(result, sessions=tuple(session(each) for each in result.sessions), summary=summary)


# What each value is written as, and the text it must read back as. PostgreSQL keeps every digit as written (0.10 is
# 0.10, 1234.5600 is 1234.5600); asyncpg alone returns a whole number with zeros at its end in exponent form (10000 as
# 1E+4, 100000 as 1.0E+5, 1000000 as 1.00E+6, 99990000 as 9.999E+7), which the store puts back in plain digits.
# A number written with a positive exponent has no exponent in PostgreSQL: 1E+4 is the number 10000 and reads as that.
# The sign of a zero is not kept, so -0.00 reads 0.00. A small number's text is the same however it is held (1E-7).
@pytest.mark.parametrize(
    ("written", "reads"),
    [
        ("1000", "1000"),
        ("10000", "10000"),
        ("20000", "20000"),
        ("100000", "100000"),
        ("1200000", "1200000"),
        ("99990000", "99990000"),
        ("100000000", "100000000"),
        ("5000.50", "5000.50"),
        ("0.10", "0.10"),
        ("1234.5600", "1234.5600"),
        ("-10000", "-10000"),
        ("0", "0"),
        ("0.00", "0.00"),
        ("0.0000001", "1E-7"),
        ("12345678901234567890.123456789000", "12345678901234567890.123456789000"),
        ("123456789012345678901234567890", "123456789012345678901234567890"),
        ("1E+4", "10000"),
        ("1E+40", "10000000000000000000000000000000000000000"),
        ("-0.00", "0.00"),
    ],
)
async def test_every_money_column_reads_back_as_the_digits_it_was_written_never_in_exponent_form(
    conn: asyncpg.Connection, tag: str, result: RunResult, written: str, reads: str
) -> None:
    run_id = await _complete(conn, _with_every_stored_money_set_to(result, Decimal(written)), tag)

    read = await read_result(conn, run_id)
    opened = await get_run(conn, run_id, is_live=GONE)

    assert read is not None and opened is not None and opened.record.headline is not None
    fields_read = _stored_money(read)
    assert len(fields_read) > 100, "the busy run stores a hundred money values, or this proves too little"
    assert [(where, str(value)) for where, value in fields_read] == [(where, reads) for where, _ in fields_read]
    assert (str(opened.record.headline.net_profit_usd), str(opened.record.headline.ending_cash_usd)) == (reads, reads)
    assert all(value == Decimal(written) for _, value in fields_read)


def _as_asyncpg_returns_it(value: Any) -> Any:
    """A whole number with zeros at its end in the exponent form asyncpg gives a NUMERIC: 10000 as 1E+4."""
    if isinstance(value, Decimal) and value.is_finite() and value != 0 and value.as_tuple().exponent == 0:
        return value.normalize()
    return value


def _columns_of(statement: str) -> list[str]:
    columns = re.search(r"\((.*?)\)\s*VALUES", statement, re.S)
    assert columns is not None
    return [name.strip() for name in columns.group(1).split(",")]


@pytest.mark.parametrize(
    ("value", "reads"),
    [(Decimal(10000), "10000"), (Decimal("1" + "0" * 40), "1" + "0" * 40)],
)
def test_a_row_as_asyncpg_returns_it_is_read_with_every_number_in_plain_digits_and_needs_no_database(
    value: Decimal, reads: str
) -> None:
    # This one gates the reading of a row on every pull request, where the tests that need a database do not run. The
    # rows are what the store writes for the busy run with every money set to ``value``, each number turned into the
    # form asyncpg returns it in (1E+4, and 1E+40), which the probe in the store's docstring found.
    busy = _with_every_stored_money_set_to(a_busy_run(), value)
    sessions = []
    for session in busy.sessions:
        session_row = dict(
            zip(
                _columns_of(saved_runs._INSERT_SESSION),
                (_as_asyncpg_returns_it(each) for each in saved_runs._session_row(1, 0, session)),
                strict=True,
            )
        )
        trade_rows = [
            dict(
                zip(
                    _columns_of(saved_runs._INSERT_TRADE),
                    (
                        _as_asyncpg_returns_it(each)
                        for each in saved_runs._trade_row(1, session.session_open_ms, n, trade)
                    ),
                    strict=True,
                )
            )
            for n, trade in enumerate(session.outcome.trades)
        ]
        assert str(session_row["cash_before_usd"]) == str(_as_asyncpg_returns_it(value)) != reads
        sessions.append(saved_runs._session(session_row, tuple(saved_runs._trade(row) for row in trade_rows)))
    record_row = {
        "id": 1, "created_at_ms": 1, "finished_at_ms": 2, "job_id": "j", "status": "complete", "setup_name": None,
        "underlying": "SPY", "first_session_open_ms": 1, "last_session_open_ms": 2, "sessions_requested": 5,
        "fee_policy_text": "x", "calendar_identity": "NYSE", "calendar_package_version": "1", "failure_code": None,
        "failure_reason": None, "failure_detail_json": None, "sessions_tested": 5, "sessions_traded": 3,
        "trades": 3, "trades_won": 1, "net_profit_usd": _as_asyncpg_returns_it(value),
        "ending_cash_usd": _as_asyncpg_returns_it(value),
    }  # fmt: skip
    headline = saved_runs._record(record_row, lambda _job_id: None).headline

    texts = [(where, str(each)) for where, each in _stored_money(replace(busy, sessions=tuple(sessions)))]
    assert len(texts) > 100
    assert texts == [(where, reads) for where, _ in texts]
    assert headline is not None and (str(headline.net_profit_usd), str(headline.ending_cash_usd)) == (reads, reads)


async def test_a_run_saved_with_the_awkward_figures_of_a_real_one_reads_back_with_the_same_text_in_every_money_column(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # Whole thousands and tens of thousands, trailing zeros after the point, and a very small sum, each in a column of
    # its own: the first session's cash before and after, and the cash change.
    first, *rest = result.sessions
    awkward = replace(
        first,
        cash_before_usd=Decimal("20000"),
        cash_after_usd=Decimal("12000.50"),
        outcome=replace(first.outcome, cash_change_usd=Decimal("-7999.5000")),
    )
    run_id = await _complete(conn, replace(result, sessions=(awkward, *rest)), tag)

    read = await read_result(conn, run_id)

    assert read is not None
    assert (
        str(read.sessions[0].cash_before_usd),
        str(read.sessions[0].cash_after_usd),
        str(read.sessions[0].outcome.cash_change_usd),
    ) == ("20000", "12000.50", "-7999.5000")


def _with_a_distinct_many_place_decimal_everywhere(value: Any, counter: list[int]) -> Any:
    """Every ``Decimal`` below ``value`` replaced by one of 26 or 27 significant digits, each different from the others."""
    if isinstance(value, Decimal):
        counter[0] += 1
        return Decimal(f"{counter[0]}.1234567890123456789012{counter[0]:03d}")
    if isinstance(value, tuple):
        return tuple(_with_a_distinct_many_place_decimal_everywhere(each, counter) for each in value)
    if is_dataclass(value) and not isinstance(value, type):
        return replace(
            value,
            **{
                field.name: _with_a_distinct_many_place_decimal_everywhere(getattr(value, field.name), counter)
                for field in fields(value)
            },
        )
    return value


async def test_a_decimal_of_many_places_in_the_stored_json_reads_back_to_the_last_digit_and_the_same_text(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # Hand-worked: the n-th decimal found is "n.1234567890123456789012" followed by n in three digits, so 1 + 22 + 3 = 26
    # significant digits for n under 10 and 27 from 10 to 99. A float holds about 16, so a path through float() cannot
    # give one of them back.
    counter = [0]
    summary = _with_a_distinct_many_place_decimal_everywhere(result.summary, counter)
    slippage = _with_a_distinct_many_place_decimal_everywhere(result.slippage, counter)
    tolerance = _with_a_distinct_many_place_decimal_everywhere(result.slippage_tolerance, counter)
    assert counter[0] > 50, "the summary, the ladder and the tolerance hold fifty decimals, or this proves too little"
    awkward = replace(result, summary=summary, slippage=slippage, slippage_tolerance=tolerance)
    run_id = await _complete(conn, awkward, tag)

    opened = await get_run(conn, run_id, is_live=GONE)

    assert opened is not None and opened.figures is not None
    _the_same_all_the_way_down(opened.figures.summary, summary)
    _the_same_all_the_way_down(opened.figures.slippage, slippage)
    _the_same_all_the_way_down(opened.figures.slippage_tolerance, tolerance)
    assert str(opened.figures.summary.net_profit_usd) == str(summary.net_profit_usd)
    assert len(str(opened.figures.summary.net_profit_usd).replace(".", "")) >= 26


async def test_a_summary_figure_that_is_zero_in_the_busy_run_is_saved_and_read_back_as_a_number_that_is_not_zero(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # The busy run leaves these seven counts at zero, so a store that wrote 0 for one of them would read back as the
    # busy run does. Each is given a number of its own here (7, 11, 13, 17, 19, 23, 29: no two alike).
    zero_in_the_busy_run = {
        "sessions_no_trade": 7,
        "sessions_break_even": 11,
        "trades_break_even": 13,
        "entries_refused_for_cash": 17,
        "entries_dropped": 19,
        "legs_without_a_starting_price": 23,
        "re_entries_lost": 29,
    }
    assert all(getattr(result.summary, name) == 0 for name in zero_in_the_busy_run), "the busy run changed"
    run_id = await _complete(conn, replace(result, summary=replace(result.summary, **zero_in_the_busy_run)), tag)

    opened = await get_run(conn, run_id, is_live=GONE)

    assert opened is not None and opened.figures is not None
    assert {name: getattr(opened.figures.summary, name) for name in zero_in_the_busy_run} == zero_in_the_busy_run


async def test_every_count_of_a_summary_comes_back_as_itself_when_no_two_counts_are_alike(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # Every whole-number field of the summary, and of its day and month rows, gets its own prime, so a field written
    # as another's, or as zero, shows. The counts that name an enum member get theirs by position.
    primes = iter([p for p in range(2, 1000) if all(p % d for d in range(2, int(p**0.5) + 1))])

    def distinct(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return next(primes)
        if isinstance(value, tuple):
            return tuple(distinct(each) for each in value)
        if is_dataclass(value) and not isinstance(value, type):
            return replace(value, **{field.name: distinct(getattr(value, field.name)) for field in fields(value)})
        return value

    # An enum member (a string) stays and its count (an int) is the one that changes.
    summary = distinct(result.summary)
    run_id = await _complete(conn, replace(result, summary=summary), tag)

    opened = await get_run(conn, run_id, is_live=GONE)

    assert opened is not None and opened.figures is not None
    _the_same_all_the_way_down(opened.figures.summary, summary)
    assert opened.record.headline is not None
    assert (opened.record.headline.sessions_tested, opened.record.headline.trades_won) == (
        summary.sessions_tested,
        summary.trades_won,
    )


# ── The history's order ──────────────────────────────────────────────────


async def test_runs_made_in_the_same_millisecond_are_listed_the_one_made_last_first(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # Six runs with one created_at_ms: a tie. Ids are handed out in the order the runs are made, so the newest has the
    # highest id and the list must run from it down. The first, third and fifth are ended, which rewrites their rows and
    # moves them on disk, so the order cannot be the order the rows were stored in by luck.
    same_ms = 1_791_800_000_000
    made = [await create_run(conn, _new(result, tag, f"tie{n}", created_at_ms=same_ms)) for n in range(6)]
    for run_id in made[::2]:
        assert await cancel_run(conn, run_id, finished_at_ms=FINISHED_AT_MS)

    listed = [record.id for record in await list_runs(conn, is_live=ALIVE, limit=500) if record.job_id.endswith(tag)]

    assert listed == sorted(made, reverse=True)


# ── A result is the run's own ────────────────────────────────────────────


async def test_a_result_for_other_sessions_than_the_run_was_accepted_for_is_refused_whatever_its_count(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    # The run was accepted for the busy run's five sessions: it holds their first and last opens and the number five.
    one_day = 86_400_000

    def moved(session: Any, by: int) -> Any:
        return replace(
            session, session_open_ms=session.session_open_ms + by, session_close_ms=session.session_close_ms + by
        )

    first, second, third, fourth, last = result.sessions
    five_a_day_later = tuple(moved(each, one_day) for each in result.sessions)
    # A day earlier than the first and a day later than the last stay in order, so only the endpoints can refuse them.
    other_first = (moved(first, -one_day), second, third, fourth, last)
    other_last = (first, second, third, fourth, moved(last, one_day))
    reversed_five = tuple(reversed(result.sessions))
    the_middle_out_of_order = (first, third, second, fourth, last)
    the_middle_twice = (first, second, second, fourth, last)
    # The same first and last and in order, with one session fewer and one more: only the count can refuse them.
    one_fewer = (first, second, fourth, last)
    one_more = (first, second, third, fourth, moved(fourth, 3_600_000), last)
    run_id = await create_run(conn, _new(result, tag))

    for label, sessions in {
        "all five a day later": five_a_day_later,
        "another first": other_first,
        "another last": other_last,
        "in reverse": reversed_five,
        "the middle out of order": the_middle_out_of_order,
        "one session twice": the_middle_twice,
        "one session fewer": one_fewer,
        "one session more": one_more,
    }.items():
        with pytest.raises(RunDoesNotMatch):
            await complete_run(conn, run_id, replace(result, sessions=sessions), finished_at_ms=FINISHED_AT_MS)
        opened = await get_run(conn, run_id, is_live=ALIVE)
        assert opened is not None and opened.record.status == "running", label
        assert await _rows_of(conn, "research_options_backtest_run_sessions", run_id) == 0, label

    await complete_run(conn, run_id, result, finished_at_ms=FINISHED_AT_MS)
    assert await read_result(conn, run_id) == result


# ── A run whose stored setup no longer validates ─────────────────────────


async def test_a_run_whose_stored_setup_no_longer_validates_still_opens_with_its_figures_and_says_why(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)
    healthy = await _complete(conn, result, tag, "healthy")
    # Edited by hand, as a later rule would leave it: a field the setup no longer has.
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        run_id,
    )

    opened = await get_run(conn, run_id, is_live=GONE)
    listed = [record for record in await list_runs(conn, is_live=GONE, limit=500) if record.job_id.endswith(tag)]

    assert opened is not None
    assert opened.setup is None and opened.setup_cannot_open_reason is not None
    assert "lots" in opened.setup_cannot_open_reason
    # The figures are stored apart from the setup, and are not touched by it.
    assert opened.figures is not None and opened.figures.summary == result.summary
    assert opened.record.status == "complete" and opened.record.headline is not None
    assert {record.id for record in listed} == {run_id, healthy}
    healthy_opened = await get_run(conn, healthy, is_live=GONE)
    assert healthy_opened is not None and healthy_opened.setup == result.setup
    assert healthy_opened.setup_cannot_open_reason is None
    with pytest.raises(RunSetupCannotBeRead, match="lots"):
        await read_result(conn, run_id)
    assert await delete_run(conn, run_id, is_live=GONE) is DeleteOutcome.DELETED


# ── What the database itself refuses ─────────────────────────────────────

_CEILING = MAX_TIMESTAMP_MS
_TRADE_INSTANTS = {
    "entry_decided_at_ms": lambda trade, ms: replace(trade, entry_decided_at_ms=ms),
    "exit_decided_at_ms": lambda trade, ms: replace(trade, exit_decided_at_ms=ms),
    "entry_bar_close_ms": lambda trade, ms: replace(trade, entry=replace(trade.entry, bar_close_ms=ms)),
    "exit_bar_close_ms": lambda trade, ms: replace(trade, exit=replace(trade.exit, bar_close_ms=ms)),
}


def _with_the_first_trades(result: RunResult, change: Callable[[LegTrade], LegTrade]) -> RunResult:
    first = next(session for session in result.sessions if session.outcome.trades)
    trade, *others = first.outcome.trades
    changed = replace(first, outcome=replace(first.outcome, trades=(change(trade), *others)))
    return replace(result, sessions=tuple(changed if session is first else session for session in result.sessions))


@pytest.mark.parametrize("column", list(_TRADE_INSTANTS))
async def test_a_trade_instant_under_zero_or_over_the_ceiling_is_refused_by_the_database_and_the_ceiling_itself_is_not(
    conn: asyncpg.Connection, tag: str, result: RunResult, column: str
) -> None:
    for refused in (-1, _CEILING + 1, 2**63 - 1):
        run_id = await create_run(conn, _new(result, tag, f"over{refused}"))
        with pytest.raises(asyncpg.CheckViolationError) as caught:
            await complete_run(
                conn,
                run_id,
                _with_the_first_trades(result, lambda trade, ms=refused: _TRADE_INSTANTS[column](trade, ms)),
                finished_at_ms=FINISHED_AT_MS,
            )
        assert caught.value.constraint_name == f"research_options_backtest_run_trades_{column}_check"
        assert (await get_run(conn, run_id, is_live=ALIVE)).record.status == "running"  # type: ignore[union-attr]
    for admitted in (0, _CEILING):
        run_id = await create_run(conn, _new(result, tag, f"at{admitted}"))
        await complete_run(
            conn,
            run_id,
            _with_the_first_trades(result, lambda trade, ms=admitted: _TRADE_INSTANTS[column](trade, ms)),
            finished_at_ms=FINISHED_AT_MS,
        )


async def _run_with_a_session_row(conn: asyncpg.Connection, result: RunResult, tag: str) -> int:
    return await _complete(conn, result, tag)


async def test_a_session_is_skipped_exactly_when_it_has_a_skip_reason(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    insert = (
        "INSERT INTO research_options_backtest_run_sessions (run_id, session_open_ms, position, session_close_ms, status, "
        "skip_reason, cash_before_usd, cash_after_usd, cash_change_usd, profit_usd, option_days_json, events_json) "
        "VALUES ($1, 1, 0, 2, $2, $3, 0, 0, 0, 0, '[]', '[]')"
    )

    for status, reason in (("skipped", None), ("traded", "window_does_not_fit"), ("no_trade", "x")):
        with pytest.raises(asyncpg.CheckViolationError) as caught:
            await conn.execute(insert, run_id, status, reason)
        assert caught.value.constraint_name == "ck_research_options_backtest_run_sessions_skip"
    await conn.execute(insert, run_id, "skipped", "window_does_not_fit")


async def test_a_trade_that_left_by_a_fill_has_a_side_and_a_price_seen_and_one_marked_has_neither(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await _complete(conn, result, tag)
    fill = "exit_kind = 'fill'"
    mark = "exit_kind = 'mark'"

    for change, kind in (
        ("exit_side = NULL", fill),
        ("exit_seen = NULL", fill),
        ("exit_side = 'sell'", mark),
        ("exit_seen = 1", mark),
    ):
        with pytest.raises(asyncpg.CheckViolationError) as caught:
            await conn.execute(
                f"UPDATE research_options_backtest_run_trades SET {change} WHERE run_id = $1 AND {kind}", run_id
            )
        assert caught.value.constraint_name == "ck_research_options_backtest_run_trades_exit", change
    # A fill turned into a mark, and a mark into a fill, each in one statement that keeps the rule: allowed.
    await conn.execute(
        "UPDATE research_options_backtest_run_trades SET exit_kind = 'mark', exit_side = NULL, exit_seen = NULL "
        "WHERE run_id = $1 AND exit_kind = 'fill'",
        run_id,
    )


async def test_the_last_session_of_a_run_cannot_be_before_the_first_or_past_the_ceiling(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    first = result.sessions[0].session_open_ms

    for last in (first - 1, _CEILING + 1):
        with pytest.raises(asyncpg.CheckViolationError) as caught:
            await conn.execute(
                "UPDATE research_options_backtest_runs SET last_session_open_ms = $2 WHERE id = $1", run_id, last
            )
        assert caught.value.constraint_name == "research_options_backtest_runs_check"
    await conn.execute(
        "UPDATE research_options_backtest_runs SET last_session_open_ms = $2 WHERE id = $1", run_id, _CEILING
    )


async def test_two_sessions_of_one_run_cannot_share_a_position(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    insert = (
        "INSERT INTO research_options_backtest_run_sessions (run_id, session_open_ms, position, session_close_ms, status, "
        "cash_before_usd, cash_after_usd, cash_change_usd, profit_usd, option_days_json, events_json) "
        "VALUES ($1, $2, $3, 9, 'traded', 0, 0, 0, 0, '[]', '[]')"
    )
    await conn.execute(insert, run_id, 1, 0)

    with pytest.raises(asyncpg.UniqueViolationError) as caught:
        await conn.execute(insert, run_id, 2, 0)
    assert caught.value.constraint_name == "uq_research_options_backtest_run_sessions_position"
    await conn.execute(insert, run_id, 2, 1)


async def test_a_run_has_one_of_the_four_stored_statuses_and_no_other(
    conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))

    # An end time keeps the other rules of a run that is not running satisfied, so only the status rule can refuse.
    with pytest.raises(asyncpg.CheckViolationError) as caught:
        await conn.execute(
            "UPDATE research_options_backtest_runs SET status = 'interrupted', finished_at_ms = 5 WHERE id = $1", run_id
        )
    assert caught.value.constraint_name == "research_options_backtest_runs_status_check"
    await conn.execute(
        "UPDATE research_options_backtest_runs SET status = 'cancelled', finished_at_ms = 5 WHERE id = $1", run_id
    )


# ── The two row locks ────────────────────────────────────────────────────


async def _wait_until_waiting_on_a_lock(observer: asyncpg.Connection, pid: int) -> None:
    for _ in range(200):
        if await observer.fetchval("SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1", pid) == "Lock":
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the call never waited on the run's row: it did not take the lock")


async def test_a_delete_waits_for_a_worker_that_holds_the_row_and_judges_the_run_as_that_worker_leaves_it(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    pid = await second_conn.fetchval("SELECT pg_backend_pid()")

    # The worker (``conn``) is part way through ending the run: its row is locked and the change is not yet committed.
    async with conn.transaction():
        await conn.execute(
            "UPDATE research_options_backtest_runs SET status = 'cancelled', finished_at_ms = $2 WHERE id = $1",
            run_id,
            FINISHED_AT_MS,
        )
        deleting = asyncio.create_task(delete_run(second_conn, run_id, is_live=ALIVE))
        await _wait_until_waiting_on_a_lock(conn, pid)
        assert not deleting.done()

    # Judged on the row as the worker left it: cancelled, not running, so it goes. Judged on the row as it was before
    # (running, and its job alive), it would be refused as still running.
    assert await asyncio.wait_for(deleting, timeout=10) is DeleteOutcome.DELETED


async def test_a_save_waits_for_whatever_holds_the_row_and_finds_the_run_no_longer_running(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, tag: str, result: RunResult
) -> None:
    run_id = await create_run(conn, _new(result, tag))
    pid = await second_conn.fetchval("SELECT pg_backend_pid()")

    # A cancel is part way through: the row is locked and its change to 'cancelled' is not yet committed.
    async with conn.transaction():
        await conn.execute(
            "UPDATE research_options_backtest_runs SET status = 'cancelled', finished_at_ms = $2 WHERE id = $1",
            run_id,
            FINISHED_AT_MS,
        )
        saving = asyncio.create_task(complete_run(second_conn, run_id, result, finished_at_ms=FINISHED_AT_MS))
        await _wait_until_waiting_on_a_lock(conn, pid)
        assert not saving.done()

    with pytest.raises(RunNotRunning):
        await asyncio.wait_for(saving, timeout=10)
    opened = await get_run(conn, run_id, is_live=GONE)
    assert opened is not None and opened.record.status == "cancelled" and opened.figures is None
    assert await _rows_of(conn, "research_options_backtest_run_sessions", run_id) == 0


# ── The save and the job's lease ─────────────────────────────────────────


def test_no_write_that_ends_a_run_waits_as_long_as_the_jobs_lease_lasts(
    monkeypatch: pytest.MonkeyPatch, result: RunResult
) -> None:
    # The job's lease is 300 seconds and is last renewed when the ``saving`` phase begins; a run whose lease has
    # lapsed reads ``interrupted``, and an interrupted run can be deleted. So the writes of the job's own thread (the
    # save, the failure and the cancel) wait 60 seconds less than a lease: 300 - 60 = 240. The history, one run and
    # the delete are awaited by the routes and take no part in this.
    timeouts: list[float | None] = []

    def run_on(coroutine: Any, *, timeout: float | None) -> None:
        coroutine.close()
        timeouts.append(timeout)

    monkeypatch.setattr(saved_runs, "run_on_background_loop", run_on)
    store = saved_runs.DbRunStore()

    store.complete(1, result, finished_at_ms=FINISHED_AT_MS)
    store.fail(1, RunFailure("X", "x"), finished_at_ms=FINISHED_AT_MS)
    store.cancel(1, finished_at_ms=FINISHED_AT_MS)

    assert timeouts == [240.0, 240.0, 240.0]
    assert all(timeout is not None and timeout < JOB_LEASE_TTL_SECONDS for timeout in timeouts)
