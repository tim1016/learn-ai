"""Requests anyone can build against the four routes that read a saved run closely never give a 500 (#3030).

Part one needs no database: a request that cannot name a run, a session, a page or a leg is refused with a 422 before
anything is asked of the store, whatever it is made of (zero, a negative, a number past what a browser holds exactly, text,
a NUL, a lone surrogate, a quote, a letter outside ASCII). Part two runs against a database attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skips without one: the requests that are well formed and name nothing that is there,
that name a leg of a run whose stored setup no longer validates, or that ask for an offset past the end are answered, and
every sweep of odd requests gives no 5xx.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx
import pytest

from app.options_backtest.run import RunResult, run_sessions
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._detail_scene import SESSIONS, THU_OPEN_MS, WED_OPEN_MS, the_scene, the_setup
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._stored_runs import PREFIX, SaveRun
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

# What a browser holds exactly is 2**53 - 1; one more is refused, and so is anything an int64 cannot hold.
BEYOND_A_BROWSER = str(2**53)
BEYOND_AN_INT64 = str(2**63)
PAST_THE_CEILING = str(MAX_TIMESTAMP_MS + 1)


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


# ── No database: refused before anything is asked of the store ───────────

_RUN_IDS = [
    "0",
    "-1",
    "abc",
    "1.5",
    "1e3",
    "%20",
    "%00",
    "%ED%A0%80",  # a lone surrogate, percent-encoded
    BEYOND_A_BROWSER,
    BEYOND_AN_INT64,
    "99999999999999999999999",
]
_SESSIONS = [
    "-1",
    "x",
    "1.5",
    "%00",
    "%ED%A0%80",
    PAST_THE_CEILING,
    "9223372036854775807",
    BEYOND_AN_INT64,
    "99999999999999999999",
]


@pytest.mark.parametrize("run_id", _RUN_IDS)
@pytest.mark.parametrize("route", ["trades", "trades.csv"])
async def test_a_run_id_that_cannot_be_one_is_a_422_on_the_trades_and_the_export(
    client: httpx.AsyncClient, route: str, run_id: str
) -> None:
    response = await client.get(f"{PREFIX}/runs/{run_id}/{route}")

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("run_id", _RUN_IDS)
async def test_a_run_id_that_cannot_be_one_is_a_422_on_a_session(client: httpx.AsyncClient, run_id: str) -> None:
    response = await client.get(f"{PREFIX}/runs/{run_id}/sessions/{WED_OPEN_MS}")

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("session", _SESSIONS)
async def test_a_session_open_that_cannot_be_one_is_a_422_on_a_session_and_on_the_trades_filter(
    client: httpx.AsyncClient, session: str
) -> None:
    on_the_path = await client.get(f"{PREFIX}/runs/1/sessions/{session}")
    on_the_filter = await client.get(f"{PREFIX}/runs/1/trades", params={"session_open_ms": session})

    assert on_the_path.status_code == 422, on_the_path.text
    assert on_the_filter.status_code == 422, on_the_filter.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "-1"},
        {"limit": "5001"},
        {"limit": "1000000"},
        {"limit": "x"},
        {"limit": ""},
        {"limit": "%00"},
        {"limit": BEYOND_AN_INT64},
        {"offset": "-1"},
        {"offset": "x"},
        {"offset": ""},
        {"offset": BEYOND_A_BROWSER},
        {"offset": BEYOND_AN_INT64},
        {"offset": "99999999999999999999"},
    ],
)
async def test_a_page_that_cannot_be_asked_for_is_a_422(client: httpx.AsyncClient, params: dict[str, str]) -> None:
    response = await client.get(f"{PREFIX}/runs/1/trades", params=params)

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "leg_id",
    [
        "",
        "\x00",
        "a\x00b",
        "x" * 33,
        "CALL",  # a leg id is lower case
        "ça",
        "💥",
        "a' OR 1=1 --",
        "call;",
        "call leg",
        "-",
        "a\nb",
        "\ud800".encode("utf-8", "replace").decode(),
    ],
)
async def test_a_leg_id_that_no_setup_can_hold_is_a_422_and_never_reaches_the_database(
    client: httpx.AsyncClient, leg_id: str
) -> None:
    response = await client.get(f"{PREFIX}/runs/1/trades", params={"leg_id": leg_id})

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"a": "1"},
        {"b": "1"},
        {"a": "0", "b": "1"},
        {"a": "1", "b": "0"},
        {"a": "-1", "b": "1"},
        {"a": "x", "b": "1"},
        {"a": "1", "b": " "},
        {"a": "1", "b": "1e3"},
        {"a": "%00", "b": "1"},
        {"a": "1", "b": "\x00"},
        {"a": BEYOND_A_BROWSER, "b": "1"},
        {"a": "1", "b": BEYOND_AN_INT64},
        {"a": "99999999999999999999", "b": "1"},
    ],
)
async def test_two_runs_that_cannot_be_named_are_a_422_about_the_query(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    response = await client.get(f"{PREFIX}/runs/compare", params=params)

    assert response.status_code == 422, response.text
    assert {error["loc"][0] for error in response.json()["detail"]} == {"query"}


# ── With a database ──────────────────────────────────────────────────────

_BIGGEST_RUN_ID = 2**53 - 1


async def test_a_well_formed_request_that_names_nothing_that_is_there_is_a_404_with_a_code_never_a_500(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "there")
    nothing = [
        f"/runs/{_BIGGEST_RUN_ID}/trades",
        f"/runs/{_BIGGEST_RUN_ID}/trades.csv",
        f"/runs/{_BIGGEST_RUN_ID}/sessions/{WED_OPEN_MS}",
        f"/runs/{run_id}/sessions/0",
        f"/runs/{run_id}/sessions/1",
        f"/runs/{run_id}/sessions/{MAX_TIMESTAMP_MS}",
        f"/runs/{run_id}/sessions/{WED_OPEN_MS + 1}",
    ]

    for path in nothing:
        response = await client.get(f"{PREFIX}{path}")
        assert response.status_code == 404, (path, response.text)
        assert {"code", "message"} <= set(response.json()["detail"]), path
    for session in (0, 1, MAX_TIMESTAMP_MS):
        response = await client.get(f"{PREFIX}/runs/{run_id}/trades", params={"session_open_ms": session})
        assert response.status_code == 404, (session, response.text)
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_SESSION_NOT_IN_RUN"
    for a, b, role in ((_BIGGEST_RUN_ID, run_id, "run a"), (run_id, _BIGGEST_RUN_ID, "run b")):
        response = await client.get(f"{PREFIX}/runs/compare", params={"a": a, "b": b})
        assert response.status_code == 404 and role in response.json()["detail"]["message"], response.text


async def test_an_offset_past_the_last_trade_or_as_large_as_a_run_id_can_be_is_an_empty_page_and_the_biggest_limit_is_one_page(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "paging")

    async def page(**params: int) -> dict[str, Any]:
        response = await client.get(f"{PREFIX}/runs/{run_id}/trades", params=params)
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    # The run has three trades.
    for offset in (3, 4, 1_000_000, _BIGGEST_RUN_ID):
        past = await page(offset=offset)
        assert (past["total"], past["trades"], past["next_offset"], past["offset"]) == (3, [], None, offset)
    biggest = await page(limit=5000, offset=_BIGGEST_RUN_ID)
    assert (biggest["total"], biggest["trades"], biggest["next_offset"]) == (3, [], None)
    assert [t["trade_number"] for t in (await page(limit=5000))["trades"]] == [0, 1, 2]
    one = await page(limit=1, offset=2)
    assert ([t["trade_number"] for t in one["trades"]], one["next_offset"]) == ([2], None)


async def test_a_run_compared_with_itself_and_a_query_with_more_than_it_needs_are_answered(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "itself")

    itself = await client.get(f"{PREFIX}/runs/compare", params={"a": run_id, "b": run_id})
    extra = await client.get(f"{PREFIX}/runs/compare", params={"a": run_id, "b": run_id, "c": "3"})

    assert itself.status_code == 200 and extra.status_code == 200, (itself.text, extra.text)
    assert itself.json()["setting_differences"] == [] and itself.json()["overlap"]["shared_sessions"] == 3


async def test_a_leg_id_a_setup_could_hold_is_a_404_for_a_leg_the_run_lacks_and_for_a_run_that_lost_its_setup_a_page_of_what_was_stored(
    client: httpx.AsyncClient, conn: asyncpg.Connection, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "legs")

    readable = await client.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "nope"})
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        run_id,
    )
    lost = await client.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "nope"})
    lost_nul = await client.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "no\x00pe"})

    assert readable.status_code == 404 and readable.json()["detail"]["code"] == "OPTIONS_BACKTEST_LEG_NOT_IN_RUN"
    # With the setup gone the run cannot say which legs it had: nothing stored is nothing stored. And a NUL in a leg id is
    # refused before it can reach the database, which would refuse the text.
    assert lost.status_code == 200 and (lost.json()["total"], lost.json()["trades"]) == (0, [])
    assert lost_nul.status_code == 422, lost_nul.text


async def test_no_odd_request_against_a_run_that_is_there_gives_a_5xx(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "odd")
    failed = await save_run(scene, "odd-failed", end="failed")
    odd = [
        f"/runs/{run_id}/trades/",
        f"/runs/{run_id}/trades.csv/",
        f"/runs/{run_id}/sessions/",
        f"/runs/{run_id}/sessions",
        f"/runs/{run_id}//trades",
        f"/runs/{run_id}/trades%2F",
        f"/runs/{run_id}/%2e%2e/trades",
        "/runs/compare/trades",
        "/runs/compare/trades.csv",
        f"/runs/{run_id}/sessions/{WED_OPEN_MS}/extra",
        f"/runs/{run_id}/sessions/{WED_OPEN_MS}?limit=0",
        f"/runs/{failed}/trades?leg_id=nope&session_open_ms=5",
        f"/runs/{failed}/sessions/{THU_OPEN_MS}",
        f"/runs/{failed}/trades.csv",
        f"/runs/{run_id}/trades?limit=5000&offset=0&session_open_ms={WED_OPEN_MS}&leg_id=call",
    ]

    for path in odd:
        response = await client.get(f"{PREFIX}{path}")
        assert response.status_code < 500, (path, response.status_code, response.text[:200])
