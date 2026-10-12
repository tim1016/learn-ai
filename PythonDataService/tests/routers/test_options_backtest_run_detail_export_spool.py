"""The export route sends a file that is whole, with the headers it always had, and closes it (#3030).

No database: the real route, ``run_detail`` and ``saved_runs`` run over a fake connection (``_fake_connection.py``) that
hands out stored trade rows through a cursor. What is held here is what the owner's download is: the same bytes and
headers as before the export was batched, an error answer and never a 200 with half a file when a batch fails, and a
temporary file that is closed whichever way the request ends.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.options_backtest import run_detail
from app.options_backtest.saved_runs import StoredTrade
from app.routers.options_backtest_run_detail import export_the_trades
from tests.options_backtest._fake_connection import FakeConnection, Pool, a_run_row, a_trade_row
from tests.options_backtest._stored_runs import PREFIX
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest.test_trade_export import THE_TWO_TRADES_CSV, _marked, _sold


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch) -> list[tempfile.SpooledTemporaryFile[bytes]]:
    """Every spooled file the export makes, so a test can ask if it is closed."""
    made: list[tempfile.SpooledTemporaryFile[bytes]] = []
    real = tempfile.SpooledTemporaryFile

    class Spied(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", Spied)
    return made


def _store(monkeypatch: pytest.MonkeyPatch, trades: list[StoredTrade], **fake: object) -> FakeConnection:
    connection = FakeConnection([a_run_row(7)], trade_rows=[a_trade_row(each) for each in trades], **fake)  # type: ignore[arg-type]
    monkeypatch.setattr(run_detail, "with_connection", Pool(connection))
    return connection


THE_TWO = [_sold(), _marked()]


@pytest.mark.parametrize("memory_bytes", [run_detail.EXPORT_MEMORY_BYTES, 100], ids=["in memory", "on disk"])
async def test_the_download_has_the_headers_and_the_bytes_it_always_had_whether_the_file_is_in_memory_or_on_disk(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, memory_bytes: int
) -> None:
    monkeypatch.setattr(run_detail, "EXPORT_MEMORY_BYTES", memory_bytes)
    connection = _store(monkeypatch, THE_TWO)
    monkeypatch.setattr(run_detail, "EXPORT_BATCH_ROWS", 1)

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/7/trades.csv")

    assert response.status_code == 200
    # The headers the route has always given: a CSV in UTF-8, saved under a name that names the run, and its length.
    assert dict(response.headers) == {
        "content-disposition": 'attachment; filename="options-backtest-run-7-trades.csv"',
        "content-length": str(len(THE_TWO_TRADES_CSV.encode("utf-8"))),
        "content-type": "text/csv; charset=utf-8",
    }
    assert response.content == THE_TWO_TRADES_CSV.encode("utf-8")
    # Two trades, a batch of one row: two fetches that give a row and a third that finds none.
    assert connection.cursors[0].gave == [1, 1, 0]


async def test_a_failure_in_the_second_batch_is_an_error_answer_and_never_a_200_with_half_a_file(
    monkeypatch: pytest.MonkeyPatch, spools: list[tempfile.SpooledTemporaryFile[bytes]]
) -> None:
    trades = [replace(_sold() if at % 2 == 0 else _marked(), trade_number=at) for at in range(5)]
    connection = _store(monkeypatch, trades, fail_at_fetch=2)
    monkeypatch.setattr(run_detail, "EXPORT_BATCH_ROWS", 2)
    # The app's own answer to an error, as a client gets it, and not the error raised into the test.
    client = httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/7/trades.csv")

    assert response.status_code == 500
    assert "text/csv" not in response.headers["content-type"] and "content-disposition" not in response.headers
    # Not one row of the first batch, which had been written, and not the header.
    assert b"unix_ts" not in response.content and b"SPY2610" not in response.content
    assert connection.cursors[0].asked == [2, 2], "the first batch was read and written, and the second fetch failed"
    [spool] = spools
    assert spool.closed


async def test_the_temporary_file_is_closed_once_the_download_is_over(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, spools: list[tempfile.SpooledTemporaryFile[bytes]]
) -> None:
    _store(monkeypatch, THE_TWO)

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/7/trades.csv")

    assert response.status_code == 200 and response.content == THE_TWO_TRADES_CSV.encode("utf-8")
    [spool] = spools
    assert spool.closed


async def test_a_run_that_is_not_complete_is_a_409_that_names_what_it_is_and_the_temporary_file_is_closed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, spools: list[tempfile.SpooledTemporaryFile[bytes]]
) -> None:
    connection = FakeConnection([a_run_row(7, "failed")])
    monkeypatch.setattr(run_detail, "with_connection", Pool(connection))

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/7/trades.csv")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "OPTIONS_BACKTEST_RUN_NOT_COMPLETE",
        "message": "Run 7 is failed. Only a complete run has trades and sessions.",
    }
    assert connection.cursors == []
    assert [spool.closed for spool in spools] == [True]


async def test_the_temporary_file_is_closed_even_when_the_client_is_gone_before_the_download_has_begun(
    monkeypatch: pytest.MonkeyPatch, spools: list[tempfile.SpooledTemporaryFile[bytes]]
) -> None:
    _store(monkeypatch, THE_TWO)
    response = await export_the_trades(7)
    blocked = asyncio.Event()

    async def gone() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        # The connection never takes the start of the answer, so the body is never asked for and the pieces' own end,
        # which closes the file after the last of them, never runs.
        await blocked.wait()

    await response({"type": "http"}, gone, send)

    [spool] = spools
    assert spool.closed
