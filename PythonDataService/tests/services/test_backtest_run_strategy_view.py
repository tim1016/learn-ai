"""A saved backtest's strategy view is a replay of the run itself, shown only when it reproduces the run (#2639 D13)."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.config import settings
from app.data_lake import run_materialization
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import polygon_mode_for
from app.engine.strategy.registry import strategy_program_version
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.research.backtest_runs.evidence_provenance import RunEvidenceProvenance
from app.research.backtest_runs.records import persisted_execution_configuration
from app.research.backtest_runs.repository import REPORT_TRADE_LIMIT, RunDetail, TradeRow
from app.routers import backtest_runs
from app.schemas.engine_backtest import EngineBacktestRequest
from app.schemas.strategy_gates import CustomGateInput, GateCandle, GateEvaluationRequest
from app.schemas.strategy_view import StrategyViewResponse
from app.services import backtest_run_strategy_view
from app.services.backtest_run_strategy_view import _request_from_run, build_backtest_run_strategy_view
from app.services.engine_backtest_service import SavedRunNotReplayable, execute_engine_backtest
from app.services.engine_bars_service import read_consolidated_bars
from app.services.strategy_gates import DRAFT_GATE_ID, evaluate_gates
from app.services.strategy_view import ResolvedStrategyView
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests._helpers.lake_fixture import seed_lake_daily, seed_lake_minute_day

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

WARMUP_DAY = date(2026, 1, 5)
EVALUATED_DAY = next_trading_day(WARMUP_DAY)
PARAMS = {"symbol": "SPY", "rsi_min": 45, "rsi_max": 75}
# The lake state the saved run recorded, and that its window materializes against again.
RECEIPT = "a" * 64


@pytest.fixture(autouse=True)
def lake_receipt(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The lake answers the run's window with ``RECEIPT`` unless a test changes it."""
    current = {"hash": RECEIPT}
    monkeypatch.setattr(
        run_materialization, "materialize_engine_run", lambda **_kwargs: SimpleNamespace(availability_hash=current["hash"])
    )
    return current


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
        evidence_provenance_json=_with_receipt(response.evidence_provenance, RECEIPT),
    )


def _with_receipt(provenance: RunEvidenceProvenance | None, receipt: str | None) -> str:
    assert provenance is not None
    return provenance.model_copy(update={"data_availability_hash": receipt}).model_dump_json()


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


# The three sessions before the run's warmup day (2026-01-01 is a holiday).
EARLIER_DAYS = (date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2))


def _seed_earlier(*days: date) -> Path:
    adjusted_root = Path(settings.LEAN_DATA_WRITE_ROOT) / lake_subpath(polygon_mode_for(True))
    for day in days:
        seed_lake_minute_day(adjusted_root, "SPY", day)
    return adjusted_root


def _bars(view: StrategyViewResponse) -> list[tuple[int, float, float, float, float, float]]:
    return [(bar.bar_close_ms, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in view.lead_in]


def test_the_lead_in_is_the_lakes_decision_bars_just_before_the_runs_first_candle(saved_run: RunDetail) -> None:
    """Catalogue indicators warm up on the bars the engine would have consolidated before the run's data (#2800)."""
    assert build_backtest_run_strategy_view(saved_run).lead_in == []  # the lake holds nothing earlier
    adjusted_root = _seed_earlier(*EARLIER_DAYS)

    view = build_backtest_run_strategy_view(saved_run)

    consolidated = read_consolidated_bars(
        roots=[adjusted_root],
        symbol="SPY",
        start=EARLIER_DAYS[0],
        end=EARLIER_DAYS[-1],
        session="regular",
        timespan="minute",
        multiplier=15,
    ).bars
    assert len(consolidated) == 3 * 26
    assert _bars(view) == [
        (bar.end_ms, float(bar.open), float(bar.high), float(bar.low), float(bar.close), float(bar.volume))
        for bar in consolidated
    ]
    assert view.lead_in[-1].bar_close_ms <= view.candles[0].bar_start_ms
    # The candles are the run's own, as before: the lead-in adds none.
    assert [candle.phase for candle in view.candles] == ["before_start"] * 26 + ["decision"] * 26


def test_a_gate_reading_a_catalogue_indicator_is_judged_from_the_runs_first_candle(saved_run: RunDetail) -> None:
    """What the page sends back, the view's candles and lead-in, is accepted and leaves no candle unjudged (#2800)."""
    _seed_earlier(*EARLIER_DAYS)
    view = build_backtest_run_strategy_view(saved_run)
    request = GateEvaluationRequest(
        symbol=view.symbol,
        settings=view.settings,
        candles=[
            GateCandle(
                bar_close_ms=candle.bar_close_ms,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
            for candle in view.candles
        ],
        lead_in=view.lead_in,
        draft=CustomGateInput(label="Above EMA 20", expression="close - EMA20", sign="gt"),
    )
    resolved = ResolvedStrategyView.for_settings(view.strategy_key, request.settings, symbol=request.symbol)

    def judged(lead_in: list) -> list[bool | None]:
        results, _, _ = evaluate_gates(
            resolved, [], request.candles, symbol=request.symbol, draft=request.draft, lead_in=lead_in
        )
        return results[DRAFT_GATE_ID]

    assert None not in judged(request.lead_in)
    # Without the lead-in the first 19 candles had no EMA20 to read.
    assert judged([])[:19] == [None] * 19


def test_a_day_the_lake_lacks_shortens_the_lead_in_and_never_fails_the_view(saved_run: RunDetail) -> None:
    first, _missing, last = EARLIER_DAYS
    _seed_earlier(first, last)

    view = build_backtest_run_strategy_view(saved_run)

    # Only the bars after the gap: a lead-in never jumps a missing day.
    assert len(view.lead_in) == 26
    assert {et_date_at_ms(bar.bar_close_ms) for bar in view.lead_in} == {last}
    assert view.run_id == "backtest-run:7"


def test_the_bars_the_candle_cap_leaves_out_become_lead_in(
    saved_run: RunDetail, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole = build_backtest_run_strategy_view(saved_run)
    monkeypatch.setattr(backtest_run_strategy_view, "MAX_STRATEGY_VIEW_CANDLES", 30)

    capped = build_backtest_run_strategy_view(saved_run)

    assert [candle.bar_close_ms for candle in capped.candles] == [candle.bar_close_ms for candle in whole.candles[-30:]]
    assert _bars(capped) == [
        (candle.bar_close_ms, candle.open, candle.high, candle.low, candle.close, candle.volume)
        for candle in whole.candles[:-30]
    ]
    assert capped.notices == ["This run has 52 decision bars; the latest 30 are shown."]


def test_a_replay_that_does_not_reproduce_the_runs_trades_is_refused(saved_run: RunDetail) -> None:
    assert saved_run.total_trades == 3
    with pytest.raises(SavedRunNotReplayable, match=r"made 3 trades, not its 4"):
        build_backtest_run_strategy_view(replace(saved_run, total_trades=4))
    first, *rest = saved_run.trades
    moved = replace(saved_run, trades=(replace(first, entry_price=first.entry_price + 0.01), *rest))
    with pytest.raises(SavedRunNotReplayable, match="changed its trade 1"):
        build_backtest_run_strategy_view(moved)


def test_a_run_read_with_only_its_newest_trades_is_not_checked_against_some_of_them(saved_run: RunDetail) -> None:
    with pytest.raises(ValueError, match="checked against every one"):
        build_backtest_run_strategy_view(replace(saved_run, trades=saved_run.trades[1:], trades_truncated=True))


def test_a_run_replays_on_its_code_whatever_data_receipt_it_or_the_lake_holds(
    saved_run: RunDetail, lake_receipt: dict[str, str]
) -> None:
    # The lake's fingerprint moved on -- a re-fetch, or days refreshed outside the window.
    lake_receipt["hash"] = "b" * 64
    assert build_backtest_run_strategy_view(saved_run).run_id == "backtest-run:7"

    unreceipted = replace(
        saved_run,
        evidence_provenance_json=_with_receipt(
            RunEvidenceProvenance.model_validate_json(saved_run.evidence_provenance_json or ""), None
        ),
    )
    assert build_backtest_run_strategy_view(unreceipted).run_id == "backtest-run:7"


def test_a_compatibility_run_is_rebuilt_without_the_flat_commission_it_never_charged(saved_run: RunDetail) -> None:
    compatibility = replace(
        saved_run,
        commission_per_order=None,
        execution_config_json=json.dumps(
            persisted_execution_configuration(
                compatibility_profile="us-equity-raw-ibkr-v1", warmup_from_date=None, slippage_per_share=0.0
            )
        ),
        data_policy_json=json.dumps({**json.loads(saved_run.data_policy_json or "{}"), "adjusted": False}),
    )

    request = _request_from_run(compatibility)

    assert request.compatibility_profile == "us-equity-raw-ibkr-v1"
    assert "commission_per_order" not in request.model_fields_set


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

    async def read(function: object, run_id: int, **_kwargs: object) -> RunDetail | None:
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


async def test_the_route_checks_the_replay_against_trades_older_than_the_report_keeps(
    saved_run: RunDetail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run report keeps only a run's newest trades; the replay is held to every one."""
    monkeypatch.setattr(backtest_runs, "_view_cache", OrderedDict())
    first, *rest = saved_run.trades
    moved = replace(saved_run, trades=(replace(first, entry_price=first.entry_price + 0.01), *rest))

    async def read(function: object, run_id: int, *, trade_limit: int | None = REPORT_TRADE_LIMIT) -> RunDetail:
        # A read capped like the report's leaves out the run's oldest trade, the one that moved.
        return moved if trade_limit is None else replace(moved, trades=tuple(rest), trades_truncated=True)

    monkeypatch.setattr(backtest_runs, "with_connection", read)
    app = FastAPI()
    app.include_router(backtest_runs.router, prefix="/api/research/backtest-runs")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.get("/api/research/backtest-runs/7/strategy-view")

    assert refused.status_code == 409
    assert "changed its trade 1" in refused.json()["detail"]["message"]


async def test_a_replay_in_flight_is_joined_and_then_kept(
    saved_run: RunDetail, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strategy view replays a whole backtest: a second read joins the first, and a third is served kept."""
    monkeypatch.setattr(backtest_runs, "_view_cache", OrderedDict())
    view = await asyncio.to_thread(build_backtest_run_strategy_view, saved_run)
    built: list[int] = []

    def build(run: RunDetail):
        built.append(run.id)
        return view

    async def read(function: object, run_id: int, **_kwargs: object) -> RunDetail:
        return saved_run

    monkeypatch.setattr(backtest_runs, "build_backtest_run_strategy_view", build)
    monkeypatch.setattr(backtest_runs, "with_connection", read)
    app = FastAPI()
    app.include_router(backtest_runs.router, prefix="/api/research/backtest-runs")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first, second = await asyncio.gather(
            client.get("/api/research/backtest-runs/7/strategy-view"),
            client.get("/api/research/backtest-runs/7/strategy-view"),
        )
        third = await client.get("/api/research/backtest-runs/7/strategy-view")

    assert [response.status_code for response in (first, second, third)] == [200, 200, 200]
    assert built == [7]
