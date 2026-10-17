"""Whole stage projections and scientific fold semantics backed by the real Options replay."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import setup_sha256
from app.research.options_golden_search.approval import verify_recorded_study
from app.research.options_golden_search.evaluator import EvaluationResult, evaluate_setup
from app.research.options_golden_search.planning import freeze_protocol
from app.research.options_golden_search.policy import classify
from app.research.options_golden_search.protocol import Knob, OptionsSearchProtocol, Policy, Window
from app.research.options_golden_search.search import Evaluation
from app.research.options_golden_search.stages import final, research
from app.research.runs.hashing import hash_payload
from app.schemas.options_golden_search import StudyDetail
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import (
    FakeRunData,
    contract_on,
    flat,
    listing_on,
    option_bars,
    steady,
    underlying_bars,
)


class ReplayEvaluator:
    """An in-memory persistence adapter; decisions, cash, traces and policy stay production code."""

    def __init__(self, protocol: OptionsSearchProtocol, data: FakeRunData) -> None:
        self.protocol = protocol
        self.data = data
        self.results: dict[str, EvaluationResult] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.trials: list[tuple[str, str]] = []
        self.fail_window: str | None = None
        self.verify_only = False

    def __call__(self, written: OptionsBacktestSetup, window: Window, scenario: str) -> Evaluation:
        if window.key == self.fail_window:
            raise RuntimeError("deliberate technical interruption")
        digest = setup_sha256(written)
        key = hash_payload({"setup": digest, "window": window.model_dump(mode="json"), "scenario": scenario})
        self.calls.append((digest, window.key, scenario))
        if key not in self.results:
            if self.verify_only:
                raise ValueError("Retained evaluation is missing; verification cannot authorize replay")
            self.results[key] = evaluate_setup(written, window, self.data)
        result = self.results[key]
        scope = "training" if window.key.startswith("training_") else "forward" if window.key.startswith("forward_") else window.key
        verdict = classify(result.metrics, self.protocol.policy, window, scope)
        return Evaluation(point_hash=digest, net_profit_usd=result.metrics.net_profit_usd,
                          max_drawdown_fraction=result.metrics.max_drawdown_fraction, sharpe=result.metrics.sharpe,
                          eligible=verdict.eligible, evidence_id=key)

    def result_for(self, evaluation: Evaluation, window: Window) -> EvaluationResult:
        return self.results[evaluation.evidence_id]

    def on_trial(self, trial: Any, window: Window) -> None:
        if not self.verify_only:
            self.trials.append((window.key, trial.role))


class StageHistory(FakeRunData):
    """Use the same dictionary data without the separate memory-liveness fixture's forced GC."""

    def handed_out_and_still_alive(self) -> int:
        return 0


@pytest.fixture(scope="module")
def experiment() -> tuple[OptionsSearchProtocol, FakeRunData]:
    days = tuple(expected_sessions(date(2024, 1, 1), date(2024, 5, 31)))
    options = {}
    for index, day in enumerate(days):
        exit_price = "2" if index % 2 == 0 else "3"
        # 10% stop loses $10 at minute 8; 20% stop survives the .85 dip and wins $100/$200.
        bars = {**flat("1", 5, 60), 7: ("1", "0.85"), 8: ("0.9", "1"), 61: (exit_price, exit_price)}
        options[(contract_on(day, "call", "500"), day)] = option_bars(day, bars)
    written = setup(leg(stop=("percent", "10")))
    policy = Policy(min_sharpe=0, min_activity_per_year=1, min_daily_observations=2,
                    min_forward_sessions=1, min_final_sessions=1)
    protocol = freeze_protocol(written, (Knob(path="legs.call.stop_loss.value", values=("10", "20")),),
        start_ms=et_midnight_ms(date(2024, 1, 1)), end_ms=et_midnight_ms(date(2024, 6, 1)),
        train_months=1, test_months=1, final_months=1, policy=policy, method="grid")
    data = StageHistory(underlying={day: underlying_bars(day, steady("500")) for day in days},
                       listings={day: listing_on(day) for day in days}, options=options)
    return protocol, data


def _row(protocol: OptionsSearchProtocol) -> dict[str, Any]:
    return {"id": "stage-study", "revision": 1, "phase": "research", "status": "running",
            "protocol_hash": protocol.protocol_hash, "setup_name": "two stop thresholds", "underlying": "SPY",
            "progress": {"stage": "Research"}, "candidates": [], "final_results": [], "gate_outcomes": [],
            "exposure": {"classification": "not_opened", "explanation": "test"}, "allowed_commands": [],
            "dispatch": None, "stage_token": None, "failure_reason": None, "qualification_id": None,
            "qualification_status": None}


def test_research_compare_and_final_use_real_whole_setup_evidence_with_valid_typed_views(
    experiment: tuple[OptionsSearchProtocol, FakeRunData],
) -> None:
    protocol, data = experiment
    evaluator = ReplayEvaluator(protocol, data)
    row = research(_row(protocol), evaluator)
    assert row["phase"] == "compare" and row["status"] == "completed"
    viewed = StudyDetail.from_record(row)
    assert len(viewed.candidates) == 2
    selected = next(candidate for candidate in viewed.candidates if not candidate.is_original)
    assert selected.verdict == "meets_rules"
    assert selected.net_profit_usd != next(candidate.net_profit_usd for candidate in viewed.candidates if candidate.is_original)
    assert len(row["research"]["folds"]) == 3
    assert all(fold["winner_hash"] == selected.candidate_hash for fold in row["research"]["folds"])
    assert all(outcome["status"] == "meets_rules" for outcome in row["gate_outcomes"])
    assert evaluator.trials
    assert not any(window == "final" for _digest, window, _scenario in evaluator.calls)
    row.update(phase="final", finalists=[candidate.candidate_hash for candidate in viewed.candidates])
    final_row = final(row, evaluator)
    final_view = StudyDetail.from_record(final_row)
    assert final_view.phase == "approval" and final_view.status == "completed"
    assert len(final_view.final_results) == len(row["finalists"]) == 2
    winner = next(candidate for candidate in final_view.final_results if not candidate.is_original)
    assert winner.verdict == "meets_rules"
    assert winner.completed_sessions == len(protocol.final.session_ms)
    assert sum(Decimal(month.net_profit_usd) for month in winner.monthly) == Decimal(winner.net_profit_usd)
    assert 2 <= len(winner.equity) <= 1000
    assert any(len(result.sessions) == len(protocol.final.session_ms) and len(result.sessions[0].observations) == 391 for result in evaluator.results.values())
    assert all(not session.attempts for session in winner.sessions)


def test_no_eligible_training_point_is_a_recorded_scientific_failure_never_a_zero_fold(
    experiment: tuple[OptionsSearchProtocol, FakeRunData],
) -> None:
    original, data = experiment
    protocol = original.model_copy(update={"policy": original.policy.model_copy(update={"min_sharpe": 1e9})})
    row = research(_row(protocol), ReplayEvaluator(protocol, data))
    assert all(fold["status"] == "no_eligible_point" and fold["forward"] is None for fold in row["research"]["folds"])
    assert any(outcome["status"] == "does_not_meet_rules" for outcome in row["gate_outcomes"])
    assert any(outcome["gate_id"] == "pooled_forward:coverage" for outcome in row["gate_outcomes"])


def test_technical_interruption_cannot_advance_a_partial_research_batch_to_compare(
    experiment: tuple[OptionsSearchProtocol, FakeRunData],
) -> None:
    protocol, data = experiment
    evaluator = ReplayEvaluator(protocol, data)
    evaluator.fail_window = "forward_1"
    row = _row(protocol)
    before = copy.deepcopy(row)
    with pytest.raises(RuntimeError, match="technical interruption"):
        research(row, evaluator)
    assert row == before


@pytest.fixture(scope="module")
def reviewed_experiment(experiment: tuple[OptionsSearchProtocol, FakeRunData]) -> tuple[dict[str, Any], ReplayEvaluator]:
    protocol, data = experiment
    evaluator = ReplayEvaluator(protocol, data)
    row = research(_row(protocol), evaluator)
    row.update(phase="final", finalists=[candidate["candidate_hash"] for candidate in row["candidates"]])
    return final(row, evaluator), evaluator


def test_reviewed_selection_folds_and_final_reconstruct_from_retained_replays_without_new_reads(
    reviewed_experiment: tuple[dict[str, Any], ReplayEvaluator],
) -> None:
    row, evaluator = reviewed_experiment
    before = copy.deepcopy(row)
    reads, trials = len(evaluator.data.asked), len(evaluator.trials)
    verify_recorded_study(row, evaluator)
    assert row == before
    assert len(evaluator.data.asked) == reads
    assert len(evaluator.trials) == trials
    assert evaluator.verify_only is False


@pytest.mark.parametrize("tampering", ["rule_threshold", "final_metric"])
def test_retained_reconstruction_rejects_tampered_rule_or_final_evidence(
    reviewed_experiment: tuple[dict[str, Any], ReplayEvaluator], tampering: str,
) -> None:
    original, evaluator = reviewed_experiment
    row = copy.deepcopy(original)
    if tampering == "rule_threshold":
        row["gate_outcomes"][0]["required"] = "invented changed threshold"
    else:
        row["final_results"][0]["net_profit_usd"] = "999999999"
    with pytest.raises(ValueError, match="do not reproduce"):
        verify_recorded_study(row, evaluator)
    assert evaluator.verify_only is False
