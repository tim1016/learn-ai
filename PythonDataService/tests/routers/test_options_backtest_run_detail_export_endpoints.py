"""The CSV export of a saved run's leg trades over HTTP (#3030).

Saved runs live in a real database, so these run only against one attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. The scene's three trades are worked by hand in
``tests/options_backtest/_detail_scene.py``; the cells are checked against them. A second run of 4,000 trades
(``_big_run.py``) is exported to prove the export is whole. The cell text itself, scale for scale, is pinned in
``tests/options_backtest/test_trade_export.py``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.options_backtest.results import Fill
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.trade_export import CSV_HEADER
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._big_run import a_big_run
from tests.options_backtest._detail_scene import (
    CALL_500,
    PUT_500,
    SESSIONS,
    WED_OPEN_MS,
    minute,
    the_scene,
    the_setup,
)
from tests.options_backtest._run_data import FRI, fake_data
from tests.options_backtest._stored_runs import PREFIX, SaveRun
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


async def _export(client: httpx.AsyncClient, run_id: int) -> httpx.Response:
    return await client.get(f"{PREFIX}/runs/{run_id}/trades.csv")


def _table(response: httpx.Response) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
    assert rows[0] == list(CSV_HEADER)
    return [dict(zip(rows[0], row, strict=True)) for row in rows[1:]]


def _worked(row: dict[str, str]) -> dict[str, Any]:
    """A row as the scene's docstring writes a trade: whole numbers as ints, prices as Decimals, empty as None."""
    whole = {"unix_ts", "session_open_ms", "trade_number", "entry_number", "expiry_ms", "contracts", "multiplier"}
    whole |= {"entry_decided_at_ms", "exit_group", "exit_decided_at_ms", "exit_bar_close_ms"}
    return {
        name: (None if cell == "" else int(cell) if name in whole else D(cell) if name.endswith("_usd") else cell)
        for name, cell in row.items()
    }


# ── The scene, cell by cell ──────────────────────────────────────────────


async def test_the_export_is_a_csv_download_named_for_the_run_with_a_row_for_every_trade_as_the_scene_works_it_out(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "scene")

    response = await _export(client, run_id)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0] == "text/csv"
    assert response.headers["content-disposition"] == f'attachment; filename="options-backtest-run-{run_id}-trades.csv"'
    expiry_ms = WED_OPEN_MS + 390 * 60_000
    common = {"session_open_ms": WED_OPEN_MS, "underlying": "SPY", "strike_usd": D("500"), "expiry_ms": expiry_ms}
    common |= {
        "contracts": 1,
        "multiplier": 100,
        "exit_kind": "sale",
        "entry_fee_usd": D("0.65"),
        "exit_fee_usd": D("0.65"),
    }
    assert [_worked(row) for row in _table(response)] == [
        {
            **common,
            "unix_ts": minute(WED_OPEN_MS, 6),
            "time_america_new_york": "2026-10-07 09:36:00",
            "trade_number": 0,
            "leg_id": "put",
            "entry_number": 0,
            "contract": PUT_500,
            "right": "put",
            "starting_price_usd": None,
            "momentum_level_usd": None,
            "stop_level_usd": None,
            "entry_decided_at_ms": minute(WED_OPEN_MS, 5),
            "entry_seen_usd": D("2.00"),
            "entry_filled_usd": D("2.00"),
            "exit_reason": "exit_time",
            "exit_group": 2,
            "exit_decided_at_ms": minute(WED_OPEN_MS, 60),
            "exit_bar_close_ms": minute(WED_OPEN_MS, 61),
            "exit_seen_usd": D("2.10"),
            "exit_price_usd": D("2.10"),
            "pnl_usd": D("8.70"),
        },
        {
            **common,
            "unix_ts": minute(WED_OPEN_MS, 11),
            "time_america_new_york": "2026-10-07 09:41:00",
            "trade_number": 1,
            "leg_id": "call",
            "entry_number": 0,
            "contract": CALL_500,
            "right": "call",
            "starting_price_usd": D("4.00"),
            "momentum_level_usd": D("5.00"),
            "stop_level_usd": D("4.08"),
            "entry_decided_at_ms": minute(WED_OPEN_MS, 10),
            "entry_seen_usd": D("5.10"),
            "entry_filled_usd": D("5.10"),
            "exit_reason": "stop",
            "exit_group": 1,
            "exit_decided_at_ms": minute(WED_OPEN_MS, 20),
            "exit_bar_close_ms": minute(WED_OPEN_MS, 21),
            "exit_seen_usd": D("3.90"),
            "exit_price_usd": D("3.90"),
            "pnl_usd": D("-121.30"),
        },
        {
            **common,
            "unix_ts": minute(WED_OPEN_MS, 31),
            "time_america_new_york": "2026-10-07 10:01:00",
            "trade_number": 2,
            "leg_id": "call",
            "entry_number": 1,
            "contract": CALL_500,
            "right": "call",
            "starting_price_usd": D("3.80"),
            "momentum_level_usd": D("4.75"),
            "stop_level_usd": D("3.88"),
            "entry_decided_at_ms": minute(WED_OPEN_MS, 30),
            "entry_seen_usd": D("4.85"),
            "entry_filled_usd": D("4.85"),
            "exit_reason": "exit_time",
            "exit_group": 2,
            "exit_decided_at_ms": minute(WED_OPEN_MS, 60),
            "exit_bar_close_ms": minute(WED_OPEN_MS, 61),
            "exit_seen_usd": D("4.55"),
            "exit_price_usd": D("4.55"),
            "pnl_usd": D("-31.30"),
        },
    ]


async def test_a_run_that_made_no_trade_exports_the_header_and_nothing_else(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: Any
) -> None:
    quiet = run_sessions(the_setup(), [FRI], fake_data({FRI: the_scene()[FRI]}))
    run_id = await save_run(quiet, "quiet")

    response = await _export(client, run_id)

    assert response.status_code == 200
    assert response.content.decode("utf-8") == ",".join(CSV_HEADER) + "\r\n"


async def test_a_leg_marked_and_not_sold_has_no_seen_price_and_its_mark_as_the_exit_price(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: Any
) -> None:
    busy = a_busy_run()
    run_id = await save_run(busy, "busy")
    saved = [trade for session in busy.sessions for trade in session.outcome.trades]

    table = _table(await _export(client, run_id))

    assert len(table) == len(saved)
    for row, trade in zip(table, saved, strict=True):
        marked = not isinstance(trade.exit, Fill)
        assert (row["exit_kind"], row["exit_seen_usd"] == "") == (("mark", True) if marked else ("sale", False))
        assert D(row["exit_price_usd"]) == trade.exit_price and D(row["pnl_usd"]) == trade.pnl_usd
        # The busy run has slippage, so what was seen and what was filled differ and a swap of the two is seen.
        assert (D(row["entry_seen_usd"]), D(row["entry_filled_usd"])) == (trade.entry.seen, trade.entry.filled)
        if not marked:
            assert D(row["exit_seen_usd"]) == trade.exit.seen
        assert int(row["unix_ts"]) == trade.entry.bar_close_ms


# ── Whole, however many ──────────────────────────────────────────────────


async def test_a_run_of_four_thousand_trades_is_exported_whole_in_order_and_adds_up(
    client: httpx.AsyncClient, save_run: SaveRun, is_the_job_alive: Any
) -> None:
    big = a_big_run(sessions=40, per_session=100)
    run_id = await save_run(big, "big")
    saved = [(s.session_open_ms, at, trade) for s in big.sessions for at, trade in enumerate(s.outcome.trades)]
    assert len(saved) == 4000

    response = await _export(client, run_id)
    paged = (await client.get(f"{PREFIX}/runs/{run_id}/trades", params={"limit": 1})).json()

    assert response.status_code == 200
    table = _table(response)
    # Every trade, once, in the order the run recorded them; none cut off at a page.
    assert paged["total"] == len(table) == 4000
    assert [(int(row["session_open_ms"]), int(row["trade_number"])) for row in table] == [
        (at, number) for at, number, _ in saved
    ]
    assert [int(row["unix_ts"]) for row in table] == [trade.entry.bar_close_ms for _, _, trade in saved]
    # The sum of the cells is the run's net profit, to the cent: exact, not close.
    assert sum((D(row["pnl_usd"]) for row in table), start=D("0")) == big.summary.net_profit_usd
    # The readable time is the New York time of the first column, in every row.
    for row in table:
        assert row["time_america_new_york"] == datetime.fromtimestamp(int(row["unix_ts"]) / 1000, tz=NEW_YORK).strftime(
            "%Y-%m-%d %H:%M:%S"
        )


# ── What it refuses ──────────────────────────────────────────────────────


async def test_a_run_that_does_not_exist_is_a_404_and_one_that_is_not_complete_is_a_409(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: dict[str, bool | None]
) -> None:
    failed = await save_run(scene, "failed", end="failed")
    cancelled = await save_run(scene, "cancelled", end="cancelled")

    missing = await _export(client, 2**52)
    from_failed = await _export(client, failed)
    from_cancelled = await _export(client, cancelled)

    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_FOUND"
    for response, status in ((from_failed, "failed"), (from_cancelled, "cancelled")):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"
        assert status in response.json()["detail"]["message"]
        assert "text/csv" not in response.headers["content-type"]


@pytest.mark.parametrize("run_id", ["0", "-1", "abc", str(2**53)])
async def test_a_run_id_that_cannot_be_one_is_a_422(client: httpx.AsyncClient, run_id: str) -> None:
    response = await client.get(f"{PREFIX}/runs/{run_id}/trades.csv")

    assert response.status_code == 422
