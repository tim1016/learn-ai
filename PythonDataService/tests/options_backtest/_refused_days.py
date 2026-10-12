"""Stored days of the busy Wednesday that the lake reader refuses, each by the error it is refused with (#3030).

One table for the series test and the route test of a saved session. A refused day makes its own series ``unreadable``
and nothing else: the stored facts and every other series are still served. The four kinds of fault the lake reader
(``lake_data.py``) and the lake's own parser of an option day now tell apart:

* the underlying holds a bar that starts off a whole minute: the stock reader accepts it and the reader refuses to place
  it (``UnderlyingBarsCannotBePlaced``, a kind of ``UnderlyingDayUnreadable``);
* the underlying holds a row the stock reader would skip, a row of five columns or an empty line, so the day would read
  as a good, shorter one (``UnderlyingDayUnreadable`` itself);
* a contract's day holds a bar that starts off a whole minute: the lake's own parser of a stored day refuses it before
  the reader looks (``OptionBarsFileError``);
* a contract's day holds a bar that starts at 23:59 New York, so it closes on the next New York date: the lake's parser
  accepts it and the reader refuses to place it (``OptionBarsCannotBePlaced``, a kind of ``OptionBarsFileError``).

Hand-worked. Wednesday 2026-10-07 opens at 1_791_379_800_000 ms (09:30 New York, 13:30 UTC) and New York is on UTC-4, so
23:59 is 14 h 29 min after the open, 869 minutes. A stored stock row is the time in ms after New York midnight, then four
prices in ten-thousandths of a dollar and the volume: 09:30 is 34_200_000 ms and 500.20 is 5_002_000. The scene's
underlying closes at 500.20 all day, so the good row below is the scene's first minute.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

import pytest

from app.options.contract import OptionContract
from tests.options_backtest._detail_scene import PUT_500, WED_OPEN_MS
from tests.options_backtest._lake import option_day_zip, stock_bar, stock_day_zip
from tests.options_backtest._run_data import WED

STOCK_FILE = "equity/usa/minute/spy/20261007_trade.zip"
PUT_500_FILE = "option/usa/minute/spy/spy261007p00500000/20261007_trade.zip"

_MINUTE = 60_000
_GOOD_ROW = "34200000,5002000,5002000,5002000,5002000,100"
_NEXT_ROW = "34260000,5002000,5002000,5002000,5002000,100"
_NEXT_ROW_OF_FIVE_COLUMNS = "34260000,5002000,5002000,5002000,5002000"


@dataclass(frozen=True)
class RefusedDay:
    """Bytes to put in place of a captured file, and what the reader refuses them with."""

    # The series that is withheld: the underlying's ticker or the contract's OCC symbol.
    symbol: str
    # The file's path under the lake's raw root.
    file: str
    payload: bytes
    # The name of the reader's error class, which the log of an unreadable series carries.
    error: str


def _stock_file_of(rows: list[str]) -> bytes:
    """The stock file as the lake writes it (one member named for the day) but with exactly these lines."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("20261007_spy_minute_trade.csv", "\n".join(rows).encode("ascii"))
    return buffer.getvalue()


def _put_500_with_bars_starting_at(*offsets_from_the_open_ms: int) -> bytes:
    put = OptionContract.from_occ_symbol(PUT_500)
    return option_day_zip(put, [WED_OPEN_MS + offset for offset in offsets_from_the_open_ms], day=WED)


REFUSED_DAYS = [
    pytest.param(
        RefusedDay(
            "SPY",
            STOCK_FILE,
            # Starts 09:31:00 and 09:32:30; the second closes at 09:33:30, which is off a whole minute.
            stock_day_zip(
                [
                    stock_bar(WED_OPEN_MS + _MINUTE, "500.20", "500.20", "500.20", "500.20"),
                    stock_bar(WED_OPEN_MS + 2 * _MINUTE + 30_000, "500.20", "500.20", "500.20", "500.20"),
                ],
                day=WED,
            ),
            "UnderlyingBarsCannotBePlaced",
        ),
        id="the underlying holds a bar off a whole minute",
    ),
    pytest.param(
        RefusedDay(
            "SPY",
            STOCK_FILE,
            # The stock reader drops the second line (five columns, not six) and reads the day as one bar: 1 of 2 rows fail.
            _stock_file_of([_GOOD_ROW, _NEXT_ROW_OF_FIVE_COLUMNS]),
            "UnderlyingDayUnreadable",
        ),
        id="the underlying holds a row of five columns",
    ),
    pytest.param(
        RefusedDay(
            "SPY",
            STOCK_FILE,
            # The stock reader drops the empty line and reads two bars: 1 of 3 lines fail.
            _stock_file_of([_GOOD_ROW, "", _NEXT_ROW]),
            "UnderlyingDayUnreadable",
        ),
        id="the underlying holds an empty line",
    ),
    pytest.param(
        RefusedDay(
            PUT_500,
            PUT_500_FILE,
            # Starts at the open plus 5 minutes (m5) and at the open plus 6 minutes and 30 seconds.
            _put_500_with_bars_starting_at(5 * _MINUTE, 6 * _MINUTE + 30_000),
            "OptionBarsFileError",
        ),
        id="a contract's day holds a bar off a whole minute",
    ),
    pytest.param(
        RefusedDay(
            PUT_500,
            PUT_500_FILE,
            # Starts at m5 and at 23:59 New York, 869 minutes after the open; that bar closes at midnight, on the 8th.
            _put_500_with_bars_starting_at(5 * _MINUTE, 869 * _MINUTE),
            "OptionBarsCannotBePlaced",
        ),
        id="a contract's day holds a bar closing on the next New York date",
    ),
]
