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

Three things fall back to the provider and say so in the response:

* a completed session the lake does not hold yet (``lake_gap``);
* any split/dividend-adjusted request, because the lake stores raw
  (unadjusted) bytes only — serving those for an adjusted chart would be a
  silent numerical error (``price_adjustment_unsupported``);
* a ticker the lake cannot address — an index or option prefix such as
  ``I:SPX``, or a path-unsafe string — which is rejected here, before any
  filesystem path is built, and served by the identical provider call the
  flag-off path makes (``symbol_not_lake_addressable``).

Stitching is also capped: past ``_MAX_PROVIDER_RUNS`` contiguous provider runs
the composition gives up and takes the flag-off path wholesale, so a lake full
of holes can never cost more provider calls than not having a lake at all.

The receipt describes the **visible** window. Composition runs over the
warmup-extended range the caller asks for, but the spans and the notice are cut
to ``visible_from_date`` — a notice about warmup bars would be a notice about
bars nobody is looking at.

Resampling and indicator math sit above this module and are untouched: what
comes back is the same list-of-dicts a provider fetch returns.

**Known precision seam — a composed series is not uniformly precise.**
Lake-served days are quantized to LEAN's deci-cent grid (``lean_writer``
multiplies by 10,000 and rounds half-up, so 1/100 of a cent is the finest
representable step), while provider-served days carry the vendor's full float
precision all the way to the chart response's 6-decimal rounding. A range that
mixes both therefore mixes two precisions, and for a sub-penny-tick name the
lake portion can differ from the provider portion in the 5th and 6th decimal.
This is a property of the LEAN on-disk format the whole data-lake plan adopts,
not something this module can fix — quantization happens at write time, before
any reader sees the bytes. Note also that the flag-on/flag-off equality test
cannot surface it: its fixture prices are 2-decimal, which round-trip through
deci-cents exactly. Ledgered for the integration slice's parity decision.
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
from app.lean_sidecar.workspace import SymbolValidationError, validate_symbol
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
    "symbol_not_lake_addressable",
]

#: Closed vocabulary the UI maps to a non-intrusive notice. ``None`` means the
#: composed range needs no notice: history came from the lake and only the
#: still-forming session came from the provider, which is the design, not a gap.
NoticeCode = Literal[
    "history_provider_fallback",
    "adjusted_prices_provider_only",
    "symbol_provider_only",
]

#: ``(from_date, to_date)`` as ISO day strings -> canonical bar dicts. Bound by
#: the caller to the very same provider fetch the flag-off path uses, so the
#: live tail and every fallback range travel one unchanged code path.
ProviderFetch = Callable[[str, str], list[dict[str, Any]]]

#: One executed segment: where it came from, why, which sessions it covers, and
#: the bars it produced. The plan is executed before any of it is reported, so
#: the receipt can be cut to the visible window without re-fetching anything.
_ExecutedSegment = tuple[BarSourceName, SpanReason, list[SessionWindow], list[dict[str, Any]]]

#: Ceiling on how many separate provider fetches a composed range may cost.
#:
#: Each contiguous provider run is one ``fetch_bars_chunked`` call, and they run
#: serially inside the chart's worker thread. A partially-backfilled lake — the
#: normal rollout state — is exactly the shape that produces many small holes,
#: so an uncapped composition would issue O(holes) Polygon calls where the
#: flag-off path issues one. Above this many runs the composition collapses to
#: the flag-off behavior: one whole-window fetch, every session marked
#: provider-served. That makes the worst case exactly today's cost.
_MAX_PROVIDER_RUNS = 3


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
        if "symbol_not_lake_addressable" in reasons:
            return "symbol_provider_only"
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


def _whole_window_from_provider(
    *,
    from_date: str,
    to_date: str,
    windows: Sequence[SessionWindow],
    reason: SpanReason,
    fetch_provider: ProviderFetch,
) -> list[_ExecutedSegment]:
    """Serve everything with the single fetch the flag-off path would make.

    The call below uses the identical range ``chart_service`` passes with the
    flag off, so this outcome is today's outcome — the same bars, and the same
    typed ``NO_DATA`` when the provider has nothing to give. Both escapes from
    the composed path land here: a ticker the lake cannot address, and a lake
    too full of holes to be worth stitching.
    """
    return [("provider", reason, list(windows), fetch_provider(from_date, to_date))]


def _visible_spans(
    executed: Sequence[_ExecutedSegment],
    visible_from: date,
) -> tuple[BarSourceSpan, ...]:
    """Report provenance for the window the operator can actually see.

    Composition runs over the **warmup-extended** window, which can reach weeks
    behind the requested range. Reporting those warmup sessions would put a
    notice on the chart about bars nobody is looking at — and at the lake's
    leading edge, where warmup routinely predates the backfill, a chart whose
    every visible session came from the lake would still show the fallback
    notice. So the receipt is cut to the visible sessions, at the same
    session-open anchor the span boundaries already use.
    """
    visible: list[tuple[BarSourceName, SpanReason, list[SessionWindow], list[dict[str, Any]]]] = []
    for source, reason, windows, segment_bars in executed:
        in_view = [window for window in windows if window.session_date >= visible_from]
        if in_view:
            visible.append((source, reason, in_view, segment_bars))
    if not visible:
        return ()

    cut_ms = visible[0][2][0].open_ms_utc
    return tuple(
        BarSourceSpan(
            source=source,
            reason=reason,
            from_session_open_ms_utc=in_view[0].open_ms_utc,
            to_session_open_ms_utc=in_view[-1].open_ms_utc,
            session_count=len(in_view),
            bar_count=sum(1 for bar in segment_bars if bar["timestamp"] >= cut_ms),
        )
        for source, reason, in_view, segment_bars in visible
    )


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


def _execute_plan(
    *,
    ticker: str,
    from_date: str,
    to_date: str,
    adjusted: bool,
    completed: Sequence[SessionWindow],
    live: Sequence[SessionWindow],
    fetch_provider: ProviderFetch,
    lake_root: Path | None,
) -> list[_ExecutedSegment]:
    """Decide where every session's bars come from, then go and get them."""
    all_windows = [*completed, *live]
    try:
        # The boundary guard: the LEAN reader joins the symbol straight into a
        # path, so anything that is not a lake-addressable ticker stops here.
        symbol = validate_symbol(ticker)
    except SymbolValidationError:
        logger.warning(
            "[CHART] %r is not addressable in the lake; serving the whole window from the provider",
            ticker,
            extra={"ticker": ticker, "from_date": from_date, "to_date": to_date},
        )
        return _whole_window_from_provider(
            from_date=from_date,
            to_date=to_date,
            windows=all_windows,
            reason="symbol_not_lake_addressable",
            fetch_provider=fetch_provider,
        )

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

    plan = _plan_segments(completed, live, lake_dates, history_fallback_reason)
    provider_runs = sum(1 for source, _reason, _windows in plan if source == "provider")
    if provider_runs > _MAX_PROVIDER_RUNS:
        # Counted before a single fetch is issued, so the cap costs nothing to
        # enforce. Stitching a lake this holey would cost more provider calls
        # than not stitching at all.
        logger.info(
            "[CHART] %s would need %d provider fetches (cap %d); serving the whole window from the provider",
            symbol,
            provider_runs,
            _MAX_PROVIDER_RUNS,
            extra={"symbol": symbol, "provider_runs": provider_runs, "cap": _MAX_PROVIDER_RUNS},
        )
        return _whole_window_from_provider(
            from_date=from_date,
            to_date=to_date,
            windows=all_windows,
            reason=history_fallback_reason,
            fetch_provider=fetch_provider,
        )

    executed: list[_ExecutedSegment] = []
    for source, reason, windows in plan:
        if source == "lake":
            segment_bars = _read_lake_bars(reader, symbol, windows)
        else:
            segment_bars = fetch_provider(
                windows[0].session_date.isoformat(),
                windows[-1].session_date.isoformat(),
            )
        executed.append((source, reason, windows, segment_bars))
    return executed


def compose_chart_bars(
    *,
    ticker: str,
    from_date: str,
    to_date: str,
    adjusted: bool,
    fetch_provider: ProviderFetch,
    visible_from_date: str | None = None,
    now_ms: int | None = None,
    lake_root: Path | None = None,
) -> ComposedBars:
    """Compose one 1-minute stream from lake history and a live provider tail.

    ``from_date`` / ``to_date`` are ISO day strings in the chart service's own
    vocabulary, where ``from_date`` is already **warmup-extended** by the
    caller. ``visible_from_date`` is the range the operator actually asked for;
    the returned spans and notice describe only that, so a notice never
    reports on warmup bars nobody can see. It defaults to ``from_date`` for
    callers with no warmup.

    ``now_ms`` and ``lake_root`` exist so tests can pin the boundary and the
    fixture root; production leaves both at their defaults.

    A ticker the lake cannot address never reaches the reader, so this function
    raises nothing the flag-off path would not also raise for the same input.
    """
    at_ms = now_ms_utc() if now_ms is None else now_ms
    completed, live, boundary_ms_utc = split_sessions_at_boundary(from_date, to_date, at_ms)

    executed = _execute_plan(
        ticker=ticker,
        from_date=from_date,
        to_date=to_date,
        adjusted=adjusted,
        completed=completed,
        live=live,
        fetch_provider=fetch_provider,
        lake_root=lake_root,
    )

    bars = [bar for _source, _reason, _windows, segment_bars in executed for bar in segment_bars]

    # Composition is an ingestion boundary of its own: a duplicate or
    # out-of-order bar at the stitch must fail loudly, never be repaired.
    assert_canonical_bar_stream(bars, ticker)

    spans = _visible_spans(executed, date.fromisoformat(visible_from_date or from_date))
    logger.info(
        "[CHART] composed %d bars for %s over %d visible span(s)",
        len(bars),
        ticker,
        len(spans),
        extra={
            "ticker": ticker,
            "boundary_ms_utc": boundary_ms_utc,
            "executed_segment_count": len(executed),
            "live_session_count": len(live),
        },
    )
    return ComposedBars(bars=bars, spans=spans, boundary_ms_utc=boundary_ms_utc)
