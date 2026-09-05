"""Parity: a grid-search cell is one engine call (PRD #1926 "Testing decisions — Parity").

Structural parity is asserted end to end: the adapter's cell projection is
compared against a direct ``execute_engine_backtest`` call built by hand
from the same receipt — identical trade list with fills and fees, equity
curve, full statistics block and consumed-bar count — and the projection
is checked field by field against that response.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.grid_search import engine_adapter, service
from app.research.sweep.grid import StrategyGridConfig, ValueListRange, expand_grid
from app.routers import engine as engine_router
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from tests._helpers.lean_store import seed_store_day

START, END = date(2025, 1, 6), date(2025, 1, 24)
SESSIONS = expected_sessions(START, END)
DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def lake(tmp_path: Path, monkeypatch) -> Path:
    write_root = tmp_path / "writer-root"
    lake_dir = write_root / lake_subpath("polygon_split_adjusted")
    lake_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(engine_router, "_save_study_sync", lambda **kwargs: None)
    for day in SESSIONS:
        seed_store_day(lake_dir, "SPY", day)
    return lake_dir


def _noop(_: str) -> None:
    return None


async def test_a_cell_and_a_direct_engine_call_over_the_same_resolved_request_are_identical(conn: asyncpg.Connection, lake: Path) -> None:
    spec = service.GridSearchSpec(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={"short_window": ValueListRange((2.0,)), "long_window": ValueListRange((5.0,)), "resolution_minutes": ValueListRange((60.0,))},
        start_ms=service.et_midnight_ms(START),
        end_ms=service.et_midnight_ms(END) + DAY_MS,
        min_trades=1,
    )
    created = await service.create(service.prepare_launch(spec, job_id=None, roots=[lake]))
    stored = service.GridSearchSpec.from_request_dict(created.request)
    candidate = next(iter(expand_grid([StrategyGridConfig(strategy_key="sma_crossover", param_ranges=dict(stored.param_ranges))], ["SPY"])))
    table = created.receipt["interval_table"]

    # The cell, as the runner executes it.
    cell_request = engine_adapter.engine_request(created, stored, candidate)
    cell_response = execute_engine_backtest(request=cell_request, on_phase=_noop, on_log=_noop, data_manifest=created.receipt["data_snapshot"]["artifacts"])
    cell = engine_adapter.cell_from_response(candidate, cell_response)

    # A direct call built by hand from the receipt's interval table.
    data_start, _ = service.window_dates(table["data_start_ms"], table["evaluation_end_ms"])
    evaluation_start, evaluation_end = service.window_dates(table["evaluation_start_ms"], table["evaluation_end_ms"])
    direct = execute_engine_backtest(
        request=EngineBacktestRequest(
            strategy_name="sma_crossover",
            params={"symbol": "SPY", "short_window": 2, "long_window": 5, "resolution_minutes": 60},
            from_date=evaluation_start.isoformat(),
            to_date=evaluation_end.isoformat(),
            warmup_from_date=data_start.isoformat(),
            save_study=False,
        ),
        on_phase=_noop,
        on_log=_noop,
    )

    assert cell_response.success and direct.success, (cell_response.error, direct.error)
    assert cell_response.trades == direct.trades  # fills, fees, quantities, timestamps
    assert cell_response.equity_curve == direct.equity_curve
    assert cell_response.statistics == direct.statistics
    assert len(cell_response.equity_curve) == len(direct.equity_curve) == cell.bars_consumed
    assert cell_response.total_trades > 0
    # The projection carries the engine's own figures, not recomputed ones.
    assert cell.total_trades == direct.total_trades
    assert cell.sharpe_ratio == direct.statistics["sharpe_ratio"]
    assert cell.net_profit == direct.statistics["net_profit"]
    assert cell.total_return_pct == direct.statistics["net_profit_pct"]
    assert cell.max_drawdown_pct == direct.statistics["max_drawdown_pct"]
