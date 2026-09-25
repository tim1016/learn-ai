"""Unit tests for app/research/return_distribution.py.

Three evidence layers (per tests/fixtures/golden/README.md):
  * hand-computed anchors/segments on tiny synthetic days (inspection),
  * scipy.stats / numpy as the independent statistical oracle for moments,
    percentile, and the normal overlay,
  * the log-space additivity identities that the study's drill-down promises.

The scipy-vs-module comparisons run at atol=1e-9: both sides evaluate the
same closed forms in float64 with different summation orders (math.fsum vs
numpy pairwise), so agreement is limited by ulp-level accumulation, roughly
1e-13 at n=300 — four orders below the tolerance used here.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from scipy import stats as scipy_stats

from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import is_early_close, session_windows_ms_utc
from app.research import return_distribution as rd

ET = ZoneInfo("America/New_York")

# Real NYSE sessions: 2024-07-01/02 regular, 2024-07-03 is the 13:00-ET
# half-day before the July 4 holiday, 2024-07-05 regular.
D1 = date(2024, 7, 1)
D2 = date(2024, 7, 2)
D3 = date(2024, 7, 3)
D5 = date(2024, 7, 5)


def _et_ms(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp() * 1000)


def _bar(d: date, h: int, m: int, open_: float, close: float, volume: int = 100) -> TradeBar:
    o, c = Decimal(str(open_)), Decimal(str(close))
    return TradeBar(
        symbol="SPY",
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=volume,
        start_ms=_et_ms(d, h, m),
        end_ms=_et_ms(d, h, m) + 60_000,
    )


def _windows(*dates: date) -> dict[date, tuple[int, int]]:
    span = session_windows_ms_utc(min(dates), max(dates))
    return {w.session_date: (w.open_ms_utc, w.close_ms_utc) for w in span}


# ---------------------------------------------------------------------------
# extract_day_anchors
# ---------------------------------------------------------------------------


def test_extract_day_anchors_full_extended_day() -> None:
    bars = {
        D2: [
            _bar(D2, 8, 0, 100.8, 101.0),
            _bar(D2, 9, 30, 101.2, 101.3),
            _bar(D2, 11, 59, 101.9, 102.0),
            _bar(D2, 12, 0, 102.0, 102.1),
            _bar(D2, 15, 59, 102.9, 103.0),
            _bar(D2, 16, 30, 103.0, 103.5),
        ]
    }
    anchors = rd.extract_day_anchors(bars, _windows(D2))
    assert len(anchors) == 1
    a = anchors[0]
    assert a.trading_date == D2
    assert a.first_open == Decimal("100.8")
    assert a.rth_open == Decimal("101.2")
    assert a.noon_boundary_close == Decimal("102.0")
    assert a.rth_close == Decimal("103.0")
    assert a.last_close == Decimal("103.5")
    assert a.has_pre_market is True
    assert a.has_after_hours is True
    assert a.volume == 600


def test_extract_day_anchors_rth_only_day_has_no_extended_segments() -> None:
    bars = {
        D1: [
            _bar(D1, 9, 30, 100.0, 100.5),
            _bar(D1, 15, 59, 100.9, 101.0),
        ]
    }
    (a,) = rd.extract_day_anchors(bars, _windows(D1))
    assert a.first_open == Decimal("100.0")
    assert a.rth_open == Decimal("100.0")
    assert a.rth_close == Decimal("101.0")
    assert a.last_close == Decimal("101.0")
    assert a.has_pre_market is False
    assert a.has_after_hours is False


def test_extract_day_anchors_half_day_close_comes_from_calendar() -> None:
    assert is_early_close(D3), "fixture choice: 2024-07-03 must be a 13:00-ET half-day"
    bars = {
        D3: [
            _bar(D3, 9, 30, 103.0, 103.1),
            _bar(D3, 11, 59, 103.4, 103.5),
            _bar(D3, 12, 59, 103.9, 104.0),
            # 13:30 is after the 13:00 scheduled close: after-hours, not RTH.
            _bar(D3, 13, 30, 104.0, 104.2),
        ]
    }
    (a,) = rd.extract_day_anchors(bars, _windows(D3))
    assert a.rth_close == Decimal("104.0")
    assert a.noon_boundary_close == Decimal("103.5")
    assert a.last_close == Decimal("104.2")
    assert a.has_after_hours is True


def test_extract_day_anchors_non_monotonic_bars_rejected() -> None:
    bars = {D1: [_bar(D1, 9, 31, 100.0, 100.0), _bar(D1, 9, 30, 100.0, 100.0)]}
    with pytest.raises(rd.NonMonotonicBarError):
        rd.extract_day_anchors(bars, _windows(D1))


def test_extract_day_anchors_date_without_scheduled_session_rejected() -> None:
    # 2024-07-04 is the NYSE holiday: a zip for it is corruption, not a day.
    bars = {date(2024, 7, 4): [_bar(date(2024, 7, 4), 9, 30, 100.0, 100.0)]}
    with pytest.raises(ValueError, match="no scheduled NYSE session"):
        rd.extract_day_anchors(bars, _windows(D1))


# ---------------------------------------------------------------------------
# factor adjustment (lookup lives in app/data_lake/factor_files.py — see
# tests/data_lake/test_factor_files.py for the LEAN parity oracle)
# ---------------------------------------------------------------------------


def _flat_day(d: date, close: float) -> rd.DayAnchors:
    return rd.DayAnchors(
        trading_date=d,
        session_open_ms_utc=_et_ms(d, 9, 30),
        first_open=Decimal(str(close)),
        rth_open=Decimal(str(close)),
        noon_boundary_close=Decimal(str(close)),
        rth_close=Decimal(str(close)),
        last_close=Decimal(str(close)),
        volume=1,
        has_pre_market=False,
        has_after_hours=False,
    )


def test_adjust_anchors_dividend_restores_total_return_across_ex_date() -> None:
    # Raw closes: 100.0 on D1, 99.0 on D2 (a $1 ex-dividend drop on D2),
    # 100.0 on D5. The factor row is dated D1 — the last completed session
    # before the ex-date (LEAN's row convention) — so D1's data carries the
    # 0.99 back-adjustment and D2 onward does not.
    anchors = [_flat_day(D1, 100.0), _flat_day(D2, 99.0), _flat_day(D5, 100.0)]
    rows = [rd.FactorRow(D1, Decimal("0.99"), Decimal(1))]
    adjusted = rd.adjust_anchors(anchors, rows)
    assert adjusted[0].rth_close == Decimal("99.00")
    assert adjusted[1].rth_close == Decimal("99.0")

    returns = rd.compute_daily_returns(adjusted, scheduled_sessions=[D1, D2, D5])
    # The −1% raw drop is exactly the dividend being restored: adjusted
    # D1→D2 is flat.
    assert returns[1].close_to_close_pct == pytest.approx(0.0, abs=1e-12)
    # D2 → D5: both post-event, plain raw move ln(100/99).
    assert returns[2].close_to_close_pct == pytest.approx(
        math.expm1(math.log(100.0 / 99.0)) * 100.0, abs=1e-12
    )


def test_adjust_anchors_split_continuity() -> None:
    anchors = [_flat_day(D1, 200.0), _flat_day(D2, 100.5)]  # 2:1 split ex-date D2
    # Row dated D1 (the session before the ex-date) carries split_factor 0.5
    # for data through D1; D2 is post-split and unadjusted.
    rows = [rd.FactorRow(D1, Decimal(1), Decimal("0.5"))]
    returns = rd.compute_daily_returns(rd.adjust_anchors(anchors, rows), scheduled_sessions=[D1, D2])
    # Adjusted: D1 close 100, D2 close 100.5 → +0.5%, not −49.75%.
    assert returns[1].close_to_close_pct == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# compute_daily_returns — hand-computed segments + identities
# ---------------------------------------------------------------------------


def _two_days_bars() -> dict[date, list[TradeBar]]:
    return {
        D1: [_bar(D1, 9, 30, 100.0, 100.5), _bar(D1, 15, 59, 100.9, 101.0)],
        D2: [
            _bar(D2, 8, 0, 100.8, 101.0),
            _bar(D2, 9, 30, 101.2, 101.3),
            _bar(D2, 11, 59, 101.9, 102.0),
            _bar(D2, 12, 0, 102.0, 102.1),
            _bar(D2, 15, 59, 102.9, 103.0),
            _bar(D2, 16, 30, 103.0, 103.5),
        ],
    }


def _pct_of(log_return: float) -> float:
    return math.expm1(log_return) * 100.0


def test_compute_daily_returns_hand_computed_percentages() -> None:
    anchors = rd.extract_day_anchors(_two_days_bars(), _windows(D1, D2))
    (first, second) = rd.compute_daily_returns(anchors, scheduled_sessions=[D1, D2])

    # First day: no previous close in-window.
    assert first.close_to_close_pct is None
    assert first.overnight_pct is None
    assert first.session_pct == pytest.approx(_pct_of(math.log(101.0 / 100.0)), abs=1e-12)
    assert first.pre_market_pct is None
    assert first.after_hours_pct is None

    assert second.close_to_close_pct == pytest.approx(_pct_of(math.log(103.0 / 101.0)), abs=1e-12)
    assert second.session_pct == pytest.approx(_pct_of(math.log(103.0 / 101.2)), abs=1e-12)
    assert second.overnight_pct == pytest.approx(_pct_of(math.log(101.2 / 101.0)), abs=1e-12)
    assert second.pre_market_pct == pytest.approx(_pct_of(math.log(101.2 / 100.8)), abs=1e-12)
    assert second.morning_pct == pytest.approx(_pct_of(math.log(102.0 / 101.2)), abs=1e-12)
    assert second.after_hours_pct == pytest.approx(_pct_of(math.log(103.5 / 103.0)), abs=1e-12)
    assert second.volume == 600


def test_compute_daily_returns_log_identities() -> None:
    anchors = rd.extract_day_anchors(_two_days_bars(), _windows(D1, D2))
    second = rd.compute_daily_returns(anchors, scheduled_sessions=[D1, D2])[1]

    # morning + afternoon = session: constructed by subtraction in log
    # space, so the round-tripped logs agree to ulp level (1e-12 floor).
    assert (
        math.log1p(second.morning_pct / 100.0) + math.log1p(second.afternoon_pct / 100.0)
    ) == pytest.approx(math.log1p(second.session_pct / 100.0), abs=1e-12)

    # overnight + session = close-to-close: three independent math.log
    # evaluations, so ulp-level agreement (1e-12 floor).
    assert (
        math.log1p(second.overnight_pct / 100.0) + math.log1p(second.session_pct / 100.0)
    ) == pytest.approx(math.log1p(second.close_to_close_pct / 100.0), abs=1e-12)

    # In displayed percent the same sums hold to second order (geometric
    # compounding): error ≈ r·s/2 in return units.
    for a_pct, b_pct, whole_pct in (
        (second.overnight_pct, second.session_pct, second.close_to_close_pct),
        (second.morning_pct, second.afternoon_pct, second.session_pct),
    ):
        second_order = (a_pct / 100.0) * (b_pct / 100.0) * 100.0
        assert abs(a_pct + b_pct - whole_pct) <= second_order + 1e-12


def test_compute_daily_returns_skips_day_without_rth_bars() -> None:
    # D2 has only pre-market bars: no session/close anchors → excluded from
    # every series. It is still the previous *scheduled* session for D5, so
    # D5's close-to-close cannot return against D1 two sessions back — the
    # chain resets and D5 keeps only its session return.
    bars = {
        D1: [_bar(D1, 9, 30, 100.0, 100.5), _bar(D1, 15, 59, 100.9, 101.0)],
        D2: [_bar(D2, 8, 0, 100.8, 101.0)],
        D5: [_bar(D5, 9, 30, 102.0, 102.1), _bar(D5, 15, 59, 103.9, 104.0)],
    }
    returns = rd.compute_daily_returns(
        rd.extract_day_anchors(bars, _windows(D1, D2, D5)),
        scheduled_sessions=[D1, D2, D5],
    )
    assert [r.trading_date for r in returns] == [D1, D5]
    assert returns[1].close_to_close_pct is None
    assert returns[1].overnight_pct is None
    assert returns[1].session_pct == pytest.approx(_pct_of(math.log(104.0 / 102.0)), abs=1e-12)


def test_compute_daily_returns_resets_chain_across_capture_gap() -> None:
    # D2 is entirely absent from the anchors (a capture gap). Returning D5
    # against D1 would label a two-session move a daily return; the chain
    # resets instead, and the session after the gap resumes adjacency.
    bars = {
        D1: [_bar(D1, 9, 30, 100.0, 100.5), _bar(D1, 15, 59, 100.9, 101.0)],
        D5: [_bar(D5, 9, 30, 102.0, 102.1), _bar(D5, 15, 59, 103.9, 104.0)],
        date(2024, 7, 8): [_bar(date(2024, 7, 8), 9, 30, 104.0, 104.1), _bar(date(2024, 7, 8), 15, 59, 104.9, 105.0)],
    }
    D8 = date(2024, 7, 8)
    returns = rd.compute_daily_returns(
        rd.extract_day_anchors(bars, _windows(D1, D5, D8)),
        scheduled_sessions=[D1, D2, D5, D8],
    )
    assert [r.trading_date for r in returns] == [D1, D5, D8]
    # D5's previous scheduled session (D2) is not captured → reset.
    assert returns[1].close_to_close_pct is None
    assert returns[1].overnight_pct is None
    # D8's previous scheduled session is D5, which is captured → adjacent.
    assert returns[2].close_to_close_pct == pytest.approx(
        _pct_of(math.log(105.0 / 104.0)), abs=1e-12
    )
    assert returns[2].overnight_pct == pytest.approx(
        _pct_of(math.log(104.0 / 104.0)), abs=1e-12
    )


def test_sessions_read_by_returns_is_the_returned_days_and_their_bases() -> None:
    # Lead-in D1, D2 (pre-market only), D3; returned from D5. D5 returns
    # against D3, the lead-in's last session. D1 and D2 feed no returned
    # day. D8 (pre-market only) is captured but compared by nothing, and D9
    # has no base: its previous session D8 has no close.
    D8, D9 = date(2024, 7, 8), date(2024, 7, 9)

    def _full(d: date) -> list[TradeBar]:
        return [_bar(d, 9, 30, 100.0, 100.5), _bar(d, 12, 30, 100.9, 101.0)]

    bars = {
        D1: _full(D1),
        D2: [_bar(D2, 8, 0, 100.8, 101.0)],
        D3: _full(D3),
        D5: _full(D5),
        D8: [_bar(D8, 8, 0, 100.8, 101.0)],
        D9: _full(D9),
    }
    anchors = rd.extract_day_anchors(bars, _windows(*bars))
    scheduled = [D1, D2, D3, D5, D8, D9]

    read = rd.sessions_read_by_returns(anchors, scheduled_sessions=scheduled, since=D5)

    assert read == [D3, D5, D9]
    # The same adjacency compute_daily_returns follows: only D5 compares
    # across days, against D3.
    returned = [r for r in rd.compute_daily_returns(anchors, scheduled_sessions=scheduled) if r.trading_date >= D5]
    assert [(r.trading_date, r.close_to_close_pct is not None) for r in returned] == [(D5, True), (D9, False)]


# ---------------------------------------------------------------------------
# compute_histogram
# ---------------------------------------------------------------------------


def test_compute_histogram_zero_is_a_bin_edge_and_membership_is_lower_inclusive() -> None:
    h = rd.compute_histogram([0.0, 0.25, 0.5, -0.5], bin_width_pct=0.5, span_pct=5.0)
    inner = [b for b in h.bins if not b.is_edge]
    # 0.0 and 0.25 → [0, 0.5); 0.5 → [0.5, 1.0); -0.5 → [-0.5, 0).
    by_bounds = {(b.lower_pct, b.upper_pct): b.count for b in inner}
    assert by_bounds[(0.0, 0.5)] == 2
    assert by_bounds[(0.5, 1.0)] == 1
    assert by_bounds[(-0.5, 0.0)] == 1
    assert h.total_count == 4


def test_compute_histogram_edge_bins_are_open_ended() -> None:
    h = rd.compute_histogram([-6.2, -5.0, 4.99, 5.0, 7.5], bin_width_pct=0.5, span_pct=5.0)
    left, *inner, right = h.bins
    assert left.is_edge and left.lower_pct is None and left.upper_pct == -5.0
    assert left.count == 1  # only -6.2; -5.0 itself belongs to [-5.0, -4.5)
    assert right.is_edge and right.lower_pct == 5.0 and right.upper_pct is None
    assert right.count == 2  # 5.0 (>=) and 7.5
    by_bounds = {(b.lower_pct, b.upper_pct): b.count for b in inner}
    assert by_bounds[(4.5, 5.0)] == 1  # 4.99
    assert by_bounds[(-5.0, -4.5)] == 1  # -5.0
    assert h.total_count == 5


def test_compute_histogram_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="bin_width_pct"):
        rd.compute_histogram([], bin_width_pct=0.0, span_pct=5.0)
    with pytest.raises(ValueError, match="integer multiple"):
        rd.compute_histogram([], bin_width_pct=0.3, span_pct=5.0)
    # The bin-count bound is a caller error of the same family: a valid-but-
    # absurd width must refuse, not attempt to allocate the edge list.
    with pytest.raises(rd.StudyRequestError, match="exceeds the maximum"):
        rd.compute_histogram([], bin_width_pct=0.01, span_pct=5.0)
    assert rd.MAX_BINS_PER_SIDE == 100
    rd.compute_histogram([], bin_width_pct=0.05, span_pct=5.0)  # 100/side: allowed


# ---------------------------------------------------------------------------
# compute_distribution_stats — scipy/numpy oracle
# ---------------------------------------------------------------------------


def _stats_fixture(n: int = 300) -> tuple[list[float], list[date], list[int]]:
    rng = np.random.default_rng(7)
    values = rng.normal(0.1, 1.3, size=n).tolist()
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(n)]
    anchors = [i * 86_400_000 for i in range(n)]
    return values, dates, anchors


def test_compute_distribution_stats_matches_scipy_numpy_oracle() -> None:
    values, dates, anchors = _stats_fixture()
    s = rd.compute_distribution_stats(values, dates, anchors)

    # atol=1e-9: math.fsum vs numpy pairwise summation differ at ulp scale
    # (~1e-13 at n=300); the closed forms themselves are identical.
    assert s.mean_pct == pytest.approx(float(np.mean(values)), abs=1e-9)
    assert s.std_pct == pytest.approx(float(np.std(values, ddof=1)), abs=1e-9)
    assert s.annualized_vol_pct == pytest.approx(
        float(np.std(values, ddof=1)) * math.sqrt(252.0), abs=1e-9
    )
    assert s.skewness == pytest.approx(float(scipy_stats.skew(values, bias=False)), abs=1e-9)
    assert s.excess_kurtosis == pytest.approx(
        float(scipy_stats.kurtosis(values, fisher=True, bias=False)), abs=1e-9
    )
    var95 = float(np.percentile(values, 5))
    assert s.var_95_pct == pytest.approx(var95, abs=1e-9)

    tail = [v for v in sorted(values) if v <= var95]
    assert s.cvar_95_pct == pytest.approx(math.fsum(tail) / len(tail), abs=1e-9)

    assert s.n_days == 300
    best_i = values.index(max(values))
    worst_i = values.index(min(values))
    assert s.best_day.trading_date == dates[best_i]
    assert s.best_day.value_pct == values[best_i]
    assert s.worst_day.trading_date == dates[worst_i]
    assert s.worst_day.value_pct == values[worst_i]


def test_compute_distribution_stats_total_on_short_series() -> None:
    # n=2: std exists, the standardized moments do not — None, not a crash.
    s = rd.compute_distribution_stats([1.0, 2.0], [D1, D2], [0, 1])
    assert s.n_days == 2
    assert s.mean_pct == pytest.approx(1.5)
    assert s.std_pct == pytest.approx(float(np.std([1.0, 2.0], ddof=1)), abs=1e-12)
    assert s.annualized_vol_pct == pytest.approx(s.std_pct * math.sqrt(252.0), abs=1e-12)
    assert s.skewness is None
    assert s.excess_kurtosis is None
    # VaR/CVaR stay defined (percentile of two points interpolates; the
    # tail at-or-below it holds only the single worst point).
    assert s.var_95_pct == pytest.approx(1.05)
    assert s.cvar_95_pct == pytest.approx(1.0)

    # n=3: kurtosis's (n−3) denominator would be zero — still no crash.
    s3 = rd.compute_distribution_stats([1.0, 2.0, 4.0], [D1, D2, D5], [0, 1, 2])
    assert s3.skewness is None
    assert s3.excess_kurtosis is None
    assert s3.std_pct is not None

    # n=1: no std at all.
    s1 = rd.compute_distribution_stats([3.0], [D1], [0])
    assert s1.std_pct is None
    assert s1.annualized_vol_pct is None
    assert s1.var_95_pct == 3.0

    # n=0 is the only rejection left.
    with pytest.raises(ValueError, match="at least 1"):
        rd.compute_distribution_stats([], [], [])


def test_compute_distribution_stats_constant_series_is_defined_not_crashing() -> None:
    # A flat price series: std 0, standardized moments undefined (None), the
    # overlay degenerate to zeros — never a ZeroDivisionError.
    values = [0.5] * 30
    dates = [D1 + timedelta(days=i) for i in range(30)]
    s = rd.compute_distribution_stats(values, dates, list(range(30)))
    assert s.mean_pct == 0.5
    assert s.std_pct == 0.0
    assert s.annualized_vol_pct == 0.0
    assert s.skewness is None
    assert s.excess_kurtosis is None
    assert s.var_95_pct == 0.5
    assert s.cvar_95_pct == 0.5
    h = rd.compute_histogram(values)
    assert rd.normal_expected_counts(h, s.mean_pct, s.std_pct) == (0.0,) * len(h.bins)


# ---------------------------------------------------------------------------
# normal overlay
# ---------------------------------------------------------------------------


def test_normal_expected_counts_sums_to_total() -> None:
    values, _, _ = _stats_fixture()
    mean = math.fsum(values) / len(values)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (len(values) - 1))
    h = rd.compute_histogram(values)
    overlay = rd.normal_expected_counts(h, mean, std)
    assert len(overlay) == len(h.bins)
    # erf rounding accumulates over ~22 bins: ulp-level per bin, 1e-9 floor.
    assert math.fsum(overlay) == pytest.approx(h.total_count, abs=1e-9)


def test_normal_expected_counts_matches_scipy_cdf() -> None:
    values, _, _ = _stats_fixture(200)
    mean = math.fsum(values) / len(values)
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (len(values) - 1))
    h = rd.compute_histogram(values, bin_width_pct=0.5, span_pct=2.0)
    overlay = rd.normal_expected_counts(h, mean, std)
    inner = [b for b in h.bins if not b.is_edge]
    overlay_inner = [c for b, c in zip(h.bins, overlay, strict=True) if not b.is_edge]
    expected = [
        h.total_count
        * (
            scipy_stats.norm.cdf(b.upper_pct, mean, std)
            - scipy_stats.norm.cdf(b.lower_pct, mean, std)
        )
        for b in inner
    ]
    assert overlay_inner == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# build_return_distribution
# ---------------------------------------------------------------------------


def _synthetic_session_bars(n_sessions: int = 40, seed: int = 11) -> dict[date, list[TradeBar]]:
    """Deterministic multi-session extended-hours days over real NYSE sessions.

    Every session gets RTH bars at 09:30 / 11:59 / 12:00 / 15:59; pre-market
    and after-hours bars appear on alternating sessions so both segment
    shapes are exercised.
    """
    from app.lean_sidecar.trading_calendar import expected_sessions

    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:n_sessions]
    rng = np.random.default_rng(seed)
    bars: dict[date, list[TradeBar]] = {}
    price = 100.0
    for i, d in enumerate(sessions):
        day_bars: list[TradeBar] = []
        pre_open = price * (1.0 + float(rng.normal(0.0, 0.002)))
        if i % 2 == 0:
            day_bars.append(_bar(d, 8, 0, pre_open, pre_open * 1.001))
        open_ = pre_open * (1.0 + float(rng.normal(0.0, 0.002)))
        noon = open_ * (1.0 + float(rng.normal(0.0, 0.004)))
        close = noon * (1.0 + float(rng.normal(0.0, 0.004)))
        day_bars.extend(
            [
                _bar(d, 9, 30, open_, open_ * 1.0005),
                _bar(d, 11, 59, noon * 0.9995, noon),
                _bar(d, 12, 0, noon, noon * 1.0005),
                _bar(d, 15, 59, close * 0.9995, close),
            ]
        )
        if i % 3 == 0:
            day_bars.append(_bar(d, 16, 30, close, close * (1.0 + float(rng.normal(0.0, 0.001)))))
        bars[d] = day_bars
        price = close
    return bars


def test_build_return_distribution_covers_all_kinds_and_days() -> None:
    bars_by_day = _synthetic_session_bars()
    sessions = sorted(bars_by_day)
    anchors = rd.extract_day_anchors(bars_by_day, _windows(*sessions))
    days = rd.compute_daily_returns(anchors, scheduled_sessions=sessions)
    assert len(days) == len(sessions)

    result = rd.build_return_distribution(days, adjustment="split_and_dividend")
    assert [k.kind for k in result.kinds] == ["close_to_close", "session", "overnight"]
    assert [d.trading_date for d in result.days] == sessions
    assert result.adjustment == "split_and_dividend"

    by_kind = {k.kind: k for k in result.kinds}
    # Every synthetic day has RTH anchors; only the first lacks a previous
    # close, so close_to_close/overnight lose exactly one day.
    assert by_kind["session"].stats.n_days == len(sessions)
    assert by_kind["close_to_close"].stats.n_days == len(sessions) - 1
    assert by_kind["overnight"].stats.n_days == len(sessions) - 1
    for kind in result.kinds:
        assert kind.histogram.total_count == kind.stats.n_days
        assert len(kind.normal_expected_counts) == len(kind.histogram.bins)
        # Each day lands in exactly one bin of its kind's histogram.
        assert sum(b.count for b in kind.histogram.bins) == kind.stats.n_days


def test_build_return_distribution_bin_indices_match_histogram_counts() -> None:
    # The stamped per-day bin identity and the histogram counts must be the
    # same classification: for every kind and every bin index, the number of
    # days stamped with that index equals the bin's count. This is the
    # contract the frontend drill-down relies on instead of re-deriving
    # membership.
    bars_by_day = _synthetic_session_bars(n_sessions=60, seed=23)
    sessions = sorted(bars_by_day)
    anchors = rd.extract_day_anchors(bars_by_day, _windows(*sessions))
    days = rd.compute_daily_returns(anchors, scheduled_sessions=sessions)
    result = rd.build_return_distribution(
        days, bin_width_pct=1.0, span_pct=5.0, adjustment="split_and_dividend"
    )

    for ki, kind in enumerate(result.kinds):
        for bin_index, bin_model in enumerate(kind.histogram.bins):
            stamped = sum(1 for d in result.days if d.bin_indices[ki] == bin_index)
            assert stamped == bin_model.count, (kind.kind, bin_index)
        # Days without a value for the kind carry None, matching the count
        # deficit between days and the kind's n.
        n_none = sum(1 for d in result.days if d.bin_indices[ki] is None)
        assert n_none == len(result.days) - kind.stats.n_days
