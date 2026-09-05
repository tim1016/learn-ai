"""The one place a Grid Search cell becomes an engine call.

Every cell executes through ``execute_engine_backtest`` — the same entry
point Strategy Lab and the Recency Chart use — with the study save
suppressed and reads bound to the receipted data snapshot. Kept apart from
``service.py`` so the orchestration layer (which the Walk-Forward runner
also drives) never imports the HTTP router; the router and the job entry
inject :func:`default_execute_cell`, and tests inject a fake.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Execution
  and parity".
Canonical implementation: this file.
Validated against: tests/research/grid_search/test_service.py (request
  shape) and tests/research/grid_search/test_engine_adapter.py (parity).
"""

from __future__ import annotations

from collections.abc import Callable

from app.research.grid_search.models import CellResult, SearchRow
from app.research.grid_search.service import GridSearchSpec, window_dates
from app.research.sweep.grid import RunSpec
from app.routers.engine import EngineBacktestRequest, EngineBacktestResponse, execute_engine_backtest


def engine_request(row: SearchRow, spec: GridSearchSpec, candidate: RunSpec) -> EngineBacktestRequest:
    table = row.receipt["interval_table"]
    data_start, _ = window_dates(table["data_start_ms"], table["evaluation_end_ms"])
    evaluation_start, evaluation_end = window_dates(table["evaluation_start_ms"], table["evaluation_end_ms"])
    return EngineBacktestRequest(
        strategy_name=spec.strategy_key,
        params={**candidate.params, "symbol": spec.symbol},
        from_date=evaluation_start.isoformat(),
        to_date=evaluation_end.isoformat(),
        warmup_from_date=data_start.isoformat() if data_start < evaluation_start else None,
        fill_mode=spec.fill_mode,
        commission_per_order=spec.commission_per_order,
        slippage_per_share=spec.slippage_per_share,
        initial_cash=spec.initial_cash,
        resolution=spec.resolution,  # type: ignore[arg-type]
        save_study=False,
        auto_fetch=False,
    )


def cell_from_response(candidate: RunSpec, response: EngineBacktestResponse) -> CellResult:
    """Project one engine response onto the summary row a search keeps."""
    if not response.success:
        return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="failed", error=response.error or "engine failed")
    stats = response.statistics or {}
    return CellResult(
        params_hash=candidate.params_hash,
        params=dict(candidate.params),
        status="completed",
        total_trades=response.total_trades,
        net_profit=stats.get("net_profit", response.net_profit),
        total_return_pct=stats.get("net_profit_pct"),
        sharpe_ratio=stats.get("sharpe_ratio"),
        max_drawdown_pct=stats.get("max_drawdown_pct"),
        win_rate=stats.get("win_rate"),
        bars_consumed=len(response.equity_curve),
    )


def default_execute_cell(row: SearchRow, spec: GridSearchSpec) -> Callable[[RunSpec], CellResult]:
    manifest = row.receipt["data_snapshot"]["artifacts"]

    def _execute(candidate: RunSpec) -> CellResult:
        response = execute_engine_backtest(
            request=engine_request(row, spec, candidate),
            on_phase=lambda phase: None,
            on_log=lambda message: None,
            data_manifest=manifest,
        )
        return cell_from_response(candidate, response)

    return _execute
