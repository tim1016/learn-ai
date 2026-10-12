"""The one place a Grid Search cell becomes an engine call.

Every cell executes through ``execute_engine_backtest`` — the same entry
point Strategy Lab and the Recency Chart use — with the study save
suppressed, reads bound to the receipted data snapshot, and the response
held to its summary: ``summary_only`` keeps the statistics and trade list
byte-identical while the per-bar artifacts a cell discards are never built
(#1941 — a minute-resolution cell held ~480 MB of response copies and
retained bars to read six numbers). A completed run also keeps its daily
equity (#2935): the engine's last equity sample of each ET date
(``equity_day_ends``, filled on a summary run too), anchored at that date's
session close by ``sweep.daily_equity``. Kept apart from
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

import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.research.grid_search.models import CellResult, SearchRow
from app.research.grid_search.service import GridSearchSpec, window_dates
from app.research.sweep.daily_equity import daily_equity
from app.research.sweep.grid import RunSpec
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.services.engine_backtest_service import execute_engine_backtest

logger = logging.getLogger(__name__)


def execution_block(spec: GridSearchSpec) -> dict[str, Any]:
    """The spec's frozen execution contract as the dispatch wire carries it (#3049).

    The one place the field list lives: the in-process adapter and the
    dispatched item build the same block, so a new spec field can never
    reach one path and silently miss the other.
    """
    return {
        "strategy_key": spec.strategy_key,
        "symbol": spec.symbol,
        "fill_mode": spec.fill_mode,
        "commission_per_order": spec.commission_per_order,
        "slippage_per_share": spec.slippage_per_share,
        "initial_cash": spec.initial_cash,
        "resolution": spec.resolution,
    }


def engine_request(row: SearchRow, spec: GridSearchSpec, candidate: RunSpec) -> EngineBacktestRequest:
    return engine_request_over(
        candidate,
        interval=row.receipt["interval_table"],
        execution=execution_block(spec),
    )


def engine_request_over(
    candidate: RunSpec,
    *,
    interval: Mapping[str, int],
    execution: Mapping[str, Any],
) -> EngineBacktestRequest:
    """One cell's engine call from its wire-shaped parts: the receipt's ms interval and the frozen execution.

    The in-process adapter and the research worker's child build the identical
    request from the same fields (#3049): the interval is the receipt's
    ``int64 ms UTC`` table and the execution block the spec's frozen contract,
    so a dispatched cell cannot drift from a locally executed one.
    """
    data_start, _ = window_dates(int(interval["data_start_ms"]), int(interval["evaluation_end_ms"]))
    evaluation_start, evaluation_end = window_dates(int(interval["evaluation_start_ms"]), int(interval["evaluation_end_ms"]))
    return EngineBacktestRequest(
        strategy_name=str(execution["strategy_key"]),
        params={**candidate.params, "symbol": execution["symbol"]},
        from_date=evaluation_start.isoformat(),
        to_date=evaluation_end.isoformat(),
        warmup_from_date=data_start.isoformat() if data_start < evaluation_start else None,
        fill_mode=str(execution["fill_mode"]),
        commission_per_order=float(execution["commission_per_order"]),
        slippage_per_share=float(execution["slippage_per_share"]),
        initial_cash=float(execution["initial_cash"]),
        resolution=execution["resolution"],  # type: ignore[arg-type]
        save_study=False,
        auto_fetch=False,
        summary_only=True,
    )


def daily_curve(response: EngineBacktestResponse) -> list[tuple[int, float]] | None:
    """The run's equity at each session close, or ``None`` when the engine kept no day ends or they cannot be anchored to sessions.

    The curve only draws the run; its numbers stand without it, so a day end
    the calendar refuses loses the curve and never the result.
    """
    try:
        return daily_equity(response.equity_day_ends) or None
    except ValueError:
        logger.warning("engine run kept no daily curve", extra={"action": "run_curve_unanchored"}, exc_info=True)
        return None


def cell_from_response(candidate: RunSpec, response: EngineBacktestResponse) -> CellResult:
    """Project one engine response onto the summary row a search keeps, with the run's daily equity."""
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
        bars_consumed=response.bars_consumed,
        daily_equity=daily_curve(response),
    )


def default_execute_cell(
    row: SearchRow,
    spec: GridSearchSpec,
    *,
    cancel_check: Callable[[], object] = lambda: None,
) -> Callable[[RunSpec], CellResult]:
    """One cell, as one engine call.

    ``cancel_check`` is the sweep loop's own raise-only check, forwarded into
    the engine gate. The loop polls it between cells; a cell that is *queued*
    behind another process-wide backtest has not started, and without this the
    only thing that could end its wait was the run ahead of it finishing
    (#1957, #1942-adjacent). Raising abandons the wait without taking the gate.
    """
    manifest = row.receipt["data_snapshot"]["artifacts"]

    def _execute(candidate: RunSpec) -> CellResult:
        response = execute_engine_backtest(
            request=engine_request(row, spec, candidate),
            on_phase=lambda phase: None,
            on_log=lambda message: None,
            data_manifest=manifest,
            while_waiting=cancel_check,
        )
        return cell_from_response(candidate, response)

    return _execute
