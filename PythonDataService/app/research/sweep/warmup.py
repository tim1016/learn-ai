"""Research run-ups use the same indicator convergence counts as live bots.

Formula: required samples = max of the program's indicator convergence counts;
  at least one decision bar, then whole sessions from the canonical calendar.
Reference: issue #2857 (fixed 0.1% memory-decay convergence).
Canonical implementation: indicator counts in app/engine/indicators/;
  calendar run-up planning in this file.
Validated against: tests/research/sweep/test_warmup.py.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from itertools import product
from pathlib import Path

from app.engine.data.availability import Resolution, check_availability
from app.engine.strategy.params import decision_timeframe_ms_for
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import (
    session_open_ms_utc,
    session_start_for_bar_count,
    session_windows_ms_utc,
)
from app.research.sweep.grid import ParamRange, expand_param

_ONE_DAY_MS = 24 * 60 * 60 * 1000


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


def probe_warmup_samples(strategy_key: str, params: Mapping[str, object]) -> WarmupProbe:
    """Read the initialized program's requirements without a synthetic price probe."""
    return _probe_cached(strategy_key, tuple(sorted((str(k), _hashable(v)) for k, v in params.items())))


PROBE_BUDGET = 512


@dataclass(frozen=True)
class SlowestProbe:
    """The slowest candidate's convergence and how it was found."""

    probe: WarmupProbe
    probed_candidates: int
    # False: every candidate was probed. True: the grid exceeded PROBE_BUDGET and the relevance shortcut
    # (monotone, independent convergence) chose which candidates to probe.
    bounded: bool


def _span_key(probe: WarmupProbe) -> tuple[int, int]:
    """Slowest means the longest run-up in time, then in bars (cadences may differ across a grid)."""
    return (probe.required_samples * probe.bar_span_ms, probe.required_samples)


def slowest_warmup_probe(strategy_key: str, symbol: str, param_ranges: Mapping[str, ParamRange]) -> SlowestProbe:
    """The slowest candidate's convergence, exactly when the grid is small and bounded when it is not.

    Up to ``PROBE_BUDGET`` candidates every assignment is probed: no
    assumption about which parameters move convergence, or about how they
    interact, is made. Past the budget the work is bounded: the baseline
    (every parameter at its smallest value) and each swept parameter alone
    at its largest value identify the parameters that move convergence; every
    combination of those is probed with the rest at baseline, or — past the
    budget again — only the extreme assignment. That path assumes convergence
    is monotone in each relevant parameter and that relevance does not depend
    on another parameter's value; it is true of lookbacks and the decision
    cadence, the settings that move convergence today, and the receipt records
    that the bounded path was taken (``bounded``).
    """
    values = {name: sorted(expand_param(spec)) for name, spec in param_ranges.items()}
    baseline = {name: vals[0] for name, vals in values.items()}

    def _probe(assignment: Mapping[str, float]) -> WarmupProbe:
        return probe_warmup_samples(strategy_key, {**assignment, "symbol": symbol})

    swept = [name for name, vals in values.items() if len(vals) > 1]
    if math.prod(len(values[name]) for name in swept) <= PROBE_BUDGET:
        assignments = [dict(zip(swept, combo, strict=True)) for combo in product(*(values[name] for name in swept))]
        probes = [_probe({**baseline, **assignment}) for assignment in assignments]
        return SlowestProbe(probe=max(probes, key=_span_key), probed_candidates=len(probes), bounded=False)

    base = _probe(baseline)
    relevant = [name for name in swept if _span_key(_probe({**baseline, name: values[name][-1]})) != _span_key(base)]
    if math.prod(len(values[name]) for name in relevant) <= PROBE_BUDGET:
        assignments = [dict(zip(relevant, combo, strict=True)) for combo in product(*(values[name] for name in relevant))]
    else:
        assignments = [{name: values[name][-1] for name in relevant}]
    probes = [base, *(_probe({**baseline, **assignment}) for assignment in assignments)]
    return SlowestProbe(probe=max(probes, key=_span_key), probed_candidates=1 + len(swept) + len(assignments), bounded=True)


def _hashable(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


@lru_cache(maxsize=8192)
def _probe_cached(strategy_key: str, assignment: tuple[tuple[str, object], ...]) -> WarmupProbe:
    params = dict(assignment)
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise WarmupProbeError(f"unknown strategy {strategy_key!r}")
    contract = registration.signal_program_contract
    if contract is None:
        raise WarmupProbeError(f"strategy {strategy_key!r} registers no signal program and cannot be swept")
    validated = registration.param_schema.model_validate(params)
    series = contract.resolved_signals(validated)
    return WarmupProbe(
        strategy_key=strategy_key,
        first_ready_sample=max((item.warmup_bars for item in series), default=1),
        required_samples=max(1, max((item.bars_to_converge or 0 for item in series), default=0)),
        bar_span_ms=decision_timeframe_ms_for(validated, qualified_ms=contract.decision_timeframe_ms),
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
