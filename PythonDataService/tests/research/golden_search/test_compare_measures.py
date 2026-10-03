"""The Compare charts' measures (#2821): trades per trading year, return changes and the stress tally."""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from app.research.golden_search.activity import TradeFloors, activity_plan, trading_years
from app.research.golden_search.compare_measures import (
    neighborhood_view,
    return_change,
    stress_tally,
    stress_view,
    trades_per_year,
)
from app.research.golden_search.planning import protocol_from_request
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import metrics
from tests._helpers.golden_search_study import frequency_plan_request, plan_request


def _run(total_return: float | None = 0.10, *, net: float | None = 1_000.0, trades: int = 50, status: str = "completed") -> dict:
    return metrics(1.0, trades=trades, net=net, total_return=total_return, status=status).as_dict()


def _window(start: date, end: date) -> tuple[int, int]:
    return et_midnight_ms(start), et_midnight_ms(end)


@pytest.mark.parametrize(
    ("years", "trades", "expected"),
    [
        # One whole year of 251 sessions is one trading year.
        ([{"year": 2026, "selected_sessions": 251, "year_sessions": 251}], 125, 125.0),
        # 123 of 251 sessions: 60 trades x 251 / 123.
        ([{"year": 2026, "selected_sessions": 123, "year_sessions": 251}], 60, 122.4390243902439),
        # A window across two years sums each year's share: 1 / (2/250 + 2/251) = 62,750 / 1,002.
        ([{"year": 2025, "selected_sessions": 2, "year_sessions": 250}, {"year": 2026, "selected_sessions": 2, "year_sessions": 251}], 1, 62.62475049900200),
    ],
)
def test_trades_per_year_divides_by_the_windows_trading_years(years: list[dict[str, int]], trades: int, expected: float) -> None:
    assert trades_per_year(_run(trades=trades), trading_years(years)) == pytest.approx(expected, abs=1e-9, rel=0)


def test_a_failed_or_missing_run_has_no_trades_per_year() -> None:
    assert trades_per_year(_run(status="failed"), Fraction(1)) is None
    assert trades_per_year(None, Fraction(1)) is None
    with pytest.raises(ValueError, match="at least one trading session"):
        trades_per_year(_run(), Fraction(0))


def test_a_frequency_plan_reads_its_frozen_trading_years_and_a_fixed_floor_plan_the_calendar() -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    receipt = {"activity": activity_plan(protocol)}
    window = receipt["activity"]["windows"][0]
    span = (window["start_ms"], window["end_ms"])
    assert TradeFloors(protocol, receipt).trading_years(span) == Fraction(60, 250)
    # The receipt wins over the calendar: a frozen half year stays a half year.
    window["years"] = [{"year": 2025, "selected_sessions": 125, "year_sessions": 250}]
    assert TradeFloors(protocol, receipt).trading_years(span) == Fraction(1, 2)
    with pytest.raises(ValueError, match="no activity policy"):
        TradeFloors(protocol, receipt).trading_years(_window(date(2026, 1, 1), date(2026, 2, 1)))
    legacy = protocol_from_request(plan_request("SPY"))
    assert TradeFloors(legacy, {}).trading_years(_window(date(2026, 1, 1), date(2026, 7, 1))) == Fraction(123, 251)


def test_a_neighborhood_names_each_rows_step_and_its_change_from_the_candidate() -> None:
    hood = {
        "knob": "gap",
        "one_sided": False,
        "rows": [
            {"value": 0.25, "status": "tested", "metrics": _run(0.04), "reason": None},
            {"value": 0.2, "status": "center", "metrics": _run(0.10), "reason": None},
            {"value": 0.15, "status": "failed", "metrics": _run(None, status="failed"), "reason": None},
        ],
    }

    rows = neighborhood_view(hood)["rows"]

    # The step follows the value, not the stored order.
    assert [(row["step"], row["return_change"]) for row in rows] == [(1, pytest.approx(-0.06, abs=1e-12, rel=0)), (0, None), (-1, None)]
    assert rows[0]["metrics"] == hood["rows"][0]["metrics"]


def test_a_neighborhood_without_its_center_is_refused() -> None:
    with pytest.raises(ValueError, match="no center row"):
        neighborhood_view({"knob": "gap", "one_sided": True, "rows": [{"value": 0.15, "status": "tested", "metrics": _run(), "reason": None}]})


def test_a_stress_run_reads_its_change_from_the_unstressed_run() -> None:
    stressed = {"scenario": "slip", "label": "Slippage x2", "metrics": _run(0.07)}
    assert stress_view(stressed, _run(0.10))["return_change"] == pytest.approx(-0.03, abs=1e-12, rel=0)
    assert stress_view(stressed, None)["return_change"] is None
    assert stress_view({**stressed, "metrics": None}, _run(0.10))["return_change"] is None
    assert return_change(_run(0.07), _run(None)) is None


def test_the_tally_counts_only_completed_runs_that_made_money() -> None:
    results = [
        {"scenario": "a", "label": "A", "metrics": _run(net=250.0)},
        {"scenario": "b", "label": "B", "metrics": _run(net=0.0)},
        {"scenario": "c", "label": "C", "metrics": _run(net=-40.0)},
        {"scenario": "d", "label": "D", "metrics": _run(net=None, status="failed")},
        {"scenario": "e", "label": "E", "metrics": None},
    ]
    assert stress_tally(results) == {"in_profit": 1, "recorded": 3, "scenarios": 5}
