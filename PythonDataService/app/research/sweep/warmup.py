"""Warmup requirement and run-up planning for a parameter sweep.

A sweep is a comparison between candidates. Run cold, a 200-period setting
spends its first 200 bars without a usable indicator while a 5-period
setting spends five, so the leaderboard tilts toward short lookbacks for a
reason that has nothing to do with the strategy. Every cell therefore runs
behind the same run-up, sized for the slowest candidate in the grid.

Formula:
  * ``required_samples(candidate)`` — the number of emitted decision-cadence
    bars after which the program's own ``evaluate_signal_bar`` first reports
    ``ready``, plus one. The probe feeds a deterministic synthetic price path
    through the real program object, so cascaded dependencies (MACD's signal
    line starts only once the slow EMA is ready), off-by-one readiness (RSI
    needs ``period + 1``), doubled windows (ADX needs ``2 x period``), and
    fixed periods (the EMA program declares no lookback parameter at all) are
    all measured rather than declared. The extra sample is the prior-state
    bar a crossover comparison needs before its first decision can differ
    from "seed".
  * ``plan_run_up`` — converts the grid's maximum requirement to calendar
    history through the canonical trading calendar, counting whole scheduled
    spans per session (so an early close contributes fewer bars and a daily
    cadence contributes one per session). Sessions before the requested
    range are used when the lake holds them; otherwise the run-up is carved
    from the front of the range and scoring starts later. A requirement the
    range cannot satisfy is refused.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Run-up
  before the window" and review finding F02 (the readiness table:
  RSI(26) -> 27 samples, ADX(200) -> 400, MACD 12/500/200 -> 699).
Canonical implementation: this file.
Validated against: tests/research/sweep/test_warmup.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from itertools import product
from pathlib import Path

from app.engine.data.availability import Resolution, check_availability
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import (
    session_open_ms_utc,
    session_start_for_bar_count,
    session_windows_ms_utc,
)
from app.research.sweep.grid import ParamRange, expand_param

# A program that is not ready after this many decision bars has no finite
# requirement this feature can size a run-up for.
MAX_PROBE_SAMPLES = 10_000
_ONE_DAY_MS = 24 * 60 * 60 * 1000
# 2024-01-02 09:30 ET: any real session open works; the probe never touches
# the calendar, it only needs monotonic bar clocks of the right width.
_PROBE_EPOCH_MS = 1_704_205_800_000


class WarmupProbeError(ValueError):
    """The strategy's warmup requirement could not be measured."""


class RunUpExceedsRangeError(ValueError):
    """The requested range cannot hold the run-up and still leave bars to score."""


@dataclass(frozen=True)
class WarmupProbe:
    strategy_key: str
    first_ready_sample: int
    required_samples: int
    bar_span_ms: int


@dataclass(frozen=True)
class RunUpPlan:
    """The interval table a primed sweep will execute (PRD #1926 F11)."""

    symbol: str
    requested_start: date
    requested_end: date
    data_start: date
    evaluation_start: date
    evaluation_end: date
    required_samples: int
    bar_span_ms: int
    run_up_sessions: int
    carved_from_range: bool

    @property
    def is_primed(self) -> bool:
        return self.data_start < self.evaluation_start


def _probe_bar(symbol: str, sample: int, bar_span_ms: int) -> TradeBar:
    """A deterministic, gently varying bar so no indicator sees a degenerate path."""
    start_ms = _PROBE_EPOCH_MS + (sample - 1) * bar_span_ms
    price = Decimal("500") + Decimal(str(round(6 * math.sin(sample / 5.0) + (sample % 3), 2)))
    return TradeBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + bar_span_ms,
        open=price,
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        volume=1_000,
    )


def probe_warmup_samples(strategy_key: str, params: Mapping[str, object]) -> WarmupProbe:
    """Measure how many decision bars ``strategy_key`` needs before it is ready.

    Constructs the registered program from validated ``params`` exactly as a
    backtest would, initializes it against a bare context, and feeds
    synthetic bars into ``evaluate_signal_bar`` — the pure decision seam that
    never emits an intent — until its decision reports ``ready``. The probe
    is deterministic in its inputs, so results are memoized per full
    assignment: a form re-preflighting on every edit, or a study probing the
    same grid for every fold, pays for each distinct candidate once.
    """
    return _probe_cached(strategy_key, tuple(sorted((str(k), _hashable(v)) for k, v in params.items())))


PROBE_BUDGET = 512


@dataclass(frozen=True)
class SlowestProbe:
    """The slowest candidate's readiness and how it was found."""

    probe: WarmupProbe
    probed_candidates: int
    # True when the readiness-relevant grid exceeded PROBE_BUDGET and only its extreme was probed.
    bounded: bool


def _span_key(probe: WarmupProbe) -> tuple[int, int]:
    """Slowest means the longest run-up in time, then in bars (cadences may differ across a grid)."""
    return (probe.required_samples * probe.bar_span_ms, probe.required_samples)


def slowest_warmup_probe(strategy_key: str, symbol: str, param_ranges: Mapping[str, ParamRange]) -> SlowestProbe:
    """The slowest candidate's readiness without probing every candidate.

    Most settings (thresholds, sizes) never move readiness; lookbacks and the
    decision cadence do. So: probe the baseline (every parameter at its
    smallest value), then each swept parameter alone at its largest value to
    learn which ones change the answer, then every combination of those with
    the rest at baseline. Past ``PROBE_BUDGET`` combinations only the extreme
    assignment (every relevant parameter at its largest value) is probed:
    readiness is monotone in a lookback, so the extreme is the slowest. The
    result says which path was taken.
    """
    values = {name: sorted(expand_param(spec)) for name, spec in param_ranges.items()}
    baseline = {name: vals[0] for name, vals in values.items()}

    def _probe(assignment: Mapping[str, float]) -> WarmupProbe:
        return probe_warmup_samples(strategy_key, {**assignment, "symbol": symbol})

    base = _probe(baseline)
    swept = [name for name, vals in values.items() if len(vals) > 1]
    relevant = [name for name in swept if _span_key(_probe({**baseline, name: values[name][-1]})) != _span_key(base)]
    combinations = math.prod(len(values[name]) for name in relevant)
    if combinations <= PROBE_BUDGET:
        assignments = [dict(zip(relevant, combo, strict=True)) for combo in product(*(values[name] for name in relevant))]
        bounded = False
    else:
        assignments = [{name: values[name][-1] for name in relevant}]
        bounded = True
    probes = [base, *(_probe({**baseline, **assignment}) for assignment in assignments)]
    return SlowestProbe(probe=max(probes, key=_span_key), probed_candidates=1 + len(swept) + len(assignments), bounded=bounded)


def _hashable(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


@lru_cache(maxsize=8192)
def _probe_cached(strategy_key: str, assignment: tuple[tuple[str, object], ...]) -> WarmupProbe:
    params = dict(assignment)
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise WarmupProbeError(f"unknown strategy {strategy_key!r}")
    factory = registration.signal_program_factory
    if factory is None:
        raise WarmupProbeError(
            f"strategy {strategy_key!r} registers no signal program; its readiness cannot be measured, "
            "so it cannot be swept"
        )
    validated = registration.param_schema.model_validate(params)
    program = factory(validated)
    strategy = program.strategy
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal(100_000)))
    program.activate_for_backtest()
    strategy.initialize()
    symbol = str(getattr(validated, "symbol", "SPY")).upper()
    bar_span_ms = program.session.timeframe_ms
    for sample in range(1, MAX_PROBE_SAMPLES + 1):
        decision = strategy.evaluate_signal_bar(_probe_bar(symbol, sample, bar_span_ms))
        if decision.ready:
            return WarmupProbe(
                strategy_key=strategy_key,
                first_ready_sample=sample,
                required_samples=sample + 1,
                bar_span_ms=bar_span_ms,
            )
    raise WarmupProbeError(
        f"strategy {strategy_key!r} with {params!r} was not ready after {MAX_PROBE_SAMPLES} decision bars"
    )


def _completed_spans(open_ms: int, close_ms: int, bar_span_ms: int) -> int:
    """Whole decision bars a scheduled session emits; one per session at daily cadence."""
    if bar_span_ms >= _ONE_DAY_MS:
        return 1
    return max(0, (close_ms - open_ms) // bar_span_ms)


def plan_run_up(
    *,
    symbol: str,
    requested_start: date,
    requested_end: date,
    required_samples: int,
    bar_span_ms: int,
    roots: Sequence[Path],
    resolution: Resolution = "minute",
) -> RunUpPlan:
    """Decide where a primed sweep reads from and where scoring starts.

    Prefers history before ``requested_start`` when the lake already holds
    every session the run-up needs, so no requested coverage is lost.
    Otherwise carves the run-up from the front of the range and moves the
    evaluation start forward by whole sessions.
    """
    if required_samples < 1:
        raise ValueError("required_samples must be positive")
    if requested_end < requested_start:
        raise ValueError("requested_end must not precede requested_start")

    sessions = session_windows_ms_utc(requested_start, requested_end)
    if not sessions:
        raise RunUpExceedsRangeError(f"no trading session between {requested_start} and {requested_end}")

    first_open_ms = session_open_ms_utc(sessions[0].session_date)
    daily = bar_span_ms >= _ONE_DAY_MS
    earliest_prior = session_start_for_bar_count(
        first_open_ms,
        target_bars=required_samples,
        bar_span_ms=None if daily else bar_span_ms,
    )
    prior_end = sessions[0].session_date - timedelta(days=1)
    prior = check_availability(roots, symbol, earliest_prior, prior_end, resolution=resolution)
    if prior.is_complete and prior.expected_days > 0:
        return RunUpPlan(
            symbol=symbol,
            requested_start=requested_start,
            requested_end=requested_end,
            data_start=earliest_prior,
            evaluation_start=requested_start,
            evaluation_end=requested_end,
            required_samples=required_samples,
            bar_span_ms=bar_span_ms,
            run_up_sessions=prior.expected_days,
            carved_from_range=False,
        )

    accumulated = 0
    for index, window in enumerate(sessions):
        accumulated += _completed_spans(window.open_ms_utc, window.close_ms_utc, bar_span_ms)
        if accumulated >= required_samples:
            if index + 1 >= len(sessions):
                break
            return RunUpPlan(
                symbol=symbol,
                requested_start=requested_start,
                requested_end=requested_end,
                data_start=requested_start,
                evaluation_start=sessions[index + 1].session_date,
                evaluation_end=requested_end,
                required_samples=required_samples,
                bar_span_ms=bar_span_ms,
                run_up_sessions=index + 1,
                carved_from_range=True,
            )
    raise RunUpExceedsRangeError(
        f"the slowest setting needs {required_samples} decision bars of run-up, which consumes every session "
        f"between {requested_start} and {requested_end}; widen the range or shorten the lookbacks"
    )
