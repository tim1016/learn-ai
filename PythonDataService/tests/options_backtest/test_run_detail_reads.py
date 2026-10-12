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
    RunsNotComplete,
    RunsOfDifferentUnderlyings,
    RunsSetupCannotBeRead,
    SessionNotInRun,
    read_comparison_inputs,
    read_every_trade,
    read_session_record,
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


# ── One session, as stored ───────────────────────────────────────────────


async def test_a_session_reads_back_equal_to_the_one_saved_with_its_trades_events_and_pins(
    conn: asyncpg.Connection, save_run: SaveRun, busy: RunResult
) -> None:
    run_id = await save_run(busy, "session")

    for saved in busy.sessions:
        record = await read_session_record(conn, run_id, saved.session_open_ms, is_live=GONE)

        _the_same_all_the_way_down(record.session.result, saved, f"session {saved.session_open_ms}")
        assert record.session.profit_usd == saved.pnl_usd and type(record.session.profit_usd) is Decimal
        assert record.header.record.id == run_id and record.header.setup == busy.setup


async def test_a_session_of_a_run_that_is_missing_or_not_complete_or_that_the_run_did_not_test_is_refused_in_that_order(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    complete = await save_run(scene, "complete")
    failed = await save_run(scene, "failed", end="failed")

    with pytest.raises(RunNotFound):
        await read_session_record(conn, 2**52, WED_OPEN_MS + 1, is_live=GONE)
    with pytest.raises(RunNotComplete) as caught:
        await read_session_record(conn, failed, WED_OPEN_MS + 1, is_live=GONE)
    assert caught.value.status == "failed"
    with pytest.raises(SessionNotInRun) as no_session:
        await read_session_record(conn, complete, WED_OPEN_MS + 1, is_live=GONE)
    assert no_session.value.session_open_ms == WED_OPEN_MS + 1


# ── Two runs ─────────────────────────────────────────────────────────────


async def test_two_complete_runs_of_one_underlying_are_read_with_the_lines_of_their_sessions(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    first = await save_run(scene, "first")
    second = await save_run(scene, "second")

    inputs = await read_comparison_inputs(conn, first, second, is_live=GONE)
    twice = await read_comparison_inputs(conn, first, first, is_live=GONE)

    assert (inputs.a.record.id, inputs.b.record.id) == (first, second)
    assert [line.session_open_ms for line in inputs.a_lines] == [WED_OPEN_MS, THU_OPEN_MS, FRI_OPEN_MS]
    assert inputs.a_lines == inputs.b_lines
    assert twice.a_lines == twice.b_lines == inputs.a_lines


async def test_a_pair_is_refused_for_the_first_missing_run_then_for_every_run_not_complete_then_for_its_underlyings(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    complete = await save_run(scene, "complete")
    failed = await save_run(scene, "failed", end="failed")
    running = await save_run(scene, "running", end="running")
    other = await save_run(
        RunResult(
            setup=scene.setup.model_copy(update={"underlying": "QQQ"}),
            sessions=scene.sessions,
            summary=scene.summary,
            slippage=scene.slippage,
            slippage_tolerance=scene.slippage_tolerance,
        ),
        "other",
    )

    with pytest.raises(RunNotFound) as missing:
        await read_comparison_inputs(conn, 2**52, 2**51, is_live=GONE)
    assert missing.value.run_id == 2**52
    with pytest.raises(RunNotFound) as missing_b:
        await read_comparison_inputs(conn, complete, 2**51, is_live=GONE)
    assert missing_b.value.run_id == 2**51
    with pytest.raises(RunsNotComplete) as neither:
        await read_comparison_inputs(conn, failed, running, is_live=GONE)
    assert neither.value.runs == (("a", failed, "failed"), ("b", running, "interrupted"))
    with pytest.raises(RunsNotComplete) as one:
        await read_comparison_inputs(conn, complete, failed, is_live=GONE)
    assert one.value.runs == (("b", failed, "failed"),)
    with pytest.raises(RunsOfDifferentUnderlyings) as different:
        await read_comparison_inputs(conn, complete, other, is_live=GONE)
    assert (different.value.a, different.value.b) == ((complete, "SPY"), (other, "QQQ"))


# ── Money is read in plain digits ────────────────────────────────────────


@pytest.mark.parametrize(
    "profit", ["1000", "10000", "20000", "100000", "1200000", "99990000", "-10000", "5000.50", "0.10", "0"]
)
async def test_a_sessions_stored_profit_is_read_in_plain_digits_as_one_session_and_as_a_line_of_a_comparison(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult, profit: str
) -> None:
    # asyncpg alone returns 10000 as 1E+4. The profit column is set by hand to each awkward figure; the text read back is
    # the text written.
    run_id = await save_run(scene, "digits")
    await conn.execute(
        "UPDATE research_options_backtest_run_sessions SET profit_usd = $3 WHERE run_id = $1 AND session_open_ms = $2",
        run_id,
        WED_OPEN_MS,
        D(profit),
    )

    stored = await saved_runs.read_session(conn, run_id, WED_OPEN_MS)
    lines = await saved_runs.read_session_lines(conn, run_id)
    record = await read_session_record(conn, run_id, WED_OPEN_MS, is_live=GONE)

    assert stored is not None
    assert str(stored.profit_usd) == profit
    assert str(lines[0].profit_usd) == profit and lines[0].session_open_ms == WED_OPEN_MS
    assert str(record.session.profit_usd) == profit


# ── A run whose stored setup no longer validates ─────────────────────────


async def _lose_the_setup(conn: asyncpg.Connection, run_id: int) -> None:
    """A row edited by hand as a later rule would leave it: a field the setup no longer has."""
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        run_id,
    )


async def test_a_run_whose_stored_setup_no_longer_validates_still_gives_its_trades_its_sessions_and_its_header(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "lost")
    healthy = await save_run(scene, "healthy")
    await _lose_the_setup(conn, run_id)

    header = await saved_runs.get_run_header(conn, run_id, is_live=GONE)
    every = await read_every_trade(conn, run_id, is_live=GONE)
    page = await read_trade_page(
        conn, run_id, is_live=GONE, session_open_ms=WED_OPEN_MS, leg_id="call", limit=10, offset=0
    )
    record = await read_session_record(conn, run_id, WED_OPEN_MS, is_live=GONE)

    assert header is not None and header.setup is None and header.setup_cannot_open_reason is not None
    assert "lots" in header.setup_cannot_open_reason
    assert (header.record.id, header.record.underlying, header.record.status) == (run_id, "SPY", "complete")
    # Wednesday's three trades; the call leg made two of them (trade numbers 1 and 2).
    assert len(every) == 3
    assert (page.total, [each.trade_number for each in page.trades]) == (2, [1, 2])
    assert record.header.setup is None and record.header.setup_cannot_open_reason == header.setup_cannot_open_reason
    assert len(record.session.result.outcome.trades) == 3
    # A neighbour that is healthy is read as ever.
    neighbour = await saved_runs.get_run_header(conn, healthy, is_live=GONE)
    assert neighbour is not None and neighbour.setup == scene.setup


async def test_with_no_setup_to_name_its_legs_a_leg_filter_is_answered_by_the_stored_trades_alone(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    run_id = await save_run(scene, "legs")
    await _lose_the_setup(conn, run_id)

    # "lag" made no trade and "nope" is no leg at all: with the setup gone the two cannot be told apart, and both are a
    # page of no stored trade. A leg that made trades is answered as ever.
    lag = await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=None, leg_id="lag", limit=10, offset=0)
    nope = await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=None, leg_id="nope", limit=10, offset=0)
    put = await read_trade_page(conn, run_id, is_live=GONE, session_open_ms=None, leg_id="put", limit=10, offset=0)

    assert (lag.total, lag.trades, nope.total, nope.trades) == (0, (), 0, ())
    assert (put.total, [each.trade_number for each in put.trades]) == (1, [0])


async def test_two_runs_cannot_be_compared_while_either_one_has_lost_its_setup_and_the_one_is_named(
    conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult
) -> None:
    lost = await save_run(scene, "lost")
    healthy = await save_run(scene, "healthy")
    await _lose_the_setup(conn, lost)

    with pytest.raises(RunsSetupCannotBeRead) as as_b:
        await read_comparison_inputs(conn, healthy, lost, is_live=GONE)
    with pytest.raises(RunsSetupCannotBeRead) as as_a:
        await read_comparison_inputs(conn, lost, healthy, is_live=GONE)

    assert [(role, run_id) for role, run_id, _ in as_b.value.runs] == [("b", lost)]
    assert [(role, run_id) for role, run_id, _ in as_a.value.runs] == [("a", lost)]
    assert "lots" in as_b.value.runs[0][2]
