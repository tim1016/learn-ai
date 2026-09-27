"""Lake-admission validation for vendor minute bars (#2451).

A vendor minute-bar fetch is validated before anything is published to the
lake. This is the fail-fast ingestion boundary ``temporal-rigor.md`` requires:
duplicates, non-monotonic timestamps and corrupt prices are signals about
upstream corruption and must surface, never be repaired, deduplicated or
silently dropped. The reader cannot catch these later — the writer stores
time-of-day only and the session date comes from the filename
(``lean_writer.build_minute_trade_zip_bytes``) — so the check has to gate the
publish, not the read.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.data_lake.polygon_fetcher import PolygonBar

_ET = ZoneInfo("America/New_York")

#: Offenders named in one error — enough to diagnose a corrupt response,
#: small enough that the failure detail stays readable on a job card.
_MAX_NAMED_OFFENDERS = 5


class CorruptVendorBarsError(ValueError):
    """A vendor minute-bar response violates the lake publication contract."""


def assert_publishable_minute_bars(
    bars: list[PolygonBar],
    *,
    symbol: str,
    trading_date: date,
) -> None:
    """Assert a fetched minute-bar stream is publishable for one session.

    The contract (one violation fails the whole capture, naming the bars):

    1. strictly increasing, unique timestamps;
    2. every bar inside the requested session date — the ET calendar date of
       the bar start equals ``trading_date`` (extended-hours bars of the same
       ET date are legitimate; the lake stores the full session);
    3. finite, positive open/high/low/close;
    4. ``low <= open, close`` and ``high >= open, close``;
    5. non-negative volume.

    Nothing is repaired, deduplicated or dropped: the first pass that finds
    any violation raises :class:`CorruptVendorBarsError` naming up to
    :data:`_MAX_NAMED_OFFENDERS` offending bars with their ET wall-clock.
    Volume integrality is the fetcher's cast (``int(r["v"])``) and is
    therefore guaranteed by construction at this boundary.
    """
    violations: list[str] = []
    seen_ts: set[int] = set()
    prev_ts: int | None = None

    for index, bar in enumerate(bars):
        problems: list[str] = []
        if bar.t_ms in seen_ts:
            problems.append("duplicate timestamp")
        elif prev_ts is not None and bar.t_ms <= prev_ts:
            problems.append(f"non-increasing timestamp after {prev_ts}")
        bar_et = datetime.fromtimestamp(bar.t_ms / 1000, tz=UTC).astimezone(_ET)
        if bar_et.date() != trading_date:
            problems.append(
                f"outside requested session {trading_date.isoformat()}"
                f" (bar ET date {bar_et.date().isoformat()})"
            )
        for name in ("open", "high", "low", "close"):
            value = getattr(bar, name)
            if not math.isfinite(value) or value <= 0:
                problems.append(f"{name}={value!r} is not finite and positive")
        if bar.low > bar.open:
            problems.append(f"low {bar.low} > open {bar.open}")
        if bar.low > bar.close:
            problems.append(f"low {bar.low} > close {bar.close}")
        if bar.high < bar.open:
            problems.append(f"high {bar.high} < open {bar.open}")
        if bar.high < bar.close:
            problems.append(f"high {bar.high} < close {bar.close}")
        if bar.volume < 0:
            problems.append(f"volume={bar.volume} is negative")
        if problems:
            violations.append(
                f"bar[{index}] t_ms={bar.t_ms} ({bar_et.isoformat()}): " + "; ".join(problems)
            )
        seen_ts.add(bar.t_ms)
        prev_ts = bar.t_ms

    if violations:
        named = violations[:_MAX_NAMED_OFFENDERS]
        overflow = len(violations) - len(named)
        detail = " | ".join(named)
        if overflow:
            detail += f" | … and {overflow} more"
        raise CorruptVendorBarsError(
            f"corrupt vendor minute bars for {symbol} {trading_date.isoformat()}: "
            f"{len(violations)} offending bar(s): {detail}"
        )
