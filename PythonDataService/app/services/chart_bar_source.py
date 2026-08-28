"""Composed chart bar sourcing: lake history stitched to a live provider tail.

Behind ``DATA_LAKE_ENABLED``. With the flag off, ``chart_service`` never calls
into this module and its per-request provider fetch is unchanged.

The split is **calendar-derived, never wall-clock**: every scheduled NYSE
session in the requested window whose scheduled close has already passed is
*completed* and is served from immutable lake artifacts through the existing
LEAN reader; the first session whose close is still ahead of ``now`` — and
everything after it — is the live tail and is fetched from the provider exactly
as today. Half-days therefore close early because the calendar says so, not
because of any literal in this file.

The two sides are disjoint sets of trading dates, so the stitched series can
neither duplicate a bar nor invent one. That is asserted rather than assumed:
the composed stream goes through :func:`assert_canonical_bar_stream`, the same
fail-fast ingestion gate the provider path uses, so any overlap surfaces as an
error instead of being silently repaired.

Two things fall back to the provider and say so in the response:

* a completed session the lake does not hold yet (``lake_gap``);
* any split/dividend-adjusted request, because the lake stores raw
  (unadjusted) bytes only — serving those for an adjusted chart would be a
  silent numerical error (``price_adjustment_unsupported``).

Resampling and indicator math sit above this module and are untouched: what
comes back is the same list-of-dicts a provider fetch returns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from app.data_lake.path_policy import resolve_lake_root
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import SessionWindow, session_windows_ms_utc
from app.lean_sidecar.workspace import validate_symbol
from app.services.dataset_service import assert_canonical_bar_stream
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

BarSourceName = Literal["lake", "provider"]

#: Closed vocabulary. ``reason`` is a machine code; operator-facing copy is
#: rendered by the UI from its own closed map, never from these strings.
SpanReason = Literal[
    "completed_sessions",
    "current_session",
    "lake_gap",
    "price_adjustment_unsupported",
]

#: Closed vocabulary the UI maps to a non-intrusive notice. ``None`` means the
#: composed range needs no notice: history came from the lake and only the
#: still-forming session came from the provider, which is the design, not a gap.
NoticeCode = Literal["history_provider_fallback", "adjusted_prices_provider_only"]

#: ``(from_date, to_date)`` as ISO day strings -> canonical bar dicts. Bound by
#: the caller to the very same provider fetch the flag-off path uses, so the
#: live tail and every fallback range travel one unchanged code path.
ProviderFetch = Callable[[str, str], list[dict[str, Any]]]


@dataclass(frozen=True)
class BarSourceSpan:
    """One contiguous run of trading sessions and where its bars came from."""

    source: BarSourceName
    reason: SpanReason
    from_session_open_ms_utc: int
    to_session_open_ms_utc: int
    session_count: int
    bar_count: int

    def as_response_dict(self) -> dict[str, Any]:
        """Wire shape. Trading dates are anchored at their scheduled session
        open so every temporal value on the wire stays ``int64 ms UTC``."""
        return {
            "source": self.source,
            "reason": self.reason,
            "from_session_open_ms_utc": self.from_session_open_ms_utc,
            "to_session_open_ms_utc": self.to_session_open_ms_utc,
            "session_count": self.session_count,
            "bar_count": self.bar_count,
        }


@dataclass(frozen=True)
class ComposedBars:
    """The stitched bar stream plus the receipt of which side served what."""

    bars: list[dict[str, Any]]
    spans: tuple[BarSourceSpan, ...]
    boundary_ms_utc: int | None

    @property
    def notice_code(self) -> NoticeCode | None:
        reasons = {span.reason for span in self.spans}
        if "price_adjustment_unsupported" in reasons:
            return "adjusted_prices_provider_only"
        if "lake_gap" in reasons:
            return "history_provider_fallback"
        return None

    def as_response_dict(self) -> dict[str, Any]:
        return {
            "boundary_ms_utc": self.boundary_ms_utc,
            "notice_code": self.notice_code,
            "spans": [span.as_response_dict() for span in self.spans],
        }


def split_sessions_at_boundary(
    from_date: str,
    to_date: str,
    now_ms: int,
) -> tuple[list[SessionWindow], list[SessionWindow], int | None]:
    """Split the scheduled sessions in ``[from_date, to_date]`` at the boundary.

    Returns ``(completed, live, boundary_ms_utc)``. A session is completed once
    its **scheduled** close is at or before ``now_ms`` — the calendar answers
    that, so an early-close half-day completes at its real 13:00-ET close and a
    regular session at 16:00 ET without either time appearing here.
    ``boundary_ms_utc`` is the scheduled open of the first live session, or
    ``None`` when every session in the window has already closed.
    """
    windows = session_windows_ms_utc(date.fromisoformat(from_date), date.fromisoformat(to_date))
    for index, window in enumerate(windows):
        if window.close_ms_utc > now_ms:
            return windows[:index], windows[index:], window.open_ms_utc
    return windows, [], None


def _plan_segments(
    completed: Sequence[SessionWindow],
    live: Sequence[SessionWindow],
    lake_dates: frozenset[date],
    history_fallback_reason: SpanReason,
) -> list[tuple[BarSourceName, SpanReason, list[SessionWindow]]]:
    """Label every session with its source, then merge adjacent equal labels.

    Merging matters: each provider segment becomes one fetch over a contiguous
    date range, so a lake with no holes costs exactly one provider call (the
    live tail) instead of one per day.
    """
    labelled: list[tuple[BarSourceName, SpanReason, SessionWindow]] = [
        ("lake", "completed_sessions", window)
        if window.session_date in lake_dates
        else ("provider", history_fallback_reason, window)
        for window in completed
    ]
    labelled.extend(("provider", "current_session", window) for window in live)

    segments: list[tuple[BarSourceName, SpanReason, list[SessionWindow]]] = []
    for source, reason, window in labelled:
        if segments and segments[-1][0] == source and segments[-1][1] == reason:
            segments[-1][2].append(window)
        else:
            segments.append((source, reason, [window]))
    return segments


def _read_lake_bars(
    reader: LeanMinuteDataReader,
    symbol: str,
    windows: Sequence[SessionWindow],
) -> list[dict[str, Any]]:
    """Convert lake ``TradeBar`` rows into canonical chart bar dicts.

    Only the six keys the provider path guarantees are emitted. The provider's
    optional ``vwap`` / ``transactions`` columns have no lake counterpart and
    never reach the chart response, so synthesising them here would be fiction.
    Provider dicts, conversely, are passed through untouched — that is what
    keeps a provider-served range byte-identical whether the flag is on or off.
    """
    out: list[dict[str, Any]] = []
    for window in windows:
        for bar in reader.read_day(symbol, window.session_date):
            out.append(
                {
                    "timestamp": bar.start_ms,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )
    return out


def compose_chart_bars(
    *,
    ticker: str,
    from_date: str,
    to_date: str,
    adjusted: bool,
    fetch_provider: ProviderFetch,
    now_ms: int | None = None,
    lake_root: Path | None = None,
) -> ComposedBars:
    """Compose one 1-minute stream from lake history and a live provider tail.

    ``from_date`` / ``to_date`` are ISO day strings in the chart service's own
    vocabulary (``from_date`` is already warmup-adjusted by the caller).
    ``now_ms`` and ``lake_root`` exist so tests can pin the boundary and the
    fixture root; production leaves both at their defaults.
    """
    symbol = validate_symbol(ticker)
    at_ms = now_ms_utc() if now_ms is None else now_ms
    completed, live, boundary_ms_utc = split_sessions_at_boundary(from_date, to_date, at_ms)

    reader = LeanMinuteDataReader(
        [lake_root if lake_root is not None else resolve_lake_root()],
        # The lake holds the whole trading day; the chart applies its own
        # RTH / extended mask downstream, so read everything and change nothing.
        session="extended",
    )

    # The lake is raw-only (``DataRunSpec.price_adjustment_mode == "raw"``), so
    # an adjusted chart cannot be served from it at any price. Fall the whole
    # history back to the provider and say why.
    history_fallback_reason: SpanReason = "price_adjustment_unsupported" if adjusted else "lake_gap"
    lake_dates: frozenset[date] = frozenset()
    if not adjusted and completed:
        held = set(reader.iter_dates(symbol, completed[0].session_date, completed[-1].session_date))
        lake_dates = frozenset(held.intersection(window.session_date for window in completed))

    bars: list[dict[str, Any]] = []
    spans: list[BarSourceSpan] = []
    for source, reason, windows in _plan_segments(completed, live, lake_dates, history_fallback_reason):
        if source == "lake":
            segment_bars = _read_lake_bars(reader, symbol, windows)
        else:
            segment_bars = fetch_provider(
                windows[0].session_date.isoformat(),
                windows[-1].session_date.isoformat(),
            )
        spans.append(
            BarSourceSpan(
                source=source,
                reason=reason,
                from_session_open_ms_utc=windows[0].open_ms_utc,
                to_session_open_ms_utc=windows[-1].open_ms_utc,
                session_count=len(windows),
                bar_count=len(segment_bars),
            )
        )
        bars.extend(segment_bars)

    # Composition is an ingestion boundary of its own: a duplicate or
    # out-of-order bar at the stitch must fail loudly, never be repaired.
    assert_canonical_bar_stream(bars, symbol)

    logger.info(
        "[CHART] composed %d bars for %s over %d span(s)",
        len(bars),
        symbol,
        len(spans),
        extra={
            "symbol": symbol,
            "boundary_ms_utc": boundary_ms_utc,
            "lake_session_count": len(lake_dates),
            "live_session_count": len(live),
        },
    )
    return ComposedBars(bars=bars, spans=tuple(spans), boundary_ms_utc=boundary_ms_utc)
