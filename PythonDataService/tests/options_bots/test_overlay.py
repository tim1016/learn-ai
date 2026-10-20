"""The overlay is the canonical backtest session answer on admitted lake bytes (#3368)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx

from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_backtest.session_market import window_only_market
from app.options_backtest.session_view import session_view_of
from app.options_bots.golden_proof import current_rules_digest
from app.options_bots.journal import OptionsBotJournal
from app.options_bots.session import OptionsSession, OptionsStep
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryResponse
from app.schemas.option_chart import ChartPremiumPane, PremiumPoint
from app.schemas.options_bot import OptionsBotConfiguration
from app.schemas.signal_program_seal import semantic_payload_hash
from app.services import options_bot_overlay as overlay
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._detail_scene import (
    CALL_500,
    NOW_MS,
    PUT_495,
    PUT_500,
    WED_OPEN_MS,
    run_the_scene,
    the_setup,
)
from tests.options_backtest._headers import a_session_record
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import WED


@pytest.fixture(autouse=True)
def clear_overlay_cache():
    overlay._replay.cache_clear()
    yield
    overlay._replay.cache_clear()


def _configuration(golden_id: str = "sealed-version-1") -> OptionsBotConfiguration:
    setup = the_setup()
    return OptionsBotConfiguration(golden_setup_id=golden_id, setup=setup,
        setup_sha256=semantic_payload_hash(setup.model_dump(mode="json")), rules_digest=current_rules_digest())


def _chart() -> ChartHistoryResponse:
    bars = [ChartBar(start_ms=WED_OPEN_MS + index * 60_000, end_ms=WED_OPEN_MS + (index + 1) * 60_000,
                    open="500.2", high="500.2", low="500.2", close="500.2", volume=1, source="polygon") for index in range(390)]
    return ChartHistoryResponse(strategy_instance_id="overlay-bot", symbol="SPY", timeframe="1m", from_ms=WED_OPEN_MS,
        to_ms=WED_OPEN_MS + 390 * 60_000, bars=bars, indicator_bars=[], indicator_bar_budget=0,
        indicator_bar_budget_satisfied=True, fill_markers=[], truncated=False, as_of_ms=NOW_MS)


@respx.mock
async def test_overlay_matches_the_session_answer_and_keeps_live_panes_first(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run_the_scene()
    expected = await session_view_of(a_session_record(result, 0))
    configuration = _configuration()
    folder = strategy_instance_artifact_dir(tmp_path, "live_state", "overlay-bot")
    journal = OptionsBotJournal(folder, strategy_instance_id="overlay-bot", setup_sha256=configuration.setup_sha256)
    try:
        session = OptionsSession(configuration.setup, window_only_market(WED))
        journal.commit(session.state, OptionsStep((), ()))
    finally:
        journal.close()
    registry = SimpleNamespace(artifacts_root=tmp_path, binding_for_control=lambda *_: SimpleNamespace(options_bot=configuration))
    monkeypatch.setattr(overlay, "get_bot_task_registry", lambda: registry)
    chart = _chart()
    from app.options.contract import OptionContract
    live = ChartPremiumPane(contract=OptionContract.from_occ_symbol(CALL_500), symbol=CALL_500, multiplier=100,
        leg_ids=["call"], entry_numbers=[0], points=[PremiumPoint(start_ms=bar.start_ms, value="99") for bar in chart.bars],
        rule_lines=[], fill_markers=[])
    chart = chart.model_copy(update={"premium_panes": [live]})
    answer = await overlay.with_options_overlay(chart, "alpaca", "overlay-bot", now_ms=NOW_MS)
    assert answer.backtest_notice.startswith("Same-day backtest ready")
    assert [pane.symbol for pane in answer.premium_panes] == [CALL_500, PUT_500, PUT_495]
    assert answer.premium_panes[0].points == live.points
    assert not answer.premium_panes[0].backtest_only
    assert all(pane.backtest_only for pane in answer.premium_panes[1:])
    levels = sorted((pane.symbol, line.kind, line.price, line.from_ms, line.to_ms, line.leg_id, line.entry_number)
                    for pane in answer.premium_panes for line in pane.rule_lines if line.source == "backtest")
    assert levels == sorted((line.contract.occ_symbol, line.kind.value, str(line.price), line.from_ms, line.to_ms, line.leg_id, line.entry_number) for line in expected.levels)
    fills = sorted((pane.symbol, fill.side, fill.quantity, fill.price, fill.filled_at_ms)
                   for pane in answer.premium_panes for fill in pane.fill_markers if fill.source == "backtest")
    assert fills == sorted((fill.contract.occ_symbol, fill.side, fill.contracts, float(fill.filled), fill.bar_close_ms - 60_000) for fill in expected.fills)
    assert len(fills) == 6 and len(levels) == 8
    assert all(point.value is None for point in answer.premium_panes[1].points)
    assert any(point.value is not None for point in answer.premium_panes[1].backtest_points)
    assert chart.premium_panes == [live], "the overlay does not mutate the journal's live answer"


@respx.mock
async def test_absent_and_partial_days_stay_not_ready_then_retry_and_cache_by_sealed_version(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration, chart = _configuration(), _chart()
    absent = await asyncio.to_thread(overlay.comparison_for_sessions, chart, configuration, (WED_OPEN_MS,), now_ms=NOW_MS)
    assert "Comparison not ready" in absent.backtest_notice and absent.premium_panes == []
    await run_the_scene()
    partial = await asyncio.to_thread(overlay.comparison_for_sessions, chart, configuration, (WED_OPEN_MS,), now_ms=WED_OPEN_MS + 60_000)
    assert "partial day is never compared" in partial.backtest_notice and partial.premium_panes == []
    calls = []
    canonical = overlay.replay_session
    def counted(*args, **kwargs):
        calls.append(args[0])
        return canonical(*args, **kwargs)
    monkeypatch.setattr(overlay, "replay_session", counted)
    for approved in (configuration, configuration, _configuration("sealed-version-2")):
        answer = await asyncio.to_thread(overlay.comparison_for_sessions, chart, approved, (WED_OPEN_MS,), now_ms=NOW_MS)
        assert answer.backtest_notice.startswith("Same-day backtest ready")
    assert calls == [configuration.setup, configuration.setup]
    mismatch = configuration.model_copy(update={"rules_digest": "0" * 64})
    refused = await asyncio.to_thread(overlay.comparison_for_sessions, chart, mismatch, (WED_OPEN_MS,), now_ms=NOW_MS)
    assert "sealed rules differ" in refused.backtest_notice and refused.premium_panes == []
