"""A saved run's leg trades as a CSV an owner can read in a spreadsheet and audit trades with (#3030).

Formula: none. Every cell is a stored figure written out; nothing is calculated, rounded or filled in.
Reference: ``.claude/rules/temporal-rigor.md`` "Readable time in exported CSVs": ``unix_ts`` first and always, and one
  display-only wall-clock column beside it named for its zone; ``dataset_service.build_csv_bytes``, the repo's other
  export, whose zone naming and wall-clock format are used here and not copied.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_trade_export.py.

The header is fixed. One row per leg trade, in the order the trades route lists them, and never a page: the caller hands
over every trade of the run and this writes them all.

* ``unix_ts`` is the entry fill's bar close in ``int64 ms UTC``. ``time_america_new_york`` is that instant as New York
  wall-clock text (``YYYY-MM-DD HH:MM:SS``), for reading; nothing is parsed back from it.
* Every other instant is an integer in a column ending ``_ms``.
* Money and prices are exact decimal text in plain digits (never an exponent, never through a float), and a column ending
  ``_usd`` is dollars. A figure a trade does not have is an empty cell.
* ``exit_kind`` is ``sale`` or ``mark``: a leg marked and not sold has no sale, so its ``exit_seen_usd`` is empty and its
  ``exit_price_usd`` is the mark.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
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
        "mark" if sale is None else "sale",
        leaving.bar_close_ms,
        "" if sale is None else _text(sale.seen),
        _text(trade.exit_price),
        _text(trade.entry_fee_usd),
        _text(trade.exit_fee_usd),
        _text(trade.pnl_usd),
    ]


def trades_csv(trades: Sequence[StoredTrade]) -> bytes:
    """The header and one row for every trade handed over, as UTF-8 CSV with CRLF row ends (the repo's other export's)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    writer.writerows(_row(stored) for stored in trades)
    return buffer.getvalue().encode("utf-8")
