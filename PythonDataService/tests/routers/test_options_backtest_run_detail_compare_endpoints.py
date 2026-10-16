"""Two saved runs compared, over HTTP (#3030).

Saved runs live in a real database, so these run only against one attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Run A is the three-leg, three-session run of
``tests/options_backtest/_detail_scene.py``. Run B is the same scene with only the ``put`` leg, over Wednesday and Thursday.
Hand-worked, over the two sessions they share (Wednesday and Thursday):

* A: Wednesday made 8.70 - 121.30 - 31.30 = -143.90 in 3 trades and Thursday was skipped (0). So -143.90, 3 trades and no
  winning session.
* B: Wednesday made the put's 8.70 in 1 trade and Thursday was skipped (0). So 8.70, 1 trade and 1 winning session.
* Friday is A's alone: one session only A has. B has none that A lacks.

Run B is also made twice more: over a market with no underlying price at the entry minute on Wednesday, so that run skips
the session run A traded (the two disagree about one shared session), and with a stored setup that no longer validates.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest

from app.options_backtest.run import RunResult, run_sessions
from tests.options_backtest._detail_scene import FRI_OPEN_MS, SESSIONS, THU_OPEN_MS, WED_OPEN_MS, the_scene, the_setup
from tests.options_backtest._run_data import THU, WED, changed, fake_data, steady
from tests.options_backtest._stored_runs import PREFIX, SaveRun
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal

# A leg only one run has is said with the sentences a setup's description makes of it.
CALL_LEG = (
    "Leg call: buys 1 at-the-money call expiring on the trading date itself. "
    "The buy waits until a minute closes at least 25% above where its premium stood at the entry time. "
    "Sells once a minute closes at least 20% below what was paid. "
    "After a stop, picks a fresh contract by the same rules, takes a new starting premium from it and waits for "
    "momentum again, up to 1 time in a session."
)
LAG_LEG = (
    "Leg lag: buys 1 put 1 strike below the money expiring on the trading date itself. "
    "The buy waits until a minute closes at least 10% below where its premium stood at the entry time."
)
A_CREATED_AT_MS = 1_791_800_000_000
B_CREATED_AT_MS = 1_791_900_000_000


@pytest.fixture(scope="module")
def run_a() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


@pytest.fixture(scope="module")
def run_b(run_a: RunResult) -> RunResult:
    put_only = changed(run_a.setup, legs=tuple(each for each in run_a.setup.legs if each.leg_id == "put"))
    return run_sessions(put_only, [WED, THU], fake_data(the_scene()))


async def _compare(client: httpx.AsyncClient, a: int, b: int) -> httpx.Response:
    return await client.get(f"{PREFIX}/runs/compare", params={"a": a, "b": b})


async def test_two_runs_are_compared_setting_by_setting_and_on_the_sessions_they_share(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, run_b: RunResult, is_the_job_alive: Any
) -> None:
    a = await save_run(run_a, "a", setup_name="three legs", created_at_ms=A_CREATED_AT_MS)
    b = await save_run(run_b, "b", setup_name=None, created_at_ms=B_CREATED_AT_MS)

    response = await _compare(client, a, b)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["underlying"] == "SPY"
    assert (body["a"]["id"], body["a"]["setup_name"]) == (a, "three legs")
    assert (body["b"]["id"], body["b"]["setup_name"]) == (b, None)
    # Each run's own time of making: never the other's.
    assert (body["a"]["created_at_ms"], body["b"]["created_at_ms"]) == (A_CREATED_AT_MS, B_CREATED_AT_MS)
    # B has only the put leg; A also has call and lag, in A's order. Nothing else differs.
    assert body["setting_differences"] == [
        {"label": "leg call", "a": CALL_LEG, "b": "not in this run"},
        {"label": "leg lag", "a": LAG_LEG, "b": "not in this run"},
    ]
    # A ran three sessions from Wednesday to Friday and B two, Wednesday and Thursday.
    assert body["range_difference"] == {
        "a": {"first_session_open_ms": WED_OPEN_MS, "last_session_open_ms": FRI_OPEN_MS, "sessions_requested": 3},
        "b": {"first_session_open_ms": WED_OPEN_MS, "last_session_open_ms": THU_OPEN_MS, "sessions_requested": 2},
    }
    overlap = body["overlap"]
    assert (overlap["shared_sessions"], overlap["only_in_a"], overlap["only_in_b"]) == (2, 1, 0)
    assert (D(overlap["a"]["net_profit_usd"]), overlap["a"]["trades"], overlap["a"]["winning_sessions"]) == (
        D("-143.90"),
        3,
        0,
    )
    assert (D(overlap["b"]["net_profit_usd"]), overlap["b"]["trades"], overlap["b"]["winning_sessions"]) == (
        D("8.70"),
        1,
        1,
    )
    assert [
        (s["session_open_ms"], s["a_status"], s["b_status"], D(s["a_profit_usd"]), D(s["b_profit_usd"]))
        for s in overlap["sessions"]
    ] == [
        (WED_OPEN_MS, "traded", "traded", D("-143.90"), D("8.70")),
        (THU_OPEN_MS, "skipped", "skipped", D("0"), D("0")),
    ]


async def test_two_runs_of_the_same_setup_over_the_same_range_differ_in_nothing_though_they_are_two_runs(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    first = await save_run(run_a, "one")
    second = await save_run(run_a, "two")

    body = (await _compare(client, first, second)).json()

    assert (body["a"]["id"], body["b"]["id"]) == (first, second)
    assert body["setting_differences"] == [] and body["range_difference"] is None


async def test_each_run_keeps_its_own_status_and_profit_on_a_session_they_share_whichever_order_they_are_asked_in(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    # Run B has no underlying price at the entry minute (m5) on Wednesday, so it skips the session run A traded.
    market = the_scene()
    market[WED] = replace(market[WED], underlying={k: v for k, v in steady("500.20").items() if k != 5})
    run_b = run_sessions(the_setup(), [WED, THU], fake_data(market))
    a = await save_run(run_a, "traded")
    b = await save_run(run_b, "skipped")

    a_then_b = (await _compare(client, a, b)).json()["overlap"]
    b_then_a = (await _compare(client, b, a)).json()["overlap"]

    # Wednesday: run A traded and lost 143.90, run B was skipped (0). Thursday: both skipped (0).
    assert [
        (s["session_open_ms"], s["a_status"], s["b_status"], D(s["a_profit_usd"]), D(s["b_profit_usd"]))
        for s in a_then_b["sessions"]
    ] == [
        (WED_OPEN_MS, "traded", "skipped", D("-143.90"), D("0")),
        (THU_OPEN_MS, "skipped", "skipped", D("0"), D("0")),
    ]
    # Asked the other way round, every pair is the other way round: A is then the skipped run.
    assert [
        (s["session_open_ms"], s["a_status"], s["b_status"], D(s["a_profit_usd"]), D(s["b_profit_usd"]))
        for s in b_then_a["sessions"]
    ] == [
        (WED_OPEN_MS, "skipped", "traded", D("0"), D("-143.90")),
        (THU_OPEN_MS, "skipped", "skipped", D("0"), D("0")),
    ]
    assert (D(a_then_b["a"]["net_profit_usd"]), a_then_b["a"]["trades"], a_then_b["a"]["winning_sessions"]) == (
        D("-143.90"),
        3,
        0,
    )
    assert (D(a_then_b["b"]["net_profit_usd"]), a_then_b["b"]["trades"], a_then_b["b"]["winning_sessions"]) == (
        D("0"),
        0,
        0,
    )


async def _lose_the_setup(conn: asyncpg.Connection, run_id: int) -> None:
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        run_id,
    )


async def test_a_run_whose_stored_setup_can_no_longer_be_read_cannot_be_compared_and_the_409_names_it_with_its_reason(
    client: httpx.AsyncClient, conn: asyncpg.Connection, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    healthy = await save_run(run_a, "healthy")
    lost = await save_run(run_a, "lost")
    also_lost = await save_run(run_a, "also-lost")
    await _lose_the_setup(conn, lost)
    await _lose_the_setup(conn, also_lost)

    as_b = await _compare(client, healthy, lost)
    as_a = await _compare(client, lost, healthy)
    both = await _compare(client, lost, also_lost)

    for response in (as_b, as_a, both):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_SETUP_CANNOT_BE_READ"
    # Plain words that name the run, its place, and the reason beside it (here a field the setup no longer has).
    as_b_message = as_b.json()["detail"]["message"]
    assert f"the stored setup of run {lost} (b) can no longer be read: lots: " in as_b_message
    assert str(healthy) not in as_b_message
    assert f"the stored setup of run {lost} (a) can no longer be read: lots: " in as_a.json()["detail"]["message"]
    both_message = both.json()["detail"]["message"]
    assert f"run {lost} (a)" in both_message and f"run {also_lost} (b)" in both_message


async def test_a_run_that_is_not_complete_is_named_before_a_run_whose_setup_is_lost(
    client: httpx.AsyncClient, conn: asyncpg.Connection, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    lost = await save_run(run_a, "lost")
    failed = await save_run(run_a, "failed", end="failed")
    await _lose_the_setup(conn, lost)

    response = await _compare(client, lost, failed)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"
    assert f"run {failed} (b) is failed" in response.json()["detail"]["message"]


# ── What it refuses ──────────────────────────────────────────────────────


async def test_a_run_that_does_not_exist_is_a_404_that_says_whether_it_is_a_or_b(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    real = await save_run(run_a, "real")

    no_a = await _compare(client, 2**52, real)
    no_b = await _compare(client, real, 2**52)

    for response, role in ((no_a, "run a"), (no_b, "run b")):
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_FOUND"
        assert role in response.json()["detail"]["message"] and str(2**52) in response.json()["detail"]["message"]


async def test_a_run_that_is_not_complete_is_a_409_naming_every_one_that_is_not_and_its_status(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: dict[str, bool | None]
) -> None:
    complete = await save_run(run_a, "complete")
    failed = await save_run(run_a, "failed", end="failed")
    cancelled = await save_run(run_a, "cancelled", end="cancelled")

    second = await _compare(client, complete, failed)
    first = await _compare(client, cancelled, complete)
    both = await _compare(client, cancelled, failed)

    for response in (second, first, both):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"
    assert f"run {failed} (b) is failed" in second.json()["detail"]["message"]
    assert str(complete) not in second.json()["detail"]["message"]
    assert f"run {cancelled} (a) is cancelled" in first.json()["detail"]["message"]
    message = both.json()["detail"]["message"]
    assert f"run {cancelled} (a) is cancelled" in message and f"run {failed} (b) is failed" in message


async def test_runs_of_different_underlyings_are_a_409_naming_both_runs_and_their_underlyings(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    spy = await save_run(run_a, "spy")
    qqq = await save_run(replace(run_a, setup=changed(run_a.setup, underlying="QQQ")), "qqq")

    response = await _compare(client, spy, qqq)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "OPTIONS_BACKTEST_RUNS_OF_DIFFERENT_UNDERLYINGS"
    for fragment in (f"run {spy} (a) trades SPY", f"run {qqq} (b) trades QQQ"):
        assert fragment in detail["message"]


@pytest.mark.parametrize(
    "params",
    [{}, {"a": 1}, {"b": 1}, {"a": 0, "b": 1}, {"a": 1, "b": -1}, {"a": "x", "b": 1}, {"a": 2**53, "b": 1}],
)
async def test_a_request_that_cannot_be_one_is_a_422_about_the_query_and_not_about_a_run_called_compare(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    response = await client.get(f"{PREFIX}/runs/compare", params=params)

    assert response.status_code == 422, response.text
    # The run service's own ``/runs/{run_id}`` would take "compare" for a run id and fail on the path: this route is
    # mounted before it, so the failures are about the query.
    assert {error["loc"][0] for error in response.json()["detail"]} == {"query"}
