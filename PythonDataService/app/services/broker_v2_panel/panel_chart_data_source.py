"""Chart endpoint wiring for the Broker V2 panel.

This module keeps bar retrieval separate from the panel command/data facade.
It receives only S2 ``FillRecord`` values from the activated SQLite authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

from app.broker.alpaca.clerk.fills import FillRecord
from app.config import settings
from app.schemas.action_plan import TradedInstrumentView, order_symbol
from app.schemas.broker_v2_panel import (
    BotPanelView,
    ChartAsset,
    ChartHistoryResponse,
    ChartHistoryTimeframe,
    ChartLiveResponse,
)
from app.services.bot_runner import get_bot_task_registry
from app.services.bot_runner_errors import UnknownBotError as RunnerUnknownBotError
from app.services.broker_v2_panel.chart_projection_service import (
    build_history_chart,
    build_live_chart,
    build_run_window_chart,
    history_fill_window,
    live_window,
    markers_in_window,
    position_pane,
    run_chart_window,
)
from app.services.broker_v2_panel.history_batch_client import build_history_batch_provider
from app.services.broker_v2_panel.panel_data_source import get_panel_with_chart_fills
from app.services.broker_v2_panel.panel_errors import (
    BotRunnerUnavailableError,
    PanelDataError,
    PanelUnavailableError,
    UnknownBotError,
)
from app.services.broker_v2_panel.panel_scope import validate_account
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqliteChartEvidence,
    SqlitePanelBotNotFound,
    SqlitePanelEconomicUnavailable,
    read_sqlite_chart_evidence,
    read_sqlite_panel_evidence,
)
from app.services.live_chart_window import (
    CHART_FEED_NOT_EXPECTED,
    ChartWindowError,
    ChartWindowResult,
    coerce_chart_timeframe,
    resolve_chart_window,
)
from app.services.sqlite_clerk_compat import custody_account_id_for_route
from app.utils.timestamps import now_ms_utc


async def _build_live_chart_from_fills(
    sid: str,
    symbol: str,
    fills: Sequence[FillRecord],
    *,
    resolution: Literal["5s", "1m"],
    now_ms: int,
) -> ChartLiveResponse:
    """Build a live chart from one authoritative attributed fill source."""
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    window = live_window(now_ms)
    open_ms, close_ms = window
    if now_ms <= open_ms:
        chart_window = ChartWindowResult(
            bars=[],
            timeframe=resolution,
            resolution=resolution,
            feed=CHART_FEED_NOT_EXPECTED,
        )
    else:
        try:
            if resolution == "5s":
                await LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(symbol)
            else:
                await LIVE_BAR_AGGREGATOR.ensure_subscribed(symbol)
            chart_window = await resolve_chart_window(
                symbol=symbol,
                timeframe=coerce_chart_timeframe(resolution),
                from_ms=open_ms,
                to_ms=min(close_ms, now_ms),
                now_ms=now_ms,
                polygon_api_key=settings.POLYGON_API_KEY,
                live_aggregator=LIVE_BAR_AGGREGATOR,
                polygon_overlay_enabled=False,
            )
        except ChartWindowError as exc:
            raise PanelDataError(
                "The live chart window is invalid.",
                detail=str(exc),
            ) from exc
    return build_live_chart(
        chart_window,
        fills,
        strategy_instance_id=sid,
        symbol=symbol,
        window=window,
        now_ms=now_ms,
    )


def _position_pane_symbol(traded: TradedInstrumentView | None) -> str | None:
    """The asset a bot's position pane draws, or ``None`` when its chart has no such pane (#2915).

    A pane of bar closes is a stock's or an ETF's. An option structure's pane
    is a mark from its legs' quotes, which this cannot draw.
    """
    return traded.symbol if traded is not None and traded.kind == "stock" else None


async def resolve_symbol_and_fills(
    broker: str,
    account_id: str,
    sid: str,
    *,
    now_ms: int,
) -> tuple[str, Sequence[FillRecord]]:
    """Resolve one bot's symbol + today's SQLite-native fills.

    Canonical implementation of "which fills back a bot's chart" — honors the
    Extracted so the bot gallery wall's marker projection
    (``gallery_hub.GalleryHub``) reuses this exact resolution instead of
    re-deriving it, keeping the wall and the single-bot detail chart from ever
    diverging on fill provenance.
    """
    resolved = await validate_account(broker, account_id)
    try:
        evidence = await read_sqlite_panel_evidence(
            broker,
            custody_account_id_for_route(broker, resolved),
            sid,
            now_ms=now_ms,
        )
    except SqlitePanelBotNotFound as exc:
        raise UnknownBotError(str(exc)) from exc
    except SqlitePanelEconomicUnavailable as exc:
        raise PanelUnavailableError(
            "The activated SQLite chart evidence is unavailable.",
            detail=str(exc),
        ) from exc
    if evidence is None:
        raise PanelUnavailableError(
            "The activated SQLite chart authority is unavailable.",
            detail="Restore or reactivate the account-scoped SQLite authority.",
        )
    return evidence.status.symbol, evidence.economics.session_fills


async def get_live_snapshot_parts(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> tuple[BotPanelView, ChartLiveResponse]:
    """Build panel and chart from the exact same authority-bound fill cut."""
    observed_at_ms = now_ms_utc()
    panel, fills = await get_panel_with_chart_fills(
        broker,
        account_id,
        sid,
        now_ms=observed_at_ms,
    )
    chart = await _build_live_chart_from_fills(
        sid,
        panel.symbol,
        fills,
        resolution=resolution,
        now_ms=observed_at_ms,
    )
    traded_symbol = _position_pane_symbol(panel.traded)
    if traded_symbol is not None:
        traded_chart = await _build_live_chart_from_fills(
            sid, traded_symbol, fills, resolution=resolution, now_ms=observed_at_ms
        )
        chart = chart.model_copy(update={"position": position_pane(chart, traded_chart)})
    return panel, chart


def _within_latest_run(
    broker: str, sid: str, window: tuple[int, int], *, timeframe: ChartHistoryTimeframe, now_ms: int
) -> tuple[int, int]:
    """The window held to the bot's latest run, as its run record dates it."""
    registry = get_bot_task_registry()
    if registry is None:
        raise BotRunnerUnavailableError
    try:
        run = registry.current_run(broker, sid)
    except RunnerUnknownBotError:
        run = None
    terminal = None if run is None else run.terminal_outcome
    return run_chart_window(
        *window,
        timeframe=timeframe,
        run_started_at_ms=None if run is None else run.started_at_ms,
        run_ended_at_ms=None if terminal is None else terminal.recorded_at_ms,
        now_ms=now_ms,
    )


async def _chart_evidence(
    broker: str, custody_account_id: str, sid: str, *, from_ms: int, to_ms: int
) -> SqliteChartEvidence:
    """One revision-fenced read of the bot's identity and its fills in ``[from_ms, to_ms)``."""
    try:
        evidence = await read_sqlite_chart_evidence(
            broker,
            custody_account_id,
            sid,
            from_ms=from_ms,
            to_ms=to_ms,
        )
    except SqlitePanelEconomicUnavailable as exc:
        raise PanelUnavailableError(
            "The activated SQLite history chart is unavailable.",
            detail=str(exc),
        ) from exc
    if evidence is None:
        raise PanelUnavailableError(
            "The activated SQLite history-chart authority is unavailable.",
            detail="Restore or reactivate the account-scoped SQLite authority.",
        )
    return evidence


async def get_history_chart(
    broker: str,
    account_id: str,
    sid: str,
    timeframe: ChartHistoryTimeframe,
    window: tuple[int, int] | None = None,
    asset: ChartAsset = "listens",
) -> ChartHistoryResponse:
    """Build bounded history from SQLite facts.

    With ``window`` (``from_ms``, ``to_ms``), the tape is that window of the
    bot's latest run instead of the newest bars (#2794); a window outside the
    run, or on a timeframe other than ``1m``, raises ``ChartWindowError``.

    ``asset`` chooses whose candles: the symbol the bot listens to, or the
    asset it trades (#2912). They are the same chart for a bot that trades
    what it listens to. For one that trades something else, the traded
    asset's chart is where its fills are drawn, at their own prices; the
    chart of the symbol it listens to carries that asset's price and fills
    as its position pane (#2915), read for the same window and bar size.

    Issue #2204: this Clerk-side assembly never calls Polygon directly —
    every Fleet Clerk boots with a present-but-empty ``POLYGON_API_KEY``
    (ADR 0062), so the actual vendor walk runs on the fleet-coordinator role,
    reached through the ``batch_provider`` seam. A Polygon failure or an
    unreachable coordinator is not raised — ``build_history_chart`` degrades
    it into a ``polygon_*`` or ``coordinator_unavailable`` notice on the
    response's ``overlay_notices`` with no fabricated bars (issue #2203).
    """
    resolved = await validate_account(broker, account_id)
    observed_at_ms = now_ms_utc()
    if window is None:
        from_ms, to_ms = history_fill_window(timeframe, observed_at_ms)
    else:
        from_ms, to_ms = _within_latest_run(broker, sid, window, timeframe=timeframe, now_ms=observed_at_ms)
    custody_account_id = custody_account_id_for_route(broker, resolved)
    evidence = await _chart_evidence(broker, custody_account_id, sid, from_ms=from_ms, to_ms=to_ms)
    status = evidence.status
    fills = evidence.fills.fills
    batch_provider = build_history_batch_provider()

    async def chart_of(symbol: str) -> ChartHistoryResponse:
        if window is not None:
            return await build_run_window_chart(
                timeframe,
                fills,
                strategy_instance_id=sid,
                symbol=symbol,
                batch_provider=batch_provider,
                window=(from_ms, to_ms),
                now_ms=observed_at_ms,
            )
        chart = await build_history_chart(
            timeframe,
            fills,
            strategy_instance_id=sid,
            symbol=symbol,
            batch_provider=batch_provider,
            now_ms=observed_at_ms,
        )
        if chart.from_ms >= from_ms:
            return chart
        # The candles start before the window the fills were read for: a sparse
        # symbol needs more sessions to fill its bars than the plan counted, and
        # a daily bar starts at midnight. Read the fills those candles stand on.
        reached = await _chart_evidence(broker, custody_account_id, sid, from_ms=chart.from_ms, to_ms=to_ms)
        return chart.model_copy(
            update={
                "fill_markers": markers_in_window(
                    reached.fills.fills, from_ms=chart.from_ms, to_ms=chart.to_ms, charted_symbol=symbol
                )
            }
        )

    if asset == "trades":
        return await chart_of(order_symbol(status.symbol, status.traded))
    traded_symbol = _position_pane_symbol(status.traded)
    if traded_symbol is None:
        return await chart_of(status.symbol)
    # Read together: each read may take the history batch's whole inner
    # timeout, and one after the other they would outlast the outer one.
    chart, traded_chart = await asyncio.gather(chart_of(status.symbol), chart_of(traded_symbol))
    return chart.model_copy(update={"position": position_pane(chart, traded_chart)})
