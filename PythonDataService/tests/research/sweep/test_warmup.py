"""Warmup requirement probe and run-up planner (PRD #1926, review F02).

The probe is checked against the readiness thresholds the review reproduced
with the raw indicators — RSI needs ``period + 1``, ADX ``2 x period``, and
MACD's signal line only starts once the slow EMA is ready — but measured
through the real registered programs, which is what makes cascaded and
fixed-period requirements come out right without a per-strategy declaration.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.sweep.warmup import (
    RunUpExceedsRangeError,
    WarmupProbeError,
    plan_run_up,
    probe_warmup_samples,
)
from tests._helpers.lean_store import seed_store_day

FIFTEEN_MIN_MS = 15 * 60_000
DAY_MS = 24 * 60 * 60_000


@pytest.mark.parametrize(
    ("strategy_key", "params", "first_ready"),
    [
        ("rsi_mean_reversion", {"window": 26}, 27),  # RSI(period) needs period + 1
        ("spy_strategy_c", {"adx_period": 200}, 400),  # ADX(period) needs 2 x period
        ("spy_strategy_a", {"macd_fast": 12, "macd_slow": 500, "macd_signal": 200}, 699),  # slow + signal - 1
        ("sma_crossover", {"short_window": 2, "long_window": 3}, 3),
    ],
)
def test_probe_reproduces_the_reviewed_readiness_thresholds(strategy_key: str, params: dict, first_ready: int) -> None:
    probe = probe_warmup_samples(strategy_key, {"symbol": "SPY", **params})

    assert probe.first_ready_sample == first_ready
    # One prior-state bar on top, so a crossover can be a fresh one on the first scored bar.
    assert probe.required_samples == first_ready + 1


def test_a_fixed_period_program_declares_a_constant_requirement_and_stays_sweepable() -> None:
    # EMA5 / EMA10 / RSI14 are fixed at construction; RSI14 governs at 15 samples.
    probe = probe_warmup_samples("ema_crossover_signal", {"symbol": "SPY"})

    assert probe.first_ready_sample == 15
    assert probe.bar_span_ms == FIFTEEN_MIN_MS


def test_probe_reads_the_decision_cadence_from_the_program() -> None:
    hourly = probe_warmup_samples("sma_crossover", {"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 60})
    daily = probe_warmup_samples("sma_crossover", {"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 1440})

    assert hourly.bar_span_ms == 60 * 60_000
    assert daily.bar_span_ms == DAY_MS
    assert hourly.required_samples == daily.required_samples == 4


def test_probe_refuses_an_unknown_strategy() -> None:
    with pytest.raises(WarmupProbeError, match="unknown strategy"):
        probe_warmup_samples("no_such_strategy", {"symbol": "SPY"})


# ── Run-up planning ──────────────────────────────────────────────────────

WINDOW = (date(2025, 1, 6), date(2025, 1, 31))


def test_run_up_is_carved_from_the_front_when_no_earlier_history_exists(tmp_path: Path) -> None:
    # 30 fifteen-minute bars: one full session holds 26, so two sessions are consumed.
    plan = plan_run_up(
        symbol="SPY",
        requested_start=WINDOW[0],
        requested_end=WINDOW[1],
        required_samples=30,
        bar_span_ms=FIFTEEN_MIN_MS,
        roots=[tmp_path],
    )

    sessions = expected_sessions(*WINDOW)
    assert plan.carved_from_range is True
    assert plan.data_start == WINDOW[0]
    assert plan.run_up_sessions == 2
    assert plan.evaluation_start == sessions[2]
    assert plan.evaluation_end == WINDOW[1]
    assert plan.is_primed


def test_earlier_history_is_used_when_the_lake_holds_it(tmp_path: Path) -> None:
    # Seed every session of the prior two weeks so nothing requested is lost.
    for day in expected_sessions(date(2024, 12, 16), date(2025, 1, 3)):
        seed_store_day(tmp_path, "SPY", day)

    plan = plan_run_up(
        symbol="SPY",
        requested_start=WINDOW[0],
        requested_end=WINDOW[1],
        required_samples=30,
        bar_span_ms=FIFTEEN_MIN_MS,
        roots=[tmp_path],
    )

    assert plan.carved_from_range is False
    assert plan.evaluation_start == WINDOW[0]
    assert plan.data_start == date(2025, 1, 2)  # the two sessions before Jan 6: Jan 2 and Jan 3 (Jan 1 closed)
    assert plan.run_up_sessions == 2


def test_an_early_close_contributes_fewer_bars(tmp_path: Path) -> None:
    # Thanksgiving week 2024: Wed 11-27 (26 bars), Thu closed, Fri 11-29 closes 13:00 ET (14 bars).
    plan = plan_run_up(
        symbol="SPY",
        requested_start=date(2024, 11, 27),
        requested_end=date(2024, 12, 6),
        required_samples=41,  # 26 + 14 = 40 is not enough; a full Friday (26) would have been
        bar_span_ms=FIFTEEN_MIN_MS,
        roots=[tmp_path],
    )

    assert plan.run_up_sessions == 3
    assert plan.evaluation_start == date(2024, 12, 3)


def test_daily_cadence_counts_one_bar_per_session(tmp_path: Path) -> None:
    plan = plan_run_up(
        symbol="SPY",
        requested_start=WINDOW[0],
        requested_end=WINDOW[1],
        required_samples=3,
        bar_span_ms=DAY_MS,
        roots=[tmp_path],
    )

    assert plan.run_up_sessions == 3
    assert plan.evaluation_start == expected_sessions(*WINDOW)[3]


def test_a_run_up_that_would_consume_the_whole_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunUpExceedsRangeError, match="consumes every session"):
        plan_run_up(
            symbol="SPY",
            requested_start=WINDOW[0],
            requested_end=date(2025, 1, 8),  # three sessions, 78 bars
            required_samples=78,
            bar_span_ms=FIFTEEN_MIN_MS,
            roots=[tmp_path],
        )
