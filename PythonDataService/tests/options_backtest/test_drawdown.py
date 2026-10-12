"""The drawdown: the worst fall of equity below its running peak, measured at the end of each session.

Equity starts at the starting cash and moves by each session's profit. A session here is one trade of ten contracts
bought at 5.00 and sold at 5.00 + profit / 1000 (ten contracts of 100 shares move 1,000 times the price), so its profit
is the figure written in the scene. The running peak starts at the starting cash: a loss on the first session is a
fall from it.
"""

from __future__ import annotations

import random
from datetime import date
from fractions import Fraction
from itertools import pairwise

import numpy as np
import pytest

from app.engine.results.statistics import max_drawdown
from app.lean_sidecar.trading_calendar import expected_sessions
from app.options_backtest.results import ExitReason, LegTrade, SessionStatus
from app.options_backtest.session_result import SessionResult
from app.options_backtest.statistics import Drawdown, Ratio, summarize_run
from tests.options_backtest._results import a_session, a_trade
from tests.options_backtest._run_data import D

DAYS = expected_sessions(date(2026, 10, 1), date(2027, 3, 31))


def _making(profit: D) -> LegTrade:
    sold = D("5.00") + profit / 1000
    return a_trade("a", entry=("5.00", "5.00"), exit=(str(sold), str(sold)), reason=ExitReason.EXIT_TIME, contracts=10)


def _curve(starting: int, profits: list[int]) -> tuple[list[SessionResult], D]:
    """Sessions that each make the profit written, in whole dollars, and the cash they start with."""
    cash, sessions = D(starting), []
    for day, profit in zip(DAYS, profits, strict=False):
        sessions.append(a_session(day, str(cash), status=SessionStatus.TRADED, trades=(_making(D(profit)),)))
        cash += profit
    return sessions, D(starting)


def _fall(summary_drawdown: Drawdown | None) -> tuple[int, int, int, date | None, date]:
    assert summary_drawdown is not None
    return (
        int(summary_drawdown.peak_equity_usd),
        int(summary_drawdown.trough_equity_usd),
        int(summary_drawdown.fall_usd),
        summary_drawdown.peak_date,
        summary_drawdown.trough_date,
    )


def _instants(drawdown: Drawdown | None) -> tuple[int | None, int]:
    """The peak's and the trough's session open in int64 ms UTC, the form that is stored."""
    assert drawdown is not None
    return drawdown.peak_session_open_ms, drawdown.trough_session_open_ms


def test_with_two_peaks_the_worst_fall_is_measured_from_the_peak_before_it() -> None:
    # Equity from 1,000: 1,200 (s1), 900 (s2), 1,500 (s3), 1,350 (s4), 1,450 (s5), 1,000 (s6), 1,300 (s7).
    #   s2 fell 300 from the first peak 1,200: 25%.   s4 fell 150 from the second peak 1,500: 10%.
    #   s6 fell 500 from 1,500, the second peak: 500 / 1,500 = one third. That is the worst, by fraction and dollars.
    sessions, start = _curve(1000, [200, -300, 600, -150, 100, -450, 300])

    summary = summarize_run(sessions, starting_cash_usd=start)

    assert _fall(summary.drawdown_by_fraction) == (1500, 1000, 500, DAYS[2], DAYS[5])
    assert _fall(summary.drawdown_by_dollars) == (1500, 1000, 500, DAYS[2], DAYS[5])
    # The instants stored are the opens of the third session (the peak) and the sixth (the trough), and no other.
    third, sixth = sessions[2].session_open_ms, sessions[5].session_open_ms
    assert third != sixth
    assert _instants(summary.drawdown_by_fraction) == (third, sixth)
    assert _instants(summary.drawdown_by_dollars) == (third, sixth)
    assert summary.drawdown_by_fraction.fall_ratio == Ratio(D("500"), D("1500"))
    assert summary.drawdown_by_fraction.fall_ratio.exact() is None
    assert summary.drawdown_by_fraction.fall_ratio.rounded_to_6_places() == D("0.333333")


def test_the_worst_fall_in_dollars_and_the_worst_as_a_fraction_can_be_different_falls() -> None:
    # Equity from 100: 50 (s1), 2,000 (s2), 1,400 (s3). s1 fell 50 of 100: half, and 50 dollars. s3 fell 600 of 2,000:
    # 30%, and 600 dollars. By fraction the first is worse; by dollars the second.
    sessions, start = _curve(100, [-50, 1950, -600])

    summary = summarize_run(sessions, starting_cash_usd=start)

    assert _fall(summary.drawdown_by_fraction) == (100, 50, 50, None, DAYS[0])  # from the starting cash
    assert _fall(summary.drawdown_by_dollars) == (2000, 1400, 600, DAYS[1], DAYS[2])
    assert _instants(summary.drawdown_by_fraction) == (None, sessions[0].session_open_ms)
    assert _instants(summary.drawdown_by_dollars) == (sessions[1].session_open_ms, sessions[2].session_open_ms)


def test_a_loss_on_the_first_session_is_a_fall_from_the_starting_cash_which_has_no_date() -> None:
    sessions, start = _curve(1000, [-100])

    drawdown = summarize_run(sessions, starting_cash_usd=start).drawdown_by_fraction

    assert drawdown is not None
    assert (drawdown.peak_date, drawdown.peak_session_open_ms) == (None, None)
    assert drawdown.trough_date == DAYS[0]
    assert drawdown.trough_session_open_ms == sessions[0].session_open_ms
    assert drawdown.fall_ratio.exact() == D("0.1")


def test_when_two_falls_are_equally_bad_the_earlier_one_is_reported() -> None:
    # Equity from 100: 50 (s1: half of 100), 200 (s2), 100 (s3: half of 200). The same fraction twice, the first wins.
    # In dollars the second (100) is worse than the first (50), so it is the one reported there.
    sessions, start = _curve(100, [-50, 150, -100])

    summary = summarize_run(sessions, starting_cash_usd=start)

    assert _fall(summary.drawdown_by_fraction) == (100, 50, 50, None, DAYS[0])
    assert _fall(summary.drawdown_by_dollars) == (200, 100, 100, DAYS[1], DAYS[2])
    assert _instants(summary.drawdown_by_fraction) == (None, sessions[0].session_open_ms)
    assert _instants(summary.drawdown_by_dollars) == (sessions[1].session_open_ms, sessions[2].session_open_ms)


def test_when_equity_comes_back_to_a_peak_without_passing_it_the_peak_is_still_the_first_time_it_was_reached() -> None:
    # Equity from 1,000: 1,100 (s1, a peak), 900 (s2), 1,100 (s3, equal to the peak, not above it), 800 (s4).
    # The worst fall is s4's 300 and it is from the peak first reached on s1.
    sessions, start = _curve(1000, [100, -200, 200, -300])

    drawdown = summarize_run(sessions, starting_cash_usd=start).drawdown_by_dollars

    assert _fall(drawdown) == (1100, 800, 300, DAYS[0], DAYS[3])
    assert _instants(drawdown) == (sessions[0].session_open_ms, sessions[3].session_open_ms)


@pytest.mark.parametrize(
    ("fee", "trough", "fall", "ratio"),
    [
        # Bought at 0.99, sold at 0.00: (0.00 - 0.99) x 100 = -99.00, and two fees of 0.50 make -100.00: equity from
        # 100.00 is 0.00, a fall of 100.00 over a peak of 100.00, the whole of it.
        ("0.50", "0.00", "100.00", "1"),
        # Two fees of 0.65 make -100.30: equity is -0.30, a fall of 100.30 over 100.00, which is 1.003: more than all.
        ("0.65", "-0.30", "100.30", "1.003"),
    ],
)
def test_a_closing_fee_can_take_equity_to_zero_or_below_and_the_fall_is_then_the_whole_peak_or_more(
    fee: str, trough: str, fall: str, ratio: str
) -> None:
    # The replay never refuses a sale and charges its fee even when it fills at nothing, so cash can end below zero.
    ruined = a_trade("a", entry=("0.99", "0.99"), exit=("0.00", "0.00"), reason=ExitReason.EXIT_TIME, fees=(fee, fee))
    sessions = [a_session(DAYS[0], "100.00", status=SessionStatus.TRADED, trades=(ruined,))]

    drawdown = summarize_run(sessions, starting_cash_usd=D("100.00")).drawdown_by_fraction

    assert drawdown is not None
    assert (drawdown.peak_equity_usd, drawdown.trough_equity_usd, drawdown.fall_usd) == (
        D("100.00"),
        D(trough),
        D(fall),
    )
    assert drawdown.fall_ratio.exact() == D(ratio)  # not clamped to one
    assert drawdown.peak_date is None


def test_equity_that_only_rises_has_no_drawdown() -> None:
    sessions, start = _curve(1000, [100, 0, 50])

    summary = summarize_run(sessions, starting_cash_usd=start)

    assert summary.drawdown_by_fraction is None and summary.drawdown_by_dollars is None


def test_a_marked_leg_is_in_the_equity_at_its_mark() -> None:
    # Bought 2.00, marked 1.00 at the session's end: 100.00 down, and equity from 1,000.00 is 900.00 at once.
    marked = a_trade("a", entry=("2.00", "2.00"), mark="1.00", reason=ExitReason.MARKED_NOT_SOLD)
    sessions = [a_session(DAYS[0], "1000.00", status=SessionStatus.INCOMPLETE, trades=(marked,))]

    drawdown = summarize_run(sessions, starting_cash_usd=D("1000.00")).drawdown_by_fraction

    assert drawdown is not None
    assert (drawdown.trough_equity_usd, drawdown.fall_usd) == (D("900.00"), D("100.00"))


def _brute_force(equities: list[int]) -> tuple[Fraction, int, int, int, int, int]:
    """The worst fall by fraction, found the slow way: for each point, the highest equity up to it.

    Returns (fall / peak, peak, trough, fall, index of the peak's first time, index of the trough), the earliest worst.
    """
    best: tuple[Fraction, int, int, int, int, int] | None = None
    for at in range(1, len(equities)):
        peak = max(equities[: at + 1])
        fall = peak - equities[at]
        if fall > 0 and (best is None or Fraction(fall, peak) > best[0]):
            best = (Fraction(fall, peak), peak, equities[at], fall, equities.index(peak), at)
    assert best is not None
    return best


def _two_falls_tie_for_the_worst(equities: list[int]) -> bool:
    """Whether two points of the path fall by exactly the same fraction of their peaks, and no point by more."""
    ratios = []
    for at in range(1, len(equities)):
        peak = max(equities[: at + 1])
        ratios.append(Fraction(peak - equities[at], peak))
    return ratios.count(max(ratios)) > 1


def test_the_drawdown_over_many_random_paths_is_the_brute_force_one_and_the_engines_float_one() -> None:
    # The fraction is compared with the engine's on every path. The engine's dates are compared only on paths with no
    # exact tie for the worst fall: which of two equal falls a float computation reports depends on how its rounding
    # falls, and this module's rule (the earliest) is exact. The brute-force oracle compares the dates on every path.
    rng = random.Random(20261012)
    compared = 0
    compared_without_a_tie = 0
    for trial in range(400):
        equity, equities, profits = 1000, [1000], []
        for _ in range(rng.randint(3, 30)):
            profit = rng.randint(-400, 400)
            if equity + profit < 1:
                profit = 1 - equity
            profits.append(profit)
            equity += profit
            equities.append(equity)
        sessions, start = _curve(1000, profits)
        if all(later >= earlier for earlier, later in pairwise(equities)):
            continue  # it never fell, so there is no drawdown to compare

        found = summarize_run(sessions, starting_cash_usd=start).drawdown_by_fraction

        ratio, peak, trough, fall, peak_at, trough_at = _brute_force(equities)
        assert found is not None, f"trial {trial}"
        assert (int(found.peak_equity_usd), int(found.trough_equity_usd), int(found.fall_usd)) == (peak, trough, fall)
        assert found.peak_date == (None if peak_at == 0 else DAYS[peak_at - 1]), f"trial {trial}"
        assert found.trough_date == DAYS[trough_at - 1], f"trial {trial}"
        assert found.peak_session_open_ms == (None if peak_at == 0 else sessions[peak_at - 1].session_open_ms)
        assert found.trough_session_open_ms == sessions[trough_at - 1].session_open_ms, f"trial {trial}"
        assert Fraction(found.fall_ratio.numerator) / Fraction(found.fall_ratio.denominator) == ratio

        # The float twin: the engine's maximum drawdown over the same equity at the same instants. The starting cash
        # is a point one millisecond before the first session opens.
        start_ms = sessions[0].session_open_ms - 1
        points = [(start_ms, float(equities[0]))] + [
            (session.session_open_ms, float(value)) for session, value in zip(sessions, equities[1:], strict=False)
        ]
        engine = max_drawdown(points)
        np.testing.assert_allclose(float(ratio), engine["max_drawdown"], atol=1e-12, rtol=0)
        compared += 1
        if _two_falls_tie_for_the_worst(equities):
            continue
        # Our stored instants against the engine's own timestamps: the starting cash is the engine's start_ms.
        assert (start_ms if found.peak_session_open_ms is None else found.peak_session_open_ms) == engine[
            "peak_timestamp"
        ], f"trial {trial}"
        assert found.trough_session_open_ms == engine["trough_timestamp"], f"trial {trial}"
        compared_without_a_tie += 1
    assert compared >= 350  # nearly every random path falls somewhere, so the comparison is made on nearly all
    assert compared_without_a_tie >= 300  # and most of them have one worst fall, so the dates are compared on most


def test_the_fraction_with_cents_agrees_with_the_engines_float_to_a_trillionth() -> None:
    # Equity from 10,000.00: 9,945.10 after -54.90, then up to 10,065.25 and down to 9,512.37. The second fall is
    # 10,065.25 - 9,512.37 = 552.88, over 10,065.25.
    profits_in_cents = [-5490, 12015, -55288]
    sessions, cash = [], D("10000.00")
    for day, cents in zip(DAYS, profits_in_cents, strict=False):
        sessions.append(a_session(day, str(cash), status=SessionStatus.TRADED, trades=(_making(D(cents) / 100),)))
        cash += D(cents) / 100
    equities = [10000.00, 9945.10, 10065.25, 9512.37]

    found = summarize_run(sessions, starting_cash_usd=D("10000.00")).drawdown_by_fraction

    assert found is not None
    assert (found.peak_equity_usd, found.trough_equity_usd, found.fall_usd) == (
        D("10065.25"),
        D("9512.37"),
        D("552.88"),
    )
    points = [(sessions[0].session_open_ms - 1, equities[0])] + [
        (session.session_open_ms, value) for session, value in zip(sessions, equities[1:], strict=False)
    ]
    exact = float(Fraction(found.fall_ratio.numerator) / Fraction(found.fall_ratio.denominator))
    np.testing.assert_allclose(exact, max_drawdown(points)["max_drawdown"], atol=1e-12, rtol=0)
