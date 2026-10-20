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
        pytest.param("rsi_mean_reversion", {"window": 26}, 27, id="rsi_needs_period_plus_one"),
        pytest.param("spy_strategy_c", {"adx_period": 200}, 400, id="adx_needs_two_times_period"),
        pytest.param("spy_strategy_a", {"macd_fast": 12, "macd_slow": 500, "macd_signal": 200}, 699, id="macd_slow_plus_signal_minus_one"),
        pytest.param("sma_crossover", {"short_window": 2, "long_window": 3}, 3, id="sma_crossover_long_window"),
        # At the default EMA5 / EMA10 lengths the fixed RSI14 governs at 15 samples.
        pytest.param("ema_crossover_signal", {}, 15, id="fixed_period_program_declares_a_constant_requirement_and_stays_sweepable"),
        # The EMA lengths are parameters (#2696): EMA(n) is ready at n samples, RSI(14) at 15.
        pytest.param("ema_crossover_signal", {"fast_period": 5, "slow_period": 10}, 15, id="ema_default_point_stated_explicitly"),
        pytest.param("ema_crossover_signal", {"fast_period": 2, "slow_period": 14}, 15, id="ema_rsi14_still_governs_below_15"),
        pytest.param("ema_crossover_signal", {"fast_period": 5, "slow_period": 15}, 15, id="ema15_and_rsi14_ready_together"),
        pytest.param("ema_crossover_signal", {"fast_period": 20, "slow_period": 30}, 30, id="ema_20_30"),
        pytest.param("ema_crossover_signal", {"fast_period": 30, "slow_period": 40}, 40, id="ema_slowest_legal_length_governs"),
    ],
)
def test_probe_reproduces_the_reviewed_readiness_thresholds(strategy_key: str, params: dict, first_ready: int) -> None:
    probe = probe_warmup_samples(strategy_key, {"symbol": "SPY", **params})

    assert probe.first_ready_sample == first_ready, "the first ready sample is the reviewed readiness threshold"
    # One prior-state bar on top, so a crossover can be a fresh one on the first scored bar.
    assert probe.required_samples == first_ready + 1, "the requirement is the first ready sample plus one prior-state bar"
    assert probe.bar_span_ms == FIFTEEN_MIN_MS, "the decision cadence is fifteen minutes"


def test_probe_ignores_the_hold_which_never_delays_readiness() -> None:
    held = probe_warmup_samples("ema_crossover_signal", {"symbol": "SPY", "hold_bars": 26})

    assert held.required_samples == probe_warmup_samples("ema_crossover_signal", {"symbol": "SPY"}).required_samples


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


@pytest.mark.parametrize(
    ("requested_start", "requested_end", "required_samples", "bar_span_ms", "run_up_sessions", "evaluation_start"),
    [
        # 30 fifteen-minute bars: one full session holds 26, so two sessions are consumed.
        pytest.param(
            WINDOW[0],
            WINDOW[1],
            30,
            FIFTEEN_MIN_MS,
            2,
            expected_sessions(*WINDOW)[2],
            id="run_up_is_carved_from_the_front_when_no_earlier_history_exists",
        ),
        # Thanksgiving week 2024: Wed 11-27 (26 bars), Thu closed, Fri 11-29 closes 13:00 ET (14 bars).
        # 41 required: 26 + 14 = 40 is not enough; a full Friday (26) would have been.
        pytest.param(
            date(2024, 11, 27),
            date(2024, 12, 6),
            41,
            FIFTEEN_MIN_MS,
            3,
            date(2024, 12, 3),
            id="an_early_close_contributes_fewer_bars",
        ),
        pytest.param(
            WINDOW[0],
            WINDOW[1],
            3,
            DAY_MS,
            3,
            expected_sessions(*WINDOW)[3],
            id="daily_cadence_counts_one_bar_per_session",
        ),
    ],
)
def test_run_up_is_carved_from_the_front_when_no_earlier_history_exists(
    tmp_path: Path,
    requested_start: date,
    requested_end: date,
    required_samples: int,
    bar_span_ms: int,
    run_up_sessions: int,
    evaluation_start: date,
) -> None:
    plan = plan_run_up(
        symbol="SPY",
        requested_start=requested_start,
        requested_end=requested_end,
        required_samples=required_samples,
        bar_span_ms=bar_span_ms,
        roots=[tmp_path],
    )

    assert plan.carved_from_range is True, "the run-up is carved from the requested range"
    assert plan.data_start == requested_start, "data starts at the requested start"
    assert plan.run_up_sessions == run_up_sessions, "the run-up consumes the expected number of sessions"
    assert plan.evaluation_start == evaluation_start, "evaluation starts after the run-up sessions"
    assert plan.evaluation_end == requested_end, "evaluation ends at the requested end"


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


# ── Slowest-candidate probe (bounded) ────────────────────────────────────


def test_slowest_probe_probes_every_candidate_while_the_grid_fits_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the budget nothing is assumed: every assignment is measured, interactions included."""
    from app.research.sweep import warmup
    from app.research.sweep.grid import ValueListRange

    seen: list[dict] = []
    real = warmup.probe_warmup_samples
    monkeypatch.setattr(warmup, "probe_warmup_samples", lambda key, params: (seen.append(dict(params)), real(key, params))[1])
    ranges = {
        "window": ValueListRange((14.0, 20.0, 26.0)),
        "oversold": ValueListRange((20.0, 30.0)),
        "overbought": ValueListRange((70.0, 80.0)),
        "resolution_minutes": ValueListRange((15.0,)),
    }
    result = warmup.slowest_warmup_probe("rsi_mean_reversion", "SPY", ranges)

    assert result.bounded is False and result.probed_candidates == 12 == len(seen)
    assert result.probe == real("rsi_mean_reversion", {"window": 26.0, "oversold": 20.0, "overbought": 70.0, "resolution_minutes": 15.0, "symbol": "SPY"})


def test_slowest_probe_uses_the_relevance_shortcut_only_past_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thresholds never change readiness, so past the budget a 3 × 5 × 5 grid costs seven probes, not seventy-five."""
    from app.research.sweep import warmup
    from app.research.sweep.grid import ValueListRange

    monkeypatch.setattr(warmup, "PROBE_BUDGET", 10)
    seen: list[dict] = []
    real = warmup.probe_warmup_samples
    monkeypatch.setattr(warmup, "probe_warmup_samples", lambda key, params: (seen.append(dict(params)), real(key, params))[1])
    ranges = {
        "window": ValueListRange((14.0, 20.0, 26.0)),
        "oversold": ValueListRange((20.0, 25.0, 30.0, 35.0, 40.0)),
        "overbought": ValueListRange((60.0, 65.0, 70.0, 75.0, 80.0)),
        "resolution_minutes": ValueListRange((15.0,)),
    }
    result = warmup.slowest_warmup_probe("rsi_mean_reversion", "SPY", ranges)

    assert result.probe == real("rsi_mean_reversion", {"window": 26.0, "oversold": 20.0, "overbought": 60.0, "resolution_minutes": 15.0, "symbol": "SPY"})
    assert result.bounded is True
    # baseline + one relevance probe per swept parameter (3) + the relevant grid (window alone: 3 values)
    assert result.probed_candidates == 1 + 3 + 3 == len(seen)
    assert {p["window"] for p in seen} == {14.0, 20.0, 26.0}


def test_slowest_probe_falls_back_to_the_extreme_assignment_past_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.sweep import warmup
    from app.research.sweep.grid import LowHighStepRange, ValueListRange

    monkeypatch.setattr(warmup, "PROBE_BUDGET", 2)
    ranges = {
        # The short window never moves readiness while the long one is larger, so it is not relevant.
        "short_window": LowHighStepRange(low=2.0, high=6.0, step=1.0),
        "long_window": LowHighStepRange(low=10.0, high=12.0, step=1.0),  # 3 relevant values > budget of 2
        "resolution_minutes": ValueListRange((15.0,)),
    }
    result = warmup.slowest_warmup_probe("sma_crossover", "SPY", ranges)

    assert result.bounded is True
    # Only the extreme of the relevant grid is probed beyond the relevance pass: long window 12 at the baseline short window.
    assert result.probe == probe_warmup_samples("sma_crossover", {"short_window": 2.0, "long_window": 12.0, "resolution_minutes": 15.0, "symbol": "SPY"})
    assert result.probed_candidates == 1 + 2 + 1
