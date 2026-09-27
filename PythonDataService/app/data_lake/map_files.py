"""LEAN map-file CSV builder.

Spec: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md § 5.1

LEAN map-file format (one CSV per symbol; see path_policy.LeanMapFilePath for
the canonical path):
  <yyyymmdd>,<ticker_lowercase>,<exchange>

A map file's rows describe the symbol's listing history as the lake knows it
(#2453): the union of every captured session and every requested window, so
widening a capture window extends the map and never leaves it ending at an
older window's end — LEAN reads a mapping that ends mid-window as a
delisting. The caller (``ensure_data._process_map_file_artifact``) computes
the coverage bounds; this module formats and parses them.

For symbols that never changed ticker, two rows: history_start and
history_end with the same ticker. For changed symbols (e.g. FB -> META on
2022-06-09), full ticker-history reconstruction is deferred to Slice 5; v1c
emits the current ticker for the entire range, which is acceptable for the
EMA-crossover smoke and for any symbol that didn't change in the test window.
"""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise

from app.data_lake.polygon_ticker_events import TickerEvent


def build_map_file_bytes(
    symbol: str,
    events: list[TickerEvent],  # v1c ignores ticker history; Slice 5 implements reconstruction
    history_start: date,
    history_end: date,
    exchange: str,
) -> bytes:
    """Build the deterministic map-file CSV body for one symbol.

    V1c emits the current ticker for the entire range; Slice 5 adds full
    historical-ticker reconstruction. The function accepts `events` to
    establish the API surface; the values are unused until then.
    """
    sym = symbol.lower()
    ex = exchange.lower()
    rows = [
        f"{history_start.strftime('%Y%m%d')},{sym},{ex}",
        f"{history_end.strftime('%Y%m%d')},{sym},{ex}",
    ]
    return ("\n".join(rows) + "\n").encode("ascii")


def map_file_coverage(payload: bytes) -> tuple[date, date]:
    """The ``[first_row_date, last_row_date]`` span a map file states.

    The one parser every consumer of a map's validity uses — the capture
    cache-hit check (``ensure_data``) and the LEAN sidecar's mount range
    check read coverage through this function, never a second decode
    (#2453). Rows must be non-decreasing by date; a map that is not
    orderable is corruption, not a coverage statement.
    """
    lines = [line for line in payload.decode("ascii").splitlines() if line]
    if len(lines) < 2:
        raise ValueError(f"map file must carry at least two rows, found {len(lines)}")
    dates: list[date] = []
    for index, line in enumerate(lines):
        try:
            dates.append(datetime.strptime(line.split(",")[0], "%Y%m%d").date())
        except ValueError as exc:
            raise ValueError(f"map file row {index} does not start with a yyyymmdd date: {line[:60]!r}") from exc
    for earlier, later in pairwise(dates):
        if later < earlier:
            raise ValueError("map file rows must be non-decreasing by date")
    return dates[0], dates[-1]
