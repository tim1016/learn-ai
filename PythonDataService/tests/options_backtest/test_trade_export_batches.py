"""The export of a run's trades is written a batch at a time to a spooled temporary file, and is the bytes it always was (#3030).

No database: ``run_detail.export_every_trade`` runs over a fake connection (``_fake_connection.py``) whose cursor hands
out stored trade rows ``n`` at a time and says whether a transaction was open. A run at the limits holds 218,400 trades,
so an export must read them a batch at a time, keep a few megabytes of the file in memory and the rest on disk, and give
the file only when every row is in it. Hand-worked below: the batches a number of trades make, and the bytes.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest

from app.options_backtest import run_detail
from app.options_backtest.run_detail import RunNotComplete, TradesExport, export_every_trade
from app.options_backtest.saved_runs import StoredTrade
from app.options_backtest.trade_export import CSV_HEADER, _row, trades_csv
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._fake_connection import FakeConnection, a_run_row, a_trade_row, many_trade_rows
from tests.options_backtest.test_trade_export import HEADER_LINE, THE_TWO_TRADES_CSV, _marked, _sold


def _as_it_was_built(trades: list[StoredTrade]) -> bytes:
    """``trades_csv`` as it was written before the export was batched: the whole file in one writer, then encoded."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    writer.writerows(_row(stored) for stored in trades)
    return buffer.getvalue().encode("utf-8")


def _the_file(export: TradesExport) -> bytes:
    data = export.spool.read()
    export.close()
    assert len(data) == export.size
    return data


def _seven() -> list[StoredTrade]:
    """Seven trades, a sold one and a marked one by turns, numbered 0 to 6."""
    return [replace(_sold() if at % 2 == 0 else _marked(), trade_number=at) for at in range(7)]


def _rows_of(trades: list[StoredTrade]) -> list[dict[str, object]]:
    return [a_trade_row(each) for each in trades]


@pytest.fixture
def spool_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A folder of this test's own, where a spooled file past its memory would be made (the suite puts others in tmp_path)."""
    folder = tmp_path / "spool"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture
def spools(monkeypatch: pytest.MonkeyPatch, spool_dir: Path) -> list[tempfile.SpooledTemporaryFile[bytes]]:
    """Every spooled file the export makes, so a test can ask if it is closed."""
    made: list[tempfile.SpooledTemporaryFile[bytes]] = []
    real = tempfile.SpooledTemporaryFile

    class Spied(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", Spied)
    return made


# ── The bytes ────────────────────────────────────────────────────────────


async def test_the_two_trades_of_the_trade_export_test_are_the_same_literal_through_the_cursor() -> None:
    fake = FakeConnection([a_run_row(7)], trade_rows=_rows_of([_sold(), _marked()]))

    export = await export_every_trade(fake, 7, batch_rows=1)

    # The literal ``test_trade_export.py`` holds, cell for cell, header first, CRLF row ends, no byte-order mark.
    data = _the_file(export)
    assert data == THE_TWO_TRADES_CSV.encode("utf-8")
    assert data.startswith(HEADER_LINE.encode("utf-8") + b"\r\n") and not data.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("batch_rows", [1, 2, 3, 7, 8, 1000])
async def test_the_file_is_the_bytes_the_whole_in_memory_export_always_made_whatever_the_size_of_a_batch(
    batch_rows: int,
) -> None:
    trades = _seven()
    fake = FakeConnection([a_run_row(7)], trade_rows=_rows_of(trades))

    export = await export_every_trade(fake, 7, batch_rows=batch_rows)

    data = _the_file(export)
    assert data == _as_it_was_built(trades) == trades_csv(trades)
    assert data.count(b"\r\n") == 1 + 7


async def test_the_busy_runs_trades_are_the_same_bytes_in_batches_of_one_and_of_five_and_whole() -> None:
    busy = a_busy_run()
    trades = [
        StoredTrade(s.session_open_ms, at, trade) for s in busy.sessions for at, trade in enumerate(s.outcome.trades)
    ]
    assert len(trades) > 10, "the busy run holds more than two batches of five, or this proves too little"
    files = []
    for batch_rows in (1, 5, len(trades)):
        fake = FakeConnection([a_run_row(7)], trade_rows=_rows_of(trades))
        files.append(_the_file(await export_every_trade(fake, 7, batch_rows=batch_rows)))

    assert files[0] == files[1] == files[2] == _as_it_was_built(trades)


async def test_a_run_that_made_no_trade_is_the_header_alone() -> None:
    export = await export_every_trade(FakeConnection([a_run_row(7)]), 7)

    assert _the_file(export) == HEADER_LINE.encode("utf-8") + b"\r\n"


# ── How it is read ───────────────────────────────────────────────────────


async def test_seven_trades_in_batches_of_three_are_three_batches_and_an_empty_one_in_one_repeatable_read_transaction() -> (
    None
):
    fake = FakeConnection([a_run_row(7)], trade_rows=_rows_of(_seven()))

    export = await export_every_trade(fake, 7, batch_rows=3)

    export.close()
    [cursor] = fake.cursors
    # 7 = 3 + 3 + 1: three fetches give rows, and a fourth finds none and ends it. No fetch is for more than a batch.
    assert (cursor.asked, cursor.gave) == ([3, 3, 3, 3], [3, 3, 1, 0])
    # One transaction, repeatable-read and read-only, and the run's header, the cursor and every fetch ran inside it.
    assert fake.transactions == [("repeatable_read", True)]
    assert (
        fake.log
        == [("research_options_backtest_runs", 1)]
        + [("research_options_backtest_run_trades", 1)]
        + [("cursor fetch", 1)] * 4
    )


async def test_a_batch_of_less_than_one_row_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        await export_every_trade(FakeConnection([a_run_row(7)]), 7, batch_rows=0)


# ── A file that is never half of one, and never left open ───────────────


async def test_a_failure_in_the_second_batch_is_an_error_and_the_file_is_closed(
    spools: list[tempfile.SpooledTemporaryFile[bytes]],
) -> None:
    # Batches of 3 from 7 trades: the first is read and written, the second fetch fails.
    fake = FakeConnection([a_run_row(7)], trade_rows=_rows_of(_seven()), fail_at_fetch=2)

    with pytest.raises(ConnectionError, match="lost"):
        await export_every_trade(fake, 7, batch_rows=3)

    [spool] = spools
    assert spool.closed
    assert fake.cursors[0].asked == [3, 3], "the second fetch was the one that failed"
    assert not fake.in_transaction


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
async def test_a_run_that_is_not_complete_is_refused_before_a_row_is_read_and_the_file_is_closed(
    status: str, spools: list[tempfile.SpooledTemporaryFile[bytes]]
) -> None:
    fake = FakeConnection([a_run_row(7, status)], trade_rows=_rows_of(_seven()))

    with pytest.raises(RunNotComplete) as caught:
        await export_every_trade(fake, 7)

    assert (caught.value.status, caught.value.job_id) == (status, "job-7")
    assert fake.cursors == []
    [spool] = spools
    assert spool.closed


async def test_a_run_that_does_not_exist_is_refused_and_the_file_is_closed(
    spools: list[tempfile.SpooledTemporaryFile[bytes]],
) -> None:
    with pytest.raises(run_detail.RunNotFound):
        await export_every_trade(FakeConnection(), 7)

    assert [spool.closed for spool in spools] == [True]


async def test_the_file_is_closed_when_its_last_piece_has_been_given(
    spools: list[tempfile.SpooledTemporaryFile[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_detail, "EXPORT_SEND_BYTES", 1000)
    trades = _seven()
    export = await export_every_trade(FakeConnection([a_run_row(7)], trade_rows=_rows_of(trades)), 7)
    [spool] = spools
    assert not spool.closed

    pieces = [piece async for piece in export.chunks()]

    # The 7 rows and the header are 1,616 bytes or so: two pieces of 1,000 at most, and together the file.
    assert [len(piece) for piece in pieces] == [1000, export.size - 1000]
    assert b"".join(pieces) == _as_it_was_built(trades)
    assert spool.closed


async def test_the_file_is_closed_when_the_reader_stops_before_the_end(
    spools: list[tempfile.SpooledTemporaryFile[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_detail, "EXPORT_SEND_BYTES", 1000)
    export = await export_every_trade(FakeConnection([a_run_row(7)], trade_rows=_rows_of(_seven())), 7)
    pieces = export.chunks()

    await pieces.__anext__()
    assert not spools[0].closed
    await pieces.aclose()

    assert spools[0].closed


# ── Past the memory it is allowed, the file is on disk, and leaves nothing ──


def _open_descriptors() -> set[str]:
    return set(os.listdir("/dev/fd"))


async def test_a_file_past_the_memory_it_is_allowed_goes_to_disk_whole_and_leaves_nothing_behind(
    spools: list[tempfile.SpooledTemporaryFile[bytes]], spool_dir: Path
) -> None:
    trades = _seven()

    # Eight rows and the header are some 1,600 bytes; the file may keep 500 of them in memory.
    export = await export_every_trade(FakeConnection([a_run_row(7)], trade_rows=_rows_of(trades)), 7, memory_bytes=500)

    [spool] = spools
    assert spool._rolled, "past 500 bytes the file is on disk (this is the one place a test asks)"
    on_disk = spool._file
    assert str(on_disk.fileno()) in _open_descriptors(), "a file on disk holds a descriptor until it is closed"
    assert _the_file(export) == _as_it_was_built(trades)
    # The file never had a name in the temporary folder, and once the export has closed it the descriptor is let go.
    assert list(spool_dir.iterdir()) == []
    assert spool.closed and on_disk.closed


async def test_a_file_that_fits_in_memory_never_touches_the_disk(
    spools: list[tempfile.SpooledTemporaryFile[bytes]], spool_dir: Path
) -> None:
    export = await export_every_trade(FakeConnection([a_run_row(7)], trade_rows=_rows_of(_seven())), 7)

    [spool] = spools
    assert not spool._rolled
    assert list(spool_dir.iterdir()) == []
    export.close()
    assert spool.closed


# ── The peak of memory ───────────────────────────────────────────────────


@pytest.mark.slow
async def test_the_export_of_fifty_thousand_trades_never_holds_more_than_a_batch_and_the_part_of_the_file_kept_in_memory() -> (
    None
):
    # What the export may hold at one time, worked out. The file keeps 4 MiB = 4,194,304 bytes in memory and the rest on
    # disk (a growing in-memory buffer may over-allocate by an eighth: at most 4.7 million). A batch is 1,000 rows, each a
    # stored row, the trade decoded from it and its CSV text, all dropped before the next batch is read; measured with
    # tracemalloc on CPython 3.12 a batch is 2.1 million bytes. Together 4.7 + 2.1 = 6.8 million at the very most, and
    # 6.3 million is what was measured. The bound is 9 million, 1.4 times that.
    # What it must catch: if every row were read at once the peak would be 80 million; if the file were kept whole in
    # memory instead of spooled it would be 13.1 million (the file is 11.2 million, and a batch is on top of it).
    # The same at the limit of 218,400 trades is a file of 49 million, and the same bound.
    bound = 9_000_000
    rows = 50_000
    fake = FakeConnection([a_run_row(7)], trade_rows=lambda: many_trade_rows(_sold(), rows))

    tracemalloc.start()
    try:
        export = await export_every_trade(fake, 7)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert peak < bound, f"the export held {peak:,} bytes at one time"
    # And it is the whole file: the header and 50,000 rows, the last one numbered 49,999 (trade_number is the fourth cell).
    lines = _the_file(export).split(b"\r\n")
    assert len(lines) == 1 + rows + 1 and lines[-1] == b""
    assert lines[-2].split(b",")[3] == b"49999"
