"""One session of a saved run, over HTTP (#3030): its stored facts, and the series, levels and fills of the one-day timeline.

Saved runs live in a real database, so these run only against one attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. The run is made over a lake in a temporary folder
(``_detail_scene.py``, whose docstring works the figures out), so the pins are the hashes of real stored files and the
route reads the same files back. A minute is "m5": the bar closing five minutes after the open.

A series that cannot be shown says why in words of its own (held by ``test_session_series.py``). A file the operating system
refuses is ``unreadable`` as a damaged one is, with the stored facts and the other series served and no path in the answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import lake_data
from app.options_backtest.results import EventKind, Fill, Mark
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._detail_scene import (
    CALL_500,
    FRI_CALL_500,
    FRI_OPEN_MS,
    FRI_PUT_495,
    FRI_PUT_500,
    PUT_495,
    PUT_500,
    SESSIONS,
    THU_OPEN_MS,
    WED_OPEN_MS,
    run_the_scene,
    the_scene,
    the_setup,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import option_day_zip, raw_root, replace_the_stored_file
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._refused_days import REFUSED_DAYS, RefusedDay
from tests.options_backtest._run_data import HALF_DAY, Day, fake_data, steady
from tests.options_backtest._stored_runs import PREFIX, SaveRun, no_date_in
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import setup_body
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


def _minute_of(at_ms: int, open_ms: int) -> int:
    return (at_ms - open_ms) // MS_PER_MINUTE


async def _get(client: httpx.AsyncClient, run_id: int, open_ms: int) -> httpx.Response:
    return await client.get(f"{PREFIX}/runs/{run_id}/sessions/{open_ms}")


def _series_minutes(series: dict[str, Any], open_ms: int) -> list[int]:
    return [_minute_of(point["at_ms"], open_ms) for point in series["points"]]


# ── The busy Wednesday ───────────────────────────────────────────────────


@respx.mock
async def test_the_busy_session_is_served_with_its_stored_facts_in_order_and_every_figure_the_scene_works_out(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "busy")

    response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["run_id"], body["session_open_ms"], body["session_close_ms"]) == (
        run_id,
        WED_OPEN_MS,
        WED_OPEN_MS + 390 * MS_PER_MINUTE,
    )
    # 10,000.00 less 8.70 - 121.30 - 31.30 = -143.90 is 9,856.10.
    assert (body["status"], body["skip_reason"]) == ("traded", None)
    assert (D(body["cash_before_usd"]), D(body["cash_after_usd"]), D(body["profit_usd"])) == (
        D("10000"),
        D("9856.10"),
        D("-143.90"),
    )
    assert [(t["trade_number"], t["leg_id"], t["entry_number"], D(t["pnl_usd"])) for t in body["trades"]] == [
        (0, "put", 0, D("8.70")),
        (1, "call", 0, D("-121.30")),
        (2, "call", 1, D("-31.30")),
    ]

    def event(e: dict[str, Any]) -> tuple[Any, ...]:
        price = None if e["price_usd"] is None else D(e["price_usd"])
        return (
            e["kind"],
            _minute_of(e["at_ms"], WED_OPEN_MS),
            e["leg_id"],
            e["entry_number"],
            e["contract"],
            price,
            e["reason"],
        )

    # In the order they happened, worked from the scene (see its docstring).
    assert [event(e) for e in body["events"]] == [
        ("picked", 5, "call", 0, CALL_500, None, None),
        ("starting_price_taken", 5, "call", 0, CALL_500, D("4.00"), None),
        ("picked", 5, "put", 0, PUT_500, None, None),
        ("picked", 5, "lag", 0, PUT_495, None, None),
        ("starting_price_taken", 5, "lag", 0, PUT_495, D("1.00"), None),
        ("entry_filled", 6, "put", 0, PUT_500, D("2.00"), None),
        ("momentum_hit", 10, "call", 0, CALL_500, D("5.00"), None),
        ("entry_filled", 11, "call", 0, CALL_500, D("5.10"), None),
        ("stop_hit", 20, "call", 0, CALL_500, D("4.00"), None),
        ("re_entry_earned", 20, "call", 1, None, None, None),
        ("close_filled", 21, "call", 0, CALL_500, D("3.90"), None),
        ("picked", 21, "call", 1, CALL_500, None, None),
        ("starting_price_taken", 21, "call", 1, CALL_500, D("3.80"), None),
        ("momentum_hit", 30, "call", 1, CALL_500, D("4.80"), None),
        ("entry_filled", 31, "call", 1, CALL_500, D("4.85"), None),
        ("exit_time", 60, "call", 1, CALL_500, None, None),
        ("exit_time", 60, "put", 0, PUT_500, None, None),
        ("leg_cancelled", 60, "lag", 0, None, None, "exit_time"),
        ("close_filled", 61, "call", 1, CALL_500, D("4.55"), None),
        ("close_filled", 61, "put", 0, PUT_500, D("2.10"), None),
    ]
    no_date_in(body)


@respx.mock
async def test_the_busy_session_draws_its_lines_and_marks_its_fills_where_the_scene_puts_them(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "lines")

    body = (await _get(client, run_id, WED_OPEN_MS)).json()

    def line(each: dict[str, Any]) -> tuple[Any, ...]:
        return (
            each["kind"],
            each["leg_id"],
            each["entry_number"],
            each["contract"],
            D(each["price_usd"]),
            _minute_of(each["from_ms"], WED_OPEN_MS),
            _minute_of(each["to_ms"], WED_OPEN_MS),
        )

    assert [line(each) for each in body["levels"]] == [
        ("starting_price", "call", 0, CALL_500, D("4.00"), 5, 10),
        ("momentum", "call", 0, CALL_500, D("5.00"), 5, 10),
        ("starting_price", "lag", 0, PUT_495, D("1.00"), 5, 60),
        ("momentum", "lag", 0, PUT_495, D("0.90"), 5, 60),
        ("stop", "call", 0, CALL_500, D("4.08"), 11, 20),
        ("starting_price", "call", 1, CALL_500, D("3.80"), 21, 30),
        ("momentum", "call", 1, CALL_500, D("4.75"), 21, 30),
        ("stop", "call", 1, CALL_500, D("3.88"), 31, 60),
    ]
    assert [
        (
            f["kind"],
            f["leg_id"],
            f["entry_number"],
            _minute_of(f["bar_close_ms"], WED_OPEN_MS),
            D(f["seen_usd"]),
            D(f["filled_usd"]),
            f["exit_reason"],
        )
        for f in body["fills"]
    ] == [
        ("entry", "put", 0, 6, D("2.00"), D("2.00"), None),
        ("entry", "call", 0, 11, D("5.10"), D("5.10"), None),
        ("exit", "call", 0, 21, D("3.90"), D("3.90"), "stop"),
        ("entry", "call", 1, 31, D("4.85"), D("4.85"), None),
        ("exit", "put", 0, 61, D("2.10"), D("2.10"), "exit_time"),
        ("exit", "call", 1, 61, D("4.55"), D("4.55"), "exit_time"),
    ]
    assert body["marks"] == []


@respx.mock
async def test_the_series_are_the_lakes_closes_with_no_point_where_nothing_traded_and_none_outside_the_session(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "series")

    body = (await _get(client, run_id, WED_OPEN_MS)).json()

    underlying = body["underlying"]
    assert (underlying["symbol"], underlying["contract"], underlying["state"]) == ("SPY", None, "shown")
    assert (underlying["reason"], underlying["message"]) == (None, None)
    assert _series_minutes(underlying, WED_OPEN_MS) == list(range(1, 391))
    assert {D(point["close_usd"]) for point in underlying["points"]} == {D("500.20")}
    assert [each["symbol"] for each in body["contracts"]] == [CALL_500, PUT_495, PUT_500]
    call, lag, put = body["contracts"]
    # No bar at m40 and m41; nothing is filled in.
    assert _series_minutes(call, WED_OPEN_MS) == [m for m in range(5, 62) if m not in (40, 41)]
    assert _series_minutes(put, WED_OPEN_MS) == list(range(5, 62))
    # The 495 put has bars closing at the open and after the close in the lake; the timeline starts at m5 and ends at m60.
    assert _series_minutes(lag, WED_OPEN_MS) == list(range(5, 61))
    assert call["contract"] == {
        "symbol": CALL_500,
        "underlying": "SPY",
        "right": "call",
        "strike_usd": "500",
        "expiry_ms": WED_OPEN_MS + 390 * MS_PER_MINUTE,
    }
    closes = {_minute_of(p["at_ms"], WED_OPEN_MS): D(p["close_usd"]) for p in call["points"]}
    assert (closes[5], closes[10], closes[20], closes[30], closes[61]) == (
        D("4.00"),
        D("5.00"),
        D("4.00"),
        D("4.80"),
        D("4.50"),
    )
    assert all(type(p["close_usd"]) is str and type(p["at_ms"]) is int for p in call["points"])


# ── A pin that does not hold ─────────────────────────────────────────────


@respx.mock
async def test_a_series_whose_file_was_deleted_is_not_served_with_its_reason_and_the_rest_of_the_session_still_is(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "gone")
    (raw_root(tmp_lake) / "option/usa/minute/spy/spy261007c00500000/20261007_trade.zip").unlink()

    response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    body = response.json()
    call = next(each for each in body["contracts"] if each["symbol"] == CALL_500)
    assert (call["state"], call["reason"], call["points"]) == ("not_shown", "gone", [])
    assert call["message"] == "The stored bars are no longer in the lake."
    assert call["contract"]["symbol"] == CALL_500
    # The other series are served, and so is everything stored: trades, events, levels, fills.
    others = [each for each in body["contracts"] if each["symbol"] != CALL_500]
    assert all(each["state"] == "shown" and each["points"] for each in others)
    assert body["underlying"]["state"] == "shown"
    assert (len(body["trades"]), len(body["events"]), len(body["levels"]), len(body["fills"])) == (3, 20, 8, 6)
    no_date_in(body)


@respx.mock
async def test_a_file_the_catalog_no_longer_admits_cannot_be_confirmed_and_is_not_said_to_have_changed(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "unconfirmed")
    (raw_root(tmp_lake) / "equity/usa/minute/spy/20261007_trade.zip").write_bytes(b"changed after its receipt")

    body = (await _get(client, run_id, WED_OPEN_MS)).json()

    # Nobody saw the file change: the catalog holds no receipt for its bytes, so they cannot be checked against what the
    # run read. The words say so, and say nothing of a change.
    assert (body["underlying"]["state"], body["underlying"]["reason"], body["underlying"]["points"]) == (
        "not_shown",
        "not_confirmed",
        [],
    )
    assert body["underlying"]["message"] == "The stored bars cannot be confirmed against what this run read."
    assert all(each["state"] == "shown" for each in body["contracts"])


@respx.mock
async def test_a_file_that_is_another_file_than_the_run_read_is_changed_in_the_words_of_a_change(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "changed")
    put = OptionContract.from_occ_symbol(PUT_500)
    # The catalog is given a receipt for the new bytes, so they read cleanly: only the run's pin tells them from the old.
    replace_the_stored_file(
        fake_catalog,
        raw_root(tmp_lake),
        "option/usa/minute/spy/spy261007p00500000/20261007_trade.zip",
        option_day_zip(put, [WED_OPEN_MS + 5 * MS_PER_MINUTE], day=date(2026, 10, 7)),
        admit=True,
    )

    body = (await _get(client, run_id, WED_OPEN_MS)).json()

    changed = next(each for each in body["contracts"] if each["symbol"] == PUT_500)
    assert (changed["state"], changed["reason"], changed["points"]) == ("not_shown", "changed", [])
    assert changed["message"] == "The stored bars have changed since this run."


@contextmanager
def _refused(path: Path) -> Iterator[None]:
    """``path`` (a file or a folder) with no permission at all; its permissions come back afterwards."""
    before = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        yield
    finally:
        path.chmod(before)


def _served_whole_but_for(body: dict[str, Any], unreadable: str) -> None:
    """Everything stored is served, and every series but ``unreadable`` is shown; that one says why not."""
    assert (len(body["trades"]), len(body["events"]), len(body["levels"]), len(body["fills"]), len(body["marks"])) == (
        3,
        20,
        8,
        6,
        0,
    )
    assert [each["symbol"] for each in body["contracts"]] == [CALL_500, PUT_495, PUT_500]
    for series in [body["underlying"], *body["contracts"]]:
        if series["symbol"] == unreadable:
            assert (series["state"], series["reason"], series["points"]) == ("not_shown", "unreadable", [])
            assert series["message"] == "The stored bars cannot be read now."
        else:
            assert series["state"] == "shown" and series["points"]


@respx.mock
async def test_a_contract_file_the_operating_system_refuses_is_an_unreadable_series_and_the_session_is_still_served(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "refused-file")
    file = raw_root(tmp_lake) / "option/usa/minute/spy/spy261007c00500000/20261007_trade.zip"

    with _refused(file):
        with pytest.raises(PermissionError):
            file.read_bytes()
        response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    _served_whole_but_for(response.json(), CALL_500)
    # No server path reaches the page.
    assert str(tmp_lake) not in response.text and ".zip" not in response.text


@respx.mock
async def test_a_contract_folder_the_operating_system_refuses_is_an_unreadable_series_and_the_session_is_still_served(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "refused-folder")
    folder = raw_root(tmp_lake) / "option/usa/minute/spy/spy261007p00500000"

    with _refused(folder):
        with pytest.raises(PermissionError):
            (folder / "20261007_trade.zip").read_bytes()
        response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    _served_whole_but_for(response.json(), PUT_500)
    assert str(tmp_lake) not in response.text


@respx.mock
async def test_a_contract_read_that_fails_with_an_operating_system_error_is_an_unreadable_series_with_no_path_in_the_answer(
    client: httpx.AsyncClient,
    save_run: SaveRun,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await save_run(await run_the_scene(), "refused-read")
    real = lake_data.read_option_day_bars

    def refuse_one(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        if contract.occ_symbol == PUT_495:
            raise PermissionError(13, "Permission denied", "/a/server/path/lake/raw/option/usa/minute/spy/x.zip")
        return real(contract, *args, **kwargs)

    monkeypatch.setattr(lake_data, "read_option_day_bars", refuse_one)

    response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    _served_whole_but_for(response.json(), PUT_495)
    assert "/a/server/path" not in response.text and "Permission denied" not in response.text


@pytest.mark.parametrize("day", REFUSED_DAYS)
@respx.mock
async def test_a_stored_day_the_lake_reader_refuses_is_an_unreadable_series_and_the_stored_facts_are_served_with_no_path(
    day: RefusedDay,
    client: httpx.AsyncClient,
    save_run: SaveRun,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    run_id = await save_run(await run_the_scene(), "refused-day")
    # The catalog is given a receipt for the bytes, so they read cleanly as far as the catalog goes; ``_refused_days.py``
    # works out what is wrong with each.
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), day.file, day.payload, admit=True)

    response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    _served_whole_but_for(response.json(), day.symbol)
    # The page gets the reason's fixed words: nothing of the file's place on the server's disk, nor of the lake's own text.
    assert str(tmp_lake) not in response.text and ".zip" not in response.text
    assert "cannot be decoded" not in response.text and "cannot be placed" not in response.text


# ── Sessions that did not trade ──────────────────────────────────────────


@respx.mock
async def test_a_session_skipped_for_want_of_an_underlying_price_answers_with_its_facts_and_the_underlyings_series(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "skipped")

    body = (await _get(client, run_id, THU_OPEN_MS)).json()

    # The cash stays at Wednesday's end. Thursday has no bar at m5, so 389 points and no point at m5.
    assert (body["status"], body["skip_reason"]) == ("skipped", "underlying_did_not_trade")
    assert (D(body["cash_before_usd"]), D(body["cash_after_usd"]), D(body["profit_usd"])) == (
        D("9856.10"),
        D("9856.10"),
        D("0"),
    )
    assert (body["trades"], body["events"], body["levels"], body["fills"], body["marks"], body["contracts"]) == (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    assert body["underlying"]["state"] == "shown"
    assert _series_minutes(body["underlying"], THU_OPEN_MS) == [m for m in range(1, 391) if m != 5]


@respx.mock
async def test_a_session_that_made_no_trade_still_has_its_lines_and_the_series_of_the_contracts_that_took_a_price(
    client: httpx.AsyncClient, save_run: SaveRun, fake_catalog: FakeCatalog, tmp_lake: Path, is_the_job_alive: Any
) -> None:
    run_id = await save_run(await run_the_scene(), "quiet")

    body = (await _get(client, run_id, FRI_OPEN_MS)).json()

    assert body["status"] == "no_trade" and D(body["profit_usd"]) == 0
    assert body["trades"] == [] and body["fills"] == [] and body["marks"] == []
    assert [(e["kind"], _minute_of(e["at_ms"], FRI_OPEN_MS), e["leg_id"]) for e in body["events"]] == [
        ("picked", 5, "call"),
        ("starting_price_taken", 5, "call"),
        ("picked", 5, "put"),
        ("picked", 5, "lag"),
        ("starting_price_taken", 5, "lag"),
        ("entry_dropped", 6, "put"),
        ("leg_cancelled", 60, "call"),
        ("leg_cancelled", 60, "lag"),
    ]
    # Worked from the setup: 4.00 x 1.25 = 5.00 for the call, 1.00 x 0.9 = 0.90 for the lag leg, both to the exit time.
    assert [(each["kind"], each["leg_id"], D(each["price_usd"])) for each in body["levels"]] == [
        ("starting_price", "call", D("4.00")),
        ("momentum", "call", D("5.00")),
        ("starting_price", "lag", D("1.00")),
        ("momentum", "lag", D("0.90")),
    ]
    # The put 500 was picked and its buy dropped, so it has no series; the other two took a starting price.
    assert [each["symbol"] for each in body["contracts"]] == [FRI_CALL_500, FRI_PUT_495]
    assert FRI_PUT_500 not in [each["symbol"] for each in body["contracts"]]


async def test_a_session_skipped_on_its_window_read_nothing_so_it_has_no_series_to_check_and_serves_none(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: Any
) -> None:
    # Entry 190 minutes after the open and exit 30 before the close: 190 to 360 on a full day, 190 to 180 on a half day.
    setup = OptionsBacktestSetup.model_validate(
        setup_body(
            entry={"anchor": "open", "offset_ms": 190 * MS_PER_MINUTE},
            exit={"anchor": "close", "offset_ms": 30 * MS_PER_MINUTE},
        )
    )
    half_day: date = HALF_DAY
    result = run_sessions(setup, [half_day], fake_data({half_day: Day(underlying=steady("500.20"))}))
    run_id = await save_run(result, "window")
    opened = session_window_for_date(half_day).open_ms_utc

    body = (await _get(client, run_id, opened)).json()

    assert (body["status"], body["skip_reason"]) == ("skipped", "window_does_not_fit")
    assert body["session_close_ms"] - body["session_open_ms"] == 210 * MS_PER_MINUTE
    # The session was skipped, so no bars were read for it: the words say that, and not that a record is missing.
    assert (body["underlying"]["state"], body["underlying"]["reason"]) == ("not_shown", "session_skipped")
    assert body["underlying"]["message"] == "This session was skipped, so no bars were read for it."
    assert body["underlying"]["points"] == [] and body["contracts"] == []


# ── What is stored does not depend on the lake ───────────────────────────


async def test_the_stored_facts_marks_and_words_are_served_as_stored_when_the_lake_holds_nothing(
    client: httpx.AsyncClient,
    save_run: SaveRun,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    busy: RunResult = a_busy_run()
    run_id = await save_run(busy, "stored")
    incomplete = next(s for s in busy.sessions if any(isinstance(t.exit, Mark) for t in s.outcome.trades))
    refused = next(s for s in busy.sessions if any(e.detail for e in s.outcome.events))

    marked = (await _get(client, run_id, incomplete.session_open_ms)).json()
    pick_refused = (await _get(client, run_id, refused.session_open_ms)).json()

    # No file is in the lake, so every series is gone and none has a point; the stored facts do not need the lake.
    for body in (marked, pick_refused):
        assert body["underlying"]["reason"] == "gone"
        assert all(each["reason"] == "gone" and each["points"] == [] for each in body["contracts"])
    # Every price seen and price filled, entries and sales, is the saved trade's: slippage made them different.
    saved_trades = incomplete.outcome.trades
    assert sorted(
        (f["kind"], f["leg_id"], f["entry_number"], D(f["seen_usd"]), D(f["filled_usd"])) for f in marked["fills"]
    ) == sorted(
        [("entry", t.leg_id, t.entry_number, t.entry.seen, t.entry.filled) for t in saved_trades]
        + [
            ("exit", t.leg_id, t.entry_number, t.exit.seen, t.exit.filled)
            for t in saved_trades
            if isinstance(t.exit, Fill)
        ]
    )
    assert any(t.entry.seen != t.entry.filled for t in saved_trades), (
        "the busy run has slippage: seen and filled differ"
    )
    saved_marks = [t for t in incomplete.outcome.trades if isinstance(t.exit, Mark)]
    assert saved_marks and [
        (m["leg_id"], m["entry_number"], m["contract"], m["bar_close_ms"], D(m["price_usd"])) for m in marked["marks"]
    ] == [(t.leg_id, t.entry_number, t.contract.occ_symbol, t.exit.bar_close_ms, t.exit.price) for t in saved_marks]
    assert marked["status"] == "incomplete"
    assert [f["kind"] for f in marked["fills"]].count("exit") == len(marked["trades"]) - len(saved_marks)
    # The words of a refused pick are the backend's prose for the owner, stored as written and served as written. They name
    # the day looked for, and the date stays in them; the rest of the body names none.
    [words] = [e["detail"] for e in pick_refused["events"] if e["detail"]]
    [stored_words] = [e.detail for e in refused.outcome.events if e.detail]
    assert words == stored_words
    assert re.search(r"listed for \d{4}-\d{2}-\d{2} with the underlying at 499.07$", words), words
    no_date_in(marked)
    no_date_in(pick_refused)


async def test_a_stored_session_whose_trades_and_events_disagree_is_a_500_with_a_code_and_the_rest_of_the_run_still_opens(
    client: httpx.AsyncClient,
    save_run: SaveRun,
    scene: RunResult,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    friday = scene.sessions[2]
    # The lag leg took a starting price and waited; without its cancellation it never ended, which the replay cannot do.
    cancelled = next(e for e in friday.outcome.events if e.kind is EventKind.LEG_CANCELLED and e.leg_id == "lag")
    events = tuple(e for e in friday.outcome.events if e is not cancelled)
    broken = replace(
        scene, sessions=(*scene.sessions[:2], replace(friday, outcome=replace(friday.outcome, events=events)))
    )
    run_id = await save_run(broken, "disagree")

    response = await _get(client, run_id, FRI_OPEN_MS)
    other = await _get(client, run_id, THU_OPEN_MS)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT"
    assert "lag" in response.json()["detail"]["message"]
    assert other.status_code == 200


async def test_a_stored_starting_price_taken_on_another_contract_than_its_trade_is_the_same_coded_500(
    client: httpx.AsyncClient,
    save_run: SaveRun,
    scene: RunResult,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    wednesday = scene.sessions[0]
    # The call leg's first starting price is 4.00 on the 500 call, and so is its trade's. Hand-edited to name the 500 put
    # at the same price, the two agree on the price and not on the contract.
    first = next(
        e
        for e in wednesday.outcome.events
        if e.kind is EventKind.STARTING_PRICE_TAKEN and e.leg_id == "call" and e.entry_number == 0
    )
    events = tuple(
        replace(e, contract=OptionContract.from_occ_symbol(PUT_500)) if e is first else e
        for e in wednesday.outcome.events
    )
    broken = replace(
        scene, sessions=(replace(wednesday, outcome=replace(wednesday.outcome, events=events)), *scene.sessions[1:])
    )
    run_id = await save_run(broken, "other-contract")

    response = await _get(client, run_id, WED_OPEN_MS)
    other = await _get(client, run_id, THU_OPEN_MS)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT"
    assert "leg 'call', entry 0" in response.json()["detail"]["message"]
    assert "different contract" in response.json()["detail"]["message"]
    assert other.status_code == 200


async def test_a_stored_fill_event_on_another_contract_than_its_trade_is_the_same_coded_500(
    client: httpx.AsyncClient,
    save_run: SaveRun,
    scene: RunResult,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    wednesday = scene.sessions[0]
    # The call leg's first entry filled on the 500 call. Hand-edited to name the 500 put, the event and its trade agree
    # on the leg and the entry number and on nothing else (#3086).
    filled = next(
        e
        for e in wednesday.outcome.events
        if e.kind is EventKind.ENTRY_FILLED and e.leg_id == "call" and e.entry_number == 0
    )
    events = tuple(
        replace(e, contract=OptionContract.from_occ_symbol(PUT_500)) if e is filled else e
        for e in wednesday.outcome.events
    )
    broken = replace(
        scene, sessions=(replace(wednesday, outcome=replace(wednesday.outcome, events=events)), *scene.sessions[1:])
    )
    run_id = await save_run(broken, "fill-on-another-contract")

    response = await _get(client, run_id, WED_OPEN_MS)
    other = await _get(client, run_id, THU_OPEN_MS)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT"
    assert "leg 'call', entry 0" in response.json()["detail"]["message"]
    assert "entry_filled" in response.json()["detail"]["message"]
    assert other.status_code == 200


async def test_a_stored_setup_edited_into_another_valid_one_without_a_leg_that_waited_is_the_coded_500_and_not_a_bare_one(
    client: httpx.AsyncClient,
    conn: asyncpg.Connection,
    save_run: SaveRun,
    scene: RunResult,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    run_id = await save_run(scene, "renamed-leg")
    # The setup's legs are call (0), put (1) and lag (2). Renaming the third makes another valid setup, with no leg called
    # "lag", while the stored trades and events still name it: Wednesday's lag leg took a starting price and never filled.
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = jsonb_set(setup_json, '{legs,2,leg_id}', '\"lag2\"') "
        "WHERE id = $1",
        run_id,
    )

    response = await _get(client, run_id, WED_OPEN_MS)
    other = await _get(client, run_id, THU_OPEN_MS)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT"
    assert "lag" in response.json()["detail"]["message"]
    # Thursday was skipped and took no starting price, so nothing in it names the leg: it opens.
    assert other.status_code == 200


@respx.mock
async def test_a_session_of_a_run_whose_stored_setup_no_longer_validates_is_served_with_its_series_and_says_why_beside_it(
    client: httpx.AsyncClient,
    conn: asyncpg.Connection,
    save_run: SaveRun,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    is_the_job_alive: Any,
) -> None:
    run_id = await save_run(await run_the_scene(), "lost-setup")
    before = (await _get(client, run_id, WED_OPEN_MS)).json()
    await conn.execute(
        "UPDATE research_options_backtest_runs SET setup_json = setup_json || '{\"lots\": 1}'::jsonb WHERE id = $1",
        run_id,
    )

    response = await _get(client, run_id, WED_OPEN_MS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert before["setup_cannot_open_reason"] is None
    assert body["setup_cannot_open_reason"].startswith("lots: ")
    # Everything stored is as it was, and so is every series: the underlying is read from the run's own record, not from
    # the setup. Eight lines become seven: the lag leg took 1.00 and never filled, and its momentum level (0.90) comes from
    # a rule that is lost. Every other line is a stored figure.
    for name in ("status", "cash_before_usd", "cash_after_usd", "profit_usd", "trades", "events", "fills", "marks"):
        assert body[name] == before[name], name
    assert body["underlying"] == before["underlying"] and body["contracts"] == before["contracts"]
    assert [(each["kind"], each["leg_id"]) for each in before["levels"] if each not in body["levels"]] == [
        ("momentum", "lag")
    ]
    assert len(body["levels"]) == 7


# ── What it refuses ──────────────────────────────────────────────────────


async def test_a_session_the_run_did_not_test_is_a_404_and_so_is_a_run_that_does_not_exist(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: Any
) -> None:
    run_id = await save_run(run_sessions(*_the_scene_args()), "refused")

    not_tested = await _get(client, run_id, WED_OPEN_MS + MS_PER_MINUTE)
    no_run = await _get(client, 2**52, WED_OPEN_MS)

    assert not_tested.status_code == 404
    assert not_tested.json()["detail"]["code"] == "OPTIONS_BACKTEST_SESSION_NOT_IN_RUN"
    assert no_run.status_code == 404 and no_run.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_FOUND"


def _the_scene_args() -> tuple[Any, ...]:
    return the_setup(), SESSIONS, fake_data(the_scene())


async def test_a_run_that_is_not_complete_has_no_session_to_show_and_is_a_409_before_anything_is_looked_for(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: dict[str, bool | None]
) -> None:
    result = run_sessions(*_the_scene_args())
    running = await save_run(result, "running", end="running")
    failed = await save_run(result, "failed", end="failed")
    is_the_job_alive["live"] = True

    from_running = await _get(client, running, WED_OPEN_MS + 1)
    from_failed = await _get(client, failed, WED_OPEN_MS + 1)

    for response, status in ((from_running, "running"), (from_failed, "failed")):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"
        assert status in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "run_id, session",
    [(0, 1), (-1, 1), ("abc", 1), (1, -1), (1, MAX_TIMESTAMP_MS + 1), (1, "x"), (2**53, 1)],
)
async def test_a_request_that_cannot_be_one_is_a_422(
    client: httpx.AsyncClient, run_id: object, session: object
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/runs/{run_id}/sessions/{session}")

    assert response.status_code == 422, response.text
