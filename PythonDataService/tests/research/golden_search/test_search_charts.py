"""The Search step's charts (#2821): the replay rebuilds exactly what the procedure scored, ``atol=1e-9``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from app.research.golden_search.declarations import declaration_for, knob_values, point_hash
from app.research.golden_search.grid_procedure import run_grid
from app.research.golden_search.models import EvaluationRecord
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.protocol import GoldenSearchProtocol
from app.research.golden_search.search_charts import GRID_PATH, NOT_REPLAYED, procedure_charts
from app.research.golden_search.selection import Metrics
from app.research.golden_search.zoom import ProcedureResult, run_zoom
from tests._helpers.golden_search import Landscape, metrics
from tests._helpers.golden_search_study import plan_request

EMA = declaration_for("ema_crossover_signal")
assert EMA is not None


def _score(point: Mapping[str, Any]) -> Metrics:
    """Best at gap 0.3 and a 7-bar hold; a hold under 4 bars trades too little to be eligible."""
    gap, hold = float(point["gap"]), float(point.get("hold_bars", 5))
    sharpe = 2.0 - abs(gap - 0.3) * 3 - abs(hold - 7) * 0.1
    return metrics(sharpe, trades=50 if hold >= 4 else 5)


def _records(landscape: Landscape) -> list[EvaluationRecord]:
    """One evaluation row per distinct point scored, in the order first asked for."""
    rows: dict[str, EvaluationRecord] = {}
    for point in landscape.sent:
        hashed = point_hash("ema_crossover_signal", point)
        if hashed in rows:
            continue
        scored = _score(point)
        rows[hashed] = EvaluationRecord(
            study_id="s", evaluation_key=hashed, point_hash=hashed, point=dict(point), window_start_ms=0, window_end_ms=1, scenario="base",
            detail=False, stage="search", fold_index=None, status="completed", attempt=1, retries=0, total_trades=scored.total_trades,
            net_profit=scored.net_profit, total_return_pct=scored.total_return_pct, sharpe_ratio=scored.sharpe_ratio,
            max_drawdown_pct=scored.max_drawdown_pct, win_rate=scored.win_rate, error=None, detail_json=None, created_at_ms=0, completed_at_ms=0,
        )  # fmt: skip
    return list(rows.values())


def _charts(protocol: GoldenSearchProtocol, result: ProcedureResult, landscape: Landscape) -> dict[str, Any]:
    record = {"window": {"start_ms": 0, "end_ms": 1}, "procedure": result.as_dict()}
    return procedure_charts(key="search", record=record, declaration=EMA, protocol=protocol, policy=_policy(protocol), records=_records(landscape))


def _policy(protocol: GoldenSearchProtocol) -> Any:
    """The plan's rules with a floor of 30 trades, as the window's floor would resolve them."""
    return replace(protocol.policy, min_trades=30)


def _zoom() -> tuple[GoldenSearchProtocol, ProcedureResult, Landscape]:
    protocol = protocol_from_request(plan_request("SPY"))
    landscape = Landscape(_score)
    return protocol, run_zoom(declaration=EMA, protocol=protocol, seed=protocol.seed, evaluate=landscape, policy=_policy(protocol)), landscape


def test_the_replay_rebuilds_every_point_zoom_scored_in_order() -> None:
    protocol, result, landscape = _zoom()
    charts = _charts(protocol, result, landscape)

    tried = charts["convergence"]["tried"]
    assert [point_hash("ema_crossover_signal", item["point"]) for item in tried] == [point_hash("ema_crossover_signal", point) for point in landscape.sent]
    seed = tried[0]
    assert (seed["order"], seed["pass_index"], seed["knob"], seed["value"]) == (0, None, None, None)
    # Each round's values, with the knob and value the round recorded.
    for item in tried[1:]:
        assert float(knob_values(EMA, item["point"])[item["knob"]]) == pytest.approx(item["value"], abs=1e-12, rel=0)


def test_best_so_far_climbs_to_the_winners_objective_and_counts_only_eligible_points() -> None:
    protocol, result, landscape = _zoom()
    tried = _charts(protocol, result, landscape)["convergence"]["tried"]

    best = [item["best_so_far"] for item in tried]
    assert all(later >= earlier for earlier, later in zip(best, best[1:], strict=False) if earlier is not None)
    assert best[-1] == pytest.approx(result.winner_metrics.sharpe_ratio, abs=1e-9, rel=0)
    # A 2- or 3-bar hold trades 5 times, under the floor of 30: ineligible, so it never raises the best.
    short = [item for item in tried if item["knob"] == "hold_bars" and item["value"] < 4]
    assert short and all(item["ineligibility"] == "TOO_FEW_TRADES" for item in short)


def test_a_path_that_does_not_rebuild_the_winner_is_not_drawn() -> None:
    protocol, result, landscape = _zoom()
    charts = _charts(protocol, replace(result, winner_hash="0" * 64), landscape)

    assert charts["convergence"] == {"status": "missing", "reason": NOT_REPLAYED}
    assert charts["profiles"] == []


def test_knob_moves_place_the_start_and_the_retained_value_in_the_searched_range() -> None:
    protocol, result, landscape = _zoom()
    moves = {move["name"]: move for move in _charts(protocol, result, landscape)["moves"]}

    # gap searched 0.0–0.6 from 0.2 to 0.3; hold 2–12 from 5 to 7.
    gap, hold = moves["gap"], moves["hold_bars"]
    assert (gap["start"], gap["retained"], gap["moved"], gap["edge_hit"]) == (pytest.approx(0.2, abs=1e-12), pytest.approx(0.3, abs=1e-12), True, False)
    assert (gap["start_position"], gap["retained_position"]) == pytest.approx((1 / 3, 0.5), abs=1e-9, rel=0)
    assert (hold["start"], hold["retained"], hold["start_position"], hold["retained_position"]) == pytest.approx((5, 7, 0.3, 0.5), abs=1e-9, rel=0)


def test_a_zoom_profile_is_its_knobs_last_pass_with_the_other_knobs_held() -> None:
    protocol, result, landscape = _zoom()
    profiles = {profile["name"]: profile for profile in _charts(protocol, result, landscape)["profiles"]}

    hold = profiles["hold_bars"]
    # Gap had already moved to 0.3 when the hold was searched in the single pass.
    assert hold["pass_index"] == 0 and {knob["name"]: knob["value"] for knob in hold["held"]}["gap"] == pytest.approx(0.3, abs=1e-12)
    values = [point["value"] for point in hold["points"]]
    assert values == sorted(set(values)) and [point["value"] for point in hold["points"] if point["retained"]] == [7]


def test_grid_slices_its_scored_points_through_the_winner_and_draws_no_path() -> None:
    protocol = replace(protocol_from_request(plan_request("SPY")), method="grid")
    landscape = Landscape(_score)
    result = run_grid(declaration=EMA, protocol=protocol, seed=protocol.seed, evaluate=landscape, policy=_policy(protocol))
    charts = _charts(protocol, result, landscape)

    assert charts["convergence"] == {"status": "missing", "reason": GRID_PATH}
    gap = next(profile for profile in charts["profiles"] if profile["name"] == "gap")
    assert gap["pass_index"] is None and len(gap["points"]) == 13  # 0.0 to 0.6 by 0.05, the hold at the winner's 7
    assert [point["value"] for point in gap["points"] if point["retained"]] == [pytest.approx(0.3, abs=1e-12)]
    assert sum(point["winner"] for point in charts["points"]) == 1 and len(charts["points"]) == len(landscape.sent)
