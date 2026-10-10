"""The evidence stage's chance readings through the real service, and what they cannot change (#3011).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). Every test seeds its own
thin lake under a symbol of its own.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.research.golden_search import stages
from app.research.golden_search.chance_final import SELECTION_NOTE
from app.research.golden_search.declarations import point_hash
from app.research.golden_search.evaluator import EvaluationRequest, EvaluationResult
from app.research.persistence import lifecycle
from app.schemas.golden_search import (
    GoldenSearchChance,
    GoldenSearchChanceRehearsalMeasured,
    GoldenSearchExamFit,
    GoldenSearchExamView,
    GoldenSearchFinalChance,
)
from tests._helpers.golden_search_study import Driver, FakeEngine, seed_lake, unique_symbol, window_ms

#: Windows starting at or after this instant peak at gap 0.10 instead of 0.35: the recent
#: months and the final window sit past it, the all-period development window does not.
RECENT_CUTOFF_MS = window_ms((date(2025, 2, 15), date(2025, 2, 16)))[0]


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
    # The baseline draws its own depth: the incumbent has a drawdown chart too (#3013).
    assert baseline["drawdown"]["status"] == "measured"
    assert baseline["drawdown"]["recorded_engine_per_bar"] is not None
    assert sum(baseline["drawdown"]["counts"].values()) == 10_000

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
        # The drawdown reading: this run's own source, over the development window's own session count.
        drawdown = chance["drawdown"]
        assert drawdown["status"] == "measured"
        assert drawdown["basis"]["thresholds"] == {"ceiling": row.protocol["policy"]["max_drawdown_ceiling"]}
        assert drawdown["basis"]["horizon"]["window"] == {"start_ms": row.protocol["development_start_ms"], "end_ms": row.protocol["development_end_ms"]}
        assert drawdown["counts"]["beyond_ceiling"] + drawdown["counts"]["within_ceiling"] + drawdown["counts"]["undefined"] == 10_000
        assert 0.0 <= drawdown["recorded_close_only"] <= 1.0

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


async def test_the_exam_stage_stores_final_chance_and_a_carry_over_without_new_evaluations(driver: Driver, symbol: str) -> None:
    row = await driver.to_candidate(symbol)
    before = row.consumed_evaluations
    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    detail = await driver.detail(row)
    exam = detail["results"]["exam"]
    GoldenSearchExamFit.model_validate(exam["fits"][0])

    for fit in exam["fits"]:
        chance = fit["chance"]
        GoldenSearchFinalChance.model_validate(chance)
        # The comparison and profitability ran on the final interval's own sessions.
        assert chance["comparison"]["status"] == "measured"
        assert chance["comparison"]["basis"]["source"]["window"] == {"start_ms": row.protocol["final_start_ms"], "end_ms": row.protocol["final_end_ms"]}
        # Every sentence names the window it resampled, and the caveat is the two-selection note, never the
        # development stage's selected-on-these-sessions one (#3014).
        assert "final sessions" in chance["comparison"]["sentence"]
        assert chance["comparison"]["bias_note"] == SELECTION_NOTE
        assert "final paths" in chance["profitability"]["sentence"]
        assert chance["profitability"]["status"] == "measured"
        # The carry-over reproduced the stored 1× rehearsal: same scheduled length the evidence stage recorded.
        carry = chance["carry_over"]
        assert carry["status"] == "measured", carry.get("reason")
        rehearsal = next(candidate["chance"]["rehearsal"] for candidate in (await driver.detail(row))["results"]["evidence"]["candidates"] if candidate["key"] == fit["key"])
        assert carry["basis"]["scheduled_sessions"] == rehearsal["basis"]["final_interval"]["scheduled_sessions"]
        assert carry["finite"] + carry["undefined"] == 10_000
    # One distinct fit ran, so the optional fit-to-fit comparison is absent by design, not failed.
    assert exam["chance"]["fit_to_fit"]["code"] == "single_fit"
    # The exam's own final runs are all the engine spent — one per fit plus the incumbent — so the diagnostics
    # added no evaluation beyond what a chance-less exam would have run.
    assert row.consumed_evaluations - before == len(exam["fits"]) + 1
    assert all(fit["outcome"] is not None for fit in exam["fits"])


async def test_two_fits_get_one_shared_fit_to_fit_draw_and_each_keeps_its_own_evidence(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection
) -> None:
    symbol = unique_symbol()
    seed_lake(lake, symbol)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])

    def drifting_score(point: dict[str, Any], window: tuple[int, int], scenario: str) -> float:
        # The development window peaks at gap 0.35 and the recent months (and the final
        # window) peak at gap 0.10, so the two fits choose different settings and the
        # exam holds two distinct fits with distinct stored rehearsals.
        gap = float(point.get("gap", 0.2))
        hold = float(point.get("hold_bars", 5))
        cost = {"base": 0.0, "slippage_1c": 0.05, "commission_1": 0.02}.get(scenario, 0.0)
        peak = 0.10 if window[0] >= RECENT_CUTOFF_MS else 0.35
        return 2.0 - 8.0 * (gap - peak) ** 2 - ((hold - 7.0) / 4.0) ** 2 - cost

    driver = Driver(roots=[lake], engine=FakeEngine(score=drifting_score))  # type: ignore[arg-type]
    row = await driver.to_candidate(symbol)
    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})
    detail = await driver.detail(row)
    exam = detail["results"]["exam"]
    assert len(exam["fits"]) == 2, "the drifting landscape made the two fits choose distinct settings"
    fit_to_fit = exam["chance"]["fit_to_fit"]
    assert fit_to_fit["status"] == "measured", fit_to_fit.get("reason")
    assert sorted(fit_to_fit["pair"]) == sorted(point_hash(row.strategy_key, dict(fit["point"])) for fit in exam["fits"])
    first, second = fit_to_fit["directions"]
    for direction in (first, second):
        assert sum(direction["counts"].values()) == 10_000
        assert direction["fit"]["point_hash"] != direction["opponent"]["point_hash"]
    assert "All-period fit" in first["sentence"] and "Recent fit" in first["sentence"]
    assert "unadjusted for that choice" in first["sentence"] or "not adjusted for that choice" in first["sentence"]
    # The sentence the panel reads follows the fit the page is about (#3029): each fit's own chance record
    # carries its own direction of the same shared draw, and the browser never computes a complement.
    by_key = {fit["key"]: fit for fit in exam["fits"]}
    for fit in exam["fits"]:
        own = fit["chance"]["fit_to_fit"]
        assert own["status"] == "measured", own.get("reason")
        assert own["fit"]["point_hash"] == point_hash(row.strategy_key, dict(fit["point"]))
        assert own["fit"]["label"] == fit["label"]
        assert own["sentence"].index(fit["label"]) < own["sentence"].index(by_key["recent" if fit["key"] == "all_period" else "all_period"]["label"])
    all_period = by_key["all_period"]["chance"]["fit_to_fit"]
    recent = by_key["recent"]["chance"]["fit_to_fit"]
    assert all_period["counts"]["better"] == recent["counts"]["worse"]
    assert all_period["counts"]["tied"] == recent["counts"]["tied"]
    assert all_period["at_least_as_good"]["count"] + recent["counts"]["undefined"] - recent["counts"]["tied"] == 10_000 - recent["counts"]["tied"]  # tied support both, undefined neither
    # Each fit's own readings stand beside the shared one, whatever the other fit did.
    for fit in exam["fits"]:
        assert fit["chance"]["comparison"]["status"] == "measured"
        assert fit["chance"]["carry_over"]["status"] == "measured"
        assert fit["outcome"] is not None


async def test_a_legacy_one_direction_fit_to_fit_record_still_serves_a_wire_valid_page(
    lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection, symbol: str
) -> None:
    """A study whose exam ran between #3014 and #3029 holds a v1 one-direction record; the page still serves (#3029)."""
    symbol = unique_symbol()
    seed_lake(lake, symbol)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])

    def drifting_score(point: dict[str, Any], window: tuple[int, int], scenario: str) -> float:
        gap = float(point.get("gap", 0.2))
        hold = float(point.get("hold_bars", 5))
        cost = {"base": 0.0, "slippage_1c": 0.05, "commission_1": 0.02}.get(scenario, 0.0)
        peak = 0.10 if window[0] >= RECENT_CUTOFF_MS else 0.35
        return 2.0 - 8.0 * (gap - peak) ** 2 - ((hold - 7.0) / 4.0) ** 2 - cost

    driver = Driver(roots=[lake], engine=FakeEngine(score=drifting_score))  # type: ignore[arg-type]
    row = await driver.to_candidate(symbol)
    row = await driver.advance(row, "open_exam", {"acknowledge_final_test": True})

    # Hold the exam the way a study examined before #3029 sits in the database: its v1 fit-to-fit record
    # carries one direction only, with no identity inside the record, exactly as #3014 wrote it.
    stored = json.loads((await conn.fetchrow("SELECT results_json FROM research_golden_search_studies WHERE id = $1", row.id))["results_json"])
    v2 = stored["exam"]["chance"]["fit_to_fit"]
    first_direction = v2["directions"][0]
    stored["exam"]["chance"]["fit_to_fit"] = {
        "status": "measured",
        "pair": v2["pair"],
        "counts": first_direction["counts"],
        "shares": first_direction["shares"],
        "at_least_as_good": first_direction["at_least_as_good"],
        "sentence": first_direction["sentence"],
        "selection_note": first_direction["selection_note"],
        "basis": {**v2["basis"], "schema_version": "golden-search-chance-fit-to-fit/v1"},
    }
    await conn.execute("UPDATE research_golden_search_studies SET results_json = $2 WHERE id = $1", row.id, json.dumps(stored))

    detail = await driver.detail(row)
    exam = detail["results"]["exam"]
    GoldenSearchExamView.model_validate(exam)  # the legacy record never breaks the wire contract
    by_key = {fit["key"]: fit for fit in exam["fits"]}
    owner_key, other_key = stored["exam"]["fits"][0]["key"], stored["exam"]["fits"][1]["key"]
    owner = by_key[owner_key]["chance"]["fit_to_fit"]
    assert owner["status"] == "measured"
    assert owner["fit"]["label"] == by_key[owner_key]["label"]
    assert owner["sentence"] == first_direction["sentence"]  # the recorded direction, honestly
    other = by_key[other_key]["chance"]["fit_to_fit"]
    assert other["status"] == "missing" and other["code"] == "direction_unavailable"
    assert "never recorded" in other["reason"]
