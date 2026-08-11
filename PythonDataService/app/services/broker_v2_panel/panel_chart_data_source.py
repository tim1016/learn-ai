"""Chart endpoint wiring for the Broker V2 panel.

This module keeps bar retrieval separate from the panel command/data facade.
When SQLite authority is active it receives only S2 ``FillRecord`` values;
legacy journal rows are normalized at this boundary before they reach the pure
chart projection service.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.broker.alpaca.clerk.fills import FillRecord, project_instance_fills
from app.config import settings
from app.data_lake.polygon_fetcher import fetch_aggregate_bars
from app.schemas.broker_v2_panel import (
    BotPanelView,
    ChartHistoryPreset,
    ChartHistoryResponse,
    ChartLiveResponse,
)
from app.services.broker_v2_panel.chart_projection_service import (
    build_history_chart,
    build_live_chart,
    history_fill_window,
    live_window,
)
from app.services.broker_v2_panel.panel_data_source import (
    PanelDataError,
    PanelUnavailableError,
    UnknownBotError,
    get_panel_with_chart_fills,
    read_legacy_chart_fills,
    read_legacy_chart_status,
    validate_panel_account_scope,
)
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqlitePanelEconomicUnavailable,
    read_sqlite_chart_evidence,
    read_sqlite_panel_evidence,
    sqlite_authority_active,
)
from app.services.live_chart_window import (
    ChartWindowError,
    ChartWindowResult,
    coerce_chart_timeframe,
    resolve_chart_window,
)
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
            is_streaming=False,
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


async def get_live_chart(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"] = "1m",
    now_ms: int | None = None,
) -> ChartLiveResponse:
    """Build the LIVE chart from SQLite or normalized legacy fills."""
    resolved = await validate_panel_account_scope(broker, account_id)
    observed_at_ms = now_ms_utc() if now_ms is None else now_ms
    if sqlite_authority_active(broker):
        try:
            evidence = await read_sqlite_panel_evidence(
                broker,
                resolved,
                sid,
                now_ms=observed_at_ms,
            )
        except SqlitePanelEconomicUnavailable as exc:
            raise PanelUnavailableError(
                "The activated SQLite chart evidence is unavailable.",
                detail=str(exc),
            ) from exc
        if evidence is None:
            raise UnknownBotError(f"No SQLite custody projection exists for bot '{sid}'.")
        status = evidence.status
        fills = evidence.economics.session_fills
    else:
        status = read_legacy_chart_status(broker, sid)
        fills = read_legacy_chart_fills(broker, resolved, sid)
    return await _build_live_chart_from_fills(
        sid,
        status.symbol,
        fills,
        resolution=resolution,
        now_ms=observed_at_ms,
    )


async def get_live_snapshot_parts(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> tuple[BotPanelView, ChartLiveResponse]:
    """Build panel and chart from the exact same authority-bound fill cut."""
    observed_at_ms = now_ms_utc()
    panel, entries, fills = await get_panel_with_chart_fills(
        broker,
        account_id,
        sid,
        now_ms=observed_at_ms,
    )
    if fills is None:
        if sqlite_authority_active(broker):
            raise PanelUnavailableError(
                "The panel chart fill evidence is unavailable.",
                detail="The active authority did not return an attributed fill set.",
            )
        fills = project_instance_fills(sid, entries)
    chart = await _build_live_chart_from_fills(
        sid,
        panel.symbol,
        fills,
        resolution=resolution,
        now_ms=observed_at_ms,
    )
    return panel, chart


async def get_history_chart(
    broker: str,
    account_id: str,
    sid: str,
    preset: ChartHistoryPreset,
) -> ChartHistoryResponse:
    """Build bounded history from SQLite facts or legacy compatibility fills."""
    resolved = await validate_panel_account_scope(broker, account_id)
    observed_at_ms = now_ms_utc()
    if sqlite_authority_active(broker):
        from_ms, to_ms = history_fill_window(preset, observed_at_ms)
        try:
            evidence = await read_sqlite_chart_evidence(
                broker,
                resolved,
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
            raise UnknownBotError(f"No SQLite custody projection exists for bot '{sid}'.")
        status = evidence.status
        fills = evidence.fills.fills
    else:
        status = read_legacy_chart_status(broker, sid)
        fills = read_legacy_chart_fills(broker, resolved, sid)

    async def _bar_source(symbol, start, end, multiplier, timespan):
        return await fetch_aggregate_bars(
            symbol,
            start,
            end,
            settings.POLYGON_API_KEY,
            multiplier=multiplier,
            timespan=timespan,
        )

    return await build_history_chart(
        preset,
        fills,
        strategy_instance_id=sid,
        symbol=status.symbol,
        bar_source=_bar_source,
        now_ms=observed_at_ms,
    )
