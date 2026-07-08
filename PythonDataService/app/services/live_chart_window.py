"""Range-based chart window resolver for the bot control workbench.

Recorded IBKR bars remain the persisted authority. Polygon minute bars are
used only as an in-memory overlay for missing historical 1-minute candles and
are never appended to ``BarPersistence``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from app.broker.ibkr.models import IbkrMinuteBar
from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)
from app.lean_sidecar.trading_calendar import (
    SessionWindow,
    session_state_at_ms,
    session_windows_ms_utc,
)

logger = logging.getLogger(__name__)

ChartTimeframe = Literal["5s", "1m", "5m", "15m", "1h", "1d"]

MS_PER_DAY = 86_400_000
MAX_CHART_RANGE_MS = 7 * MS_PER_DAY
TIMEFRAME_MS: dict[ChartTimeframe, int] = {
    "5s": 5_000,
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "1d": MS_PER_DAY,
}
CHART_TIMEFRAMES: frozenset[str] = frozenset(TIMEFRAME_MS)
_NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ChartOverlayNotice:
    code: str
    message: str
    session_date: str | None = None
    source: Literal["polygon"] = "polygon"


@dataclass(frozen=True)
class ChartWindowResult:
    bars: list[IbkrMinuteBar]
    timeframe: ChartTimeframe
    resolution: Literal["5s", "1m"]
    is_streaming: bool
    overlay_notices: list[ChartOverlayNotice] = field(default_factory=list)


class ChartWindowError(ValueError):
    """Raised when a chart window request is outside the supported contract."""


def coerce_chart_timeframe(raw: str) -> ChartTimeframe:
    value = raw.strip()
    if value not in CHART_TIMEFRAMES:
        raise ChartWindowError("timeframe must be one of 5s, 1m, 5m, 15m, 1h, 1d")
    return value  # type: ignore[return-value]


def validate_chart_window(*, from_ms: int, to_ms: int, now_ms: int) -> None:
    if from_ms < 0 or to_ms < 0:
        raise ChartWindowError("from_ms and to_ms must be non-negative UTC milliseconds")
    if to_ms <= from_ms:
        raise ChartWindowError("to_ms must be greater than from_ms")
    if to_ms > now_ms:
        raise ChartWindowError("to_ms cannot be in the future")
    if to_ms - from_ms > MAX_CHART_RANGE_MS:
        raise ChartWindowError("chart range cannot exceed the past 7 days")
    if from_ms < now_ms - MAX_CHART_RANGE_MS:
        raise ChartWindowError("chart range cannot start before the past 7 days")


async def resolve_chart_window(
    *,
    symbol: str | None,
    timeframe: ChartTimeframe,
    from_ms: int,
    to_ms: int,
    now_ms: int,
    polygon_api_key: str,
    live_aggregator,
) -> ChartWindowResult:
    """Resolve chart bars for an explicit UTC millisecond window."""
    validate_chart_window(from_ms=from_ms, to_ms=to_ms, now_ms=now_ms)
    resolution: Literal["5s", "1m"] = "5s" if timeframe == "5s" else "1m"
    if symbol is None:
        return ChartWindowResult(
            bars=[],
            timeframe=timeframe,
            resolution=resolution,
            is_streaming=False,
        )

    recorded = _recorded_bars(
        symbol=symbol,
        resolution=resolution,
        from_ms=from_ms,
        to_ms=to_ms,
        live_aggregator=live_aggregator,
    )
    notices: list[ChartOverlayNotice] = []
    if resolution == "1m":
        overlay, notices = await _polygon_overlay_bars(
            symbol=symbol,
            recorded_bars=recorded,
            from_ms=from_ms,
            to_ms=to_ms,
            now_ms=now_ms,
            polygon_api_key=polygon_api_key,
        )
        base_bars = _merge_base_bars(recorded, overlay)
    else:
        base_bars = recorded

    bars = _aggregate_bars(
        bars=base_bars,
        timeframe=timeframe,
        from_ms=from_ms,
        to_ms=to_ms,
    )
    return ChartWindowResult(
        bars=bars,
        timeframe=timeframe,
        resolution=resolution,
        is_streaming=_is_streaming(
            symbol=symbol,
            resolution=resolution,
            from_ms=from_ms,
            to_ms=to_ms,
            now_ms=now_ms,
            live_aggregator=live_aggregator,
        ),
        overlay_notices=notices,
    )


def _recorded_bars(
    *,
    symbol: str,
    resolution: Literal["5s", "1m"],
    from_ms: int,
    to_ms: int,
    live_aggregator,
) -> list[IbkrMinuteBar]:
    days = _utc_dates_for_window(from_ms, to_ms)
    by_start: dict[int, IbkrMinuteBar] = {}
    persistence = getattr(live_aggregator, "_persistence", None)
    if persistence is not None:
        for day in days:
            for bar in persistence.read_parquet(symbol, resolution, day):
                if _bar_overlaps(bar, from_ms, to_ms):
                    by_start[bar.start_ms] = bar
            for bar in persistence.replay(symbol, resolution, day):
                if _bar_overlaps(bar, from_ms, to_ms):
                    by_start[bar.start_ms] = bar

    snapshot = (
        live_aggregator.snapshot_5s(symbol)
        if resolution == "5s"
        else live_aggregator.snapshot(symbol)
    )
    for bar in snapshot:
        if _bar_overlaps(bar, from_ms, to_ms):
            by_start[bar.start_ms] = bar
    return sorted(by_start.values(), key=lambda bar: bar.start_ms)


async def _polygon_overlay_bars(
    *,
    symbol: str,
    recorded_bars: list[IbkrMinuteBar],
    from_ms: int,
    to_ms: int,
    now_ms: int,
    polygon_api_key: str,
) -> tuple[list[IbkrMinuteBar], list[ChartOverlayNotice]]:
    if not symbol:
        return [], []
    windows = _session_windows_for_ms_range(from_ms, to_ms)
    if not windows:
        return [], []

    recorded_starts = {bar.start_ms for bar in recorded_bars}
    overlay: list[IbkrMinuteBar] = []
    notices: list[ChartOverlayNotice] = []
    for window in windows:
        expected_starts = _expected_minute_starts(window, from_ms, min(to_ms, now_ms))
        if not expected_starts:
            continue
        missing = expected_starts - recorded_starts
        if not missing:
            continue
        session_label = window.session_date.isoformat()
        if not polygon_api_key:
            notices.append(
                ChartOverlayNotice(
                    code="polygon_api_key_missing",
                    message="Polygon overlay is unavailable because POLYGON_API_KEY is not configured.",
                    session_date=session_label,
                )
            )
            continue
        try:
            polygon_bars = await fetch_minute_trade_aggregates(
                symbol,
                window.session_date,
                window.session_date,
                polygon_api_key,
            )
        except PolygonAuthError as exc:
            notices.append(_notice("polygon_auth_error", exc, session_label))
            continue
        except PolygonEntitlementError as exc:
            notices.append(_notice("polygon_entitlement_error", exc, session_label))
            continue
        except PolygonRateLimitedError as exc:
            notices.append(_notice("polygon_rate_limited", exc, session_label))
            continue
        except PolygonUnknownSymbolError as exc:
            notices.append(_notice("polygon_unknown_symbol", exc, session_label))
            continue
        except PolygonFetchError as exc:
            notices.append(_notice("polygon_fetch_error", exc, session_label))
            continue

        converted = _convert_polygon_bars(
            symbol=symbol,
            session=window,
            polygon_bars=polygon_bars,
            allowed_starts=missing,
            fetched_at_ms=now_ms,
        )
        overlay.extend(converted)
        if missing and not converted:
            notices.append(
                ChartOverlayNotice(
                    code="polygon_overlay_empty",
                    message="Polygon returned no unadjusted minute bars for missing scheduled candles.",
                    session_date=session_label,
                )
            )
    return sorted(overlay, key=lambda bar: bar.start_ms), notices


def _notice(code: str, exc: Exception, session_date: str) -> ChartOverlayNotice:
    return ChartOverlayNotice(code=code, message=str(exc), session_date=session_date)


def _convert_polygon_bars(
    *,
    symbol: str,
    session: SessionWindow,
    polygon_bars,
    allowed_starts: set[int],
    fetched_at_ms: int,
) -> list[IbkrMinuteBar]:
    out: list[IbkrMinuteBar] = []
    last_start_ms: int | None = None
    seen: set[int] = set()
    for bar in polygon_bars:
        start_ms = int(bar.t_ms)
        if last_start_ms is not None and start_ms <= last_start_ms:
            logger.warning(
                "polygon overlay rejected non-monotonic bar",
                extra={"symbol": symbol, "start_ms": start_ms, "session_date": session.session_date.isoformat()},
            )
            return []
        last_start_ms = start_ms
        if start_ms in seen:
            logger.warning(
                "polygon overlay rejected duplicate bar",
                extra={"symbol": symbol, "start_ms": start_ms, "session_date": session.session_date.isoformat()},
            )
            return []
        seen.add(start_ms)
        if start_ms not in allowed_starts:
            continue
        if not (session.open_ms_utc <= start_ms < session.close_ms_utc):
            continue
        out.append(
            IbkrMinuteBar(
                symbol=symbol.upper(),
                start_ms=start_ms,
                end_ms=start_ms + 60_000,
                open=Decimal(str(bar.open)),
                high=Decimal(str(bar.high)),
                low=Decimal(str(bar.low)),
                close=Decimal(str(bar.close)),
                volume=int(bar.volume),
                fetched_at_ms=fetched_at_ms,
                source="polygon",
            )
        )
    return out


def _aggregate_bars(
    *,
    bars: list[IbkrMinuteBar],
    timeframe: ChartTimeframe,
    from_ms: int,
    to_ms: int,
) -> list[IbkrMinuteBar]:
    if timeframe in ("5s", "1m"):
        return sorted((bar for bar in bars if _bar_overlaps(bar, from_ms, to_ms)), key=lambda bar: bar.start_ms)
    if not bars:
        return []
    windows = _session_windows_for_ms_range(from_ms, to_ms)
    if timeframe == "1d":
        return _aggregate_daily(bars, windows)
    return _aggregate_intraday(bars, windows, TIMEFRAME_MS[timeframe])


def _aggregate_intraday(
    bars: list[IbkrMinuteBar],
    windows: list[SessionWindow],
    bucket_ms: int,
) -> list[IbkrMinuteBar]:
    buckets: dict[int, list[IbkrMinuteBar]] = {}
    bucket_end_by_start: dict[int, int] = {}
    for bar in sorted(bars, key=lambda item: item.start_ms):
        session = _session_for_bar(bar, windows)
        if session is None:
            continue
        bucket_start = session.open_ms_utc + ((bar.start_ms - session.open_ms_utc) // bucket_ms) * bucket_ms
        bucket_end_by_start[bucket_start] = min(bucket_start + bucket_ms, session.close_ms_utc)
        buckets.setdefault(bucket_start, []).append(bar)
    return [
        _bucket_to_bar(bucket_start, bucket_end_by_start[bucket_start], grouped)
        for bucket_start, grouped in sorted(buckets.items())
    ]


def _aggregate_daily(
    bars: list[IbkrMinuteBar],
    windows: list[SessionWindow],
) -> list[IbkrMinuteBar]:
    out: list[IbkrMinuteBar] = []
    for window in windows:
        grouped = [
            bar
            for bar in bars
            if window.open_ms_utc <= bar.start_ms < window.close_ms_utc
        ]
        if grouped:
            out.append(_bucket_to_bar(window.open_ms_utc, window.close_ms_utc, grouped))
    return out


def _bucket_to_bar(start_ms: int, end_ms: int, bars: list[IbkrMinuteBar]) -> IbkrMinuteBar:
    ordered = sorted(bars, key=lambda bar: bar.start_ms)
    sources = {getattr(bar, "source", "ibkr") for bar in ordered}
    source: Literal["ibkr", "polygon", "mixed"] = sources.pop() if len(sources) == 1 else "mixed"
    return IbkrMinuteBar(
        symbol=ordered[0].symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        open=ordered[0].open,
        high=max(bar.high for bar in ordered),
        low=min(bar.low for bar in ordered),
        close=ordered[-1].close,
        volume=sum(int(bar.volume) for bar in ordered),
        fetched_at_ms=max(int(bar.fetched_at_ms) for bar in ordered),
        source=source,
    )


def _merge_base_bars(recorded: list[IbkrMinuteBar], overlay: list[IbkrMinuteBar]) -> list[IbkrMinuteBar]:
    by_start = {bar.start_ms: bar for bar in overlay}
    for bar in recorded:
        by_start[bar.start_ms] = bar
    return sorted(by_start.values(), key=lambda bar: bar.start_ms)


def _is_streaming(
    *,
    symbol: str,
    resolution: Literal["5s", "1m"],
    from_ms: int,
    to_ms: int,
    now_ms: int,
    live_aggregator,
) -> bool:
    threshold_ms = 30_000 if resolution == "5s" else 180_000
    if from_ms > now_ms or to_ms < now_ms - threshold_ms:
        return False
    if session_state_at_ms(now_ms) != "RTH_OPEN":
        return False
    status, _last_error, last_bar_ms = (
        live_aggregator.status_5s(symbol)
        if resolution == "5s"
        else live_aggregator.status(symbol)
    )
    if status != "streaming" or last_bar_ms is None:
        return False
    return now_ms - int(last_bar_ms) <= threshold_ms


def _expected_minute_starts(window: SessionWindow, from_ms: int, to_ms: int) -> set[int]:
    start = max(window.open_ms_utc, from_ms)
    end = min(window.close_ms_utc, to_ms)
    if end <= start:
        return set()
    first = window.open_ms_utc + max(0, (start - window.open_ms_utc) // 60_000) * 60_000
    if first < start and first + 60_000 <= start:
        first += 60_000
    return set(range(first, end, 60_000))


def _session_for_bar(bar: IbkrMinuteBar, windows: list[SessionWindow]) -> SessionWindow | None:
    for window in windows:
        if window.open_ms_utc <= bar.start_ms < window.close_ms_utc:
            return window
    return None


def _session_windows_for_ms_range(from_ms: int, to_ms: int) -> list[SessionWindow]:
    start = datetime.fromtimestamp(from_ms / 1000, tz=UTC).astimezone(_NY_TZ).date()
    end = datetime.fromtimestamp(max(from_ms, to_ms - 1) / 1000, tz=UTC).astimezone(_NY_TZ).date()
    return session_windows_ms_utc(start, end)


def _utc_dates_for_window(from_ms: int, to_ms: int) -> list[date]:
    start = datetime.fromtimestamp(from_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(max(from_ms, to_ms - 1) / 1000, tz=UTC).date()
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return days


def _bar_overlaps(bar: IbkrMinuteBar, from_ms: int, to_ms: int) -> bool:
    return bar.start_ms < to_ms and bar.end_ms > from_ms
