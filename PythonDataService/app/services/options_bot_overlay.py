"""Compare a retained live session with its sealed setup on a complete lake day (#3368).

Formula: one canonical Options session replay; canonical levels and fills copied for display.
Reference: ADR 0079 and #3368.
Canonical implementation: app/options_backtest/replay.py and session_levels.py (called here).
Validated against: tests/options_bots/test_overlay.py.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.engine.live.identity import strategy_instance_artifact_dir
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.data import Captured
from app.options_backtest.fees import fee_for_policy
from app.options_backtest.lake_data import LakeRunData, UnderlyingDayUnreadable
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import STANDARD_MULTIPLIER
from app.options_backtest.replay import replay_session
from app.options_backtest.results import EventKind, SessionOutcome
from app.options_backtest.run import SessionDataMissing, load_session_market
from app.options_backtest.session_levels import fills_of, levels_of
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from app.options_bots.golden_proof import GoldenProofError, current_rules_digest
from app.options_bots.journal import JOURNAL_FILENAME, OptionsBotJournal
from app.schemas.broker_v2_panel import ChartHistoryResponse, ChartLiveResponse
from app.schemas.option_chart import ChartPremiumPane, ChartRuleLine, PremiumFill, PremiumPoint
from app.schemas.options_bot import OptionsBotConfiguration
from app.services.bot_runner import get_bot_task_registry
from app.services.options_bot_chart import EXIT_WORDS
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)


class _ReplayData(LakeRunData):
    """Keep the exact bars the replay read so its faint series uses those same bytes."""

    def __init__(self, underlying: str) -> None:
        super().__init__(underlying)
        self.days: dict[OptionContract, tuple[MinuteBar, ...]] = {}

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[tuple[MinuteBar, ...]] | None:
        captured = super().option_day(contract, trading_date)
        if captured is not None:
            self.days[contract] = captured.value
        return captured


@dataclass(frozen=True)
class _Replay:
    setup: OptionsBacktestSetup
    outcome: SessionOutcome
    days: dict[OptionContract, tuple[MinuteBar, ...]]


@lru_cache(maxsize=64)
def _replay(
    golden_id: str, setup_sha256: str, rules_digest: str, setup_json: str, session_open_ms: int,
) -> _Replay:
    setup = OptionsBacktestSetup.model_validate_json(setup_json)
    data = _ReplayData(setup.underlying)
    market, _pins = load_session_market(setup, et_date_at_ms(session_open_ms), data)
    outcome = replay_session(setup, market, cash_usd=setup.starting_cash_usd, fee_for=fee_for_policy(setup.fees))
    if current_rules_digest() != rules_digest:
        raise GoldenProofError("The sealed Options rules changed while the comparison ran.")
    return _Replay(setup, outcome, data.days)


def add_overlay(
    chart: ChartLiveResponse | ChartHistoryResponse, replay: _Replay,
) -> ChartLiveResponse | ChartHistoryResponse:
    """Use the backtest session answer's vocabulary, preserving live pane order and prices."""
    panes = {pane.symbol: pane.model_copy(deep=True) for pane in chart.premium_panes}
    lower, upper = ((chart.trading_date_open_ms, chart.trading_date_close_ms)
                    if isinstance(chart, ChartLiveResponse) else (chart.from_ms, chart.to_ms))
    for event in replay.outcome.events:
        if event.kind is not EventKind.PICKED or event.contract is None:
            continue
        contract = event.contract
        symbol = contract.occ_symbol
        if symbol not in panes:
            panes[symbol] = ChartPremiumPane(
                contract=contract, symbol=symbol, multiplier=STANDARD_MULTIPLIER,
                leg_ids=[], entry_numbers=[], points=[PremiumPoint(start_ms=bar.start_ms, value=None) for bar in chart.bars],
                rule_lines=[], fill_markers=[], backtest_only=True,
                notice="Backtest only: the live bot did not pick this contract.",
            )
        pane = panes[symbol]
        if event.leg_id not in pane.leg_ids:
            pane.leg_ids.append(event.leg_id)
        if event.entry_number not in pane.entry_numbers:
            pane.entry_numbers.append(event.entry_number)
        if pane.backtest_only:
            closes = {bar.close_ms: str(bar.close) for bar in replay.days.get(contract, ())}
            existing = {point.start_ms: point.value for point in pane.backtest_points}
            pane.backtest_points = [PremiumPoint(start_ms=bar.start_ms, value=closes.get(bar.end_ms, existing.get(bar.start_ms))) for bar in chart.bars]
    for level in levels_of(replay.setup, replay.outcome):
        panes[level.contract.occ_symbol].rule_lines.append(ChartRuleLine(
            kind=level.kind.value, price=str(level.price), from_ms=level.from_ms, to_ms=level.to_ms,
            leg_id=level.leg_id, entry_number=level.entry_number, source="backtest",
        ))
    for index, fill in enumerate(fills_of(replay.outcome.trades)):
        # The engine names a bar by its close; its modeled fill is at that bar's open.
        at_ms = fill.bar_close_ms - MS_PER_MINUTE
        if lower <= at_ms < upper:
            panes[fill.contract.occ_symbol].fill_markers.append(PremiumFill(
                filled_at_ms=at_ms, side=fill.side, quantity=fill.contracts, price=float(fill.filled),
                symbol=fill.contract.occ_symbol, order_ref=f"backtest:{fill.leg_id}:{fill.entry_number}",
                event_key=f"backtest:{fill.bar_close_ms}:{index}",
                exit_reason=None if fill.exit_reason is None else EXIT_WORDS[fill.exit_reason.value], source="backtest",
            ))
    return chart.model_copy(update={"premium_panes": list(panes.values())})


def comparison_for_sessions(
    chart: ChartLiveResponse | ChartHistoryResponse, configuration: OptionsBotConfiguration,
    session_opens: tuple[int, ...], *, now_ms: int,
) -> ChartLiveResponse | ChartHistoryResponse:
    if configuration.rules_digest != current_rules_digest():
        return chart.model_copy(update={"backtest_notice": "Comparison not ready: this deployment's sealed rules differ from the running replay."})
    reasons = []
    if not session_opens:
        reasons.append("the saved session identity is unavailable")
    for opened in session_opens:
        window = session_window_for_date(et_date_at_ms(opened))
        if now_ms < window.close_ms_utc:
            reasons.append("the session has not finished; a partial day is never compared")
            continue
        try:
            replay = _replay(configuration.golden_setup_id, configuration.setup_sha256, configuration.rules_digest,
                             configuration.setup.model_dump_json(), opened)
            chart = add_overlay(chart, replay)
        except (SessionDataMissing, LakeAdmissionError, CatalogUnavailableError, OptionBarsFileError,
                OptionListingFileError, UnderlyingDayUnreadable, GoldenProofError, ValueError, OSError) as exc:
            logger.info("Options comparison is not ready", extra={"action": "options_overlay_not_ready", "session_open_ms": opened, "reason": str(exc)})
            reasons.append(f"the complete Polygon day is not available in the lake ({exc})")
    notice = "Comparison not ready: " + "; ".join(dict.fromkeys(reasons)) + "." if reasons else "Same-day backtest ready · faint markers and dashed rule lines."
    return chart.model_copy(update={"backtest_notice": notice})


async def with_options_overlay(
    chart: ChartLiveResponse | ChartHistoryResponse, broker: str, sid: str, *, now_ms: int,
) -> ChartLiveResponse | ChartHistoryResponse:
    registry = get_bot_task_registry()
    if registry is None:
        return chart.model_copy(update={"backtest_notice": "Comparison not ready: the saved session identity is unavailable."})
    binding = registry.binding_for_control(broker, sid)
    assert binding.options_bot is not None
    folder = strategy_instance_artifact_dir(registry.artifacts_root, "live_state", sid)

    def read() -> ChartLiveResponse | ChartHistoryResponse:
        lower, upper = ((chart.trading_date_open_ms, chart.trading_date_close_ms)
                        if isinstance(chart, ChartLiveResponse) else (chart.from_ms, chart.to_ms))
        opens: tuple[int, ...] = ()
        if (Path(folder) / JOURNAL_FILENAME).is_file():
            journal = OptionsBotJournal(folder)
            try:
                opens = tuple(state.session_open_ms for state in journal.sessions(lower, upper))
            finally:
                journal.close()
        return comparison_for_sessions(chart, binding.options_bot, opens, now_ms=now_ms)

    try:
        return await asyncio.to_thread(read)
    except (sqlite3.Error, ValueError, GoldenProofError) as exc:
        logger.exception("Options comparison could not be read", extra={"action": "options_overlay_read_failed", "strategy_instance_id": sid})
        return chart.model_copy(update={"backtest_notice": f"Comparison not ready: {exc}"})
