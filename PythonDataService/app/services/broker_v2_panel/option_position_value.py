"""A structure's close-now cost from retained minute quotes and actual holdings (#3368).

Formula: canonical FIFO holdings at each minute's close (or the live read), valued by canonical exit_estimate.
Reference: ADR 0076 decision 28 and #3368.
Canonical implementation: app/broker/alpaca/clerk/fifo_pnl.py and exit_estimate.py (called here).
Validated against: tests/broker/v2panel/test_option_position_value.py.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.exit_estimate import OwedClose, exit_estimate
from app.broker.alpaca.clerk.fifo_pnl import LotWriteOff, compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuote, KeptOptionQuotes, KeptOptionQuotesUnavailable
from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError, SqliteEconomicProjectionReader
from app.broker.contract.models import OrderSide
from app.options.exact import whole_units
from app.options.requirement import ContractAtMultiplier
from app.schemas.broker_v2_panel import (
    ChartHistoryResponse,
    ChartLiveResponse,
    ChartOverlayNoticeView,
    ChartPositionPane,
    ChartPositionPoint,
)
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.chart_projection_service import fill_to_marker
from app.services.broker_v2_panel.exit_estimate_read import (
    exit_estimate_view,
    minute_exit_estimate,
    owed_closes_for_position,
)

logger = logging.getLogger(__name__)


def held_at(fills: Sequence[FillRecord], at_ms: int, write_offs: Sequence[LotWriteOff] = ()) -> dict[ContractAtMultiplier, int]:
    """Canonical FIFO holdings at the observation; whole contracts, with their recorded multipliers."""
    held: dict[ContractAtMultiplier, int] = {}
    result = compute_fifo_pnl((fill for fill in fills if fill.filled_at_ms <= at_ms),
                              write_offs=(event for event in write_offs if event.written_off_at_ms <= at_ms))
    for lot in result.open_lots:
        if lot.multiplier is None or lot.written_off:
            continue
        quantity = whole_units(lot.exact_qty, units_per_one=1, what="held option quantity", unit="contracts")
        key = (lot.symbol, lot.multiplier)
        held[key] = held.get(key, 0) + (quantity if lot.side is OrderSide.BUY else -quantity)
    return {key: value for key, value in held.items() if value}


def position_value_pane(
    chart: ChartLiveResponse | ChartHistoryResponse, fills: Sequence[FillRecord], rows: Sequence[KeptOptionQuote],
    *, symbol: str, through_ms: int, write_offs: Sequence[LotWriteOff] = (), owed_closes: Sequence[OwedClose] | None = (),
) -> ChartPositionPane:
    lower, upper = ((chart.trading_date_open_ms, chart.trading_date_close_ms)
                    if isinstance(chart, ChartLiveResponse) else (chart.from_ms, chart.to_ms))
    minutes: dict[int, list[KeptOptionQuote]] = defaultdict(list)
    for row in rows:
        if row.minute_ms is not None and row.kept_at_ms <= through_ms:
            minutes[row.minute_ms].append(row)
    values: dict[int, str | None] = {}
    for minute, quotes in minutes.items():
        at_ms = min(minute + 59_999, through_ms)
        held = held_at(fills, at_ms, write_offs)
        if not held:
            values[minute] = None
            continue
        available = {row.symbol: row.quote.held for row in quotes}
        complete = held and all(
            (quote := available.get(contract)) is not None and quote.bid is not None and quote.ask is not None
            for contract, _ in held
        )
        estimate = exit_estimate(held, available, now_ms=at_ms, quotes_from="Saved minute quotes", none_held="No saved minute quote")
        figure = estimate.net_close
        values[minute] = dollars(figure.amount_cents) if complete and figure.state == "known" and figure.amount_cents is not None else None
    points = []
    for bar in chart.bars:
        # A value minute sits on its ending bar on a finer tape, with no invented 5-second samples.
        if bar.end_ms - bar.start_ms < 60_000 and bar.end_ms % 60_000:
            continue
        minute = (bar.end_ms - 1) // 60_000 * 60_000
        observed = [at for at in values if bar.start_ms <= at < bar.end_ms] if bar.end_ms - bar.start_ms >= 60_000 else [minute]
        points.append(ChartPositionPoint(start_ms=bar.start_ms, value=values.get(max(observed)) if observed else None))
    current_held = held_at(fills, through_ms, write_offs) if isinstance(chart, ChartLiveResponse) else {}
    current_estimate = None if not current_held else minute_exit_estimate(current_held, rows, now_ms=through_ms, owed_closes=owed_closes)
    if isinstance(chart, ChartLiveResponse) and lower <= through_ms < upper:
        step = 5_000 if chart.resolution == "5s" else 60_000
        start = through_ms // step * step
        figure = None if current_estimate is None else current_estimate.net_close
        value = dollars(figure.amount_cents) if figure is not None and figure.state == "known" and figure.amount_cents is not None else None
        points = [point for point in points if point.start_ms != start]
        points.append(ChartPositionPoint(start_ms=start, value=value))
        points.sort(key=lambda point: point.start_ms)
    markers = [fill_to_marker(fill) for fill in fills
               if fill.multiplier is not None and lower <= fill.filled_at_ms < upper and fill.filled_at_ms <= through_ms]
    return ChartPositionPane(
        kind="value", symbol=symbol, heading="Close-now position value · positive paid, negative received",
        points=points, fill_markers=markers,
        exit_estimate=None if current_estimate is None else exit_estimate_view(current_estimate),
        overlay_notices=[] if rows else [ChartOverlayNoticeView(
            code="option_minutes_unavailable", message="No held-leg quote minutes have been saved for this window.", source="ibkr",
        )],
    )


async def with_option_position(
    chart: ChartLiveResponse | ChartHistoryResponse, broker: str, sid: str, *, symbol: str, through_ms: int, expected_revision: int,
) -> ChartLiveResponse | ChartHistoryResponse:
    registry = get_bot_task_registry()
    binding = None if registry is None else registry.binding_for_control(broker, sid)
    runtime = None if binding is None or binding.sealed_account_id is None else get_clerk_runtime(binding.sealed_account_id)
    repo = None if runtime is None else runtime.sqlite_repository

    def read() -> ChartPositionPane:
        if repo is None:
            raise ValueError("The selected Clerk's option holdings are unavailable.")
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            evidence = reader.bot_option_value_history(sid)
            if evidence is None:
                raise ValueError("The bot's option fill history is unavailable.")
            if evidence.control_revision != expected_revision:
                raise ValueError("The bot's holdings changed while the chart was read. Refresh for the current value.")
            lower, upper = ((chart.trading_date_open_ms, chart.trading_date_close_ms)
                            if isinstance(chart, ChartLiveResponse) else (chart.from_ms, chart.to_ms))
            rows = KeptOptionQuotes.beside(repo.db_path).minutes(from_ms=lower, to_ms=min(upper, through_ms + 1))
            return position_value_pane(chart, evidence.fills, rows, symbol=symbol, through_ms=through_ms,
                                       write_offs=evidence.write_offs, owed_closes=owed_closes_for_position(repo, sid, now_ms=through_ms))
        finally:
            reader.close()

    try:
        pane = await asyncio.to_thread(read)
    except (ValueError, EconomicProjectionError, KeptOptionQuotesUnavailable, sqlite3.Error) as exc:
        logger.exception("Option position value is unavailable", extra={"action": "option_position_value_unavailable", "strategy_instance_id": sid})
        pane = position_value_pane(chart, (), (), symbol=symbol, through_ms=through_ms).model_copy(update={
            "overlay_notices": [ChartOverlayNoticeView(code="option_value_unavailable", message=str(exc), source="ibkr")],
        })
    return chart.model_copy(update={"position": pane})
