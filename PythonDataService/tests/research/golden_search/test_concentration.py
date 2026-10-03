"""How much of a development result rests on its best month or trades (#2815): hand-worked answers, ``atol=1e-9``."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.golden_search.concentration import NO_PROFIT_TO_SHARE, NOT_EVALUATED, best_count, concentration, concentration_curve
from app.research.golden_search.selection import Metrics
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import metrics

CAPITAL = 100_000.0
COMMISSION = 1.0
# January +1,000, February +3,000, March -500: 3,500 net.
MONTH_ENDS = ((date(2024, 1, 31), 101_000.0), (date(2024, 2, 29), 104_000.0), (date(2024, 3, 28), 103_500.0))


def _trades(*nets: float) -> list[dict[str, Any]]:
    """One trade per net profit, entered an hour apart in that order; P&L before fees carries both commissions."""
    start = session_close_ms_utc(date(2024, 1, 2)) - 6 * 3_600_000
    return [{"entry_ms": start + i * 3_600_000, "exit_ms": start + i * 3_600_000 + 1_800_000, "pnl": net + 2 * COMMISSION} for i, net in enumerate(nets)]


def _detail(trades: list[dict[str, Any]], month_ends: tuple[tuple[date, float], ...] = MONTH_ENDS) -> dict[str, Any]:
    return {"initial_cash": CAPITAL, "daily_equity": [[session_close_ms_utc(day), equity] for day, equity in month_ends], "trades": trades}


def _measure(nets: tuple[float, ...], net: float, month_ends: tuple[tuple[date, float], ...] = MONTH_ENDS) -> dict[str, Any]:
    return concentration(metrics(1.0, net=net, trades=len(nets)), _detail(_trades(*nets), month_ends), commission_per_order=COMMISSION)


@pytest.mark.parametrize(("trades", "best"), [(0, 0), (1, 1), (19, 1), (20, 1), (21, 2), (60, 3), (188, 10)])
def test_the_best_five_percent_is_one_in_twenty_rounded_up(trades: int, best: int) -> None:
    # 0.05 * 60 is 3.0000000000000004 in floating point; the count stays exact.
    assert best_count(trades) == best


def test_a_spread_out_result_meets_the_rule() -> None:
    measure = _measure((2_000.0, 1_500.0, 500.0, -300.0, -200.0), 3_500.0)
    assert (measure["status"], measure["reason"], measure["trades"]) == ("meets", None, 5)
    assert measure["best_month"] == {"month_start_ms": et_midnight_ms(date(2024, 2, 1)), "net_profit": pytest.approx(3_000.0, abs=1e-9, rel=0)}
    # Without February: 3,500 - 3,000. Without the one best trade (5% of 5, rounded up): 3,500 - 2,000.
    assert measure["without_best_month"] == pytest.approx(500.0, abs=1e-9, rel=0)
    assert measure["without_best_trades"] == pytest.approx(1_500.0, abs=1e-9, rel=0)
    assert measure["best_trades_net_profit"] == pytest.approx(2_000.0, abs=1e-9, rel=0)
    (removed,) = measure["best_trades"]
    assert removed["net_profit"] == pytest.approx(2_000.0, abs=1e-9, rel=0) and removed["entry_ms"] == _trades(2_000.0)[0]["entry_ms"]


def test_a_result_resting_on_one_trade_is_a_concern() -> None:
    measure = _measure((4_000.0, 300.0, -200.0, -300.0, -300.0), 3_500.0)
    assert measure["status"] == "concern"
    assert measure["without_best_trades"] == pytest.approx(-500.0, abs=1e-9, rel=0)
    assert measure["without_best_month"] == pytest.approx(500.0, abs=1e-9, rel=0)


def test_nothing_left_without_the_best_month_is_a_concern() -> None:
    # One month earns everything: without it the result is exactly $0, which the rule counts.
    measure = _measure((600.0, 400.0), 1_000.0, ((date(2024, 1, 31), 101_000.0),))
    assert (measure["status"], measure["without_best_month"]) == ("concern", 0.0)


def test_a_losing_result_whose_best_month_still_loses_is_a_concern() -> None:
    # January -1,000 and February -500: the best month is February's loss, and removing it leaves -1,000.
    months = ((date(2024, 1, 31), 99_000.0), (date(2024, 2, 29), 98_500.0))
    measure = _measure((-700.0, -800.0), -1_500.0, months)
    assert measure["status"] == "concern"
    assert measure["best_month"]["month_start_ms"] == et_midnight_ms(date(2024, 2, 1))
    assert measure["without_best_month"] == pytest.approx(-1_000.0, abs=1e-9, rel=0)
    # The best trade is the smaller loss: -1,500 - (-700).
    assert measure["without_best_trades"] == pytest.approx(-800.0, abs=1e-9, rel=0)


def test_ties_take_the_earliest_month_and_the_earlier_trade() -> None:
    months = ((date(2024, 1, 31), 101_000.0), (date(2024, 2, 29), 102_000.0))
    # 21 trades: the best 5% is two, and the second place is a tie the earlier entry wins.
    nets = (100.0, 500.0, 300.0, 300.0, *([50.0] * 17))
    measure = _measure(nets, 2_050.0, months)
    assert measure["best_month"]["month_start_ms"] == et_midnight_ms(date(2024, 1, 1))
    trades = _trades(*nets)
    assert [(item["entry_ms"], item["net_profit"]) for item in measure["best_trades"]] == [(trades[1]["entry_ms"], 500.0), (trades[2]["entry_ms"], 300.0)]
    assert (measure["best_trades_net_profit"], measure["without_best_trades"]) == pytest.approx((800.0, 1_250.0), abs=1e-9, rel=0)


@pytest.mark.parametrize(
    ("run", "detail", "reason"),
    [
        (None, None, NOT_EVALUATED),
        (metrics(None, status="failed", net=None), None, "The development run failed, so there is nothing to measure."),
        (metrics(1.0, net=None), _detail(_trades(10.0)), "The development run recorded no net profit."),
        (metrics(1.0, net=10.0), None, "The development run kept no trade list."),
        (metrics(1.0, net=0.0, trades=0), _detail([]), "The development run made no trades."),
    ],
)
def test_a_run_with_nothing_to_measure_is_missing_never_a_pass(run: Metrics | None, detail: dict[str, Any] | None, reason: str) -> None:
    measure = concentration(run, detail, commission_per_order=COMMISSION)
    assert (measure["status"], measure["reason"], measure["without_best_month"], measure["without_best_trades"]) == ("missing", reason, None, None)


def test_trades_that_do_not_add_up_to_the_run_leave_it_unmeasured() -> None:
    # The trades net 3,000 against 3,500 recorded: a position still open at the window's end, say.
    measure = _measure((2_000.0, 1_000.0), 3_500.0)
    assert measure["status"] == "missing" and "$3,000.00" in measure["reason"] and "$3,500.00" in measure["reason"]
    curve = concentration_curve(metrics(1.0, net=3_500.0), _detail(_trades(2_000.0, 1_000.0)), commission_per_order=COMMISSION)
    assert (curve["points"], curve["reason"]) == ([], measure["reason"])


def test_the_curve_climbs_through_the_winners_and_ends_at_the_whole_result() -> None:
    nets = (-300.0, 2_000.0, 500.0, -200.0, 1_500.0)
    curve = concentration_curve(metrics(1.0, net=3_500.0), _detail(_trades(*nets)), commission_per_order=COMMISSION)
    assert (curve["best_count"], curve["reason"]) == (1, None)
    points = curve["points"]
    assert [point["trades"] for point in points] == [0, 1, 2, 3, 4, 5]
    assert [point["share_of_trades"] for point in points] == pytest.approx([0.0, 0.2, 0.4, 0.6, 0.8, 1.0], abs=1e-9, rel=0)
    # Best first: 2,000, 3,500, 4,000, 3,800, 3,500 of 3,500.
    assert [point["net_profit"] for point in points] == pytest.approx([0.0, 2_000.0, 3_500.0, 4_000.0, 3_800.0, 3_500.0], abs=1e-9, rel=0)
    assert [point["share_of_profit"] for point in points] == pytest.approx([0.0, 4 / 7, 1.0, 8 / 7, 38 / 35, 1.0], abs=1e-9, rel=0)


def test_no_curve_is_drawn_for_a_result_that_did_not_make_money() -> None:
    curve = concentration_curve(metrics(1.0, net=-1_500.0), _detail(_trades(-700.0, -800.0)), commission_per_order=COMMISSION)
    assert curve == {"points": [], "best_count": 1, "reason": NO_PROFIT_TO_SHARE}
