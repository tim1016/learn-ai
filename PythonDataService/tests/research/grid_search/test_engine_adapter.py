"""Parity: a grid-search cell is one engine call (PRD #1926 "Testing decisions — Parity").

Structural parity is asserted end to end: the adapter's cell projection is
compared against a direct ``execute_engine_backtest`` call built by hand
from the same receipt — identical trade list with fills and fees, full
statistics block and consumed-bar count — and the projection
is checked field by field against that response. The cell runs
``summary_only`` (its per-bar artifacts are never built, #1941), so the
byte-identity of the statistics against a full response is itself the
parity claim under test.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import asyncpg
import pytest
from pydantic import ValidationError

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.grid_search import engine_adapter, service
from app.research.sweep.grid import RunSpec, StrategyGridConfig, ValueListRange, expand_grid
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services import engine_backtest_service as engine_service
from app.services.engine_backtest_service import execute_engine_backtest
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
    monkeypatch.setattr(engine_service, "persist_engine_response_sync", lambda **kwargs: None)
    for day in SESSIONS:
        seed_store_day(lake_dir, "SPY", day)
    return lake_dir


def _noop(_: str) -> None:
    return None


def test_an_empty_engine_run_becomes_a_failed_sweep_cell(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(tmp_path / "empty-writer-root"))
    candidate = RunSpec(
        symbol="SPY",
        strategy_key="sma_crossover",
        params={"short_window": 2.0, "long_window": 5.0},
        params_hash="empty-cell",
    )
    response = execute_engine_backtest(
        request=EngineBacktestRequest(
            strategy_name="sma_crossover",
            params={"symbol": "SPY", "short_window": 2, "long_window": 5},
            from_date=START.isoformat(),
            to_date=END.isoformat(),
            auto_fetch=False,
            save_study=False,
        ),
        on_phase=_noop,
        on_log=_noop,
    )

    cell = engine_adapter.cell_from_response(candidate, response)

    assert cell.status == "failed"
    assert cell.error == "missing data: backtest evaluated zero bars for the requested window"
    assert cell.bars_consumed is None


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

    # The cell, as the runner executes it. Both calls go through a thread
    # because that is where every production caller runs one: the engine gate
    # refuses an event-loop caller outright, since a blocking acquire on the
    # app loop would stall every route (#1957).
    cell_request = engine_adapter.engine_request(created, stored, candidate)
    cell_response = await asyncio.to_thread(
        execute_engine_backtest,
        request=cell_request,
        on_phase=_noop,
        on_log=_noop,
        data_manifest=created.receipt["data_snapshot"]["artifacts"],
    )
    cell = engine_adapter.cell_from_response(candidate, cell_response)

    # A direct call built by hand from the receipt's interval table.
    data_start, _ = service.window_dates(table["data_start_ms"], table["evaluation_end_ms"])
    evaluation_start, evaluation_end = service.window_dates(table["evaluation_start_ms"], table["evaluation_end_ms"])
    direct = await asyncio.to_thread(
        execute_engine_backtest,
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
    # The summary cell's statistics are the full run's statistics, byte for
    # byte: ``summary_only`` skips building the artifacts a cell discards,
    # never the samples the statistics consume (#1941).
    assert cell_response.statistics == direct.statistics
    # The bar count the curve used to be counted from, carried as a counter.
    assert cell_response.bars_consumed == len(direct.equity_curve) == cell.bars_consumed
    assert direct.bars_consumed == len(direct.equity_curve)  # full runs keep the invariant too
    # The summary shape: the per-bar artifacts are empty, not computed-then-dropped.
    assert cell_response.equity_curve == []
    assert cell_response.chart_bars == []
    assert cell_response.insights == []
    assert cell_response.lean_statistics is None
    assert cell_response.validation_analytics is None
    assert cell_response.total_trades > 0
    # The projection carries the engine's own figures, not recomputed ones.
    assert cell.total_trades == direct.total_trades
    assert cell.sharpe_ratio == direct.statistics["sharpe_ratio"]
    assert cell.net_profit == direct.statistics["net_profit"]
    assert cell.total_return_pct == direct.statistics["net_profit_pct"]
    assert cell.max_drawdown_pct == direct.statistics["max_drawdown_pct"]


def test_a_summary_run_is_byte_identical_to_a_full_run_without_a_database(lake: Path) -> None:
    """The PR-gate half of the parity claim: no ``conn`` fixture, so this runs
    on every CI shard (``ci.yml`` sets no ``POSTGRES_URL``; the receipt-minting
    parity test above runs daily instead). The same request executes twice —
    once full, once ``summary_only`` — and everything a cell reads must match
    byte for byte (#1941)."""
    common = dict(
        strategy_name="sma_crossover",
        params={"symbol": "SPY", "short_window": 2, "long_window": 5, "resolution_minutes": 60},
        from_date=SESSIONS[1].isoformat(),
        to_date=END.isoformat(),
        warmup_from_date=START.isoformat(),
        save_study=False,
        auto_fetch=False,
    )
    full, summary = (
        asyncio.run(
            asyncio.to_thread(
                execute_engine_backtest,
                request=EngineBacktestRequest(**common, summary_only=summary_only),
                on_phase=_noop,
                on_log=_noop,
            )
        )
        for summary_only in (False, True)
    )

    assert full.success and summary.success, (full.error, summary.error)
    # The byte-identity the summary mode is named for.
    assert summary.statistics == full.statistics
    assert summary.trades == full.trades
    assert summary.total_trades == full.total_trades == full.statistics["total_trades"]
    assert summary.bars_consumed == len(full.equity_curve)
    # The summary shape: the per-bar artifacts are never built.
    assert summary.equity_curve == []
    assert summary.chart_bars == []
    assert summary.insights == []
    assert summary.lean_statistics is None
    assert summary.validation_analytics is None


def test_a_summary_run_cannot_ask_for_a_persisted_study() -> None:
    """``summary_only`` pairs with ``save_study=False`` by contract; the
    request model rejects the combination rather than persisting a row whose
    per-bar evidence was never built (#1941)."""
    with pytest.raises(ValidationError, match="summary_only cannot be combined with save_study"):
        EngineBacktestRequest(strategy_name="sma_crossover", params={"symbol": "SPY"}, summary_only=True)
