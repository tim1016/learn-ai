"""Saved decision minutes, atomicity and the chart HTTP contract (#3368)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_backtest.market import MinuteBar
from app.options_bots.journal import JOURNAL_FILENAME, OptionsBotJournal
from app.options_bots.observations import ConsumedMinute
from app.options_bots.session import OptionsSession, OptionsStep
from app.routers.broker_v2_panel import router
from app.schemas.broker_v2_panel import ChartBar, ChartHistoryResponse
from app.schemas.fleet_history_batch import HistoryBatchResponse
from app.services import options_bot_chart
from app.services.broker_v2_panel import panel_chart_data_source as source
from tests.options_backtest._market import D, TableMarket, at, contract, flat, leg, picked, setup, steady
from tests.options_bots.test_options_bot_runtime import Clerk, Clock, _binding, _duty
from tests.options_bots.test_session import _observed

CALL = contract("call", "500")


async def test_premium_fills_use_their_recorded_attempts_exit_reason(tmp_path: Path) -> None:
    clock = Clock(at(5) + 8_000)
    clerk = Clerk(clock)
    duty, journal, _ = _duty(tmp_path, _binding(setup(leg(stop=("percent", "25")))), clock, clerk)
    for minute in (5, 6, 7, 8):
        clock.minute(minute)
        await duty.tick()
    rows = [fill for snapshot in clerk.snapshots.values() for fill in (*snapshot.entry_fills, *snapshot.close_fills)]
    fills = [FillRecord(account_id="paper-test", sid="options-bot", intent_id="intent", order_ref=row.order_ref,
                        event_key=str(index), symbol=row.contract, side=OrderSide(row.side), quantity=row.quantity,
                        fill_price=row.price, filled_at_ms=row.filled_at_ms, fee=None, multiplier=row.multiplier)
             for index, row in enumerate(rows)]
    bars = [ChartBar(start_ms=at(m - 1), end_ms=at(m), open="500", high="500", low="500", close="500", volume=1, source="ibkr") for m in range(5, 10)]
    chart = ChartHistoryResponse(strategy_instance_id="options-bot", symbol="SPY", timeframe="1m", from_ms=at(0), to_ms=at(60),
                                 bars=bars, indicator_bars=bars, indicator_bar_budget=0, indicator_bar_budget_satisfied=True,
                                 fill_markers=[], truncated=False, as_of_ms=at(60))
    pane = options_bot_chart.premium_panes(journal, chart, fills, at(0), at(60))[0]
    assert [fill.exit_reason for fill in pane.fill_markers] == [None, "Stop"]
    assert {fill.order_ref for fill in pane.fill_markers} == {row.order_ref for row in rows}
    await duty.close()
    journal.close()


async def test_runtime_records_consumed_minutes_once_and_keeps_first_correction(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(at(5) + 8_000)
    duty, journal, _ = _duty(tmp_path, _binding(setup(leg(momentum=("percent_up", "25")))), clock, Clerk(clock))
    await duty.tick()
    first = journal.minutes(at(0))
    assert {(m.symbol, m.close_ms) for m in first} == {("SPY", at(5)), (CALL.occ_symbol, at(5))}
    await duty.tick()
    assert journal.minutes(at(0)) == first
    changed = ConsumedMinute(CALL.occ_symbol, at(5), MinuteBar(at(5), D(9), D(9), D(9), D(9)))
    journal.commit(journal.state, OptionsStep((), ()), minutes=(changed,), read_at_ms=clock())
    assert journal.minutes(at(0)) == first
    assert journal.ignored_corrections() == 1
    assert any(getattr(record, "action", None) == "options_consumed_minute_correction_ignored" for record in caplog.records)
    await duty.close()
    journal.close()


def test_failed_session_commit_rolls_back_its_consumed_minutes(tmp_path: Path) -> None:
    journal = OptionsBotJournal(tmp_path, strategy_instance_id="options-bot", setup_sha256="a" * 64)
    rules = OptionsSession(setup(leg()), TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2", 5, 8)}))
    step = rules.advance(at(5))
    with sqlite3.connect(tmp_path / JOURNAL_FILENAME) as conn:
        conn.execute("CREATE TRIGGER crash BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="crash"):
        journal.commit(rules.state, step, minutes=(ConsumedMinute(CALL.occ_symbol, at(5), MinuteBar(at(5), D(2), D(2), D(2), D(2))),), read_at_ms=at(5))
    assert journal.state is None
    assert journal.minutes(at(0)) == ()
    assert journal.decisions() == ()
    journal.close()


async def test_history_route_exposes_saved_gaps_and_partial_fill_rule_intervals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written = setup(leg(contracts=3, momentum=("percent_up", "25"), stop=("percent", "25")))
    folder = strategy_instance_artifact_dir(tmp_path, "live_state", "options-bot")
    journal = OptionsBotJournal(folder, strategy_instance_id="options-bot", setup_sha256="a" * 64)
    market = TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: {5: ("2", "2"), 7: ("2.6", "2.6"), 8: ("3", "3")}})
    rules = OptionsSession(written, market)
    for minute in (5, 6, 7):
        journal.commit(rules.state, rules.advance(at(minute)), minutes=(ConsumedMinute(CALL.occ_symbol, at(minute), market.option_bar(CALL, at(minute))),), read_at_ms=at(minute))
    journal.commit(rules.state, rules.advance(at(8), (_observed("partial", minute=8, quantity=1, price="3", terminal=False),)), read_at_ms=at(8) + 8_000)
    full = _observed("full", minute=9, quantity=3, price="4")
    full = replace(full, fill=replace(full.fill, bar_close_ms=at(8) + 30_000))
    journal.commit(rules.state, rules.advance(at(9), (full,)), read_at_ms=at(9) + 8_000)
    journal.close()
    binding = _binding(written)
    monkeypatch.setattr(options_bot_chart, "get_bot_task_registry", lambda: SimpleNamespace(artifacts_root=tmp_path, binding_for_control=lambda *_: binding))
    async def validate(*_args):
        return "paper-test"
    async def evidence(*_args, **_kwargs):
        return SimpleNamespace(status=SimpleNamespace(symbol="SPY", traded=None, options_bot=binding.options_bot), fills=SimpleNamespace(fills=[]))
    bars = [ChartBar(start_ms=at(m - 1), end_ms=at(m), open="500", high="500", low="500", close="500", volume=1, source="polygon") for m in range(5, 10)]
    async def batch(_query):
        return HistoryBatchResponse(bars=bars, source="polygon", overlay_notices=[], effective_as_of_ms=at(10))
    monkeypatch.setattr(source, "validate_account", validate)
    monkeypatch.setattr(source, "read_sqlite_chart_evidence", evidence)
    monkeypatch.setattr(source, "build_history_batch_provider", lambda: batch)
    monkeypatch.setattr(source, "now_ms_utc", lambda: at(10))
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/accounts/paper-test/bots/options-bot/chart/history?timeframe=1m")
        assert response.status_code == 200, response.text
        pane = response.json()["premium_panes"][0]
        assert [p["value"] for p in pane["points"]] == ["2", None, "2.6", None, None]
        assert [(D(r["price"]), r["from_ms"], r["to_ms"]) for r in pane["rule_lines"] if r["kind"] == "stop"] == [(D("2.25"), at(8), at(8) + 30_000), (D("3"), at(8) + 30_000, None)]
        assert next(r for r in pane["rule_lines"] if r["kind"] == "momentum")["to_ms"] == at(7)
        refused = await client.get("/api/brokers/alpaca/accounts/paper-test/bots/options-bot/chart/history?timeframe=15m")
        assert refused.status_code == 422
