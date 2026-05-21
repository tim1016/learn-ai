"""LEAN map-file CSV builder.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1

LEAN map-file format (one CSV per symbol; see path_policy.LeanMapFilePath for
the canonical path):
  <yyyymmdd>,<ticker_lowercase>,<exchange>

For symbols that never changed ticker, two rows: history_start and
history_end with the same ticker. For changed symbols (e.g. FB -> META on
2022-06-09), full ticker-history reconstruction is deferred to Slice 5; v1c
emits the current ticker for the entire range, which is acceptable for the
EMA-crossover smoke and for any symbol that didn't change in the test window.
"""

from __future__ import annotations

from datetime import date

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
