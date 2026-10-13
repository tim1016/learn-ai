"""A saved run's leg trades as a CSV an owner can read in a spreadsheet and audit trades with (#3030).

Formula: none. Every cell is a stored figure written out; nothing is calculated, rounded or filled in.
Reference: ``.claude/rules/temporal-rigor.md`` "Readable time in exported CSVs": ``unix_ts`` first and always, and one
  display-only wall-clock column beside it named for its zone; ``dataset_service.build_csv_bytes``, the repo's other
  export, whose zone naming and wall-clock format are used here and not copied.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_trade_export.py (the cells), tests/options_backtest/test_trade_export_batches.py
  (the pieces joined are the whole).

The header is fixed. One row per leg trade, in the order the trades route lists them, and never a page: the caller hands
over every trade of the run and this writes them all. The bytes can be made whole (``trades_csv``) or in pieces, the
header (``header_bytes``) and then the rows of each batch of trades (``rows_bytes``); the pieces joined are the whole,
byte for byte, because a CSV writer keeps nothing between one row and the next. The export route writes the pieces, a
batch at a time, so that a run of any size is never held whole (``run_detail.export_every_trade``).

* ``unix_ts`` is the entry fill's bar close in ``int64 ms UTC``. ``time_america_new_york`` is that instant as New York
  wall-clock text (``YYYY-MM-DD HH:MM:SS``), for reading; nothing is parsed back from it.
* Every other instant is an integer in a column ending ``_ms``.
* Money and prices are exact decimal text in plain digits (never an exponent, never through a float), and a column ending
  ``_usd`` is dollars. A figure a trade does not have is an empty cell.
* ``exit_kind`` is ``sale``, ``buy_back`` or ``mark``, and it is the only thing that tells a leg that was closed from
  one that was not. A sale (a bought leg's) or a buy-back (a sold leg's) has the bar it filled in
  (``exit_bar_close_ms``), the price seen in that bar (``exit_seen_usd``), the price it filled at (``exit_price_usd``)
  and its fee (``exit_fee_usd``). A leg marked was never closed: its ``exit_price_usd`` is the mark (the close of the
  last bar its contract traded in), ``exit_seen_usd`` is empty, and ``exit_fee_usd`` is ``0`` because no order was
  made. So a sum of ``exit_price_usd`` over every row adds sale prices to buy-back prices to marks: add the rows of one
  ``exit_kind`` at a time.
* ``side`` is the last column (#3109): ``buy`` for a leg that opened by a purchase and ``sell`` for one that opened by a
  sale, the side of the entry fill. It says how the rest of the row reads: a ``sell`` leg's ``entry_*`` columns are a
  sale and its ``exit_*`` columns a buy-back, and a ``sell`` leg whose ``exit_kind`` is ``mark`` is owed. A run stored
  before sold legs has it too, ``buy`` on every row.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal

from app.options_backtest.results import Fill
from app.options_backtest.saved_runs import StoredTrade
from app.services.dataset_service import READABLE_TIME_FORMAT, time_column_name
from app.utils.timestamps import ny_datetime

# The zone the readable column is written in, and so the zone it is named for.
READABLE_ZONE = "America/New_York"

CSV_HEADER: tuple[str, ...] = (
    "unix_ts",
    time_column_name(READABLE_ZONE),
    "session_open_ms",
    "trade_number",
    "leg_id",
    "entry_number",
    "contract",
    "underlying",
    "right",
    "strike_usd",
    "expiry_ms",
    "contracts",
    "multiplier",
    "starting_price_usd",
    "momentum_level_usd",
    "stop_level_usd",
    "entry_decided_at_ms",
    "entry_seen_usd",
    "entry_filled_usd",
    "exit_reason",
    "exit_group",
    "exit_decided_at_ms",
    "exit_kind",
    "exit_bar_close_ms",
    "exit_seen_usd",
    "exit_price_usd",
    "entry_fee_usd",
    "exit_fee_usd",
    "pnl_usd",
    "side",
)


def csv_file_name(run_id: int) -> str:
    """The file a run's export is saved as: named for the run."""
    return f"options-backtest-run-{run_id}-trades.csv"


def _text(value: Decimal | None) -> str:
    """A figure as plain digits, or an empty cell for none."""
    return "" if value is None else format(value, "f")


def _row(stored: StoredTrade) -> list[str | int]:
    trade, entry, leaving = stored.trade, stored.trade.entry, stored.trade.exit
    contract = trade.contract
    sale = leaving if isinstance(leaving, Fill) else None
    return [
        entry.bar_close_ms,
        ny_datetime(entry.bar_close_ms).strftime(READABLE_TIME_FORMAT),
        stored.session_open_ms,
        stored.trade_number,
        trade.leg_id,
        trade.entry_number,
        contract.occ_symbol,
        contract.root,
        contract.right,
        _text(contract.strike),
        contract.expiry_ms,
        trade.contracts,
        trade.multiplier,
        _text(trade.starting_price),
        _text(trade.momentum_level),
        _text(trade.stop_level),
        trade.entry_decided_at_ms,
        _text(entry.seen),
        _text(entry.filled),
        trade.exit_reason.value,
        trade.exit_group,
        trade.exit_decided_at_ms,
        "mark" if sale is None else ("sale" if sale.side == "sell" else "buy_back"),
        leaving.bar_close_ms,
        "" if sale is None else _text(sale.seen),
        _text(trade.exit_price),
        _text(trade.entry_fee_usd),
        _text(trade.exit_fee_usd),
        _text(trade.pnl_usd),
        entry.side,
    ]


def _csv_bytes(rows: Iterable[Sequence[str | int]]) -> bytes:
    """The rows as UTF-8 CSV with CRLF row ends (the repo's other export's) and no byte-order mark."""
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


def header_bytes() -> bytes:
    """The header row."""
    return _csv_bytes([CSV_HEADER])


def rows_bytes(trades: Iterable[StoredTrade]) -> bytes:
    """One row for every trade handed over, and no header."""
    return _csv_bytes(_row(stored) for stored in trades)


def trades_csv(trades: Iterable[StoredTrade]) -> bytes:
    """The header and one row for every trade handed over, whole."""
    return header_bytes() + rows_bytes(trades)
