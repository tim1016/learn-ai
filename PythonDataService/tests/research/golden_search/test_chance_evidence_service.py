"""The evidence stage's chance readings through the real service, and what they cannot change (#3011).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). Every test seeds its own
thin lake under a symbol of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.research.golden_search import stages
from app.research.golden_search.evaluator import EvaluationRequest, EvaluationResult
from app.research.persistence import lifecycle
from app.schemas.golden_search import GoldenSearchChance, GoldenSearchChanceRehearsalMeasured
from tests._helpers.golden_search_study import Driver, FakeEngine, seed_lake, unique_symbol


@pytest.fixture
def symbol() -> str:
    return unique_symbol()


@pytest.fixture
def lake(tmp_path: Path, symbol: str) -> Path:
    return seed_lake(tmp_path, symbol)


@pytest.fixture
def driver(lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> Driver:
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    return Driver(roots=[lake])


def _chance(detail: dict[str, Any], key: str) -> dict[str, Any]:
    return next(candidate["chance"] for candidate in detail["results"]["evidence"]["candidates"] if candidate["key"] == key)


async def test_the_evidence_stage_persists_chance_readings_the_contract_can_read_back(driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol)
    detail = await driver.detail(row)
    candidates = {candidate["key"]: candidate for candidate in detail["results"]["evidence"]["candidates"]}

    baseline = candidates["incumbent"]["chance"]
    assert baseline["comparison"]["status"] == "baseline"
    assert baseline["profitability"]["status"] == "missing"
    assert baseline["rehearsal"]["status"] == "missing"
    assert baseline["rehearsal"]["code"] == "baseline"

    measured = [candidates[key]["chance"] for key in ("all_period", "recent") if key in candidates]
    assert measured, "the plan fits at least the all-period fit"
    for chance in measured:
        GoldenSearchChance.model_validate(chance)
        comparison = chance["comparison"]
        assert comparison["status"] == "measured"
        assert sum(comparison["counts"].values()) == 10_000
        assert comparison["basis"]["source"]["window"] == {"start_ms": row.protocol["development_start_ms"], "end_ms": row.protocol["development_end_ms"]}
        assert chance["profitability"]["status"] == "measured"
        assert sum(chance["profitability"]["counts"].values()) == 10_000
        # The rehearsal ladder the stage stored: the frozen final interval's own scheduled sessions and floors.
        rehearsal = chance["rehearsal"]
        GoldenSearchChanceRehearsalMeasured.model_validate(rehearsal)
        assert rehearsal["basis"]["final_interval"]["window"] == {"start_ms": row.protocol["final_start_ms"], "end_ms": row.protocol["final_end_ms"]}
        final_sessions = rehearsal["basis"]["final_interval"]["scheduled_sessions"]
        assert [rung["sessions"] for rung in rehearsal["rungs"]] == [k * final_sessions for k in (1, 2, 3, 4)]
        for rung in rehearsal["rungs"]:
            assert rung["denominator"] == 10_000
            assert rung["passes"]["pass_joint"] <= min(count for name, count in rung["passes"].items() if name != "pass_joint")
        assert set(rehearsal["basis"]["seeds"]) == {"1x", "2x", "3x", "4x"}
        assert len(rehearsal["objective_percentiles_1x"]["p"]) == 99

    # Same-point fits share one result: the fake landscape peaks both procedures at the same settings.
    if len(measured) > 1:
        assert measured[0] == measured[1], "candidates that chose the same settings read one shared record"

    # Reading the page again serves the same stored payload; a poll never redraws.
    again = await driver.detail(row)
    assert _chance(again, "all_period") == _chance(detail, "all_period")


async def test_a_failing_rehearsal_never_erases_the_readings_beside_it_or_spends_engine_runs(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection
) -> None:
    healthy_symbol, broken_symbol = unique_symbol(), unique_symbol()
    seed_lake(lake, healthy_symbol)
    seed_lake(lake, broken_symbol)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])

    healthy = Driver(roots=[lake])
    healthy_row = await healthy.to_candidate(healthy_symbol)
    healthy_detail = await healthy.detail(healthy_row)

    def refusing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("rehearsal computation exploded")

    monkeypatch.setattr(stages, "rehearsal_reading", refusing)
    broken = Driver(roots=[lake])
    broken_row = await broken.to_candidate(broken_symbol)
    broken_detail = await broken.detail(broken_row)

    for candidate in broken_detail["results"]["evidence"]["candidates"]:
        rehearsal = candidate["chance"]["rehearsal"]
        if candidate["key"] == "incumbent":
            assert rehearsal["code"] == "baseline"
            continue
        assert rehearsal["status"] == "failed"
        assert "rehearsal computation exploded" in rehearsal["reason"]
        assert candidate["chance"]["comparison"]["status"] == "measured"
        assert candidate["chance"]["profitability"]["status"] == "measured"
    assert broken_row.consumed_evaluations == healthy_row.consumed_evaluations
    assert broken_detail["results"]["evidence"]["recommendation"] == healthy_detail["results"]["evidence"]["recommendation"]


async def test_the_rehearsal_stages_evidence_without_touching_the_final_interval(driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol)
    final_start = row.protocol["final_start_ms"]
    assert driver.engine.calls, "the evidence stage ran"
    for request in driver.engine.calls:
        assert request.window[0] < final_start, f"the engine was asked for the final interval before the test opened: {request.window}"
    detail = await driver.detail(row)
    rehearsal = next(candidate["chance"]["rehearsal"] for candidate in detail["results"]["evidence"]["candidates"] if candidate["key"] == "all_period")
    # The ladder knows the final interval only as its scheduled length, never as data.
    assert rehearsal["status"] == "measured"
    assert rehearsal["basis"]["final_interval"]["window"]["start_ms"] == final_start


async def test_chance_diagnostics_change_no_frozen_result_and_spend_no_engine_runs(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection
) -> None:
    healthy_symbol, broken_symbol = unique_symbol(), unique_symbol()
    seed_lake(lake, healthy_symbol)
    seed_lake(lake, broken_symbol)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])

    healthy = Driver(roots=[lake])
    healthy_row = await healthy.to_candidate(healthy_symbol)
    healthy_detail = await healthy.detail(healthy_row)

    def refusing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("chance computation exploded")

    monkeypatch.setattr(stages, "development_readings", refusing)
    monkeypatch.setattr(stages, "incumbent_readings", refusing)
    broken = Driver(roots=[lake])
    broken_row = await broken.to_candidate(broken_symbol)
    broken_detail = await broken.detail(broken_row)

    # The diagnostics failed loudly per component, and the frozen evidence is untouched.
    for candidate in broken_detail["results"]["evidence"]["candidates"]:
        for component in ("comparison", "profitability"):
            if component in candidate["chance"]:
                assert candidate["chance"][component]["status"] == "failed", component
                assert "chance computation exploded" in candidate["chance"][component]["reason"]
    assert broken_detail["results"]["evidence"]["recommendation"] == healthy_detail["results"]["evidence"]["recommendation"]
    assert broken_row.consumed_evaluations == healthy_row.consumed_evaluations
    assert broken_row.results["evidence"]["candidates"][0]["development_metrics"] is not None


async def test_a_fit_whose_run_has_no_detail_records_named_missing_not_a_failure(lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> None:
    symbol = unique_symbol()
    seed_lake(lake, symbol)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])

    def withholding_detail(request: EvaluationRequest) -> EvaluationResult:
        result = FakeEngine()(request)
        if request.detail and request.stage == "evidence":
            return EvaluationResult(metrics=result.metrics, detail=None, curve=result.curve)
        return result

    driver = Driver(roots=[lake], engine=FakeEngine())  # type: ignore[arg-type]
    driver.engine = withholding_detail  # type: ignore[assignment]
    row = await driver.to_candidate(symbol)
    detail = await driver.detail(row)
    for candidate in detail["results"]["evidence"]["candidates"]:
        if candidate["key"] == "incumbent":
            assert candidate["chance"]["comparison"]["status"] == "baseline"
            continue
        assert candidate["chance"]["comparison"]["status"] == "missing"
        assert candidate["chance"]["comparison"]["code"] in ("no_trade_list", "not_evaluated", "incumbent_unavailable")
        assert candidate["chance"]["profitability"]["status"] == "missing"
