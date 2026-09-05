"""The warmup / evaluation-window gate at the engine HTTP boundary.

``EngineBacktestRequest.warmup_from_date`` primes the strategy on bars the
response never reports; ``from_date`` is where scoring starts. ``save_study``
lets a sweep keep its own summary rows instead of a full study per cell.
Contract: PRD #1925 "The gate", PRD #1926 "Execution and parity".
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.routers import engine as engine_router
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from tests._helpers.lean_store import seed_store_day

_ET = ZoneInfo("America/New_York")
DAYS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))


def _ny_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=_ET).timestamp() * 1000)


def _noop(_: str) -> None:
    return None


@pytest.fixture
def seeded_lake(monkeypatch, tmp_path: Path) -> Path:
    """Four January sessions in the adjusted lake root the default policy reads."""
    write_root = tmp_path / "writer-root"
    lake_dir = write_root / lake_subpath("polygon_split_adjusted")
    lake_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    for day in DAYS:
        seed_store_day(lake_dir, "SPY", day)
    return lake_dir


@pytest.fixture
def recorded_persistence(monkeypatch) -> dict[str, list]:
    """Capture the study save and parity dispatch instead of calling .NET."""
    calls: dict[str, list] = {"save": [], "parity": []}

    def _save(**kwargs):
        calls["save"].append(kwargs)
        return 42

    monkeypatch.setattr(engine_router, "_save_study_sync", _save)
    monkeypatch.setattr(
        engine_router,
        "_dispatch_requested_parity_companion",
        lambda **kwargs: calls["parity"].append(kwargs),
    )
    return calls


def _request(**overrides) -> EngineBacktestRequest:
    body = dict(
        strategy_name="sma_crossover",
        params={"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 1},
        from_date=DAYS[1].isoformat(),
        to_date=DAYS[3].isoformat(),
    )
    body.update(overrides)
    return EngineBacktestRequest(**body)


def _run(request: EngineBacktestRequest):
    return execute_engine_backtest(request=request, on_phase=_noop, on_log=_noop)


# ── Request validation ────────────────────────────────────────────────────


def test_warmup_must_precede_the_evaluation_start() -> None:
    with pytest.raises(ValidationError, match="must precede from_date"):
        _request(warmup_from_date=DAYS[1].isoformat())


def test_warmup_requires_an_evaluation_start() -> None:
    with pytest.raises(ValidationError, match="requires from_date"):
        EngineBacktestRequest(strategy_name="sma_crossover", params={"symbol": "SPY"}, warmup_from_date="2024-01-02")


# ── Behaviour ─────────────────────────────────────────────────────────────


def test_an_unprimed_run_reports_one_window_and_saves_its_study(seeded_lake, recorded_persistence) -> None:
    response = _run(_request())

    assert response.success, response.error
    assert response.evaluation_window is not None
    assert response.evaluation_window.model_dump() == {
        "data_start": DAYS[1].isoformat(),
        "evaluation_start": DAYS[1].isoformat(),
        "evaluation_end": DAYS[3].isoformat(),
        "warmup_primed": False,
    }
    assert response.study_id == 42
    assert len(recorded_persistence["save"]) == 1
    assert recorded_persistence["save"][0]["start_date"] == DAYS[1].isoformat()


def test_a_primed_run_scopes_every_reported_figure_to_the_evaluation_window(seeded_lake, recorded_persistence) -> None:
    primed = _run(_request(warmup_from_date=DAYS[0].isoformat()))
    cold = _run(_request())

    assert primed.success, primed.error
    boundary = _ny_midnight_ms(DAYS[1])
    assert primed.evaluation_window is not None
    assert primed.evaluation_window.warmup_primed is True
    assert primed.evaluation_window.data_start == DAYS[0].isoformat()
    assert primed.evaluation_window.evaluation_start == DAYS[1].isoformat()
    # Nothing from the warmup day reaches the record.
    assert all(trade.entry_time >= boundary for trade in primed.trades)
    assert all(point["timestamp"] >= boundary for point in primed.equity_curve)
    assert all(bar["t"] >= boundary for bar in primed.chart_bars)
    assert any("EVALUATION START" in line for line in primed.log_lines)
    # The curve is rebased at the evaluation start and spans the same bars a
    # cold run of the same window does.
    assert primed.equity_curve[0]["equity"] == pytest.approx(primed.initial_cash)
    assert len(primed.equity_curve) == len(cold.equity_curve)
    assert len(primed.chart_bars) == len(cold.chart_bars)
    # The persisted study describes the scored window, not the bars read.
    assert recorded_persistence["save"][0]["start_date"] == DAYS[1].isoformat()


def test_a_primed_run_is_not_cold_started_where_the_unprimed_one_is(seeded_lake, recorded_persistence) -> None:
    """The same evaluation window, primed vs cold, differs only in readiness.

    With a 3-sample SMA the cold run spends its first bars unready; the primed
    run's indicators already carry the warmup day. On this sawtooth fixture
    that shows up as the primed run seeing at least as many crossover
    decisions in the first minutes of the window as the cold run does.
    """
    primed = _run(_request(warmup_from_date=DAYS[0].isoformat()))
    cold = _run(_request())

    first_minutes = _ny_midnight_ms(DAYS[1]) + (9 * 60 + 35) * 60_000
    primed_early = [t for t in primed.trades if t.entry_time <= first_minutes]
    cold_early = [t for t in cold.trades if t.entry_time <= first_minutes]
    assert len(primed_early) >= len(cold_early)
    assert primed.total_trades > 0


def test_save_study_false_keeps_the_engine_result_only(seeded_lake, recorded_persistence) -> None:
    logs: list[str] = []
    response = execute_engine_backtest(request=_request(save_study=False), on_phase=_noop, on_log=logs.append)

    assert response.success, response.error
    assert response.study_id is None
    assert recorded_persistence["save"] == []
    assert recorded_persistence["parity"] == []
    assert any("suppressed" in line for line in logs)


def test_a_warmup_too_short_to_ready_the_program_fails_the_run(seeded_lake, recorded_persistence) -> None:
    # A 3-sample SMA on 300-minute bars sees at most one warmup bar in a day.
    response = _run(
        _request(
            params={"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 300},
            warmup_from_date=DAYS[0].isoformat(),
        )
    )

    assert response.success is False
    assert response.error is not None
    assert "not ready" in response.error
