"""The final-test rehearsal ladder: floors, predicates, joints and their basis (#3012)."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

import numpy as np
import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc
from app.research.golden_search.activity import TradeFloors, activity_plan
from app.research.golden_search.chance_rehearsal import (
    NOT_MEASURED,
    final_session_count,
    rehearsal_reading,
    rung_floor,
    stored_rehearsal,
)
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.selection import Metrics
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search_study import frequency_plan_request, plan_request

CAPITAL = 100_000.0


def _completed() -> Metrics:
    return Metrics(status="completed", total_trades=40, net_profit=1_000.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=0.1, win_rate=0.5)


def _sessions(n: int) -> tuple[int, ...]:
    return tuple(session_close_ms_utc(day) for day in expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:n])


def _detail(returns: list[float], *, exits: tuple[int, ...] = (), capital: float = CAPITAL) -> dict[str, Any]:
    session_ms = _sessions(len(returns))
    equity = [capital]
    for value in returns:  # compounded, as the engine's daily equity curve is
        equity.append(equity[-1] * (1.0 + value))
    return {
        "initial_cash": capital,
        "daily_equity": [[ms, value] for ms, value in zip(session_ms, equity[1:], strict=True)],
        "trades": [{"exit_ms": session_ms[position] - 60_000} for position in exits],
    }


def _window_for(n: int) -> tuple[int, int]:
    days = expected_sessions(date(2025, 1, 2), date(2027, 12, 31))[:n]
    return et_midnight_ms(days[0]), et_midnight_ms(days[-1] + timedelta(days=1))


def _frequency_world(rate: int = 50) -> tuple[TradeFloors, int, Fraction, tuple[int, int]]:
    """A frequency plan whose final interval's frozen year fraction makes the two rounding rules disagree."""
    final_window = (et_midnight_ms(date(2026, 1, 1)), et_midnight_ms(date(2026, 9, 1)))
    protocol = protocol_from_request(
        frequency_plan_request(
            "SPY",
            rate=rate,
            development_start_ms=et_midnight_ms(date(2025, 1, 1)),
            development_end_ms=et_midnight_ms(date(2025, 4, 1)),
            final_start_ms=final_window[0],
            final_end_ms=final_window[1],
        )
    )
    floors = TradeFloors(protocol, {"activity": activity_plan(protocol)})
    years = floors.trading_years(final_window)
    return floors, rate, years, final_window


def _legacy_world(exam_min_trades: int = 5) -> tuple[TradeFloors, None, None, tuple[int, int]]:
    final_window = (et_midnight_ms(date(2025, 4, 1)), et_midnight_ms(date(2025, 5, 1)))
    protocol = protocol_from_request(plan_request("SPY", development_start_ms=et_midnight_ms(date(2025, 1, 1)), development_end_ms=et_midnight_ms(date(2025, 4, 1))))
    protocol = type(protocol).from_dict({**protocol.as_dict(), "exam_min_trades": exam_min_trades})
    return TradeFloors(protocol, {}), None, None, final_window


def _reading(
    candidate: list[float],
    incumbent: list[float],
    *,
    objective: str = "total_return_pct",
    ceiling: float = 0.20,
    require_positive_net: bool = True,
    floors: TradeFloors | None = None,
    rate: int | None = None,
    final_years: Fraction | None = None,
    final_window: tuple[int, int] | None = None,
    exits: tuple[int, ...] = (),
    cancel_check: Any = lambda: None,
    hashes: tuple[str, str] = ("cafebabe", "deadbeef"),
) -> dict[str, Any]:
    world = _legacy_world() if floors is None else (floors, rate, final_years, final_window)
    return rehearsal_reading(
        objective=objective,  # type: ignore[arg-type]
        window=_window_for(len(candidate)),
        final_window=world[3],
        floors=world[0],
        rate=world[1],
        final_years=world[2],
        ceiling=ceiling,
        require_positive_net=require_positive_net,
        candidate_metrics=_completed(),
        candidate_detail=_detail(candidate, exits=exits),
        candidate_hash=hashes[0],
        incumbent_metrics=_completed(),
        incumbent_detail=_detail(incumbent),
        incumbent_hash=hashes[1],
        cancel_check=cancel_check,
    )


def _smooth(n: int, seed: int) -> list[float]:
    return list(np.random.default_rng(seed).normal(0.0005, 0.01, n))


# ── Floors ────────────────────────────────────────────────────────────────


def test_a_frequency_rung_rounds_once_on_the_frozen_years_and_a_fixed_floor_plan_keeps_its_final_floor_on_every_rung() -> None:
    from math import ceil

    floors, rate, years, final_window = _frequency_world()
    # Precondition, so a calendar change fails here loudly instead of asserting wrong numbers:
    # this final interval's frozen fraction makes ceil(k x rate x years) disagree with k x ceil(rate x years) at k = 2.
    assert ceil(2 * rate * years) != 2 * ceil(rate * years), "precondition: the frozen fraction makes the two rounding rules disagree at k = 2"
    assert final_session_count(final_window) == len(expected_sessions(date(2026, 1, 1), date(2026, 8, 31))), "the final window's session count is the calendar's"
    assert rung_floor(floors, final_window, 1, rate=rate, final_years=years) == floors.at(final_window, final=True), "the first rung is the final floor"
    assert rung_floor(floors, final_window, 2, rate=rate, final_years=years) == ceil(2 * rate * years), "the second rung rounds once on the frozen years"
    assert rung_floor(floors, final_window, 3, rate=rate, final_years=years) == ceil(3 * rate * years), "the third rung rounds once on the frozen years"
    assert rung_floor(floors, final_window, 2, rate=rate, final_years=years) != 2 * rung_floor(floors, final_window, 1, rate=rate, final_years=years), "a rung is not the rounded floor times k"

    legacy_floors, _, _, legacy_window = _legacy_world(exam_min_trades=7)
    for k in (1, 2, 3, 4):
        assert rung_floor(legacy_floors, legacy_window, k, rate=None, final_years=None) == 7, f"a fixed-floor legacy plan uses its final floor on rung {k}"


# ── The ladder ────────────────────────────────────────────────────────────


def test_a_measured_ladder_counts_every_rule_and_the_joint_over_all_paths() -> None:
    reading = _reading(_smooth(60, 1), _smooth(60, 2), exits=(0, 5, 10))
    assert reading["status"] == "measured"
    assert [rung["k"] for rung in reading["rungs"]] == [1, 2, 3, 4]
    m = final_session_count((et_midnight_ms(date(2025, 4, 1)), et_midnight_ms(date(2025, 5, 1))))
    assert [rung["sessions"] for rung in reading["rungs"]] == [k * m for k in (1, 2, 3, 4)]
    for rung in reading["rungs"]:
        assert rung["denominator"] == 10_000
        assert rung["passes"]["pass_joint"] <= min(rung["passes"][name] for name in rung["passes"] if name != "pass_joint")
        assert sum(1 for name in rung["passes"] if name.startswith("pass_")) == 5  # joint + four rules
        assert set(rung.get("undefined", {})) <= {"undefined_positive_net", "undefined_drawdown", "undefined_objective"}
    assert reading["active_rules"] == ["positive_net", "drawdown", "exits", "objective"]
    basis = reading["basis"]
    assert basis["schema_version"] == "golden-search-chance-rehearsal/v1"
    assert basis["thresholds"] == {"rehearsal_target": 0.80}
    assert basis["rules"]["drawdown_basis"] == "session closes"
    assert set(basis["seeds"]) == {"1x", "2x", "3x", "4x"}
    assert basis["seeds"]["1x"]["inputs"][-1] == 1
    assert len(reading["objective_percentiles_1x"]["p"]) == 99
    assert reading["objective_percentiles_1x"]["finite"] > 0


def test_the_same_inputs_redraw_identically_and_cancellation_propagates() -> None:
    from app.jobs.progress import JobCancelled

    first = _reading(_smooth(60, 3), _smooth(60, 4))
    second = _reading(_smooth(60, 3), _smooth(60, 4))
    assert first == second

    def cancelled() -> None:
        raise JobCancelled("job 7 cancelled")

    with pytest.raises(JobCancelled):
        _reading(_smooth(60, 3), _smooth(60, 4), cancel_check=cancelled)


def test_switching_positivity_off_removes_the_rule_entirely() -> None:
    reading = _reading(_smooth(60, 5), _smooth(60, 6), require_positive_net=False)
    assert "positive_net" not in reading["active_rules"]
    assert all("pass_positive_net" not in rung["passes"] and "undefined_positive_net" not in rung.get("undefined", {}) for rung in reading["rungs"])


def test_correlated_failures_make_the_joint_not_the_product_of_the_marginals() -> None:
    # One catastrophic session decides the net rule and the drawdown rule on exactly the same paths (0.7 x 1.01^20
    # compounds below 1, and its close-to-peak fall passes 30% against a 20% ceiling); every session carries an exit
    # (the floor is met deterministically) and the incumbent loses half its equity a session (the objective rule
    # passes deterministically), so the joint is the share of paths avoiding that session — and the product of the
    # marginals, which double-counts the shared failure, is its square.
    catastrophic = [0.01] * 60  # a deterministic 1% drift: only the catastrophic session can fail either rule
    catastrophic[30] = -0.30
    reading = _reading(catastrophic, [-0.50] * 60, objective="total_return_pct", ceiling=0.20, exits=tuple(range(60)))
    first = reading["rungs"][0]
    joint = first["passes"]["pass_joint"] / first["denominator"]
    net = first["passes"]["pass_positive_net"] / first["denominator"]
    drawdown = first["passes"]["pass_drawdown"] / first["denominator"]
    assert first["passes"]["pass_exits"] == first["denominator"]
    assert first["passes"]["pass_objective"] == first["denominator"]
    assert joint == pytest.approx(net, abs=0.001)
    assert joint == pytest.approx(drawdown, abs=0.001)
    assert abs(joint - net * drawdown) > 0.05


def test_a_ladder_may_fall_as_it_lengthens_and_the_guidance_says_so() -> None:
    catastrophic = _smooth(60, 9)
    catastrophic[30] = -0.30
    reading = _reading(catastrophic, _smooth(60, 10), ceiling=0.20, exits=tuple(range(0, 60, 2)))
    joint_shares = [rung["passes"]["pass_joint"] / rung["denominator"] for rung in reading["rungs"]]
    assert joint_shares[-1] < joint_shares[0], "longer rungs almost surely draw the catastrophic session"
    assert "does not rise with length" in reading["guidance"]
    assert "too short" not in reading["guidance"]
    assert "80% target is the owner's reading heuristic" in reading["guidance"]


def test_unreachable_activity_limits_the_ladder_and_is_named() -> None:
    floors, _, _, final_window = _legacy_world(exam_min_trades=10_000)
    reading = _reading(_smooth(60, 11), _smooth(60, 12), floors=floors, final_window=final_window, exits=(0, 1, 2))
    assert all(rung["passes"]["pass_exits"] == 0 for rung in reading["rungs"])
    assert "trade-count rule" in reading["guidance"]
    assert "No rung reaches" in reading["guidance"]


def test_undefined_objectives_count_as_undefined_never_as_passing() -> None:
    # An exactly-flat run leaves Sharpe undefined on every path ("every return equal" is tested directly).
    flat = [0.0] * 60
    reading = _reading(flat, _smooth(60, 13), objective="sharpe_ratio", exits=(0, 1, 2))
    first = reading["rungs"][0]
    assert first["passes"]["pass_objective"] == 0
    assert first["undefined"]["undefined_objective"] == 10_000
    assert first["passes"]["pass_joint"] == 0
    # The sentence says the comparison could not be computed on any path, not that a measured 0% of paths passed it.
    assert "undefined for the objective comparison with the current settings (10,000 of 10,000)" in reading["guidance"]
    # A ladder whose every rule was defined on every path says nothing of the kind.
    assert "could not be computed" not in _reading(_smooth(60, 14), _smooth(60, 15), exits=(0, 1, 2))["guidance"]


def test_the_drawdown_rule_reads_session_closes_a_per_bar_breach_is_not_its_concern() -> None:
    # A single -25% session recovered by the close of the next: the close-to-close fall is ~25%, within a 30% ceiling,
    # though the engine's per-bar gate could see deeper inside the session. The rehearsal passes it and says its basis.
    dip = _smooth(60, 14)
    dip[30] = -0.25
    dip[31] = 0.25
    reading = _reading(dip, _smooth(60, 15), ceiling=0.30, exits=(0, 1, 2))
    assert reading["basis"]["rules"]["drawdown_basis"] == "session closes"
    assert any(rung["passes"]["pass_drawdown"] > 0 for rung in reading["rungs"])


# ── Missing states and stored normalization ───────────────────────────────


def test_same_settings_missing_incumbent_and_legacy_records() -> None:
    same = _reading(_smooth(60, 16), _smooth(60, 17), hashes=("twin", "twin"))
    assert same["status"] == "missing"
    assert same["code"] == "same_settings"

    window = _window_for(60)
    no_incumbent = rehearsal_reading(
        objective="total_return_pct",
        window=window,
        final_window=(et_midnight_ms(date(2025, 4, 1)), et_midnight_ms(date(2025, 5, 1))),
        floors=_legacy_world()[0],
        rate=None,
        final_years=None,
        ceiling=0.2,
        require_positive_net=True,
        candidate_metrics=_completed(),
        candidate_detail=_detail(_smooth(60, 18)),
        candidate_hash="cafebabe",
        incumbent_metrics=None,
        incumbent_detail=None,
        incumbent_hash="deadbeef",
    )
    assert no_incumbent["code"] == "incumbent_unavailable"

    legacy = stored_rehearsal({"chance": {"rehearsal": None}})
    assert legacy["code"] == NOT_MEASURED[0]
    unsupported = stored_rehearsal({"chance": {"rehearsal": {"status": "from_the_future"}}})
    assert unsupported["code"] == "unsupported"
    measured = _reading(_smooth(60, 19), _smooth(60, 20))
    partial = stored_rehearsal({"chance": {"rehearsal": copy.deepcopy(measured)}})
    assert partial == measured
    # A measured record that fails the served contract reads unavailable instead of failing the study page.
    torn = copy.deepcopy(measured)
    del torn["rungs"]
    assert stored_rehearsal({"chance": {"rehearsal": torn}})["code"] == "invalid_record"
