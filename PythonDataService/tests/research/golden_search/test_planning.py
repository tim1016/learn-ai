"""Plan-time rules: reading a requested plan, the common run-up, preflight refusals as data, and default intervals (#2696)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.data_lake.adjustment_versions import companion_path
from app.data_lake.types import trading_date_to_calendar_anchor_ms
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search.budget import estimate
from app.research.golden_search.budget import review_protocol as review
from app.research.golden_search.declarations import declaration_for
from app.research.golden_search.models import GoldenSearchRefusal
from app.research.golden_search.planning import (
    UNKNOWN_HISTORY_MONTHS,
    FinalInterval,
    RangeFit,
    StudyRange,
    _sessions_needed,
    default_final_interval,
    default_protocol,
    fit_range,
    preflight_view,
    prepare_lock,
    protocol_from_request,
    range_fit_sentence,
    registry_incumbent,
    review_plan,
    slowest_requirement,
)
from app.research.golden_search.protocol import (
    BackfillRemedy,
    GoldenSearchProtocol,
    IncumbentRef,
    KnobPlan,
    by_importance,
)
from app.research.grid_search import service as grid_service
from app.research.sweep.warmup import probe_warmup_samples
from app.schemas.golden_search import GoldenSearchPreflight
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from tests._helpers.golden_search_study import (
    DEVELOPMENT,
    LAKE_FROM,
    frequency_plan_request,
    plan_request,
    seed_lake,
    unique_symbol,
)

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


# ── Importance (ADR 0074 decision 10) ────────────────────────────────────


def _ranked(request: dict[str, object], scores: dict[str, int | None]) -> dict[str, object]:
    knobs = [{**knob, "importance": scores.get(knob["name"], 5)} for knob in request["knobs"]]  # type: ignore[attr-defined]
    return {**request, "knobs": knobs}


def test_a_new_plan_starts_every_knob_at_the_middle_importance_in_the_declared_order() -> None:
    plan = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=None)
    assert [knob.importance for knob in plan.knobs] == [5] * len(_ema().knobs)
    assert [knob.name for knob in plan.knobs] == [knob.name for knob in _ema().knobs]


def test_importance_orders_the_frozen_search_highest_first_with_ties_in_the_declared_order() -> None:
    request = plan_request("SPY")
    shuffled = {**request, "knobs": list(reversed(request["knobs"]))}
    protocol = protocol_from_request(_ranked(shuffled, {"hold_bars": 9, "rsi_min": 7, "gap": 7}))
    assert [knob.name for knob in protocol.knobs] == ["hold_bars", "gap", "rsi_min", "rsi_max", "fast_period", "slow_period", "gap_bps"]
    assert [knob.name for knob in protocol.search_knobs] == ["hold_bars", "gap"]
    # The scores are part of the frozen plan.
    assert protocol.protocol_hash() != protocol_from_request(_ranked(request, {})).protocol_hash()


ORDER_FIXTURE = Path(__file__).resolve().parents[4] / "contracts" / "fixtures" / "golden-search-importance-order-v1.json"


def test_the_search_order_matches_the_order_the_plan_page_shows() -> None:
    fixture = json.loads(ORDER_FIXTURE.read_text())
    assert fixture["declared"] == [knob.name for knob in _ema().knobs]
    for case in fixture["cases"]:
        knobs = [KnobPlan(name=name, mode="fixed", low=0.0, high=0.0, fixed_value=0.0, importance=case["scores"][name]) for name in reversed(fixture["declared"])]
        assert [knob.name for knob in by_importance(knobs, fixture["declared"])] == case["order"]


def test_a_plan_without_importance_keeps_its_own_order_and_writes_no_score() -> None:
    request = plan_request("SPY")
    shuffled = {**request, "knobs": list(reversed(request["knobs"]))}
    protocol = protocol_from_request(shuffled)
    assert [knob.name for knob in protocol.knobs] == [knob["name"] for knob in shuffled["knobs"]]
    assert all("importance" not in knob for knob in protocol.as_dict()["knobs"])


@pytest.mark.parametrize("bad", [0, 11, True, None])
def test_every_knob_needs_an_importance_on_the_scale_once_any_has_one(bad: int | None) -> None:
    protocol = protocol_from_request(_ranked(plan_request("SPY"), {"rsi_max": bad}))
    refusals = {(item.code, item.field) for item in review(protocol, _ema()).refusals}
    assert refusals == {("IMPORTANCE_INVALID", "knobs.rsi_max.importance")}


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


@pytest.mark.parametrize(
    ("lake_from", "servable"),
    [
        # 2024-09-01 keeps whole folds too, but it starts before the lake does.
        (date(2024, 9, 4), date(2024, 10, 1)),
        # 2024-10-01 starts inside the lake, but its run-up would reach before the lake's first session.
        (date(2024, 9, 30), date(2024, 11, 1)),
    ],
)
def test_a_development_start_inside_a_hole_in_the_lake_is_refused_naming_the_earliest_start_it_can_serve(
    tmp_path: Path, lake_from: date, servable: date
) -> None:
    symbol = unique_symbol()
    # Development is asked from 2024-06-01, ten whole months before its end and months before the lake holds the symbol.
    seed_lake(tmp_path, symbol, start=lake_from)
    asked = protocol_from_request(plan_request(symbol, development_start_ms=et_midnight_ms(date(2024, 6, 1))))

    [refusal] = review_plan(asked, roots=[tmp_path]).refusals

    assert (refusal.code, refusal.field) == ("RUN_UP_HISTORY_MISSING", "development_start_ms")
    assert refusal.nearest_valid_ms == (et_midnight_ms(servable),)
    assert refusal.message.endswith(f" The earliest start the lake can serve with whole folds is {servable.isoformat()}.")
    moved = review_plan(replace(asked, development_start_ms=refusal.nearest_valid_ms[0]), roots=[tmp_path])
    assert moved.refusals == () and moved.run_up is not None


def test_a_run_up_refusal_names_no_start_when_no_later_one_keeps_a_fold(tmp_path: Path) -> None:
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, start=DEVELOPMENT[0])
    # Two months of training and one of test fill the three development months: one fold, none to drop.
    plan = review_plan(protocol_from_request(plan_request(symbol, training_months=2)), roots=[tmp_path])

    [refusal] = plan.refusals

    assert refusal.code == "RUN_UP_HISTORY_MISSING" and refusal.nearest_valid_ms == ()
    assert refusal.message.endswith("the run-up is never carved from the scored range.")


def test_a_run_up_the_lake_holds_but_cannot_read_is_refused_as_unreadable_not_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History on disk without its adjustment record is not history the lake lacks: a backfill rebuilds it (#2887)."""
    # The remedy depends on what the provider still serves, so the day is pinned.
    monkeypatch.setattr(grid_service, "now_ms_utc", lambda: et_midnight_ms(date(2025, 6, 2)))
    root = tmp_path / "polygon_split_adjusted"
    symbol = unique_symbol()
    seed_lake(root, symbol)
    for day in expected_sessions(LAKE_FROM, DEVELOPMENT[0] - timedelta(days=1)):
        companion_path(root / "equity" / "usa" / "minute" / symbol.lower() / f"{day:%Y%m%d}_trade.zip").unlink()

    [refusal] = review_plan(protocol_from_request(plan_request(symbol)), roots=[root]).refusals

    assert (refusal.code, refusal.field) == ("DATA_UNREADABLE", "development_start_ms")
    assert "is on disk but cannot be read" in refusal.message and "does not hold them" not in refusal.message
    assert f"backfill {symbol} in polygon_split_adjusted mode to rebuild the unreadable files" in refusal.message
    # The readable sessions start with development, so a start one fold later has its run-up.
    assert refusal.nearest_valid_ms == (et_midnight_ms(date(2025, 2, 1)),)
    assert refusal.message.endswith(" The earliest start the lake can serve with whole folds is 2025-02-01.")


def test_missing_sessions_are_a_refusal_in_the_preflight_not_an_error(tmp_path: Path) -> None:
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 4, 20))
    plan = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path])

    view = preflight_view(plan, None)
    assert [refusal["code"] for refusal in view["refusals"]] == ["DATA_MISSING"]
    assert view["run_up"] is not None and view["estimate"]["budget_cap"] == 5000


def _pin_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    """What a backfill can fetch depends on the provider's history and the day, so the day is pinned."""
    monkeypatch.setattr(grid_service, "now_ms_utc", lambda: et_midnight_ms(today) + 12 * 3_600_000)


def test_a_lake_that_stops_before_the_final_test_does_names_its_backfill_and_the_end_it_can_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, date(2025, 6, 2))
    symbol = unique_symbol()
    # 2025-04-17 is the last session on or before the 20th: the 18th was Good Friday.
    seed_lake(tmp_path, symbol, end=date(2025, 4, 20))
    asked = protocol_from_request(plan_request(symbol))

    plan = review_plan(asked, roots=[tmp_path])

    [refusal] = plan.refusals
    assert refusal.code == "DATA_MISSING"
    assert refusal.backfill == BackfillRemedy(
        symbol=symbol,
        price_adjustment_mode="polygon_split_adjusted",
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2025, 4, 21)),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2025, 4, 30)),
    )
    assert (refusal.field, refusal.nearest_valid_ms) == ("final_end_ms", (et_midnight_ms(date(2025, 4, 18)),))
    [sent] = GoldenSearchPreflight.model_validate(preflight_view(plan, None)).refusals
    assert sent.backfill is not None and sent.backfill.model_dump() == refusal.backfill.as_dict()
    assert review_plan(replace(asked, final_end_ms=refusal.nearest_valid_ms[0]), roots=[tmp_path]).refusals == ()


def test_a_hole_inside_the_plan_names_its_backfill_and_the_later_start_past_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_today(monkeypatch, date(2025, 6, 2))
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 1, 9))
    seed_lake(tmp_path, symbol, start=date(2025, 1, 15))
    asked = protocol_from_request(plan_request(symbol))

    [refusal] = review_plan(asked, roots=[tmp_path]).refusals

    assert refusal.code == "DATA_MISSING" and refusal.backfill is not None
    assert (refusal.backfill.start_trading_date_ms, refusal.backfill.end_trading_date_ms) == (
        trading_date_to_calendar_anchor_ms(date(2025, 1, 10)),
        trading_date_to_calendar_anchor_ms(date(2025, 1, 14)),
    )
    # One fold later: its run-up starts after the hole.
    assert (refusal.field, refusal.nearest_valid_ms) == ("development_start_ms", (et_midnight_ms(date(2025, 2, 1)),))
    assert review_plan(replace(asked, development_start_ms=refusal.nearest_valid_ms[0]), roots=[tmp_path]).refusals == ()


@pytest.mark.parametrize(
    ("today", "window"),
    [
        # The market has not reached the sessions after the 24th.
        (date(2025, 4, 24), (date(2025, 4, 21), date(2025, 4, 24))),
        # Five years on, the provider serves none of them.
        (date(2031, 1, 2), None),
    ],
)
def test_a_backfill_is_named_only_for_missing_sessions_it_can_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, today: date, window: tuple[date, date] | None
) -> None:
    _pin_today(monkeypatch, today)
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 4, 20))

    [refusal] = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path]).refusals

    named = None if refusal.backfill is None else (refusal.backfill.start_trading_date_ms, refusal.backfill.end_trading_date_ms)
    assert named == (None if window is None else tuple(trading_date_to_calendar_anchor_ms(day) for day in window))
    # The end the lake serves does not depend on what a backfill could fetch.
    assert refusal.field == "final_end_ms"


@pytest.mark.parametrize("tail_held", [True, False])
def test_a_hole_in_the_final_test_names_the_end_before_it_whatever_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail_held: bool
) -> None:
    """Ending after the lake's last session would leave an earlier hole in the plan; only the end before the first hole is accepted."""
    _pin_today(monkeypatch, date(2025, 6, 2))
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 4, 8))
    seed_lake(tmp_path, symbol, start=date(2025, 4, 11), end=date(2025, 4, 30 if tail_held else 17))
    asked = protocol_from_request(plan_request(symbol))

    [refusal] = review_plan(asked, roots=[tmp_path]).refusals

    assert refusal.code == "DATA_MISSING" and refusal.backfill is not None
    assert (refusal.backfill.start_trading_date_ms, refusal.backfill.end_trading_date_ms) == (
        trading_date_to_calendar_anchor_ms(date(2025, 4, 9)),
        trading_date_to_calendar_anchor_ms(date(2025, 4, 10) if tail_held else date(2025, 4, 30)),
    )
    assert (refusal.field, refusal.nearest_valid_ms) == ("final_end_ms", (et_midnight_ms(date(2025, 4, 9)),))
    assert review_plan(replace(asked, final_end_ms=refusal.nearest_valid_ms[0]), roots=[tmp_path]).refusals == ()


def test_a_hole_in_development_and_a_lake_that_stops_early_name_no_date_that_serves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_today(monkeypatch, date(2025, 6, 2))
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 1, 9))
    seed_lake(tmp_path, symbol, start=date(2025, 1, 15), end=date(2025, 4, 17))

    [refusal] = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path]).refusals

    # No one date passes both gaps; the backfill's one window reaches from the first to the last missing session.
    assert (refusal.code, refusal.field, refusal.nearest_valid_ms) == ("DATA_MISSING", None, ())
    assert refusal.backfill is not None and (refusal.backfill.start_trading_date_ms, refusal.backfill.end_trading_date_ms) == (
        trading_date_to_calendar_anchor_ms(date(2025, 1, 10)),
        trading_date_to_calendar_anchor_ms(date(2025, 4, 30)),
    )


def test_a_lake_that_stops_before_the_final_test_starts_names_no_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_today(monkeypatch, date(2025, 6, 2))
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol, end=date(2025, 3, 20))

    [refusal] = review_plan(protocol_from_request(plan_request(symbol)), roots=[tmp_path]).refusals

    assert (refusal.code, refusal.field, refusal.nearest_valid_ms) == ("DATA_MISSING", None, ())
    assert refusal.backfill is not None


def test_a_strategy_without_a_declaration_is_named_unavailable() -> None:
    plan = review_plan(protocol_from_request(plan_request("SPY", strategy_key="sma_crossover")), roots=[])
    assert [refusal.code for refusal in plan.refusals] == ["STRATEGY_UNAVAILABLE"]
    assert preflight_view(plan, None)["estimate"] is None


def test_a_seed_equal_to_the_qualified_tuple_is_refused_and_the_registry_point_is_not(tmp_path: Path) -> None:
    # The qualified tuple was chosen on data the folds test, so it may be the benchmark but never where they start (ADR 0074).
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol)
    registry = registry_incumbent(EMA, symbol).params
    qualified = {**registry, "gap": 0.35}
    request = plan_request(symbol, incumbent={"source": "qualification", "qualification_id": "gq-1", "params": qualified})

    leaked = review_plan(protocol_from_request({**request, "seed": qualified}), roots=[tmp_path])
    assert [(refusal.code, refusal.field) for refusal in leaked.refusals] == [("SEED_IS_QUALIFIED", "seed")]

    assert review_plan(protocol_from_request({**request, "seed": registry}), roots=[tmp_path]).refusals == ()
    # A plan that omits its seed starts at the registry point, never at the qualified tuple.
    omitted = protocol_from_request({**request, "seed": None})
    assert omitted.seed == registry and review_plan(omitted, roots=[tmp_path]).refusals == ()
    # A qualified tuple that is the registry point itself leaks nothing the registry did not already fix.
    same_point = plan_request(symbol, incumbent={"source": "qualification", "qualification_id": "gq-2", "params": registry})
    assert review_plan(protocol_from_request(same_point), roots=[tmp_path]).refusals == ()


# ── Default intervals ────────────────────────────────────────────────────


def test_new_plans_default_to_50_per_year_and_lock_the_user_frequency_with_the_preview(tmp_path: Path) -> None:
    defaults = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=None)
    assert (defaults.expected_trades_per_year, defaults.policy.min_trades, defaults.exam_min_trades) == (50, None, None)
    assert not review(defaults, _ema()).refusals
    symbol = unique_symbol()
    seed_lake(tmp_path, symbol)
    protocol = protocol_from_request(frequency_plan_request(symbol, 100))
    preview = preflight_view(review_plan(protocol, roots=[tmp_path]), None)["activity"]
    locked = prepare_lock(protocol, idempotency_key="frequency-policy", roots=[tmp_path])
    assert locked.receipt["activity"] == preview
    assert preview["expected_trades_per_year"] == 100
    assert preview["windows"][0]["minimum_trades"] == 24


def test_frequency_plan_refuses_a_final_window_without_trading_days() -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    boundary = et_midnight_ms(date(2024, 6, 1))
    protocol = replace(protocol, development_start_ms=et_midnight_ms(date(2024, 1, 1)), development_end_ms=boundary, final_start_ms=boundary, final_end_ms=et_midnight_ms(date(2024, 6, 3)))
    plan = review_plan(protocol, roots=[])
    assert not plan.lockable
    assert [(item.code, item.field) for item in plan.refusals] == [("NO_TRADING_DAYS", "final_end_ms")]


def _dates(protocol: GoldenSearchProtocol) -> tuple[date, date, date, date]:
    return tuple(et_date_at_ms(ms) for ms in (protocol.development_start_ms, protocol.development_end_ms, protocol.final_start_ms, protocol.final_end_ms))  # type: ignore[return-value]


def _intervals(**kwargs: object) -> tuple[date, date, date, date]:
    return _dates(default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, **kwargs))  # type: ignore[arg-type]


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


# Two years ending on Friday 2026-10-02, as the picker's 2Y hands it over: ``[start, end)``.
TWO_YEARS = StudyRange(date(2024, 10, 3), date(2026, 10, 3))


def _fit(study_range: StudyRange = TWO_YEARS, *, earliest_session: date | None = None, final: int = 3, training: int = 6, test: int = 2):
    return fit_range(study_range, final_months=final, training_months=training, test_months=test, earliest_session=earliest_session)


def _note(fit: RangeFit, *, final: int = 3, training: int = 6, test: int = 2) -> str | None:
    return range_fit_sentence(fit, final_months=final, training_months=training, test_months=test)


def test_a_picked_range_ends_the_final_test_with_the_range_and_starts_development_on_its_first_whole_fold() -> None:
    fit = _fit()

    # 21 months lie before the final test; 6 of training and seven 2-month tests take 20 of them.
    assert (fit.development_start, fit.final_start, fit.final_end) == (date(2024, 11, 3), date(2026, 7, 3), date(2026, 10, 3))
    assert (len(fit.unused_sessions), fit.unused_sessions[0], fit.unused_sessions[-1]) == (22, date(2024, 10, 3), date(2024, 11, 1))
    assert fit.lake_from is None and fit.final_early_days == 0
    assert _note(fit) == (
        "Development starts on 2024-11-03. The 22 sessions from 2024-10-03 to 2024-11-01 are left out: "
        "the months before the final test have to split into 6 months of training and whole tests of 2 months."
    )


def test_a_range_that_splits_evenly_is_used_whole_and_says_nothing() -> None:
    fit = _fit(StudyRange(date(2024, 11, 3), date(2026, 10, 3)))

    assert fit.development_start == date(2024, 11, 3) and fit.unused_sessions == ()
    assert _note(fit) is None
    # A range starting on a weekend leaves no session out either: 2024-11-03 is a Sunday.
    one_month_tests = _fit(test=1)
    assert one_month_tests.development_start == date(2024, 10, 3) and one_month_tests.unused_sessions == ()


def test_a_range_from_the_lakes_first_session_keeps_its_first_month_for_the_run_up() -> None:
    # The lake starts on 2024-10-03, so December is the first month with a whole month behind it.
    fit = _fit(earliest_session=date(2024, 10, 3))

    assert fit.development_start == date(2025, 1, 3) and fit.lake_from == date(2024, 10, 3)
    assert _note(fit) == (
        "Development starts on 2025-01-03. The 63 sessions from 2024-10-03 to 2025-01-02 are left out: "
        "the lake's unbroken history starts on 2024-10-03, its first month is kept for the run-up, "
        "and the rest has to split into 6 months of training and whole tests of 2 months."
    )
    # A range that starts after that month is not held back.
    assert _fit(earliest_session=date(2023, 1, 3)).lake_from is None


def test_one_session_left_out_is_named_as_one() -> None:
    # 2024-11-01 is a Friday; development starts on Sunday the 3rd.
    fit = _fit(StudyRange(date(2024, 11, 1), date(2026, 10, 3)), test=1, training=1)

    assert fit.development_start == date(2024, 11, 3)
    assert _note(fit, training=1, test=1) == (
        "Development starts on 2024-11-03. The session on 2024-11-01 is left out: "
        "the months before the final test have to split into 1 month of training and whole tests of 1 month."
    )


def test_a_range_too_short_for_one_fold_is_refused_with_what_the_plan_needs() -> None:
    with pytest.raises(GoldenSearchRefusal) as short:
        _fit(StudyRange(date(2026, 1, 3), date(2026, 10, 3)))

    assert (short.value.code, short.value.field) == ("INTERVALS_INVALID", "range_start_ms")
    assert str(short.value) == (
        "The range is too short: 6 months of training, one test of 2 months and a final test of 3 months "
        "need development to start by 2025-11-03, and the range starts on 2026-01-03."
    )


def test_a_range_too_short_by_itself_is_told_so_whatever_the_lake_holds() -> None:
    # The lake's history is short too, but no backfill would make nine months hold eleven.
    with pytest.raises(GoldenSearchRefusal) as short:
        _fit(StudyRange(date(2026, 1, 3), date(2026, 10, 3)), earliest_session=date(2025, 12, 15))

    assert short.value.field == "range_start_ms"
    assert str(short.value).endswith("need development to start by 2025-11-03, and the range starts on 2026-01-03.")


def test_a_range_the_lake_is_too_young_for_is_refused_naming_the_lake_not_the_range() -> None:
    # The range is eleven months long; the lake's unbroken history starts two months before it ends.
    with pytest.raises(GoldenSearchRefusal) as young:
        _fit(StudyRange(date(2025, 11, 3), date(2026, 10, 3)), earliest_session=date(2026, 1, 5))

    assert (young.value.code, young.value.field) == ("INTERVALS_INVALID", None)
    assert str(young.value) == (
        "The lake's unbroken history starts on 2026-01-05, and its first month is kept for the run-up, so development "
        "cannot start before 2026-03-01. 6 months of training, one test of 2 months and a final test of 3 months that "
        "end with this range need it to start by 2025-11-03."
    )


def test_a_range_ending_at_a_months_end_starts_the_final_test_where_developments_whole_months_end() -> None:
    # The final test's three months start on 2026-07-31. Twenty months before is 2024-11-30, and twenty on from there is 07-30.
    fit = _fit(StudyRange(date(2024, 10, 31), date(2026, 10, 31)))
    protocol = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=None, fit=fit)

    assert (fit.development_start, fit.final_start, fit.final_end, fit.final_early_days) == (date(2024, 11, 30), date(2026, 7, 30), date(2026, 10, 31), 1)
    assert _dates(protocol) == (date(2024, 11, 30), date(2026, 7, 30), date(2026, 7, 30), date(2026, 10, 31))
    assert review(protocol, _ema()).refusals == () and len(review(protocol, _ema()).folds) == 7
    assert _note(fit) == (
        "Development starts on 2024-11-30. The 21 sessions from 2024-10-31 to 2024-11-29 are left out: "
        "the months before the final test have to split into 6 months of training and whole tests of 2 months. "
        "The final test starts on 2026-07-30, 1 day before its 3 months: development's whole months end there."
    )


@pytest.mark.parametrize(
    ("study_range", "development_start", "final_start"),
    [
        # One year ending on Friday 2026-10-30: eleven months are asked and twelve are there.
        (StudyRange(date(2025, 10, 31), date(2026, 10, 31)), date(2025, 11, 30), date(2026, 7, 30)),
        # A final test whose months start on the 31st of May: eight months before is February's last day.
        (StudyRange(date(2025, 9, 1), date(2026, 8, 31)), date(2025, 9, 30), date(2026, 5, 30)),
        (StudyRange(date(2024, 12, 1), date(2025, 12, 31)), date(2025, 1, 30), date(2025, 9, 30)),
    ],
)
def test_a_range_with_room_for_the_months_asked_is_laid_out_whatever_day_it_ends_on(
    study_range: StudyRange, development_start: date, final_start: date
) -> None:
    fit = _fit(study_range)
    protocol = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=None, fit=fit)

    assert (fit.development_start, fit.final_start) == (development_start, final_start)
    assert review(protocol, _ema()).refusals == ()


def test_the_start_a_too_short_range_is_told_it_needs_is_one_the_layout_takes() -> None:
    with pytest.raises(GoldenSearchRefusal, match="need development to start by 2025-11-30, and the range starts on 2026-01-31"):
        _fit(StudyRange(date(2026, 1, 31), date(2026, 10, 31)))

    assert _fit(StudyRange(date(2025, 11, 30), date(2026, 10, 31))).development_start == date(2025, 11, 30)


def test_a_picked_range_is_laid_out_whole_whatever_the_run_cap_admits() -> None:
    # The default layout cuts a long history to the folds the cap admits; a range the owner picked is theirs.
    long_range = StudyRange(date(2010, 10, 3), date(2026, 10, 3))
    protocol = default_protocol(EMA, "SPY", registry_incumbent(EMA, "SPY"), now_ms=OCTOBER_15, earliest_session=None, fit=_fit(long_range))

    assert _dates(protocol) == (date(2010, 11, 3), date(2026, 7, 3), date(2026, 7, 3), date(2026, 10, 3))
    assert estimate(protocol, folds=len(review(protocol, _ema()).folds)).total_max > protocol.budget_cap


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


@pytest.mark.parametrize("params", [{"gap": "wide"}, {"gap": None}, {"gap": True}, {"gap": float("nan")}, {"bogus": 1}])
def test_an_incumbent_that_is_not_numbers_for_declared_knobs_is_refused_not_raised(params: dict[str, object]) -> None:
    request = plan_request("SPY")
    request["seed"] = dict(request["incumbent"]["params"])  # a clean seed, so only the incumbent is wrong
    request["incumbent"]["params"] = {**request["incumbent"]["params"], **params}

    plan = review_plan(protocol_from_request(request), roots=[])

    assert [refusal.code for refusal in plan.refusals] == ["INCUMBENT_INVALID"]
    assert (plan.refusals[0].field or "").startswith("incumbent.params")


def test_a_lock_refused_for_several_reasons_carries_every_refusal(tmp_path: Path) -> None:
    request = plan_request("SPY", budget_cap=10)
    request["zoom"]["points"] = 2
    with pytest.raises(GoldenSearchRefusal) as refused:
        prepare_lock(protocol_from_request(request), idempotency_key="k", roots=[tmp_path])
    assert refused.value.code == "ZOOM_SETTINGS_INVALID"
    assert [item["code"] for item in refused.value.refusals] == ["ZOOM_SETTINGS_INVALID", "WORKLOAD_LIMIT"]
