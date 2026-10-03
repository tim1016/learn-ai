"""Research runs under live terms: the decision-minute fill and the $0 default fee (#2599, #2601).

Both request surfaces a researcher grades with -- Strategy Lab's engine run
and Grid Search (which Walk-Forward sweeps through) -- accept the
``decision_minute_open`` fill mode and record it in their receipts, and both
default the flat fee to $0: Alpaca charges no commission, and its regulatory
fees are not modelled. Grid Search defaults to that fill mode too. The engine
request's own default stays the signal bar's close, which a LEAN-paired run
relies on: ``tests/integration/test_engine_persistence_data_policy.py`` pins
it. The engine-level fill rule itself is pinned in
``tests/engine/test_engine_fill_modes.py``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.backtest_runs.service import SAVE_FAILED
from app.research.grid_search import engine_adapter, service
from app.research.sweep.grid import RunSpec, ValueListRange
from app.schemas.engine_backtest import EngineBacktestRequest
from app.schemas.grid_search import GridSearchSpecRequest, to_grid_spec
from app.services import engine_backtest_service
from tests._helpers.lean_store import make_minute_bars, seed_store_day

START, END = date(2025, 1, 6), date(2025, 1, 10)
SESSIONS = expected_sessions(START, END)
DAY_MS = 24 * 60 * 60 * 1000
SMA_PARAMS: dict[str, Any] = {"short_window": 2, "long_window": 5, "resolution_minutes": 15}


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for day in SESSIONS:
        seed_store_day(tmp_path, "SPY", day)
    return tmp_path


def _noop(_: str) -> None:
    return None


@pytest.mark.parametrize("spelling", ["decision_minute_open", " DECISION_MINUTE_OPEN "])
def test_an_engine_run_fills_at_the_decision_minute_open_and_records_it(
    lake: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """Whatever spelling the request used, every record names the canonical mode, so a rerun restores it."""
    persisted: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        persisted.update(kwargs)
        return SAVE_FAILED

    monkeypatch.setattr(engine_backtest_service, "_resolve_lean_data_roots", lambda **_: [lake])
    monkeypatch.setattr(engine_backtest_service, "persist_engine_response_sync", _capture)
    request = EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY", **SMA_PARAMS},
        from_date=START.isoformat(),
        to_date=END.isoformat(),
        fill_mode=spelling,
        auto_fetch=False,
    )

    response = engine_backtest_service.execute_engine_backtest(request=request, on_phase=_noop, on_log=_noop)

    assert response.success, response.error
    assert response.total_trades > 0
    # Each entry fills at the open of the minute starting at its fill time --
    # the minute the bucket is emitted on -- which in these bars is never the
    # bucket's closing price.
    opens = {bar.start_ms: float(bar.open) for day in SESSIONS for bar in make_minute_bars("SPY", day)}
    closes = {bar.end_ms: float(bar.close) for day in SESSIONS for bar in make_minute_bars("SPY", day)}
    assert [trade.entry_price for trade in response.trades] == [opens[trade.entry_time] for trade in response.trades]
    assert all(trade.entry_price != closes[trade.entry_time] for trade in response.trades)
    assert response.fill_mode == "decision_minute_open"
    assert persisted["response"].fill_mode == "decision_minute_open"
    # The default flat fee charges nothing, and the run row records the $0.
    assert response.total_fees == 0.0
    assert persisted["commission_per_order"] == 0.0


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("close", "signal_bar_close"),
        ("open", "next_bar_open"),
        ("NextBarOpen", "next_bar_open"),
        ("Signal-Bar-Close", "signal_bar_close"),
    ],
)
def test_an_engine_run_still_parses_the_aliases(
    lake: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, canonical: str
) -> None:
    monkeypatch.setattr(engine_backtest_service, "_resolve_lean_data_roots", lambda **_: [lake])
    request = EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY", **SMA_PARAMS},
        from_date=START.isoformat(),
        to_date=END.isoformat(),
        fill_mode=spelling,
        auto_fetch=False,
        save_study=False,
    )

    response = engine_backtest_service.execute_engine_backtest(request=request, on_phase=_noop, on_log=_noop)

    assert response.success, response.error
    assert response.fill_mode == canonical


def test_an_unknown_fill_mode_names_every_mode_an_engine_run_accepts(lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_backtest_service, "_resolve_lean_data_roots", lambda **_: [lake])
    request = EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY", **SMA_PARAMS},
        from_date=START.isoformat(),
        to_date=END.isoformat(),
        fill_mode="decision_bar_open",
        auto_fetch=False,
        save_study=False,
    )

    with pytest.raises(HTTPException) as refused:
        engine_backtest_service.execute_engine_backtest(request=request, on_phase=_noop, on_log=_noop)

    assert refused.value.status_code == 400
    assert "signal_bar_close, next_bar_open or decision_minute_open" in refused.value.detail


def _grid_body(**overrides: Any) -> GridSearchSpecRequest:
    return GridSearchSpecRequest(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={name: {"type": "value_list", "values": [float(value)]} for name, value in SMA_PARAMS.items()},
        start_ms=service.et_midnight_ms(START),
        end_ms=service.et_midnight_ms(END) + DAY_MS,
        min_trades=1,
        **overrides,
    )


CANDIDATE = RunSpec(symbol="SPY", strategy_key="sma_crossover", params=dict(SMA_PARAMS), params_hash="x")


@pytest.mark.parametrize("named", [{"fill_mode": "decision_minute_open"}, {}], ids=["named", "by default"])
def test_a_grid_search_carries_the_decision_minute_open_into_its_receipt_and_cells(lake: Path, named: dict[str, str]) -> None:
    """Named or not: the sweep answers "will this survive live?", so the mode is also its default."""
    body = _grid_body(**named)

    record = service.prepare_launch(to_grid_spec(body), job_id=None, roots=[lake])
    stored = service.GridSearchSpec.from_request_dict(record.request)
    cell_request = engine_adapter.engine_request(record, stored, CANDIDATE)

    assert record.receipt["execution_contract"]["fill_mode"] == "decision_minute_open"
    assert cell_request.fill_mode == "decision_minute_open"
    # Nobody asked for a fee, so none is charged.
    assert record.receipt["execution_contract"]["commission_per_order"] == 0.0
    assert cell_request.commission_per_order == 0.0


def test_a_sweep_saved_before_it_recorded_a_fill_mode_still_runs_at_the_signal_bar_close(lake: Path) -> None:
    """Finish reads the stored request; a keyless one must not pick up today's default."""
    record = service.prepare_launch(to_grid_spec(_grid_body()), job_id=None, roots=[lake])
    saved = {key: value for key, value in record.request.items() if key != "fill_mode"}

    stored = service.GridSearchSpec.from_request_dict(saved)

    assert stored.fill_mode == "signal_bar_close"
    assert engine_adapter.engine_request(record, stored, CANDIDATE).fill_mode == "signal_bar_close"


def test_the_research_request_surfaces_default_to_no_fee() -> None:
    engine = EngineBacktestRequest(strategy_name="sma_crossover")
    grid = GridSearchSpecRequest(strategy_key="sma_crossover", symbol="SPY", start_ms=0, end_ms=DAY_MS)
    spec = service.GridSearchSpec(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={"short_window": ValueListRange((2.0,))},
        start_ms=0,
        end_ms=DAY_MS,
    )

    assert engine.commission_per_order == 0.0
    assert grid.commission_per_order == 0.0
    assert spec.commission_per_order == 0.0
