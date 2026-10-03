"""The Compare evidence's trade charts (#2821): hand-worked answers, ``atol=1e-9``; money reconciles to the cent."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.research.golden_search.evaluator import EXIT_AT_WINDOW_END
from app.research.golden_search.trade_charts import (
    MAX_BINS,
    NO_CADENCE,
    NO_RSI_GATES,
    NO_RSI_RECORDED,
    NO_SETTINGS,
    STRATEGY_EXIT,
    UNALIGNED_CADENCE,
    bars_held,
    decision_bar_closes,
    decision_bar_span_ms,
    entry_rsi_gates,
    entry_times,
    histogram,
    rsi_bands,
    trade_charts,
)
from tests._helpers.golden_search import metrics

ET = ZoneInfo("America/New_York")
FIFTEEN_MINUTES = 15 * 60_000
DAY = 86_400_000
COMMISSION = 1.0


def _et(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET).timestamp() * 1000)


def _held(entry: tuple[date, int, int], exit_: tuple[date, int, int], span: int = FIFTEEN_MINUTES) -> int:
    return bars_held(_et(*entry), _et(*exit_), decision_bar_closes(session_windows_ms_utc(entry[0], exit_[0]), span))


# ── Decision bars held ───────────────────────────────────────────────────


def test_a_hold_inside_one_session_counts_the_bars_closing_after_entry_and_by_exit() -> None:
    # 10:00 to 11:15 ET: the bars closing 10:15, 10:30, 10:45, 11:00 and 11:15.
    assert _held((date(2024, 7, 18), 10, 0), (date(2024, 7, 18), 11, 15)) == 5


def test_a_hold_over_the_night_or_the_weekend_never_counts_the_gap() -> None:
    # 15:45 and 16:00, then 9:45 to 10:15 the next session.
    assert _held((date(2024, 7, 18), 15, 30), (date(2024, 7, 19), 10, 15)) == 5
    # Friday's 16:00 bar, then Monday's 9:45 to 10:30.
    assert _held((date(2024, 7, 19), 15, 45), (date(2024, 7, 22), 10, 30)) == 5


def test_a_hold_across_a_half_day_and_a_holiday_counts_only_bars_that_close() -> None:
    # 2024-07-03 closes at 13:00 and 2024-07-04 is a holiday: 12:45 and 13:00, then 9:45 and 10:00 on the 5th.
    assert _held((date(2024, 7, 3), 12, 30), (date(2024, 7, 5), 10, 0)) == 4


def test_a_daily_cadence_counts_session_closes() -> None:
    assert _held((date(2024, 7, 17), 10, 0), (date(2024, 7, 19), 16, 0), span=DAY) == 3


def test_the_ema_strategy_decides_on_fifteen_minute_bars_inside_the_settings_rsi_gates() -> None:
    point = {"symbol": "SPY", "gap": 0.2, "rsi_min": 48.0, "rsi_max": 66.0}
    assert decision_bar_span_ms("ema_crossover_signal", point) == FIFTEEN_MINUTES
    assert entry_rsi_gates("ema_crossover_signal", point) == (48.0, 66.0)
    assert decision_bar_span_ms("no_such_strategy", point) is None
    assert entry_rsi_gates("no_such_strategy", point) == NO_RSI_GATES
    # Settings the parameter model no longer accepts: the strategy has gates, but these cannot be read.
    assert entry_rsi_gates("ema_crossover_signal", {**point, "rsi_min": "low"}) == NO_SETTINGS


# ── Histogram ────────────────────────────────────────────────────────────


def _bins(nets: list[float]) -> list[tuple[float, float, int, int, int]]:
    return [(b["low"], b["high"], b["trades"], b["wins"], b["losses"]) for b in histogram(nets)["bins"]]


def test_bins_are_freedman_diaconis_wide_with_zero_an_edge() -> None:
    # Quartiles (type 7) 1.25 and 45, so IQR 43.75 and h = 2 · 43.75 · 8^(−1/3) = 43.75.
    nets = [-30.0, -10.0, 5.0, 15.0, 20.0, 40.0, 60.0, 100.0]
    assert histogram(nets)["bin_width"] == pytest.approx(43.75, abs=1e-9, rel=0) and histogram(nets)["rule"] == "freedman_diaconis"
    assert _bins(nets) == pytest.approx(
        [(-43.75, 0.0, 2, 0, 2), (0.0, 43.75, 4, 4, 0), (43.75, 87.5, 1, 1, 0), (87.5, 131.25, 1, 1, 0)], abs=1e-9, rel=0
    )


def test_a_win_and_a_loss_never_share_a_bin() -> None:
    # IQR 1, h = 2 · 2^(−1/3): the loss sits left of $0 and the win right of it.
    h = 2 * 2 ** (-1 / 3)
    assert _bins([-1.0, 1.0]) == pytest.approx([(-h, 0.0, 1, 0, 1), (0.0, h, 1, 1, 0)], abs=1e-9, rel=0)


def test_with_no_spread_between_the_quartiles_the_width_falls_back_to_sturges() -> None:
    # IQR 0, so h = 14 / ⌈log2 6 + 1⌉ = 3.5; the empty bins between stay, as zero trades.
    assert histogram([-5.0, 1.0, 1.0, 1.0, 1.0, 9.0])["rule"] == "sturges"
    assert _bins([-5.0, 1.0, 1.0, 1.0, 1.0, 9.0]) == pytest.approx(
        [(-7.0, -3.5, 1, 0, 1), (-3.5, 0.0, 0, 0, 0), (0.0, 3.5, 4, 4, 0), (3.5, 7.0, 0, 0, 0), (7.0, 10.5, 1, 1, 0)], abs=1e-9, rel=0
    )


def test_trades_that_all_net_the_same_make_one_bin() -> None:
    assert histogram([-2.0, -2.0]) == {"bin_width": 0.0, "rule": "single", "bins": [{"low": -2.0, "high": -2.0, "trades": 2, "wins": 0, "losses": 2}]}
    assert _bins([7.5]) == [(7.5, 7.5, 1, 1, 0)]


def test_float_noise_never_splits_trades_that_net_the_same_cents() -> None:
    # Six trades each moving $0.10 on 100 shares, less $2 of commission: every one nets $8.00 to the cent.
    nets = [((price + 0.1) - price) * 100 - 2.0 for price in (100.0, 101.37, 250.55, 512.13, 33.3, 77.77)]
    assert len(set(nets)) > 1  # the noise is real
    assert histogram(nets) == {"bin_width": 0.0, "rule": "single", "bins": [{"low": 8.0, "high": 8.0, "trades": 6, "wins": 6, "losses": 0}]}
    # A trade that nets a few attodollars is $0: neither a win nor a loss.
    assert _bins([5.6e-17, 10.0])[0][2:] == (1, 0, 0)


def test_one_outlier_widens_the_bins_to_at_most_the_cap() -> None:
    # The quartile spread of a cent would need hundreds of thousands of bins.
    result = histogram([0.0, 0.01, 0.02, 0.03, 10_000.0])
    assert result["rule"] == "capped"
    assert result["bin_width"] == pytest.approx(10_000.0 / (MAX_BINS - 2), abs=1e-9, rel=0)
    assert len(result["bins"]) <= MAX_BINS
    assert sum(b["trades"] for b in result["bins"]) == 5


# ── RSI bands ────────────────────────────────────────────────────────────


def _bands(entries: list[tuple[float | None, float]], gates: tuple[float, float]) -> tuple[list[tuple[float, float, int, float | None]], int]:
    result = rsi_bands(entries, gates)
    return [(b["low"], b["high"], b["trades"], b["mean_net_profit"]) for b in result["bands"]], result["unbanded"]


def test_bands_cut_the_gates_every_five_points_and_the_upper_gate_closes_the_last() -> None:
    entries = [(50.0, 10.0), (54.99, 20.0), (55.0, -5.0), (70.0, 7.0), (49.9, 3.0), (None, 4.0)]
    bands, unbanded = _bands(entries, (50.0, 70.0))
    assert bands == [(50.0, 55.0, 2, 15.0), (55.0, 60.0, 1, -5.0), (60.0, 65.0, 0, None), (65.0, 70.0, 1, 7.0)]
    # One entry below the gates and one with no RSI recorded.
    assert unbanded == 2


def test_gates_off_the_five_point_grid_keep_their_own_edges() -> None:
    bands, _ = _bands([(67.5, 1.0)], (47.0, 68.0))
    assert [(low, high) for low, high, _, _ in bands] == [(47.0, 50.0), (50.0, 55.0), (55.0, 60.0), (60.0, 65.0), (65.0, 68.0)]
    assert _bands([(60.0, 2.0)], (60.0, 60.0)) == ([(60.0, 60.0, 1, 2.0)], 0)


# ── Entry times ──────────────────────────────────────────────────────────


def _cells(result: dict[str, Any]) -> dict[tuple[str, str], tuple[int, float, float, bool]]:
    return {
        (result["weekdays"][c["weekday"]], result["half_hours"][c["half_hour"]]): (c["trades"], c["mean_net_profit"], c["total_net_profit"], c["too_few"])
        for c in result["cells"]
    }


def test_entries_fall_in_their_eastern_half_hour_across_daylight_saving() -> None:
    # A summer 10:00 (EDT) and a winter 10:15 (EST) are both Thursday 10:00 ET.
    result = entry_times([(_et(date(2024, 7, 18), 10, 0), 10.0), (_et(date(2024, 1, 18), 10, 15), 20.0)], session_windows_ms_utc(date(2024, 1, 18), date(2024, 7, 18)))
    assert _cells(result) == {("Thu", "10:00"): (2, 15.0, 30.0, True)}
    # Every half hour a regular session covers, 09:30 to 15:30.
    assert result["half_hours"] == [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in (0, 30)][1:]
    assert result["weekdays"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]


def test_a_cell_needs_five_trades_to_count() -> None:
    mondays = [date(2024, 7, 15) + timedelta(weeks=week) for week in range(5)]
    entries = [(_et(monday, 11, 5), 1.0) for monday in mondays]
    assert _cells(entry_times(entries, session_windows_ms_utc(mondays[0], mondays[-1])))[("Mon", "11:00")] == (5, 1.0, 5.0, False)


def test_the_rows_are_the_weekdays_the_sessions_hold_and_an_off_session_entry_adds_its_half_hour() -> None:
    # 2024-07-04 is a Thursday holiday, so 1 to 5 July holds no Thursday session.
    result = entry_times([(_et(date(2024, 7, 5), 16, 0), -3.0)], session_windows_ms_utc(date(2024, 7, 1), date(2024, 7, 5)))
    assert result["weekdays"] == ["Mon", "Tue", "Wed", "Fri"]
    assert result["half_hours"][-2:] == ["15:30", "16:00"]
    assert _cells(result) == {("Fri", "16:00"): (1, -3.0, -3.0, True)}


# ── The charts together ──────────────────────────────────────────────────


def _trade(entry: int, exit_: int, net: float, *, rsi: float | None = 55.0, window_end: bool = False) -> dict[str, Any]:
    return {
        "entry_ms": entry,
        "exit_ms": exit_,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "quantity": 10,
        "pnl": net + 2 * COMMISSION,
        "pnl_pct": 0.01,
        "indicators": {} if rsi is None else {"rsi": rsi},
        "exit_reason": EXIT_AT_WINDOW_END if window_end else None,
    }


def _charts(trades: list[dict[str, Any]], net: float, *, span: int | None = FIFTEEN_MINUTES, gates: tuple[float, float] | str = (50.0, 70.0)) -> dict[str, Any]:
    detail = {"initial_cash": 100_000.0, "daily_equity": [], "trades": trades}
    return trade_charts(metrics(1.0, net=net, trades=len(trades)), detail, commission_per_order=COMMISSION, bar_span_ms=span, rsi_gates=gates)


def test_each_trade_carries_its_net_running_net_and_hold_in_exit_order() -> None:
    day = date(2024, 7, 18)
    # Listed out of order: the second exits first.
    late = _trade(_et(day, 11, 0), _et(day, 15, 0), -40.0, window_end=True)
    early = _trade(_et(day, 10, 0), _et(day, 10, 30), 100.0)
    charts = _charts([late, early], 60.0)
    assert charts["status"] == "measured" and (charts["bar_span_ms"], charts["hold_reason"]) == (FIFTEEN_MINUTES, None)
    rows = [(t["net_profit"], t["running_net_profit"], t["bars_held"], t["exit_kind"], t["exit_reason"]) for t in charts["trades"]]
    assert rows == pytest.approx([(100.0, 100.0, 2, "strategy", STRATEGY_EXIT), (-40.0, 60.0, 16, "window_end", EXIT_AT_WINDOW_END)], abs=1e-9, rel=0)
    assert charts["trades"][0]["pnl"] == pytest.approx(102.0, abs=1e-9, rel=0)
    assert charts["entry_rsi"]["status"] == "measured"


def test_trades_exiting_together_run_in_entry_order() -> None:
    day = date(2024, 7, 18)
    later = _trade(_et(day, 10, 30), _et(day, 11, 0), 5.0)
    earlier = _trade(_et(day, 10, 0), _et(day, 11, 0), 7.0)
    charts = _charts([later, earlier], 12.0)
    assert [(t["net_profit"], t["running_net_profit"]) for t in charts["trades"]] == pytest.approx([(7.0, 7.0), (5.0, 12.0)], abs=1e-9, rel=0)


def test_a_cadence_off_the_half_hour_or_unread_counts_no_hold_and_keeps_the_other_charts() -> None:
    day = date(2024, 7, 18)
    trades = [_trade(_et(day, 10, 0), _et(day, 11, 0), 5.0)]
    for span, reason in ((60 * 60_000, UNALIGNED_CADENCE), (None, NO_CADENCE)):
        charts = _charts(trades, 5.0, span=span)
        assert (charts["status"], charts["hold_reason"], charts["bar_span_ms"]) == ("measured", reason, None)
        assert charts["trades"][0]["bars_held"] is None and charts["histogram"]["bins"]


def test_trades_that_do_not_reconcile_draw_no_trade_chart() -> None:
    day = date(2024, 7, 18)
    charts = _charts([_trade(_et(day, 10, 0), _et(day, 10, 30), 100.0)], 250.0)
    assert charts == {"status": "missing", "reason": "Its trades add up to $100.00 after commission, but the run's net profit is $250.00, so the trades do not account for the whole result."}
    assert _charts([], 0.0) == {"status": "missing", "reason": "The development run made no trades."}


def test_no_rsi_says_why() -> None:
    day = date(2024, 7, 18)
    trades = [_trade(_et(day, 10, 0), _et(day, 10, 30), 5.0, rsi=None)]
    assert _charts(trades, 5.0)["entry_rsi"] == {"status": "missing", "reason": NO_RSI_RECORDED}
    assert _charts(trades, 5.0, gates=NO_RSI_GATES)["entry_rsi"] == {"status": "missing", "reason": NO_RSI_GATES}
