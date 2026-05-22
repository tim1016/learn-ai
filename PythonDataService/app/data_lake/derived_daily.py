"""Daily-trade aggregation: minute-trade artifacts to daily zip.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

LEAN daily format (inner CSV name: `<sym_lower>.csv`):
  Columns (no header): "<YYYYMMDD HH:MM>", open*10000, high*10000, low*10000,
                       close*10000, volume
  Timestamp column always "<YYYYMMDD> 00:00" (session-start midnight).

Aggregation rules:
  - One row per trading_date that appears in the minute-bar input.
  - open = first bar's open
  - close = last bar's close
  - high = max(highs)
  - low = min(lows)
  - volume = sum(volumes)

Deterministic: same inputs produce byte-identical zip output.

NOT a vendor-equivalent of LEAN's own daily bars (those are separately
sourced and use slightly different bar boundaries). Repo-internal
consistency only.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from app.data_lake.lean_writer import MinuteTradeBar, to_deci_cent

# Regular trading hours for US equities (exchange-local). A minute bar
# whose start falls in [09:30, 16:00) belongs to the regular session;
# 09:30 starts the 09:30–09:31 bar, 15:59 starts the last (15:59–16:00).
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)

_DETERMINISTIC_ZIP_DATE_TIME: tuple[int, int, int, int, int, int] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)


@dataclass(frozen=True)
class DailyAggregate:
    """OHLCV for a single trading_date in exchange-local terms."""

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def aggregate_minute_to_daily(
    bars: list[MinuteTradeBar],
) -> list[DailyAggregate]:
    """Bucket minute bars by ET trading date and emit one OHLCV row per date.

    Input must be sorted ascending by bar_start_et; the function buckets by
    date as it iterates (relies on the sorted-input contract from the caller).
    """
    if not bars:
        return []

    out: list[DailyAggregate] = []
    cur_date: date | None = None
    cur_open: Decimal | None = None
    cur_high: Decimal | None = None
    cur_low: Decimal | None = None
    cur_close: Decimal | None = None
    cur_vol = 0

    for bar in bars:
        d = bar.bar_start_et.date()
        if d != cur_date:
            if cur_date is not None:
                out.append(
                    DailyAggregate(
                        trading_date=cur_date,
                        open=cur_open,  # type: ignore[arg-type]
                        high=cur_high,  # type: ignore[arg-type]
                        low=cur_low,  # type: ignore[arg-type]
                        close=cur_close,  # type: ignore[arg-type]
                        volume=cur_vol,
                    )
                )
            cur_date = d
            cur_open = bar.open
            cur_high = bar.high
            cur_low = bar.low
            cur_close = bar.close
            cur_vol = bar.volume
        else:
            cur_high = max(cur_high, bar.high)  # type: ignore[arg-type]
            cur_low = min(cur_low, bar.low)  # type: ignore[arg-type]
            cur_close = bar.close
            cur_vol += bar.volume

    if cur_date is not None:
        out.append(
            DailyAggregate(
                trading_date=cur_date,
                open=cur_open,  # type: ignore[arg-type]
                high=cur_high,  # type: ignore[arg-type]
                low=cur_low,  # type: ignore[arg-type]
                close=cur_close,  # type: ignore[arg-type]
                volume=cur_vol,
            )
        )

    return out


def rth_daily_closes(bars: list[MinuteTradeBar]) -> dict[date, Decimal]:
    """Regular-trading-hours close per session date.

    Keeps only bars whose ET start falls in [09:30, 16:00) and records
    each date's last such close. Captures may include extended-hours
    bars (04:00–20:00); those must not contribute to the close LEAN
    uses to price dividends (see ``factor_files``). Input must be sorted
    ascending by ``bar_start_et``.
    """
    closes: dict[date, Decimal] = {}
    for bar in bars:
        start = bar.bar_start_et
        if _RTH_OPEN <= start.time() < _RTH_CLOSE:
            closes[start.date()] = bar.close
    return closes


def build_daily_zip_bytes(
    symbol: str,
    aggregates: list[DailyAggregate],
) -> bytes:
    """Build the LEAN daily-zip payload for a symbol.

    Deterministic: same inputs produce byte-identical output (pinned ZIP
    epoch matches lean_writer.build_minute_trade_zip_bytes).
    """
    sym_lower = symbol.lower()
    lines = [
        (
            f"{a.trading_date.strftime('%Y%m%d')} 00:00,"
            f"{to_deci_cent(a.open)},"
            f"{to_deci_cent(a.high)},"
            f"{to_deci_cent(a.low)},"
            f"{to_deci_cent(a.close)},"
            f"{a.volume}"
        )
        for a in aggregates
    ]
    csv_body = "\n".join(lines) + ("\n" if lines else "")

    buf = io.BytesIO()
    info = zipfile.ZipInfo(
        filename=f"{sym_lower}.csv",
        date_time=_DETERMINISTIC_ZIP_DATE_TIME,
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, csv_body)
    return buf.getvalue()
