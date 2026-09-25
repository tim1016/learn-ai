"""Strategy Spec refuses a window its data source does not fully cover (#2445).

The Spec endpoint reads the legacy ``LEAN_DATA_ROOT`` / ``LEAN_DATA_CACHE``
folders, and the reader skips a session with no zip without a word. Before
this fix a run on a symbol those folders did not hold — or on a window running
past their last session — reported ``success=True`` with zero trades and the
starting cash untouched: indistinguishable from a strategy that simply never
fired. Codex review V3 reproduced it at the real route boundary.

These tests drive the real default data-source factory (no dependency
override) against a temporary LEAN root, so the refusal is proven where
production meets it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.strategy.spec import StrategySpec
from app.lean_sidecar.trading_calendar import expected_sessions, is_early_close
from app.main import app
from app.research.runs import RunRequest, run_date_to_ms, run_strategy_spec
from app.routers.spec_strategy import _FIXTURES_DIR, _default_data_source_factory, get_data_source_factory
from tests._helpers.lean_store import seed_store_day

# Thanksgiving fortnight: 2024-11-28 is a closure and 2024-11-29 closes at
# 13:00 ET, so the window carries both calendar cases the check must honour.
WINDOW = (date(2024, 11, 25), date(2024, 12, 6))
THANKSGIVING = date(2024, 11, 28)
BLACK_FRIDAY = date(2024, 11, 29)


@pytest.fixture
def lean_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default factory at an empty LEAN root and nothing else."""
    root = tmp_path / "lean-data"
    root.mkdir()
    monkeypatch.setenv("LEAN_DATA_ROOT", str(root))
    monkeypatch.delenv("LEAN_DATA_CACHE", raising=False)
    return root


def _sma_spec(symbol: str = "SPY") -> dict[str, Any]:
    spec = StrategySpec.model_validate_json((_FIXTURES_DIR / "sma_crossover.spec.json").read_text(encoding="utf-8"))
    payload = spec.model_dump(mode="json")
    payload["symbols"] = [symbol]
    return payload


async def _post_backtest(spec: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/spec-strategy/backtest",
            json={"spec": spec, "start_date": start.isoformat(), "end_date": end.isoformat()},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed(root: Path, symbol: str, days: list[date]) -> None:
    for day in days:
        seed_store_day(root, symbol, day)


async def test_spec_backtest_on_a_symbol_with_no_data_fails_naming_the_gap(lean_root: Path) -> None:
    """The root holds SPY for the whole window; the spec asks for QQQ."""
    sessions = expected_sessions(*WINDOW)
    _seed(lean_root, "SPY", sessions)

    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "QQQ" in error
    assert f"{len(sessions)} of {len(sessions)}" in error
    assert "missing 2024-11-25..2024-12-06" in error


async def test_spec_backtest_on_a_partially_covered_window_fails_naming_each_gap(lean_root: Path) -> None:
    """Holes inside the window are named as session ranges; a closure is not a hole.

    Missing: 11-27 and the 11-29 half day (one run of sessions, since the
    Thanksgiving closure between them is no session), 12-04 and 12-06.
    """
    missing = {date(2024, 11, 27), BLACK_FRIDAY, date(2024, 12, 4), date(2024, 12, 6)}
    sessions = expected_sessions(*WINDOW)
    assert THANKSGIVING not in sessions
    assert BLACK_FRIDAY in sessions and is_early_close(BLACK_FRIDAY)
    _seed(lean_root, "SPY", [day for day in sessions if day not in missing])

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "SPY" in error
    assert f"4 of {len(sessions)}" in error
    assert "missing 2024-11-27..2024-11-29, 2024-12-04, 2024-12-06" in error
    assert THANKSGIVING.isoformat() not in error


async def test_spec_backtest_on_a_fully_covered_window_is_unchanged(lean_root: Path) -> None:
    """Admission adds nothing to a covered run: same response as a bare reader over the same root."""
    _seed(lean_root, "SPY", expected_sessions(*WINDOW))

    admitted = await _post_backtest(_sma_spec(), *WINDOW)

    app.dependency_overrides[get_data_source_factory] = lambda: (
        lambda symbol, start, end: LeanMinuteDataReader([lean_root])
    )
    try:
        bare = await _post_backtest(_sma_spec(), *WINDOW)
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    assert admitted["success"] is True, admitted
    assert admitted["total_trades"] > 0, "the covered window must exercise a real run, not an empty one"
    assert admitted == bare


def test_research_run_through_the_default_factory_fails_on_a_window_past_the_data(lean_root: Path) -> None:
    """Research runs share the Spec factory, so they refuse the same gap as a failed ledger."""
    _seed(lean_root, "SPY", [day for day in expected_sessions(*WINDOW) if day < date(2024, 12, 2)])
    request = RunRequest(
        spec=StrategySpec.model_validate(_sma_spec()),
        start_ms=run_date_to_ms(WINDOW[0]),
        end_ms=run_date_to_ms(WINDOW[1]),
    )

    ledger, result = run_strategy_spec(
        request,
        data_source_factory=_default_data_source_factory,
        data_root_revision="test-revision",
    )

    assert ledger.status == "failed"
    assert ledger.failure_reason is not None
    assert "2024-12-02..2024-12-06" in ledger.failure_reason
    assert result.trades == []
