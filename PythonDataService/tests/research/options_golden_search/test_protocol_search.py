"""Executable frozen-procedure examples; synthetic profits are exact decimal dollars."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.options_backtest.setup import OptionsBacktestSetup
from app.research.options_golden_search.planning import budget_plan, freeze_protocol
from app.research.options_golden_search.protocol import (
    CostScenario,
    Knob,
    Policy,
    apply_scenario,
    materialize,
    point_hash,
)
from app.research.options_golden_search.search import Evaluation, run_forward, run_search
from app.utils.session_anchors import et_midnight_ms


def setup() -> OptionsBacktestSetup:
    return OptionsBacktestSetup.model_validate({
        "underlying": "SPY", "entry": {"anchor": "open", "offset_ms": 60000},
        "exit": {"anchor": "close", "offset_ms": 60000}, "square_off": "partial",
        "legs": [{"leg_id": "call_a", "right": "call", "contracts": 1, "expiry": {"kind": "same_day"},
                  "strike": {"kind": "atm"}, "stop_loss": {"kind": "percent", "value": "10"}},
                 {"leg_id": "put_b", "right": "put", "contracts": 1, "expiry": {"kind": "same_day"},
                  "strike": {"kind": "atm"}, "stop_loss": {"kind": "percent", "value": "10"}}],
        "fees": {"kind": "estimated"}, "starting_cash_usd": "10000.00"})


def protocol(*, method="grid", knobs=None, **kwargs):
    return freeze_protocol(setup(), tuple(knobs or [Knob(path="legs.call_a.stop_loss.value", values=("20", "30", "40"))]),
        start_ms=et_midnight_ms(date(2024, 1, 1)), end_ms=et_midnight_ms(date(2025, 7, 1)),
        method=method, **kwargs)


def result(s, profit, eligible=True):
    return Evaluation(point_hash=point_hash(s), net_profit_usd=Decimal(profit),
                      max_drawdown_fraction=Decimal("0.1"), sharpe=2, eligible=eligible, evidence_id=point_hash(s))


def test_a_search_ranks_money_exactly_keeps_a_tie_escapes_an_ineligible_seed_and_finds_pair_regions():
    p = protocol()
    seen = []
    def evaluate(s, w, scenario):
        value = s.legs[0].stop_loss.value
        seen.append(value)
        # Sub-float precision still decides the winner.
        return result(s, {Decimal(10): "1", Decimal(20): "2.00000000000000000001", Decimal(30): "2", Decimal(40): "0"}[value])
    searched = run_search(p, p.development, evaluate)
    assert searched.winner.net_profit_usd == Decimal("2.00000000000000000001"), "grid: the exact money ranking decides the winner"
    assert seen == [Decimal(10), Decimal(20), Decimal(30), Decimal(40)], "grid: the original outside categories are all evaluated in order"
    assert len(searched.eligible) == 4, "grid: every point is eligible"
    assert searched.unique_evaluations == 4, "grid: four unique evaluations"

    p = protocol(method="zoom")
    tied = run_search(p, p.development, lambda s, w, c: result(s, "1"))
    assert tied.winner.point_hash == point_hash(p.setup), "zoom: an exact objective tie keeps the original seed"
    escaped = run_search(p, p.development, lambda s, w, c: result(s, str(s.legs[0].stop_loss.value), s.legs[0].stop_loss.value != 10))
    assert escaped.winner.net_profit_usd == 40, "zoom: an ineligible seed can be escaped"
    none = run_search(p, p.development, lambda s, w, c: result(s, "100", False))
    assert none.winner is None and not none.eligible, "zoom: no eligible point leaves no winner"

    knobs = [Knob(path=f"legs.{name}.stop_loss.value", values=("10", "20")) for name in ("call_a", "put_b")]
    def both_legs(s, w, c):
        both = all(leg.stop_loss.value == 20 for leg in s.legs)
        return result(s, "10" if both else "-1", both)
    p = protocol(method="zoom", knobs=knobs)
    assert run_search(p, p.development, both_legs).winner is None, "pair audit: without the audit the region needing both leg changes is missed"
    audited = p.model_copy(update={"pair_audits": ((knobs[0].path, knobs[1].path),)})
    assert run_search(audited, p.development, both_legs).winner.net_profit_usd == 10, "pair audit: the audit finds the region needing simultaneous leg changes"


def test_stable_leg_ids_and_frozen_structure_sessions_and_budget():
    p = protocol()
    updated = materialize(p, {"legs.call_a.stop_loss.value": "20"})
    assert updated.legs[1] == p.setup.legs[1], "a stable leg id leaves the other leg as saved"
    assert updated.legs[0].contracts == p.setup.legs[0].contracts, "a stable leg id leaves the edited leg's contracts as saved"
    with pytest.raises(ValueError, match="unsupported"):
        protocol(knobs=[Knob(path="legs.call_a.contracts", values=(1, 2))])
    with pytest.raises(ValueError, match="unknown stable leg"):
        protocol(knobs=[Knob(path="legs.0.stop_loss.value", values=("10", "20"))])

    dates = [t for fold in p.folds for t in fold.forward.session_ms]
    assert len(dates) == len(set(dates)), "the frozen forward sessions are disjoint"
    assert p.folds[-1].forward.end_ms == p.development.end_ms == p.final.start_ms, "the last forward window ends where development ends and the final test starts"
    budget = budget_plan(p)
    assert budget.mandatory_downstream > budget.final > p.finalist_count, "the budget includes the mandatory downstream work"
    assert budget.unique_upper_bound > budget.selection_total, "the unique upper bound exceeds the selection total"
    assert budget.replay_session_upper_bound > budget.unique_upper_bound, "the replay session bound exceeds the unique upper bound"
    with pytest.raises(ValueError):
        freeze_protocol(setup(), p.knobs, start_ms=p.development.start_ms,
                        end_ms=et_midnight_ms(date(2025, 6, 1)))


def test_illegal_dependency_is_excluded_and_zero_reentry_is_canonical_absence():
    p = protocol(knobs=[Knob(path="legs.call_a.stop_loss", values=(None, {"kind": "percent", "value": "10"})),
                        Knob(path="legs.call_a.reentry_on_stop", values=(None, {"kind": "at_once", "count": 1}))])
    searched = run_search(p, p.development, lambda s, w, c: result(s, "1"))
    assert sum(t.status == "invalid" for t in searched.trials) == 1
    assert searched.unique_evaluations == 3


def test_fold_selection_resets_original_ranges_and_cannot_observe_future():
    p = protocol()
    def evaluate(s, w, c):
        value = s.legs[0].stop_loss.value
        desired = 20 if w.key == "training_0" else 30
        return result(s, str(100 - abs(value - desired)))
    before = run_forward(p, evaluate)
    def perturbed(s, w, c):
        return result(s, "-999", False) if w.key.startswith("forward") else evaluate(s, w, c)
    after = run_forward(p, perturbed)
    assert [f.selection.winner.point_hash for f in before] == [f.selection.winner.point_hash for f in after]
    assert [f.selection.trials[0].point_hash for f in before] == [point_hash(p.setup)] * 3
    assert before[0].selection.winner.point_hash != before[1].selection.winner.point_hash


def test_apply_scenario_keeps_the_estimated_policy_and_refuses_a_stress_of_zero_saved_costs():
    s = setup()
    stressed = apply_scenario(s, CostScenario(key="fees", fee_multiplier=Decimal(2)))
    assert stressed.fees == s.fees, "stress keeps the estimated fee policy for canonical tier scaling"
    stressed = apply_scenario(s, CostScenario(key="slippage", slippage_multiplier=Decimal(2)))
    assert stressed.slippage_percent == Decimal("0.6"), "a slippage stress doubles the saved slippage"
    assert Policy().max_drawdown_fraction == Decimal("0.2"), "the default policy drawdown limit"

    original = setup()
    zero = OptionsBacktestSetup.model_validate({**original.model_dump(mode="json"),
        "slippage_percent": "0", "fees": {"kind": "not_charged"}})
    # Zero saved costs need a positive explicit stress, not a multiplier.
    with pytest.raises(ValueError, match="zero"):
        apply_scenario(zero, CostScenario(key="slippage", slippage_multiplier=Decimal(2)))
    with pytest.raises(ValueError, match="zero"):
        apply_scenario(zero, CostScenario(key="fees", fee_multiplier=Decimal(2)))
    scenario = CostScenario(key="explicit", slippage_percent=Decimal("0.0123"),
                            fee_per_contract_usd=Decimal("0.0123"), fee_multiplier=Decimal(2))
    stressed = apply_scenario(zero, scenario)
    assert stressed.slippage_percent == Decimal("0.0123"), "an explicit stress preserves the exact slippage amount"
    assert stressed.fees.per_contract_usd == Decimal("0.0123"), "an explicit stress preserves the exact fee amount"
    assert scenario.effective_fee_multiplier == 1, "an explicit fee stress has an effective multiplier of one"
    estimated = apply_scenario(original, CostScenario(key="fees", fee_multiplier=Decimal(2)))
    assert estimated.fees.kind == "estimated", "a fee stress on saved estimated fees stays estimated"


def test_batched_completion_order_is_reassembled_before_adaptive_decisions():
    p = protocol(method="zoom")
    def scalar(s, w, c):
        return result(s, str(s.legs[0].stop_loss.value))
    expected = run_search(p, p.development, scalar)
    class Batched:
        def __init__(self):
            self.trials = []
            self.batches = []
        def __call__(self, s, w, c):
            return scalar(s, w, c)
        def evaluate_batch(self, requests):
            self.batches.append(len(requests))
            return [scalar(*request) for request in requests]
        def on_trial(self, trial, window):
            self.trials.append(trial)
    evaluator = Batched()
    actual = run_search(p, p.development, evaluator)
    assert actual == expected
    assert evaluator.trials == list(actual.trials)
    assert max(evaluator.batches) > 1


@pytest.mark.parametrize("method", ["grid", "zoom"])
@pytest.mark.parametrize("objective,winner", [("net_profit", "20"), ("sharpe", "30"), ("drawdown", "40")])
def test_frozen_objective_selects_only_passing_candidates(method, objective, winner):
    p = protocol(method=method, policy=Policy(rank_by=objective))
    def evaluate(s, w, c):
        value = int(s.legs[0].stop_loss.value)
        profit, drawdown, sharpe = {10: (1, "0.2", 1), 20: (30, "0.15", 2),
                                    30: (20, "0.1", 4), 40: (10, "0.05", 3)}[value]
        return result(s, str(profit)).model_copy(update={"max_drawdown_fraction": Decimal(drawdown), "sharpe": sharpe})
    searched = run_search(p, p.development, evaluate)
    assert materialize(p, searched.points[searched.winner.point_hash]).legs[0].stop_loss.value == Decimal(winner)
    assert searched.eligible[0].point_hash == searched.winner.point_hash
    refused = run_search(p, p.development, lambda s, w, c: evaluate(s, w, c).model_copy(update={"eligible": False}))
    assert refused.winner is None and not refused.eligible


def test_legacy_profit_protocol_identity_and_new_objective_round_trip():
    from app.research.options_golden_search.protocol import OptionsSearchProtocol

    p = protocol()
    data = p.model_dump(mode="json")
    assert "rank_by" not in data["policy"]
    assert OptionsSearchProtocol.model_validate(data).protocol_hash == p.protocol_hash
    ranked = protocol(policy=Policy(rank_by="sharpe"))
    assert ranked.protocol_hash != p.protocol_hash
    assert OptionsSearchProtocol.model_validate(ranked.model_dump(mode="json")).policy.rank_by == "sharpe"


def test_shared_rule_is_one_axis_without_cross_leg_combinations():
    values = tuple({leg: {"kind": "percent", "value": amount} for leg in ("call_a", "put_b")} for amount in ("20", "30"))
    p = protocol(knobs=[Knob(path="all_legs.stop_loss", values=values)])
    visited = []
    def evaluate(s, w, c):
        visited.append(tuple(leg.stop_loss.value for leg in s.legs))
        return result(s, str(s.legs[0].stop_loss.value))
    run_search(p, p.development, evaluate)
    assert visited == [(Decimal(10), Decimal(10)), (Decimal(20), Decimal(20)), (Decimal(30), Decimal(30))]
    with pytest.raises(ValueError, match="shared rule and its individual"):
        protocol(knobs=[Knob(path="all_legs.stop_loss", values=values), Knob(path="legs.call_a.stop_loss.value", values=("10", "20"))])
    with pytest.raises(ValueError, match="every stable leg"):
        protocol(knobs=[Knob(path="all_legs.stop_loss", values=({"call_a": None}, {"call_a": {"kind": "percent", "value": "20"}}))])


def test_shared_seed_preserves_different_saved_rules_and_validates_each_leg():
    from app.research.options_golden_search.protocol import read_path, validate_value

    original = setup()
    data = original.model_dump(mode="json")
    data["legs"][1]["stop_loss"]["value"] = "15"
    original = OptionsBacktestSetup.model_validate(data)
    saved = read_path(original, "all_legs.stop_loss")
    assert saved["call_a"]["value"] == "10" and saved["put_b"]["value"] == "15"
    validate_value(original, "all_legs.stop_loss", saved)
    with pytest.raises(ValueError, match="same rule"):
        validate_value(original, "all_legs.stop_loss", {"call_a": None, "put_b": {"kind": "percent", "value": "30"}})
    with pytest.raises(ValueError, match="below 100"):
        validate_value(original, "all_legs.stop_loss", {leg.leg_id: {"kind": "percent", "value": "100"} for leg in original.legs})


def test_decimal_candidate_identity_v2_preserves_the_saved_seed_and_deduplicates_range_spellings():
    knobs = [Knob(path="legs.call_a.stop_loss.value", values=("10.0000", "20.0000"))]
    p = protocol(knobs=knobs, candidate_identity_version=2)
    assert point_hash(materialize(p, {knobs[0].path: "10.0000"})) == point_hash(p.setup)
    assert str(materialize(p, {knobs[0].path: "20.0000"}).legs[0].stop_loss.value) == "20"
    legacy = protocol(knobs=knobs)
    assert str(materialize(legacy, {knobs[0].path: "20.0000"}).legs[0].stop_loss.value) == "20.0000"
    assert "candidate_identity_version" not in legacy.model_dump(mode="json")
