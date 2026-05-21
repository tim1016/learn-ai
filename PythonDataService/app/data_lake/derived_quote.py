"""Quote-zip synthesis from same-day minute-trade bars.

LEAN's default behavior is to load the matching `*_quote.zip` alongside
`*_trade.zip` if it exists; without one, you get a runtime warning. In v1c
we synthesize quote = trade with zero spread + zero size. This is enough
for LEAN to load without warnings and matches the existing
lean_sidecar_service.stage_quote_bars behavior.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Real quote data from Polygon (when the plan tier permits) is a Slice 5
deferred item.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

from app.data_lake.lean_writer import MinuteTradeBar, to_deci_cent

_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)


def _ms_since_midnight_et(bar_start_et: datetime) -> int:
    midnight = bar_start_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((bar_start_et - midnight).total_seconds() * 1000)


def build_minute_quote_zip_bytes(
    symbol: str,
    trading_date_yyyymmdd: str,
    bars: list[MinuteTradeBar],
) -> bytes:
    """Build the LEAN quote zip for one (symbol, trading_date).

    Each row's bid OHLC equals the trade OHLC; ask is the same; sizes are 0.
    Deterministic: same inputs produce byte-identical output (pinned ZIP
    epoch matches lean_writer.build_minute_trade_zip_bytes).
    """
    sym_lower = symbol.lower()
    csv_name = f"{trading_date_yyyymmdd}_{sym_lower}_minute_quote.csv"
    lines = [
        ",".join(
            (
                str(_ms_since_midnight_et(b.bar_start_et)),
                # Bid OHLCV
                str(to_deci_cent(b.open)),
                str(to_deci_cent(b.high)),
                str(to_deci_cent(b.low)),
                str(to_deci_cent(b.close)),
                "0",
                # Ask OHLCV
                str(to_deci_cent(b.open)),
                str(to_deci_cent(b.high)),
                str(to_deci_cent(b.low)),
                str(to_deci_cent(b.close)),
                "0",
            )
        )
        for b in bars
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(filename=csv_name, date_time=_DETERMINISTIC_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
