"""Lake-admission validation for vendor minute bars (#2451).

A vendor minute-bar fetch is validated before anything is published to the
lake. This is the fail-fast ingestion boundary ``temporal-rigor.md`` requires:
duplicates, non-monotonic timestamps and corrupt prices are signals about
upstream corruption and must surface, never be repaired, deduplicated or
silently dropped. The reader cannot catch these later — the writer stores
time-of-day only and the session date comes from the filename
(``lean_writer.build_minute_trade_zip_bytes``) — so the check has to gate the
publish, not the read.

The same contract also gates legacy cache hits (#2527 review): artifacts
published before validation existed are re-validated from their stored bytes
when reused, so a corrupt pre-validation row rebuilds instead of surviving
forever behind a matching data contract. One implementation decides both —
``assert_publishable_minute_bars`` (vendor stream) and
``assert_publishable_stored_minute_bars`` (stored bars) delegate to the same
core rules.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from app.data_lake.polygon_fetcher import PolygonBar

_ET = ZoneInfo("America/New_York")

#: Offenders named in one error — enough to diagnose a corrupt response,
#: small enough that the failure detail stays readable on a job card.
_MAX_NAMED_OFFENDERS = 5


class CorruptVendorBarsError(ValueError):
    """A vendor minute-bar response violates the lake publication contract."""


class _ValidatableBar(Protocol):
    """The bar shape the publication contract applies to."""

    t_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int | float


def assert_publishable_minute_bars(
    bars: list[PolygonBar],
    *,
    symbol: str,
    trading_date: date,
) -> None:
    """Assert a fetched minute-bar stream is publishable for one session.

    The contract (one violation fails the whole capture, naming the bars):

    1. strictly increasing, unique, representable timestamps;
    2. every bar inside the requested session date — the ET calendar date of
       the bar start equals ``trading_date`` (extended-hours bars of the same
       ET date are legitimate; the lake stores the full session);
    3. finite, positive open/high/low/close;
    4. ``low <= open, close`` and ``high >= open, close``;
    5. non-negative, integral volume — the fetcher preserves the vendor's
       raw number, so a fractional ``v`` is corruption, not something to
       truncate silently (#2527 review).

    Nothing is repaired, deduplicated or dropped: a pass that finds any
    violation raises :class:`CorruptVendorBarsError` naming up to
    :data:`_MAX_NAMED_OFFENDERS` offending bars with their ET wall-clock.
    """
    _assert_contract(bars, symbol=symbol, trading_date=trading_date)


def assert_publishable_stored_minute_bars(
    bars: Sequence,
    *,
    symbol: str,
    trading_date: date,
) -> None:
    """Assert bars decoded from a stored artifact still satisfy the contract.

    The legacy-cache-hit gate (#2527 review): a minute zip published before
    validation existed is re-validated when a capture would reuse it. The
    stored :class:`~app.data_lake.lean_writer.MinuteTradeBar` rows are adapted
    onto the same core rules — there is no second copy of the contract.
    """
    from app.data_lake.lean_writer import MinuteTradeBar

    adapted: list[PolygonBar] = []
    for bar in bars:
        if not isinstance(bar, MinuteTradeBar):
            raise TypeError(f"stored-bar validation expects MinuteTradeBar, got {type(bar).__name__}")
        adapted.append(
            PolygonBar(
                t_ms=int(bar.bar_start_et.timestamp() * 1000),
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=bar.volume,
                vwap=float(bar.close),
                n=0,
            )
        )
    _assert_contract(adapted, symbol=symbol, trading_date=trading_date)


def _assert_contract(bars: Sequence[_ValidatableBar], *, symbol: str, trading_date: date) -> None:
    violations: list[str] = []
    seen_ts: set[int] = set()
    prev_ts: int | None = None

    for index, bar in enumerate(bars):
        problems: list[str] = []
        if bar.t_ms in seen_ts:
            problems.append("duplicate timestamp")
        elif prev_ts is not None and bar.t_ms <= prev_ts:
            problems.append(f"non-increasing timestamp after {prev_ts}")
        try:
            bar_et = datetime.fromtimestamp(bar.t_ms / 1000, tz=UTC).astimezone(_ET)
        except (ValueError, OverflowError, OSError):
            # A timestamp Python cannot represent is corruption like any
            # other: report it as an offending bar (#2527 review), never as
            # an uncaught conversion error that strands the claim.
            violations.append(
                f"bar[{index}] t_ms={bar.t_ms} is not a representable instant"
            )
            continue
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
        elif not isinstance(bar.volume, int) and not float(bar.volume).is_integer():
            problems.append(f"volume={bar.volume} is not an integer")
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
