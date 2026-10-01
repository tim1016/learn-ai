"""The study evaluator's pure contract: cache identity, capability, the common run-up and the engine projection (#2696)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.research.golden_search.evaluator import (
    EXIT_AT_WINDOW_END,
    CapabilityError,
    EvaluationCapability,
    EvaluationRequest,
    context_digest,
    engine_request,
    evaluation_key,
    execution_context,
    project_response,
    warmup_session,
)
from app.research.golden_search.protocol import ExecutionAssumptions, StressScenario
from app.schemas.engine_backtest import EngineBacktestResponse, EngineTradeResponse
from app.utils.session_anchors import et_midnight_ms

DAY = 24 * 60 * 60 * 1000
JAN = (et_midnight_ms(date(2025, 1, 6)), et_midnight_ms(date(2025, 1, 8)))
POINT = {"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


def _context(**overrides: object) -> dict:
    identity = {"git_revision": "abc", "tree_state": "clean", "source_digest": "s" * 64, "environment_digest": "e" * 64, "digest_scheme": 2}
    base = execution_context(
        strategy_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        parameter_schema_version="ema-crossover-signal-params/v3",
        code_identity=identity,
        data_snapshot_digest="d" * 64,
        execution=ExecutionAssumptions(),
        data_policy={"source": "polygon", "adjusted": True, "session": "regular"},
        run_up={"required_samples": 16, "bar_span_ms": 900_000, "run_up_sessions": 1},
    )
    return {**base, **overrides}


def test_evaluation_key_changes_with_every_part_of_its_identity() -> None:
    digest = context_digest(_context())
    key = evaluation_key(digest, "p" * 64, JAN, "base", False)

    assert key == evaluation_key(digest, "p" * 64, JAN, "base", False)
    variants = {
        evaluation_key(context_digest(_context(data_snapshot_digest="x" * 64)), "p" * 64, JAN, "base", False),
        evaluation_key(digest, "q" * 64, JAN, "base", False),
        evaluation_key(digest, "p" * 64, (JAN[0], JAN[1] + DAY), "base", False),
        evaluation_key(digest, "p" * 64, JAN, "slippage_1c", False),
        evaluation_key(digest, "p" * 64, JAN, "base", True),
    }
    assert key not in variants and len(variants) == 5


def test_the_context_digest_ignores_provenance_but_not_the_code_it_runs() -> None:
    base = _context()
    moved_commit = {**base, "code_identity": {**base["code_identity"], "git_revision": "def", "tree_state": "dirty"}}
    changed_code = {**base, "code_identity": {**base["code_identity"], "source_digest": "t" * 64}}

    assert context_digest(moved_commit) == context_digest(base)
    assert context_digest(changed_code) != context_digest(base)


def test_the_capability_admits_a_window_inside_an_interval_and_refuses_one_that_reaches_past_it() -> None:
    development = (et_midnight_ms(date(2025, 1, 1)), et_midnight_ms(date(2025, 4, 1)))
    capability = EvaluationCapability(allowed=(development,))

    capability.check(development)
    capability.check((development[0] + DAY, development[1] - DAY))
    for window in ((development[0], development[1] + DAY), (development[1], development[1] + 30 * DAY), (development[0], development[0])):
        with pytest.raises(CapabilityError):
            capability.check(window)


def test_the_warmup_session_counts_trading_sessions_back_from_the_window() -> None:
    # 2025-01-01 is a holiday: the sessions before it are Dec 31 and Dec 30.
    assert warmup_session(et_midnight_ms(date(2025, 1, 1)), 2) == date(2024, 12, 30)
    # Christmas is skipped; the half-day before it counts as a session.
    assert warmup_session(et_midnight_ms(date(2024, 12, 26)), 1) == date(2024, 12, 24)
    with pytest.raises(ValueError):
        warmup_session(JAN[0], 0)


def _request(**overrides: object) -> EvaluationRequest:
    base = dict(
        point=POINT,
        point_hash="p" * 64,
        window=JAN,
        warmup_from=date(2025, 1, 3),
        scenario=None,
        detail=False,
        stage="search",
        fold_index=None,
    )
    return EvaluationRequest(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_engine_request_scores_the_window_after_the_common_run_up() -> None:
    execution = ExecutionAssumptions(fill_mode="next_bar_open", commission_per_order=1.0, slippage_per_share=0.01, initial_cash=50_000.0)

    request = engine_request(_request(), strategy_key="ema_crossover_signal", execution=execution)

    assert (request.from_date, request.to_date, request.warmup_from_date) == ("2025-01-06", "2025-01-07", "2025-01-03")
    assert request.params == POINT and request.strategy_name == "ema_crossover_signal"
    assert (request.fill_mode, request.commission_per_order, request.slippage_per_share, request.initial_cash) == (
        "next_bar_open",
        1.0,
        0.01,
        50_000.0,
    )
    assert request.summary_only and not request.save_study and not request.auto_fetch and request.resolution == "minute"


def test_a_stress_scenario_adds_its_costs_and_a_detail_run_keeps_the_per_bar_artifacts() -> None:
    execution = ExecutionAssumptions(commission_per_order=1.0, slippage_per_share=0.01)
    scenario = StressScenario("harsh", "Harsh", slippage_add=0.02, commission_add=0.5, fill_mode="signal_bar_close")

    request = engine_request(_request(scenario=scenario, detail=True), strategy_key="ema_crossover_signal", execution=execution)

    assert request.commission_per_order == pytest.approx(1.5, abs=1e-12)
    assert request.slippage_per_share == pytest.approx(0.03, abs=1e-12)
    assert request.fill_mode == "signal_bar_close" and not request.summary_only


def _response(**overrides: object) -> EngineBacktestResponse:
    days = (date(2025, 1, 6), date(2025, 1, 7))
    curve = []
    for index, day in enumerate(days):
        open_ms = session_open_ms_utc(day)
        curve.append({"timestamp": open_ms + 60_000, "equity": 100_000.0 + index * 10, "cash": 0.0, "holdings_value": 0.0})
        curve.append({"timestamp": session_close_ms_utc(day), "equity": 100_500.0 + index * 250, "cash": 0.0, "holdings_value": 0.0})
    trades = [
        EngineTradeResponse(trade_number=1, entry_time=curve[0]["timestamp"], entry_price=500.0, exit_time=curve[1]["timestamp"], exit_price=502.5,
                            quantity=200, indicators={"rsi": 55.0}, pnl_pts=2.5, pnl_pct=0.005, result="WIN"),
        EngineTradeResponse(trade_number=2, entry_time=curve[2]["timestamp"], entry_price=502.0, exit_time=curve[3]["timestamp"], exit_price=501.0,
                            quantity=100, pnl_pts=-1.0, pnl_pct=-0.002, result="LOSS", is_synthetic_exit=True),
    ]
    base = dict(
        success=True, strategy_name="ema_crossover_signal", fill_mode="decision_minute_open", initial_cash=100_000.0,
        final_equity=100_750.0, net_profit=750.0, total_fees=0.0, total_trades=2, winning_trades=1, losing_trades=1, win_rate=0.5,
        statistics={"net_profit": 750.0, "net_profit_pct": 0.0075, "sharpe_ratio": 1.25, "max_drawdown_pct": 0.004, "win_rate": 0.5},
        trades=trades, equity_curve=curve,
    )
    return EngineBacktestResponse(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_projection_keeps_the_sweep_statistics_and_bounded_daily_detail() -> None:
    result = project_response(_request(detail=True), _response(), strategy_key="ema_crossover_signal", initial_cash=100_000.0)

    metrics = result.metrics
    assert (metrics.status, metrics.total_trades, metrics.net_profit, metrics.total_return_pct) == ("completed", 2, 750.0, 0.0075)
    assert (metrics.sharpe_ratio, metrics.max_drawdown_pct, metrics.win_rate) == (1.25, 0.004, 0.5)
    assert result.detail is not None
    assert result.detail["daily_equity"] == [
        [session_close_ms_utc(date(2025, 1, 6)), 100_500.0],
        [session_close_ms_utc(date(2025, 1, 7)), 100_750.0],
    ]
    first, second = result.detail["trades"]
    assert math.isclose(first["pnl"], 500.0, abs_tol=1e-9, rel_tol=0) and first["exit_reason"] is None
    assert math.isclose(second["pnl"], -100.0, abs_tol=1e-9, rel_tol=0) and second["exit_reason"] == EXIT_AT_WINDOW_END


def test_a_summary_run_has_no_detail_and_an_undefined_statistic_reads_none() -> None:
    result = project_response(
        _request(),
        _response(statistics={"net_profit": 750.0, "net_profit_pct": 0.0075, "sharpe_ratio": float("nan"), "max_drawdown_pct": 0.004, "win_rate": 0.5}),
        strategy_key="ema_crossover_signal",
        initial_cash=100_000.0,
    )
    assert result.detail is None and result.metrics.sharpe_ratio is None


def test_a_failed_engine_run_is_a_failed_evaluation_with_its_reason() -> None:
    result = project_response(_request(detail=True), _response(success=False, error="no bars"), strategy_key="ema_crossover_signal", initial_cash=1.0)
    assert (result.metrics.status, result.metrics.error, result.detail) == ("failed", "no bars", None)
