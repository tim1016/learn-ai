"""The CSV of a saved run's leg trades (#3030): a fixed header, and every cell as the owner reads it.

The trades are written by hand with the scale of every figure chosen on purpose, so the expected text is what was saved.
Hand-worked times, all in New York wall-clock time (EDT is UTC-4 and EST is UTC-5):

* The session opens at 13:30 UTC = 09:30 EDT on 2026-10-07 = 1791379800000 ms. The first trade's entry bar closes 11 minutes
  later, 660000 ms, at 13:41 UTC = 09:41:00 EDT; the marked trade's entry bar closes 6 minutes after the open, at 09:36:00.
  The 16:00 New York anchor of the same day is 6 hours 30 minutes after the open: 23400000 ms, 1791403200000.
* 2026-12-01 15:00 UTC is 10:00:00 EST, 1796137200000 ms.
* Daylight time began on Sunday 2026-03-08, so 14:31 UTC on Friday the 6th is 09:31:00 EST (1772807460000) and
  13:31 UTC on Monday the 9th is 09:31:00 EDT (1773063060000). It ended on Sunday 2026-11-01, so 13:31 UTC on Friday
  2026-10-30 is 09:31:00 EDT (1793367060000) and 14:31 UTC on Monday 2026-11-02 is 09:31:00 EST (1793629860000).
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.options.contract import OptionContract
from app.options_backtest.results import ExitReason, Fill, LegTrade, Mark
from app.options_backtest.saved_runs import StoredTrade
from app.options_backtest.trade_export import CSV_HEADER, csv_file_name, trades_csv

D = Decimal
OPEN_MS = 1_791_379_800_000
CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 7), right="call", strike=D("500.5"))
PUT = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 7), right="put", strike=500)

HEADER_LINE = (
    "unix_ts,time_america_new_york,session_open_ms,trade_number,leg_id,entry_number,contract,underlying,right,strike_usd,"
    "expiry_ms,contracts,multiplier,starting_price_usd,momentum_level_usd,stop_level_usd,entry_decided_at_ms,"
    "entry_seen_usd,entry_filled_usd,exit_reason,exit_group,exit_decided_at_ms,exit_kind,exit_bar_close_ms,"
    "exit_seen_usd,exit_price_usd,entry_fee_usd,exit_fee_usd,pnl_usd"
)


def _sold() -> StoredTrade:
    return StoredTrade(
        OPEN_MS,
        2,
        LegTrade(
            leg_id="call",
            entry_number=1,
            contract=CALL,
            contracts=2,
            multiplier=100,
            starting_price=D("4.00"),
            momentum_level=D("5.0000"),
            stop_level=D("4.080"),
            entry_decided_at_ms=OPEN_MS + 600_000,
            entry=Fill("call", CALL, "buy", 2, 100, OPEN_MS + 660_000, D("5.0"), D("5.02")),
            exit_reason=ExitReason.STOP,
            exit_group=3,
            exit_decided_at_ms=OPEN_MS + 1_200_000,
            exit=Fill("call", CALL, "sell", 2, 100, OPEN_MS + 1_260_000, D("3.9"), D("3.88")),
            entry_fee_usd=D("1.30"),
            exit_fee_usd=D("1.30"),
            pnl_usd=D("-230.60"),
        ),
    )


def _marked() -> StoredTrade:
    return StoredTrade(
        OPEN_MS,
        0,
        LegTrade(
            leg_id="put",
            entry_number=0,
            contract=PUT,
            contracts=1,
            multiplier=100,
            starting_price=None,
            momentum_level=None,
            stop_level=None,
            entry_decided_at_ms=OPEN_MS + 300_000,
            entry=Fill("put", PUT, "buy", 1, 100, OPEN_MS + 360_000, D("2.00"), D("2.00")),
            exit_reason=ExitReason.MARKED_NOT_SOLD,
            exit_group=1,
            exit_decided_at_ms=OPEN_MS + 3_600_000,
            exit=Mark(OPEN_MS + 6_000_000, D("1.75")),
            entry_fee_usd=D("0.65"),
            exit_fee_usd=D("0"),
            pnl_usd=D("-25.65"),
        ),
    )


def _rows(payload: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(payload.decode("utf-8"))))


def test_the_header_is_fixed_and_begins_with_the_unix_time_and_one_readable_time_named_for_its_zone() -> None:
    assert ",".join(CSV_HEADER) == HEADER_LINE
    assert CSV_HEADER[:2] == ("unix_ts", "time_america_new_york")
    assert len(CSV_HEADER) == len(set(CSV_HEADER)) == 29
    # Every other instant is a whole number of milliseconds in a column that says so, and money says dollars.
    assert [name for name in CSV_HEADER if name.endswith("_ms")] == [
        "session_open_ms",
        "expiry_ms",
        "entry_decided_at_ms",
        "exit_decided_at_ms",
        "exit_bar_close_ms",
    ]
    assert not [name for name in CSV_HEADER if "time" in name and name not in ("unix_ts", "time_america_new_york")]


def test_a_sold_trade_and_a_marked_one_are_written_cell_for_cell_as_they_were_saved() -> None:
    payload = trades_csv([_sold(), _marked()]).decode("utf-8")

    assert (
        payload
        == (
            HEADER_LINE
            + "\r\n"
            # The entry bar closed at 13:41 UTC = 09:41:00 EDT. 500.5 is the strike and 1791403200000 the 16:00 anchor.
            + "1791380460000,2026-10-07 09:41:00,1791379800000,2,call,1,SPY261007C00500500,SPY,call,500.5,1791403200000,"
            "2,100,4.00,5.0000,4.080,1791380400000,5.0,5.02,stop,3,1791381000000,sale,1791381060000,3.9,3.88,"
            "1.30,1.30,-230.60"
            + "\r\n"
            # No rule: three empty cells. A mark has no sale: its seen price is empty and its price is the mark.
            + "1791380160000,2026-10-07 09:36:00,1791379800000,0,put,0,SPY261007P00500000,SPY,put,500,1791403200000,"
            "1,100,,,,1791380100000,2.00,2.00,marked_not_sold,1,1791383400000,mark,1791385800000,,1.75,"
            "0.65,0,-25.65" + "\r\n"
        )
    )


def test_a_run_with_no_trade_is_the_header_alone() -> None:
    assert trades_csv([]).decode("utf-8") == HEADER_LINE + "\r\n"


def test_every_row_has_one_cell_for_every_column_and_the_first_is_the_entry_fills_bar_close() -> None:
    rows = _rows(trades_csv([_sold(), _marked(), _sold()]))

    assert rows[0] == list(CSV_HEADER)
    assert [len(row) for row in rows] == [29, 29, 29, 29]
    assert [int(row[0]) for row in rows[1:]] == [OPEN_MS + 660_000, OPEN_MS + 360_000, OPEN_MS + 660_000]


@pytest.mark.parametrize(
    ("unix_ts", "readable"),
    [
        (1_796_137_200_000, "2026-12-01 10:00:00"),
        (1_772_807_460_000, "2026-03-06 09:31:00"),
        (1_773_063_060_000, "2026-03-09 09:31:00"),
        (1_793_367_060_000, "2026-10-30 09:31:00"),
        (1_793_629_860_000, "2026-11-02 09:31:00"),
    ],
)
def test_the_readable_time_follows_new_york_across_a_clock_change_and_not_a_fixed_offset(
    unix_ts: int, readable: str
) -> None:
    stored = _marked()
    moved = replace(stored, trade=replace(stored.trade, entry=replace(stored.trade.entry, bar_close_ms=unix_ts)))

    [_, row] = _rows(trades_csv([moved]))

    assert (row[0], row[1]) == (str(unix_ts), readable)


def test_a_figure_is_plain_digits_whatever_form_the_decimal_holds_it_in() -> None:
    stored = _sold()
    odd = replace(
        stored,
        trade=replace(
            stored.trade,
            starting_price=D("1E+1"),
            momentum_level=D("1E-10"),
            stop_level=D("0E-7"),
            pnl_usd=D("-1.5E+3"),
        ),
    )

    [_, row] = _rows(trades_csv([odd]))

    named = dict(zip(CSV_HEADER, row, strict=True))
    assert (named["starting_price_usd"], named["momentum_level_usd"], named["stop_level_usd"], named["pnl_usd"]) == (
        "10",
        "0.0000000001",
        "0.0000000",
        "-1500",
    )


def test_the_file_is_named_for_the_run() -> None:
    assert csv_file_name(12) == "options-backtest-run-12-trades.csv"
