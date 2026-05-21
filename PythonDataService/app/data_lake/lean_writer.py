"""LEAN deci-cent CSV-in-zip writer.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1
Reference for the on-disk format: PythonDataService/app/engine/data/lean_format.py
(existing writer; this module supersedes it inside the data lake but does not
remove the existing one until Slice 1d).

LEAN minute-trade zip layout (path constructed by app.data_lake.path_policy):
    <yyyymmdd>_trade.zip
      └── <yyyymmdd>_<sym_lower>_minute_trade.csv
           no header; columns:
             ms_since_midnight_et, open*10000, high*10000, low*10000,
             close*10000, volume
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# LEAN's price scale factor: prices on disk are multiplied by 10_000.
_PRICE_SCALE = Decimal(10_000)
_QUANT = Decimal(1)  # round to integer after scaling

# ZIP archive epoch — pinned so two runs with identical inputs produce
# byte-identical zips. ZipFile default is "now", which would break the
# data_availability_hash determinism gate.
_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)


@dataclass(frozen=True)
class MinuteTradeBar:
    """One minute trade bar in exchange-local (ET) wall clock.

    bar_start_et is the inclusive start of the minute (e.g. 09:30:00 ET
    represents the [09:30:00, 09:31:00) bar). LEAN's CSV column 0 is
    ms_since_midnight_et computed from this value.
    """

    bar_start_et: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def to_deci_cent(price: Decimal) -> int:
    """Multiply by 10_000 and round half-up to integer.

    Rejects negative prices (LEAN never serializes them; a negative would
    indicate upstream data corruption).
    """
    if price < 0:
        raise ValueError(f"deci-cent encoding refuses negative price: {price}")
    return int((price * _PRICE_SCALE).quantize(_QUANT, rounding=ROUND_HALF_UP))


def _ms_since_midnight_et(bar_start_et: datetime) -> int:
    """ms from midnight in the bar's tz (the bar_start_et is expected ET-aware)."""
    midnight = bar_start_et.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = bar_start_et - midnight
    return int(delta.total_seconds() * 1000)


def build_minute_trade_zip_bytes(
    symbol: str,
    trading_date_yyyymmdd: str,
    bars: list[MinuteTradeBar],
) -> bytes:
    """Build the deci-cent zip payload for a single (symbol, trading_date).

    Deterministic: same inputs produce byte-identical output. Caller writes
    the result via app.data_lake.atomic.atomic_write_and_promote.
    """
    sym_lower = symbol.lower()
    csv_name = f"{trading_date_yyyymmdd}_{sym_lower}_minute_trade.csv"
    lines = [
        ",".join(
            (
                str(_ms_since_midnight_et(bar.bar_start_et)),
                str(to_deci_cent(bar.open)),
                str(to_deci_cent(bar.high)),
                str(to_deci_cent(bar.low)),
                str(to_deci_cent(bar.close)),
                str(bar.volume),
            )
        )
        for bar in bars
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(filename=csv_name, date_time=_DETERMINISTIC_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
