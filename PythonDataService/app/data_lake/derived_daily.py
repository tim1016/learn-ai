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

This module also owns the *read* side of that reduction —
``read_minute_trade_bars`` and the two whole-history reductions built on it —
so the minute-to-daily fold lives in one place. They are pure functions of
bars and paths, with no catalog, lease or async concern, and every caller runs
them off the event loop (#1943).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pandas as pd

from app.data_lake.lean_writer import MinuteTradeBar, to_deci_cent
from app.data_lake.types import ArtifactRecord
from app.lean_sidecar.trading_calendar import regular_session_mask_ms_utc

_ET = ZoneInfo("America/New_York")

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

    Keeps only bars whose ET start falls in the scheduled NYSE regular
    session and records each date's last such close. Captures may include extended-hours
    bars (04:00–20:00); those must not contribute to the close LEAN
    uses to price dividends (see ``factor_files``). Input must be sorted
    ascending by ``bar_start_et``.

    The in-session test is the calendar module's vectorized mask, which reads
    the span's session windows once. Asking per bar rebuilt a calendar
    schedule for every minute of a multi-year history and froze the service
    for minutes per backfill (issue #1943). Callers reach this through
    :func:`factor_file_reference_closes`, which runs the zip parse and this
    reduction together off the event loop — the other half of #1943.
    """
    start_ms = pd.Series([int(bar.bar_start_et.timestamp() * 1000) for bar in bars], dtype="int64")
    in_session = regular_session_mask_ms_utc(start_ms).to_numpy()
    closes: dict[date, Decimal] = {}
    for bar, keep in zip(bars, in_session, strict=True):
        if keep:
            closes[bar.bar_start_et.date()] = bar.close
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


def read_minute_trade_bars(file_path: str, lake_root: Path) -> list[MinuteTradeBar]:
    """Read a complete minute-trade artifact from disk and reconstruct MinuteTradeBar list.

    The zip contains one CSV: <yyyymmdd>_<sym>_minute_trade.csv. Each row:
      ms_since_midnight_et, open*10000, high*10000, low*10000, close*10000, volume

    The trading date is inferred from the file path (equity/<mkt>/minute/<sym>/<yyyymmdd>_trade.zip).
    """
    full_path = lake_root / Path(*PurePosixPath(file_path).parts)
    with zipfile.ZipFile(full_path) as zf:
        names = zf.namelist()
        if not names:
            return []
        csv_bytes = zf.read(names[0])

    # Parse the date and symbol from the CSV filename: <yyyymmdd>_<sym>_minute_trade.csv
    csv_name = names[0]
    date_part = csv_name[:8]
    trading_year = int(date_part[:4])
    trading_month = int(date_part[4:6])
    trading_day = int(date_part[6:8])

    bars: list[MinuteTradeBar] = []
    for line in csv_bytes.decode("ascii").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        ms_since_midnight = int(parts[0])
        open_dc = int(parts[1])
        high_dc = int(parts[2])
        low_dc = int(parts[3])
        close_dc = int(parts[4])
        volume = int(parts[5])

        # Reconstruct bar_start_et from ms_since_midnight and the trading date.
        hours = ms_since_midnight // 3_600_000
        minutes = (ms_since_midnight % 3_600_000) // 60_000
        bar_start_et = datetime(
            trading_year,
            trading_month,
            trading_day,
            hours,
            minutes,
            0,
            tzinfo=_ET,
        )
        bars.append(
            MinuteTradeBar(
                bar_start_et=bar_start_et,
                open=Decimal(open_dc) / Decimal(10_000),
                high=Decimal(high_dc) / Decimal(10_000),
                low=Decimal(low_dc) / Decimal(10_000),
                close=Decimal(close_dc) / Decimal(10_000),
                volume=volume,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Pass 1 helpers
# ---------------------------------------------------------------------------


class MinuteBarReadError(Exception):
    """A minute-trade artifact could not be read; carries which one, for the failure detail."""

    def __init__(self, file_path: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.file_path = file_path


def _read_history_minute_bars(
    records: Sequence[ArtifactRecord], *, lake_root: Path, fallback_date: date
) -> list[MinuteTradeBar]:
    """Every captured minute bar for a symbol's whole history, ascending by session.

    Hundreds of zips for a multi-year backfill, and about 960 bars in each
    once extended hours are captured. The two callers below reduce the list
    immediately, so neither returns it — both run this and their reduction in
    one thread and hand back only the small result, which keeps the bar list
    from crossing the thread boundary at all (#1943).
    """
    all_bars: list[MinuteTradeBar] = []
    for src in sorted(records, key=lambda r: r.trading_date or fallback_date):
        try:
            all_bars.extend(read_minute_trade_bars(src.file_path, lake_root))
        except Exception as e:
            raise MinuteBarReadError(src.file_path, e) from e
    return all_bars


def factor_file_reference_closes(
    records: Sequence[ArtifactRecord], *, lake_root: Path, fallback_date: date
) -> dict[date, Decimal]:
    """RTH close per session across a symbol's history — the factor file's reference prices.

    Pure CPU and file I/O; callers run it off the event loop. Measured at
    ~1.2 s for 523 sessions (~500k bars) — about 0.9 s of zip parsing and
    0.3 s of reduction. The nine-minute freeze #1943 reported was the
    per-bar calendar rebuild inside :func:`rth_daily_closes`, fixed
    separately; what is left is seconds, and seconds on a loop that also
    runs live execution and the Alpaca lease heartbeat is still a stall.
    The daily rollup repeats this read once per backfilled day, so the cost
    recurs across a backfill rather than being paid once.
    """
    return rth_daily_closes(_read_history_minute_bars(records, lake_root=lake_root, fallback_date=fallback_date))


def daily_zip_from_minute_history(
    records: Sequence[ArtifactRecord], *, symbol: str, lake_root: Path, fallback_date: date
) -> tuple[bytes, int]:
    """The daily-trade artifact's payload and its row count. Off the loop, like the closes above."""
    aggregates = aggregate_minute_to_daily(
        _read_history_minute_bars(records, lake_root=lake_root, fallback_date=fallback_date)
    )
    return build_daily_zip_bytes(symbol=symbol, aggregates=aggregates), len(aggregates)
