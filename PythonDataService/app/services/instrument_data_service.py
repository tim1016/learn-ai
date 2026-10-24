"""One instrument's data as the lake can admit it, for a window (UX redesign slice 13, #3503).

``GET /api/data-lake/coverage`` already answers, per scheduled session, what
the catalog and the files on disk say. This layers on it what the Data tab
needs to be honest before anything is filled:

* **Which days count.** The sessions are the canonical calendar's
  (``app.lean_sidecar.trading_calendar``): a weekend or a holiday is no session,
  an early close is one. Nothing here names a session time.
* **Which sessions are not finished.** The lake stores whole-day minute files,
  so a session is immutable only once its trading date has rolled (the same
  split the chart and the Returns capture use). Until then it is ``forming``, or
  ``upcoming``: never ``missing``, and a Fill never asks for it.
* **Which gaps a Fill can close.** The provider serves a rolling window
  (``polygon_history_floor``). A session older than it that the lake still
  holds stays ``ready`` and readable; one it lacks is ``blocked_by``
  ``before_provider_history`` and says so. A session the provider answered with
  no bars is ``provider_no_data`` (the lake learns a symbol was not trading, for
  instance before it listed, from that empty answer, and never from a guess).
* **The derived daily file**, separately. A daily-resolution run reads it and a
  minute run never opens it, so its state never turns a minute session unready.

Raw-tree files are judged by their catalog row alone, as ``/coverage`` judges
them; a split-adjusted file also needs its adjustment record, which
``/coverage`` already checks.

Nothing here writes. The Fill is the existing backfill job over ``fill_window``.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from app.data_lake import catalog_client, path_policy
from app.data_lake.adjustment_versions import AdjustmentVersionError, current_version
from app.data_lake.ensure_data import _cache_matches, _daily_dch, _daily_trade_sources
from app.data_lake.run_readiness import needs_adjustment_record
from app.data_lake.types import (
    ArtifactIdentity,
    CoverageDay,
    CoverageResponse,
    PriceAdjustmentMode,
    trading_date_at_ms,
    trading_date_to_calendar_anchor_ms,
)
from app.lean_sidecar.trading_calendar import SessionWindow
from app.schemas.instrument_data import (
    Blocker,
    DailyRollup,
    FillWindow,
    InstrumentDataResponse,
    InstrumentDataSummary,
    InstrumentSession,
)
from app.services.chart_bar_source import split_sessions_at_boundary

#: The failure reason the catalog records when the provider answered with no bars.
PROVIDER_NO_DATA = "provider_no_data"

#: Session states a Fill can act on.
_REPAIRABLE = frozenset({"needs_rebuild", "missing", "failed"})


def _blocker(session_date: date, state: str, reason: str | None, floor: date) -> Blocker | None:
    if state not in _REPAIRABLE:
        return None
    if session_date < floor:
        return "before_provider_history"
    if state == "failed" and reason == PROVIDER_NO_DATA:
        return "provider_no_data"
    return None


def _classify(day: CoverageDay, floor: date, unfinished: dict[date, SessionWindow], now_ms: int) -> InstrumentSession:
    session_date = trading_date_at_ms(day.trading_date_ms)
    window = unfinished.get(session_date)
    if window is not None:
        return InstrumentSession(
            trading_date_ms=day.trading_date_ms,
            state="upcoming" if window.open_ms_utc > now_ms else "forming",
            artifact_id=day.artifact_id,
        )
    if day.status == "complete":
        return InstrumentSession(trading_date_ms=day.trading_date_ms, state="ready", artifact_id=day.artifact_id)
    if day.status == "fetching":
        return InstrumentSession(trading_date_ms=day.trading_date_ms, state="building", artifact_id=day.artifact_id)
    if day.status == "needs_rebuild":
        state, reason = "needs_rebuild", day.rebuild_reason
    elif day.status == "stale":
        state, reason = "needs_rebuild", "catalog_marked_stale"
    elif day.status == "failed":
        state, reason = "failed", day.failure_reason
    else:
        state, reason = "missing", None
    blocker = _blocker(session_date, state, reason, floor)
    return InstrumentSession(
        trading_date_ms=day.trading_date_ms,
        state=state,
        artifact_id=day.artifact_id,
        reason=reason,
        fetchable=blocker is None,
        blocked_by=blocker,
    )


def build_instrument_data(
    coverage: CoverageResponse,
    *,
    start: date,
    end: date,
    now_ms: int,
    floor: date,
    rollup: DailyRollup,
) -> InstrumentDataResponse:
    """Fold one coverage read into the Data tab's reading of it. Pure.

    ``start`` and ``end`` are the requested window, so the calendar-day count
    names every day asked for, including the ones that are no session.
    """
    _completed, live, _boundary = split_sessions_at_boundary(start.isoformat(), end.isoformat(), now_ms, "extended")
    unfinished = {window.session_date: window for window in live}
    sessions = [_classify(day, floor, unfinished, now_ms) for day in coverage.days]

    def count(state: str) -> int:
        return sum(1 for session in sessions if session.state == state)

    fetchable = [session for session in sessions if session.fetchable]
    finished = len(sessions) - count("forming") - count("upcoming")
    summary = InstrumentDataSummary(
        calendar_day_count=(end - start).days + 1,
        session_count=len(sessions),
        ready=count("ready"),
        building=count("building"),
        needs_rebuild=count("needs_rebuild"),
        missing=count("missing"),
        failed=count("failed"),
        forming=count("forming"),
        upcoming=count("upcoming"),
        fetchable=len(fetchable),
        before_provider_history=sum(1 for session in sessions if session.blocked_by == "before_provider_history"),
        provider_no_data=sum(1 for session in sessions if session.blocked_by == "provider_no_data"),
        run_readable=finished > 0 and count("ready") == finished,
    )
    fill_window = (
        FillWindow(
            start_trading_date_ms=trading_date_to_calendar_anchor_ms(trading_date_at_ms(fetchable[0].trading_date_ms)),
            end_trading_date_ms=trading_date_to_calendar_anchor_ms(trading_date_at_ms(fetchable[-1].trading_date_ms)),
        )
        if fetchable
        else None
    )
    return InstrumentDataResponse(
        market=coverage.market,
        symbol=coverage.symbol,
        price_adjustment_mode=coverage.price_adjustment_mode,
        data_root_id=coverage.data_root_id,
        provider_history_start_ms=trading_date_to_calendar_anchor_ms(floor),
        as_of_ms=now_ms,
        summary=summary,
        sessions=sessions,
        fill_window=fill_window,
        daily_rollup=rollup,
    )


async def read_daily_rollup(*, symbol: str, price_adjustment_mode: PriceAdjustmentMode, floor: date) -> DailyRollup:
    """Is the symbol's derived daily file the one the minute sessions it holds would build?

    Judged by the writer's own rule (``ensure_data._daily_dch`` over
    ``_daily_trade_sources``, and ``_cache_matches`` for the adjustment
    evidence), so this says "current" exactly when ``ensure_data`` would reuse
    the file, and "stale" exactly when it would rebuild it.
    """
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol=symbol,
        trading_date=None,
        resolution="daily",
        data_type="trade",
        provider="learn_ai_derived",
        price_adjustment_mode=price_adjustment_mode,
    )
    cached = await catalog_client.select_complete_aggregated_bar_artifact(identity)
    if cached is None:
        return DailyRollup(state="not_built")
    lake_root: Path = path_policy.resolve_lake_root(price_adjustment_mode)
    version: str | None = None
    if needs_adjustment_record(price_adjustment_mode):
        try:
            version = await asyncio.to_thread(current_version, lake_root, symbol)
        except AdjustmentVersionError:
            return DailyRollup(state="stale", reason="adjustment_evidence_invalid")
    sources = await _daily_trade_sources(identity, lake_root, version, floor)
    contract = _daily_dch([source.id for source in sources], [source.file_sha256 for source in sources], price_adjustment_mode, version)
    if cached.data_contract_hash != contract:
        return DailyRollup(state="stale", reason="sources_changed")
    if not await asyncio.to_thread(_cache_matches, cached, contract, lake_root, version):
        return DailyRollup(state="stale", reason="adjustment_evidence_invalid")
    return DailyRollup(state="current")
