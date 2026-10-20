"""Draw saved Options observations and rule intervals, never re-fetch premiums."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from app.broker.alpaca.clerk.fills import FillRecord
from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_backtest.results import EventKind
from app.options_bots.journal import JOURNAL_FILENAME, OptionsBotJournal
from app.schemas.broker_v2_panel import ChartHistoryResponse, ChartLiveResponse
from app.schemas.option_chart import ChartPremiumPane, PremiumFill, PremiumPoint
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.chart_projection_service import ChartWindowError, fill_to_marker

logger = logging.getLogger(__name__)
EXIT_WORDS = {"stop": "Stop", "square_off": "Square-off", "closed_with_its_protection": "Closed with its protection",
              "exit_time": "Exit time", "clerk_reduction": "Closed by Clerk"}


def options_chart_binding(broker: str, sid: str) -> BrokerBotBinding | None:
    registry = get_bot_task_registry()
    if registry is None:
        return None
    binding = registry.binding_for_control(broker, sid)
    return binding if binding.options_bot is not None else None


def require_options_minute(broker: str, sid: str, interval: str) -> None:
    if interval != "1m" and options_chart_binding(broker, sid) is not None:
        raise ChartWindowError("Options bot charts use completed 1-minute bars only.")


def with_options_panes(
    chart: ChartLiveResponse | ChartHistoryResponse, broker: str, sid: str, fills: Sequence[FillRecord],
) -> ChartLiveResponse | ChartHistoryResponse:
    registry = get_bot_task_registry()
    assert registry is not None
    folder = strategy_instance_artifact_dir(registry.artifacts_root, "live_state", sid)
    if not (folder / JOURNAL_FILENAME).is_file():
        return chart.model_copy(update={"premium_notice": "Saved premium minutes are not available for this run."})
    journal = None
    try:
        journal = OptionsBotJournal(folder)
        lower, upper = ((chart.trading_date_open_ms, chart.trading_date_close_ms)
                        if isinstance(chart, ChartLiveResponse) else (chart.from_ms, chart.to_ms))
        panes = premium_panes(journal, chart, fills, lower, upper)
        return chart.model_copy(update={"premium_panes": panes})
    except (sqlite3.Error, ValueError) as exc:
        logger.exception("Options chart journal could not be read", extra={"action": "options_chart_read_failed", "strategy_instance_id": sid})
        return chart.model_copy(update={"premium_notice": f"Saved premium minutes cannot be read: {exc}"})
    finally:
        if journal is not None:
            journal.close()


def premium_panes(
    journal: OptionsBotJournal, chart: ChartLiveResponse | ChartHistoryResponse,
    fills: Sequence[FillRecord], lower: int, upper: int,
) -> list[ChartPremiumPane]:
    panes: dict[str, ChartPremiumPane] = {}
    for state in journal.sessions(lower, upper):
        minutes = journal.minutes(state.session_open_ms)
        reasons = journal.fill_reasons(state.session_open_ms)
        for event in state.events:
            if event.kind is not EventKind.PICKED or event.contract is None:
                continue
            symbol = event.contract.occ_symbol
            if symbol not in panes:
                panes[symbol] = ChartPremiumPane(
                    contract=event.contract, symbol=symbol, multiplier=100,
                    leg_ids=[], entry_numbers=[], points=[PremiumPoint(start_ms=b.start_ms, value=None) for b in chart.bars],
                    rule_lines=[], fill_markers=[],
                )
            pane = panes[symbol]
            if event.leg_id not in pane.leg_ids:
                pane.leg_ids.append(event.leg_id)
            if event.entry_number not in pane.entry_numbers:
                pane.entry_numbers.append(event.entry_number)
        for symbol, pane in panes.items():
            observed = {m.close_ms: m.bar for m in minutes if m.symbol == symbol}
            for point, bar in zip(pane.points, chart.bars, strict=True):
                minute = observed.get(bar.end_ms)
                if minute is not None:
                    point.value = str(minute.close)
            pane.rule_lines.extend(journal.rules(state.session_open_ms, symbol))
            if not observed and not any(point.value is not None for point in pane.points):
                pane.notice = "No saved premium minutes for this contract in this session."
            elif observed:
                pane.notice = None
            for fill in fills:
                owned = (fill.order_ref, fill.symbol)
                if fill.symbol != symbol or not lower <= fill.filled_at_ms < upper:
                    continue
                if owned not in reasons and not state.session_open_ms <= fill.filled_at_ms < state.session_close_ms:
                    continue
                if any(marker.event_key == fill.event_key for marker in pane.fill_markers):
                    continue
                marker = fill_to_marker(fill)
                pane.fill_markers.append(PremiumFill(**marker.model_dump(), exit_reason=EXIT_WORDS.get(reasons.get(owned))))
                if owned not in reasons:
                    pane.notice = "Some fill reasons were not recorded with this session."
    return list(panes.values())
