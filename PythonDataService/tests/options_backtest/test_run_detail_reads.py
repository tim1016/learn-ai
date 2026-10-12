"""Reading a saved run closely, on a real database (#3030): its leg trades, one of its sessions, and the lines two runs
are compared by.

These run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Two
runs are saved: the busy run of ``_a_run.py`` (stops, re-entries, a square-off, legs marked and not sold), whose trades
must read back equal to what was saved, and the three-session run of ``_detail_scene.py``, whose figures are worked by
hand in that file's docstring.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from app.options_backtest import run_detail, saved_runs
from app.options_backtest.results import SessionStatus
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.run_detail import (
    LegNotInRun,
    RunNotComplete,
    RunNotFound,
    SessionNotInRun,
    read_every_trade,
    read_trade_page,
)
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._detail_scene import (
    FRI_OPEN_MS,
    SESSIONS,
    THU_OPEN_MS,
    WED_OPEN_MS,
    the_scene,
    the_setup,
)
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._stored_runs import SaveRun
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique
from tests.options_backtest.test_saved_runs import _the_same_all_the_way_down

D = Decimal
ALIVE = lambda _job_id: True  # noqa: E731
GONE = lambda _job_id: False  # noqa: E731


@pytest.fixture(scope="module")
def busy() -> RunResult:
    return a_busy_run()


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


def _every_trade(result: RunResult) -> list[tuple[int, int, object]]:
    """(session open, place in the session, trade) for every trade of a run, in the order the run recorded them."""
    return [
        (session.session_open_ms, number, trade)
        for session in result.sessions
        for number, trade in enumerate(session.outcome.trades)
    ]


# ── The trades ───────────────────────────────────────────────────────────


async def test_every_trade_of_a_run_reads_back_equal_to_the_one_saved_in_the_order_the_run_recorded(
    conn: asyncpg.Connection, save_run: SaveRun, busy: RunResult
) -> None:
    run_id = await save_run(busy, "busy")

    stored = await saved_runs.read_trades(conn, run_id)

    expected = _every_trade(busy)
    assert len(expected) >= 10, "the busy run must have enough trades to make an order worth checking"
    assert [(each.session_open_ms, each.trade_number) for each in stored] == [
        (at, number) for at, number, _ in expected
    ]
    for kept, (_, _, saved) in zip(stored, expected, strict=True):
        _the_same_all_the_way_down(kept.trade, saved, f"trade {kept.trade_number} of {kept.session_open_ms}")


async def test_the_trades_are_counted_and_narrowed_by_session_and_by_leg(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "scene")

    async def numbers(**filters: object) -> list[tuple[int, int]]:
        return [
            (each.session_open_ms, each.trade_number) for each in await saved_runs.read_trades(conn, run_id, **filters)
        ]  # type: ignore[arg-type]

    # Wednesday holds three trades: the put (0), the call that was stopped (1) and its re-entry (2). The other two
    # sessions hold none.
    assert await saved_runs.count_trades(conn, run_id) == 3
    assert await numbers() == [(WED_OPEN_MS, 0), (WED_OPEN_MS, 1), (WED_OPEN_MS, 2)]
    assert await saved_runs.count_trades(conn, run_id, session_open_ms=WED_OPEN_MS) == 3
    assert await saved_runs.count_trades(conn, run_id, session_open_ms=THU_OPEN_MS) == 0
    assert await saved_runs.count_trades(conn, run_id, session_open_ms=FRI_OPEN_MS) == 0
    # The call leg made trades 1 and 2, the put leg trade 0, and the lag leg none.
    assert await numbers(leg_id="call") == [(WED_OPEN_MS, 1), (WED_OPEN_MS, 2)]
    assert await numbers(leg_id="put") == [(WED_OPEN_MS, 0)]
    assert await numbers(leg_id="lag") == []
    assert await saved_runs.count_trades(conn, run_id, leg_id="call") == 2
    assert await numbers(session_open_ms=WED_OPEN_MS, leg_id="call") == [(WED_OPEN_MS, 1), (WED_OPEN_MS, 2)]
    assert await numbers(session_open_ms=THU_OPEN_MS, leg_id="call") == []


async def test_a_limit_and_an_offset_take_a_window_of_the_trades_in_order(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "paged")

    async def numbers(limit: int | None, offset: int) -> list[int]:
        return [each.trade_number for each in await saved_runs.read_trades(conn, run_id, limit=limit, offset=offset)]

    assert await numbers(2, 0) == [0, 1]
    assert await numbers(2, 1) == [1, 2]
    assert await numbers(2, 2) == [2]
    assert await numbers(2, 3) == []
    assert await numbers(None, 1) == [1, 2]


async def test_the_sessions_lines_say_each_status_its_stored_profit_and_how_many_trades_it_holds(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "lines")

    lines = await saved_runs.read_session_lines(conn, run_id)

    # Wednesday 8.70 - 121.30 - 31.30 = -143.90 over three trades; Thursday was skipped and Friday traded nothing.
    assert [(line.session_open_ms, line.status, line.profit_usd, line.trades) for line in lines] == [
        (WED_OPEN_MS, SessionStatus.TRADED, D("-143.90"), 3),
        (THU_OPEN_MS, SessionStatus.SKIPPED, D("0"), 0),
        (FRI_OPEN_MS, SessionStatus.NO_TRADE, D("0"), 0),
    ]
    assert all(type(line.profit_usd) is Decimal for line in lines)


# ── What a page of trades refuses, in order ──────────────────────────────


async def test_a_page_of_trades_says_the_total_and_where_the_rest_begins(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "page")

    async def page(limit: int, offset: int) -> run_detail.TradePage:
        return await read_trade_page(
            conn, run_id, is_live=ALIVE, session_open_ms=None, leg_id=None, limit=limit, offset=offset
        )

    first = await page(2, 0)
    assert (first.total, len(first.trades), first.next_offset) == (3, 2, 2)
    last = await page(2, 2)
    assert (last.total, [each.trade_number for each in last.trades], last.next_offset) == (3, [2], None)
    exact = await page(3, 0)
    assert (exact.total, len(exact.trades), exact.next_offset) == (3, 3, None)
    beyond = await page(2, 5)
    assert (beyond.total, beyond.trades, beyond.next_offset) == (3, (), None)


async def test_a_run_that_is_missing_or_not_complete_has_no_trades_to_page_and_is_refused_first(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    running = await save_run(scene, "running", end="running")
    failed = await save_run(scene, "failed", end="failed")
    cancelled = await save_run(scene, "cancelled", end="cancelled")
    # A refusal about the session or the leg must not hide that the run itself is not one: ask with both wrong.
    wrong = {"session_open_ms": 1, "leg_id": "nope", "limit": 10, "offset": 0}

    with pytest.raises(RunNotFound):
        await read_trade_page(conn, 2**52, is_live=GONE, **wrong)
    for run_id, status in ((failed, "failed"), (cancelled, "cancelled")):
        with pytest.raises(RunNotComplete) as caught:
            await read_trade_page(conn, run_id, is_live=GONE, **wrong)
        assert caught.value.status == status
    with pytest.raises(RunNotComplete) as caught:
        await read_trade_page(conn, running, is_live=ALIVE, **wrong)
    assert caught.value.status == "running"
    with pytest.raises(RunNotComplete) as caught:
        await read_trade_page(conn, running, is_live=GONE, **wrong)
    assert caught.value.status == "interrupted"
    with pytest.raises(RunNotComplete):
        await read_every_trade(conn, failed, is_live=GONE)


async def test_a_session_the_run_did_not_test_and_a_leg_it_does_not_have_are_refused_and_not_answered_with_nothing(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "filters")
    ask = {"limit": 10, "offset": 0}

    with pytest.raises(SessionNotInRun) as no_session:
        await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=WED_OPEN_MS + 1, leg_id=None, **ask)
    assert (no_session.value.run_id, no_session.value.session_open_ms) == (run_id, WED_OPEN_MS + 1)
    with pytest.raises(LegNotInRun) as no_leg:
        await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=None, leg_id="nope", **ask)
    assert no_leg.value.leg_id == "nope"
    # A session that was tested and made no trade, and a leg the setup has that made none, are answers: empty pages.
    quiet = await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=THU_OPEN_MS, leg_id=None, **ask)
    idle = await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=None, leg_id="lag", **ask)
    assert (quiet.total, quiet.trades) == (0, ())
    assert (idle.total, idle.trades) == (0, ())


async def test_every_trade_of_a_run_is_read_with_no_limit(
    conn: asyncpg.Connection, save_run: SaveRun, busy: RunResult
) -> None:
    run_id = await save_run(busy, "every")

    everything = await read_every_trade(conn, run_id, is_live=GONE)

    assert len(everything) == len(_every_trade(busy))
    assert [(each.session_open_ms, each.trade_number) for each in everything] == [
        (at, number) for at, number, _ in _every_trade(busy)
    ]
