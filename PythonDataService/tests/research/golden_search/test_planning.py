"""Plan-time rules: reading a requested plan, the common run-up, preflight refusals as data, and default intervals (#2696)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.research.golden_search.budget import estimate
from app.research.golden_search.budget import review_protocol as review
from app.research.golden_search.declarations import declaration_for
from app.research.golden_search.models import GoldenSearchRefusal
from app.research.golden_search.planning import (
    UNKNOWN_HISTORY_MONTHS,
    FinalInterval,
    _sessions_needed,
    default_final_interval,
    default_protocol,
    preflight_view,
    prepare_lock,
    protocol_from_request,
    registry_incumbent,
    review_plan,
    slowest_requirement,
)
from app.research.golden_search.protocol import IncumbentRef, KnobPlan
from app.research.sweep.warmup import probe_warmup_samples
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests._helpers.golden_search_study import DEVELOPMENT, plan_request, seed_lake, unique_symbol

EMA = "ema_crossover_signal"
OCTOBER_15 = et_midnight_ms(date(2026, 10, 15)) + 15 * 3_600_000


def _ema():
    declaration = declaration_for(EMA)
    assert declaration is not None
    return declaration


# ── Reading a requested plan ─────────────────────────────────────────────


def test_a_request_without_a_seed_starts_from_the_incumbent_in_canonical_form() -> None:
    request = plan_request("spy")
    request["incumbent"]["params"] = {"symbol": "spy", "gap": 0.30000000000000004, "fast_period": 5.0, "rsi_min": 50, "rsi_max": 70.0}

    protocol = protocol_from_request(request)

    canonical = {"symbol": "SPY", "gap": 0.3, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
    assert protocol.symbol == "SPY"
    assert protocol.incumbent.params == canonical and protocol.seed == canonical


def test_values_no_canonical_point_admits_are_kept_for_validation_to_name() -> None:
    request = plan_request("SPY", seed={"symbol": "SPY", "gap": 9.0})

    protocol = protocol_from_request(request)

    assert protocol.seed == {"symbol": "SPY", "gap": 9.0}
    assert "SEED_OUTSIDE_DOMAIN" in {refusal.code for refusal in review(protocol, _ema()).refusals}


def test_a_request_that_is_not_a_plan_is_refused_as_malformed() -> None:
    request = plan_request("SPY")
    del request["knobs"]
    with pytest.raises(GoldenSearchRefusal) as refused:
        protocol_from_request(request)
    assert refused.value.code == "PROTOCOL_MALFORMED"


# ── Run-up ───────────────────────────────────────────────────────────────


def test_the_slowest_requirement_reaches_one_neighbor_step_past_the_searched_range() -> None:
    protocol = protocol_from_request(plan_request("SPY"))
    knobs = tuple(
        KnobPlan(name="slow_period", mode="search", low=8.0, high=30.0, fixed_value=10.0, step=1.0) if plan.name == "slow_period" else plan
        for plan in protocol.knobs
    )

    probe, probed = slowest_requirement(replace(protocol, knobs=knobs), _ema())

    widest = probe_warmup_samples(EMA, {"symbol": "SPY", "slow_period": 31})
    assert (probe.required_samples, probe.bar_span_ms) == (widest.required_samples, widest.bar_span_ms)
    assert probed == 5  # seed 10 (= incumbent) and its neighbor 11, low 8, high 30, and 31 for the neighbor audit


def test_a_seed_above_the_searched_range_is_probed_one_neighbor_step_past_itself() -> None:
    protocol = protocol_from_request(plan_request("SPY", seed={"symbol": "SPY", "fast_period": 5, "slow_period": 35}))
    knobs = tuple(
        KnobPlan(name="slow_period", mode="search", low=8.0, high=30.0, fixed_value=10.0, step=1.0) if plan.name == "slow_period" else plan
        for plan in protocol.knobs
    )

    probe, _ = slowest_requirement(replace(protocol, knobs=knobs), _ema())

    # No move beats the seed, so 35 stays the winner and its neighbor audit runs slow 36.
    widest = probe_warmup_samples(EMA, {"symbol": "SPY", "slow_period": 36})
    assert (probe.required_samples, probe.bar_span_ms) == (widest.required_samples, widest.bar_span_ms)


def test_points_that_break_a_constraint_are_never_probed() -> None:
    protocol = protocol_from_request(plan_request("SPY"))
    knobs = tuple(
        KnobPlan(name="fast_period", mode="search", low=3.0, high=12.0, fixed_value=5.0, step=1.0) if plan.name == "fast_period" else plan
        for plan in protocol.knobs
    )

    probe, probed = slowest_requirement(replace(protocol, knobs=knobs), _ema())

    # fast 12 and 13 are not below the fixed slow length 10, so only fast 3, 5 (seed) and 6 (its neighbor) are probed.
    assert probed == 3
    assert probe.required_samples == probe_warmup_samples(EMA, {"symbol": "SPY"}).required_samples


def test_a_window_behind_an_early_close_needs_one_more_run_up_session() -> None:
    fifteen_minutes = 15 * 60 * 1000
    # 2024-11-29 closed at 13:00 ET: 14 decision bars instead of 26.
    assert _sessions_needed(et_midnight_ms(date(2024, 12, 2)), 20, fifteen_minutes) == 2
    assert _sessions_needed(et_midnight_ms(date(2024, 12, 9)), 20, fifteen_minutes) == 1


def test_a_run_up_the_lake_cannot_supply_before_development_is_refused_never_carved(tmp_path: Path) -> None:
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, start=DEVELOPMENT[0])
    plan = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path])
    assert [refusal.code for refusal in plan.refusals] == ["RUN_UP_HISTORY_MISSING"] and plan.run_up is None


def test_missing_sessions_are_a_refusal_in_the_preflight_not_an_error(tmp_path: Path) -> None:
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 4, 20))
    plan = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path])

    view = preflight_view(plan, None)
    assert [refusal["code"] for refusal in view["refusals"]] == ["DATA_MISSING"]
    assert view["run_up"] is not None and view["estimate"]["budget_cap"] == 5000


def test_a_strategy_without_a_declaration_is_named_unavailable() -> None:
    plan = review_plan(protocol_from_request(plan_request("SPY", strategy_key="sma_crossover")), roots=[])
    assert [refusal.code for refusal in plan.refusals] == ["STRATEGY_UNAVAILABLE"]
    assert preflight_view(plan, None)["estimate"] is None


# ── Default intervals ────────────────────────────────────────────────────


def _intervals(**kwargs: object) -> tuple[date, date, date, date]:
    protocol = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, **kwargs)  # type: ignore[arg-type]
    return tuple(et_date_at_ms(ms) for ms in (protocol.development_start_ms, protocol.development_end_ms, protocol.final_start_ms, protocol.final_end_ms))  # type: ignore[return-value]


def test_the_final_test_is_the_last_whole_months_before_the_current_month() -> None:
    assert _intervals(earliest_session=None)[2:] == (date(2026, 7, 1), date(2026, 10, 1))
    assert _intervals(earliest_session=None, final_months=1)[2:] == (date(2026, 9, 1), date(2026, 10, 1))
    first_of_month = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=et_midnight_ms(date(2026, 10, 1)), earliest_session=None)
    assert et_date_at_ms(first_of_month.final_end_ms) == date(2026, 10, 1)


def test_a_lake_a_few_sessions_behind_ends_the_final_test_after_its_last_session() -> None:
    # 2026-09-25 is a Friday: the final test ends with it rather than run into the three sessions the lake lacks.
    assert _intervals(earliest_session=None, latest_session=date(2026, 9, 25))[2:] == (date(2026, 7, 1), date(2026, 9, 26))
    assert default_final_interval(OCTOBER_15, 3, date(2026, 9, 25)).sessions_cut == 3
    # The development interval and its folds do not move with the cut.
    assert _intervals(earliest_session=None, latest_session=date(2026, 9, 25))[:2] == _intervals(earliest_session=None)[:2]


def test_a_lake_further_behind_keeps_the_whole_months_for_the_availability_check_to_refuse() -> None:
    # 2026-09-22 leaves six sessions (23rd–30th) unread: a backfill to name, not a shorter final test.
    assert default_final_interval(OCTOBER_15, 3, date(2026, 9, 22)) == FinalInterval(date(2026, 7, 1), date(2026, 10, 1), 0)
    assert default_final_interval(OCTOBER_15, 3, date(2026, 7, 1)).end == date(2026, 10, 1)
    # A lake stopping before the final months start, or reaching the boundary, keeps them too.
    assert default_final_interval(OCTOBER_15, 3, date(2026, 6, 30)).end == date(2026, 10, 1)
    assert default_final_interval(OCTOBER_15, 3, date(2026, 10, 2)) == FinalInterval(date(2026, 7, 1), date(2026, 10, 1), 0)


def test_a_month_ending_on_a_weekend_or_holiday_is_whole_when_the_lake_holds_its_last_session() -> None:
    # October 2026 ends on a Saturday; its last session is Friday the 30th.
    assert default_final_interval(et_midnight_ms(date(2026, 11, 15)), 3, date(2026, 10, 30)) == FinalInterval(date(2026, 8, 1), date(2026, 11, 1), 0)
    # 2027-05-31 is Memorial Day; May's last session is Friday the 28th.
    assert default_final_interval(et_midnight_ms(date(2027, 6, 10)), 3, date(2027, 5, 28)) == FinalInterval(date(2027, 3, 1), date(2027, 6, 1), 0)


def test_unknown_lake_history_proposes_a_two_year_development_interval() -> None:
    start, end, final_start, _ = _intervals(earliest_session=None)
    assert UNKNOWN_HISTORY_MONTHS == 24 and (start, end) == (date(2024, 7, 1), date(2026, 7, 1)) and end == final_start


def test_development_leaves_a_month_of_lake_history_for_the_run_up() -> None:
    # 2025-03-03 is March's first session: April is the first month with a whole month behind it.
    # 15 months then hold four 6+2 folds, so development is 14 months.
    assert _intervals(earliest_session=date(2025, 3, 3))[0] == date(2025, 5, 1)
    # From mid-March, April has only part of a month behind it.
    assert _intervals(earliest_session=date(2025, 3, 14))[0] == date(2025, 5, 1)
    assert _intervals(earliest_session=date(2025, 3, 14), training_months=1, test_months=1)[0] == date(2025, 5, 1)


def test_a_long_lake_history_is_cut_to_the_folds_the_budget_admits() -> None:
    protocol = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=date(2005, 1, 3))
    folds = len(review(protocol, _ema()).folds)

    assert estimate(protocol, folds=folds).total_max <= protocol.budget_cap < estimate(protocol, folds=folds + 1).total_max
    assert review(protocol, _ema()).lockable


def test_the_default_plan_searches_the_declared_ranges_from_the_incumbent() -> None:
    incumbent = registry_incumbent(EMA, "SPY")
    protocol = default_protocol(EMA, "SPY", incumbent, now_ms=OCTOBER_15, earliest_session=None)

    assert protocol.seed == incumbent.params == {"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}
    assert [plan.name for plan in protocol.search_knobs] == ["gap", "rsi_min", "rsi_max", "fast_period", "slow_period", "hold_bars"]
    assert protocol.pair_audits == (("fast_period", "slow_period"), ("rsi_min", "rsi_max"))
    assert review(protocol, _ema()).lockable


def test_a_qualified_incumbent_off_the_registry_on_a_fixed_knob_still_yields_a_lockable_default_plan() -> None:
    registry = registry_incumbent(EMA, "SPY")
    qualified = IncumbentRef(source="qualification", qualification_id="gq-1", params={**registry.params, "gap": 0.35, "gap_bps": 1.5})
    protocol = default_protocol(EMA, "SPY", qualified, now_ms=OCTOBER_15, earliest_session=None)

    # The folds start from the registry point, and the fixed knobs agree with that start.
    assert protocol.seed == registry.params
    assert next(plan for plan in protocol.knobs if plan.name == "gap_bps").fixed_value == 0.0
    assert review(protocol, _ema()).lockable


def test_a_default_range_widens_to_hold_the_seed_and_the_incumbent() -> None:
    registry = registry_incumbent(EMA, "SPY")
    # The declared fast range is 3–12 and slow 8–30; this qualified incumbent sits outside both.
    qualified = IncumbentRef(source="qualification", qualification_id="gq-2", params={**registry.params, "fast_period": 2, "slow_period": 34})
    protocol = default_protocol(EMA, "SPY", qualified, now_ms=OCTOBER_15, earliest_session=None)
    ranges = {plan.name: (plan.low, plan.high) for plan in protocol.knobs}

    assert ranges["fast_period"] == (2.0, 12.0) and ranges["slow_period"] == (8.0, 34.0)
    # Knobs whose start and benchmark already sit inside keep their declared range.
    assert ranges["gap"] == (0.0, 0.6) and ranges["hold_bars"] == (2.0, 12.0)
    assert review(protocol, _ema()).lockable


def test_a_widened_range_stays_inside_the_domain_on_the_quantum() -> None:
    # A qualification minted under an older declaration can sit off today's quantum or outside its domain.
    registry = registry_incumbent(EMA, "SPY")
    stale = IncumbentRef(
        source="qualification", qualification_id="gq-3", params={**registry.params, "gap": 2.5, "rsi_min": 29.6, "rsi_max": 90.4}
    )
    protocol = default_protocol(EMA, "SPY", stale, now_ms=OCTOBER_15, earliest_session=None)
    ranges = {plan.name: (plan.low, plan.high) for plan in protocol.knobs}

    assert ranges["gap"] == (0.0, 2.0)
    # Each end rounds outward, so the range still holds the benchmark: half-even would give 30 and 90.
    assert ranges["rsi_min"] == (29.0, 60.0) and ranges["rsi_max"] == (60.0, 91.0)


def test_the_preflight_counts_each_knobs_settings_exactly() -> None:
    request = plan_request("SPY")
    for knob in request["knobs"]:
        if knob["name"] == "gap":
            knob.update(mode="search", low=0.0, high=0.6, step=0.05)
        if knob["name"] == "hold_bars":
            knob.update(mode="fixed", fixed_value=5.0)
        if knob["name"] == "rsi_min":
            knob.update(mode="search", low=40.0, high=30.0, step=1.0)
    counts = {item["name"]: item["values"] for item in preflight_view(review_plan(protocol_from_request(request), roots=[]), None)["knob_values"]}

    # 0..0.6 at 0.05 is thirteen settings; float division would make it twelve.
    assert counts["gap"] == 13
    assert counts["hold_bars"] == 1
    # An empty range has no count; the refusal names it.
    assert counts["rsi_min"] is None
    assert list(counts) == [knob["name"] for knob in request["knobs"]]


def test_interval_lengths_below_one_month_are_refused() -> None:
    with pytest.raises(GoldenSearchRefusal) as refused:
        _intervals(earliest_session=None, final_months=0)
    assert refused.value.code == "INTERVALS_INVALID"


def test_a_lock_refused_for_several_reasons_carries_every_refusal(tmp_path: Path) -> None:
    request = plan_request("SPY", budget_cap=10)
    request["zoom"]["points"] = 2
    with pytest.raises(GoldenSearchRefusal) as refused:
        prepare_lock(protocol_from_request(request), idempotency_key="k", roots=[tmp_path])
    assert refused.value.code == "ZOOM_SETTINGS_INVALID"
    assert [item["code"] for item in refused.value.refusals] == ["ZOOM_SETTINGS_INVALID", "WORKLOAD_LIMIT"]
