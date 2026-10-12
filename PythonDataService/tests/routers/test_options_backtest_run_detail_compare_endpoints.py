"""Two saved runs compared, over HTTP (#3030).

Saved runs live in a real database, so these run only against one attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. Run A is the three-leg, three-session run of
``tests/options_backtest/_detail_scene.py``. Run B is the same scene with only the ``put`` leg, over Wednesday and Thursday.
Hand-worked, over the two sessions they share (Wednesday and Thursday):

* A: Wednesday made 8.70 - 121.30 - 31.30 = -143.90 in 3 trades and Thursday was skipped (0). So -143.90, 3 trades and no
  winning session.
* B: Wednesday made the put's 8.70 in 1 trade and Thursday was skipped (0). So 8.70, 1 trade and 1 winning session.
* Friday is A's alone: one session only A has. B has none that A lacks.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.options_backtest.run import RunResult, run_sessions
from tests.options_backtest._detail_scene import FRI_OPEN_MS, SESSIONS, THU_OPEN_MS, WED_OPEN_MS, the_scene, the_setup
from tests.options_backtest._run_data import THU, WED, changed, fake_data
from tests.options_backtest._stored_runs import PREFIX, SaveRun
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal

CALL_LEG = (
    "call, 1 contract, expiry same day, strike at the money; stop loss 20% below the price paid; "
    "momentum 25% up from where it stood at the entry time; re-entry on stop on momentum, up to 1 time"
)
LAG_LEG = (
    "put, 1 contract, expiry same day, strike 1 strike below the money; "
    "momentum 10% down from where it stood at the entry time"
)


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
    a = await save_run(run_a, "a", setup_name="three legs")
    b = await save_run(run_b, "b", setup_name=None)

    response = await _compare(client, a, b)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["underlying"] == "SPY"
    assert (body["a"]["id"], body["a"]["setup_name"], body["a"]["sessions_requested"]) == (a, "three legs", 3)
    assert (body["b"]["id"], body["b"]["setup_name"], body["b"]["sessions_requested"]) == (b, None, 2)
    assert body["a"]["fee_policy"] == body["b"]["fee_policy"] == "Fees are $0.65 per contract traded."
    # B has only the put leg; A also has call and lag, in A's order. Nothing else differs.
    assert body["setting_differences"] == [
        {"setting": "leg", "leg_id": "call", "label": "leg call", "a": CALL_LEG, "b": "not in this run"},
        {"setting": "leg", "leg_id": "lag", "label": "leg lag", "a": LAG_LEG, "b": "not in this run"},
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


async def test_a_run_compared_with_itself_differs_in_nothing_and_shares_every_session(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    a = await save_run(run_a, "alone")

    body = (await _compare(client, a, a)).json()

    assert body["setting_differences"] == [] and body["range_difference"] is None
    assert (body["overlap"]["shared_sessions"], body["overlap"]["only_in_a"], body["overlap"]["only_in_b"]) == (3, 0, 0)
    assert body["overlap"]["a"] == body["overlap"]["b"]
    assert D(body["overlap"]["a"]["net_profit_usd"]) == D("-143.90")


async def test_two_runs_of_the_same_setup_over_the_same_range_differ_in_nothing_though_they_are_two_runs(
    client: httpx.AsyncClient, save_run: SaveRun, run_a: RunResult, is_the_job_alive: Any
) -> None:
    first = await save_run(run_a, "one")
    second = await save_run(run_a, "two")

    body = (await _compare(client, first, second)).json()

    assert (body["a"]["id"], body["b"]["id"]) == (first, second)
    assert body["setting_differences"] == [] and body["range_difference"] is None


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
