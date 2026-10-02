"""A saved backtest's strategy view is a replay of the run itself, shown only when it reproduces the run (#2639 D13)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import polygon_mode_for
from app.engine.strategy.registry import strategy_program_version
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.research.backtest_runs.records import persisted_execution_configuration
from app.research.backtest_runs.repository import RunDetail, TradeRow
from app.routers import backtest_runs
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services.backtest_run_strategy_view import build_backtest_run_strategy_view
from app.services.engine_backtest_service import SavedRunNotReplayable, execute_engine_backtest
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.lake_fixture import seed_lake_daily, seed_lake_minute_day

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

WARMUP_DAY = date(2026, 1, 5)
EVALUATED_DAY = next_trading_day(WARMUP_DAY)
PARAMS = {"symbol": "SPY", "rsi_min": 45, "rsi_max": 75}


@pytest.fixture
def two_day_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two seeded SPY sessions: the run's warmup day, then its one evaluated day."""
    write_root = tmp_path / "lean-data-writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    adjusted_root = write_root / lake_subpath(polygon_mode_for(True))
    for day in (WARMUP_DAY, EVALUATED_DAY):
        seed_lake_minute_day(adjusted_root, "SPY", day)
    seed_lake_daily(adjusted_root, "SPY", [WARMUP_DAY, EVALUATED_DAY])


@pytest.fixture
def saved_run(two_day_store: None) -> RunDetail:
    """A primed engine run, as the run history stores it."""
    request = EngineBacktestRequest(
        strategy_name="ema_crossover_signal",
        params=PARAMS,
        from_date=EVALUATED_DAY.isoformat(),
        to_date=EVALUATED_DAY.isoformat(),
        warmup_from_date=WARMUP_DAY.isoformat(),
        save_study=False,
    )
    response = execute_engine_backtest(request=request, on_phase=lambda _phase: None, on_log=lambda _line: None)
    assert response.success, response.error
    return RunDetail(
        id=7,
        source="engine",
        strategy_name=response.strategy_name,
        program_version=strategy_program_version(response.strategy_name),
        execution_config_json=json.dumps(
            persisted_execution_configuration(
                compatibility_profile=None, warmup_from_date=request.warmup_from_date, slippage_per_share=0.0
            )
        ),
        symbol="SPY",
        lean_run_id=None,
        parameters_json=json.dumps(PARAMS),
        start_ms=et_midnight_ms(EVALUATED_DAY),
        end_ms=et_midnight_ms(EVALUATED_DAY),
        executed_at_ms=0,
        total_trades=response.total_trades,
        total_pnl=response.net_profit,
        commission_per_order=0.0,
        brokerage_policy=None,
        notes=None,
        data_policy_json=None if response.data_policy is None else response.data_policy.model_dump_json(),
        verdict_grade=None,
        verdict_signal=None,
        parity_group_id=None,
        requested_engine="python",
        fill_mode=response.fill_mode,
        timespan="minute",
        duration_ms=0,
        winning_trades=response.winning_trades,
        losing_trades=response.losing_trades,
        win_rate=response.win_rate,
        initial_cash=response.initial_cash,
        final_equity=response.final_equity,
        total_fees=response.total_fees,
        max_drawdown=0.0,
        sharpe_ratio=None,
        sortino_ratio=None,
        profit_factor=None,
        lean_statistics_json=None,
        lean_analysis_json=None,
        run_verdict_json=None,
        verdict_version=None,
        equity_curve_json=None,
        validation_analytics_json=None,
        insight_summary_json=None,
        metric_documentation_json=None,
        trades=tuple(
            TradeRow(
                id=trade.trade_number,
                trade_number=trade.trade_number,
                entry_ms=trade.entry_time,
                exit_ms=trade.exit_time,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                pnl=trade.pnl_pts,
                signal_reason=trade.signal_reason,
                is_synthetic_exit=trade.is_synthetic_exit,
            )
            for trade in response.trades
        ),
        trades_truncated=False,
        parity_verdicts=(),
        evidence_provenance_json=(
            None if response.evidence_provenance is None else response.evidence_provenance.model_dump_json()
        ),
    )


def test_a_saved_runs_view_replays_its_own_decisions_behind_its_warmup(saved_run: RunDetail) -> None:
    view = build_backtest_run_strategy_view(saved_run)

    phases = [candle.phase for candle in view.candles]
    assert phases == ["before_start"] * 26 + ["decision"] * 26
    assert all(candle.bar_close_ms <= saved_run.start_ms for candle in view.candles[:26])
    assert view.run_started_at_ms == session_open_ms_utc(EVALUATED_DAY)
    assert view.run_id == "backtest-run:7"
    # The run's deployed band labels the default gate.
    assert view.declaration.gates[0].label == "RSI in 45–75"
    assert {candle.outcome for candle in view.candles[26:]} <= {"no_action", "enter_intent", "exit_intent"}


def test_a_replay_that_does_not_reproduce_the_runs_trades_is_refused(saved_run: RunDetail) -> None:
    assert saved_run.total_trades == 3
    with pytest.raises(SavedRunNotReplayable, match=r"made 3 trades, not its 4"):
        build_backtest_run_strategy_view(replace(saved_run, total_trades=4))
    first, *rest = saved_run.trades
    moved = replace(saved_run, trades=(replace(first, entry_price=first.entry_price + 0.01), *rest))
    with pytest.raises(SavedRunNotReplayable, match="changed its trade 1"):
        build_backtest_run_strategy_view(moved)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"source": "lean-sidecar"}, "This is a LEAN run"),
        ({"program_version": "an-older-program"}, "The strategy has changed since this run"),
        ({"program_version": None}, "recorded no program version"),
        ({"data_policy_json": None}, "recorded no data policy"),
    ],
)
def test_a_run_that_cannot_be_replayed_exactly_says_why(
    saved_run: RunDetail, change: dict[str, object], reason: str
) -> None:
    with pytest.raises(SavedRunNotReplayable, match=reason):
        build_backtest_run_strategy_view(replace(saved_run, **change))


async def test_the_route_answers_a_refusal_with_its_reason(
    saved_run: RunDetail, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = {7: replace(saved_run, source="lean-sidecar")}

    async def read(function: object, run_id: int) -> RunDetail | None:
        return runs.get(run_id)

    monkeypatch.setattr(backtest_runs, "with_connection", read)
    app = FastAPI()
    app.include_router(backtest_runs.router, prefix="/api/research/backtest-runs")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.get("/api/research/backtest-runs/7/strategy-view")
        missing = await client.get("/api/research/backtest-runs/8/strategy-view")

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "STRATEGY_VIEW_NOT_REPLAYABLE"
    assert refused.json()["detail"]["message"].startswith("This is a LEAN run")
    assert missing.status_code == 404
