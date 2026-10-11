"""The frozen backtest baseline: qualification corpus, harness, and report (#3034 slice 1).

This module is PRD #3034's measurement foundation; it optimizes nothing. It
freezes, deterministically, the evaluation identities every later executor
must answer for, runs today's serial execution path over them, and reports
machine-readable timings and hashes next to the exact environment they were
produced under.

Corpus
------
Exactly 1,000 distinct full evaluation keys — 200 over the full development
window, 800 spread across its calendar-derived fold windows — across several
strategy/parameter groups chosen to include runs that trade heavily and runs
that place no trade at all. Windows come from the canonical calendar (never
hardcoded), parameter groups are declared here, and the identity assignment
is a seeded, order-stable draw, so the same inputs regenerate the same
manifest byte for byte. Inputs are the local lake only: every artifact is
admitted and checksummed through the data snapshot before any run, and no
provider or broker is contacted.

Modes
-----
``baseline`` runs the full corpus; the operator fires it manually and its
outputs live as issue evidence on #3036, never merged. The quick-tier smoke
check runs the same generator scaled down over a seeded fixture lake and
fails on any drift in identity count, corpus digest or result hash. Reports
distinguish the execution modes later slices must compare —
``serial-original`` (today's path), ``serial-same-resource`` and
``serial-decoded-cache`` (the future executors; fields present,
unpopulated) — and warm versus preparation-inclusive timing, which counts
worker startup and imports.

Timing vocabulary: an *additive* timing partitions the run (fetch,
consolidate, simulate, aggregate, persist — they sum toward the total); a
*nested* timing sits inside one additive span (admission, decoding, queue
wait and IPC live inside fetch/simulate). The serial path can only observe
the additive boundaries today; the nested fields exist so later executors
populate them against this same corpus without changing it.

The completed-evaluation cache has no entry in baseline throughput: every
corpus identity is a fresh evaluation key, so a cache hit in a baseline run
is a fault to report, never throughput.

Canonical implementation: this file.
Validated against: tests/research/test_benchmark.py.
"""

from __future__ import annotations

import os
import platform
import random
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from app.research.golden_search.declarations import point_hash as hash_point
from app.research.golden_search.evaluator import (
    BASE_SCENARIO,
    ExecutionAssumptions,
    context_digest,
    evaluation_key,
    execution_context,
)
from app.research.grid_search.service import window_dates
from app.research.runs.hashing import hash_payload
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.research.sweep.snapshot import DataSnapshot, capture_data_snapshot
from app.utils.session_anchors import et_midnight_ms

CORPUS_FULL_DEVELOPMENT_IDENTITIES = 200
CORPUS_FOLD_IDENTITIES = 800
CORPUS_FOLD_COUNT = 8
CORPUS_SEED = 20261010
BASELINE_REPETITIONS = 3
CONNECTION_INVENTORY_RESEARCH_CAP = 12
CONNECTION_INVENTORY_REQUIRED_FREE = 20
# The PostgreSQL server limit the inventory checks against when the operator
# has not supplied the deployed one. Reported as an assumption, never repaired.
ASSUMED_SERVER_MAX_CONNECTIONS = 100

WindowKind = Literal["full-development", "fold"]


@dataclass(frozen=True)
class ParameterGroup:
    """One declared strategy/parameter group of the corpus.

    ``expectation`` is the freeze-time intent — ``trade-heavy`` runs must
    produce trades, ``no-trade`` runs must produce none — and the baseline
    report reconciles the runs against it rather than trusting the label.
    """

    strategy: str
    label: str
    params: dict[str, Any]
    expectation: Literal["trade-heavy", "no-trade"]


# Declared groups over the two registered signal programs the research paths
# drive. A no-trade group is part of the contract: a corpus of only
# trade-heavy runs would never exercise the empty-trade-list projections the
# serial path must keep byte-identical.
PARAMETER_GROUPS: tuple[ParameterGroup, ...] = (
    ParameterGroup("sma_crossover", "sma-fast", {"short_window": 2, "long_window": 3, "resolution_minutes": 1}, "trade-heavy"),
    ParameterGroup("sma_crossover", "sma-typical", {"short_window": 10, "long_window": 30, "resolution_minutes": 15}, "trade-heavy"),
    # ``sma-no-trade`` demands 900 *daily* bars of warmup — more history
    # than any window the corpus holds — so the run scores a curve and
    # places no trade. A no-trade group is part of the contract: a corpus of
    # only trade-heavy runs would never exercise the empty-trade-list
    # projections the serial path must keep byte-identical.
    ParameterGroup("sma_crossover", "sma-no-trade", {"short_window": 10, "long_window": 900, "resolution_minutes": 1440}, "no-trade"),
    ParameterGroup(
        "ema_crossover_signal",
        "ema-fast",
        {"gap": 0.0, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0, "fast_period": 5.0, "slow_period": 10.0, "hold_bars": 2.0},
        "trade-heavy",
    ),
    ParameterGroup(
        "ema_crossover_signal",
        "ema-no-trade",
        {"gap": 0.5, "gap_bps": 0.0, "rsi_min": 99.0, "rsi_max": 100.0, "fast_period": 5.0, "slow_period": 10.0, "hold_bars": 2.0},
        "no-trade",
    ),
)

# Additive spans the serial path's phase callbacks mark; ``nested`` fields are
# populated by later executors and the serial report carries them as None.
ADDITIVE_PHASES: tuple[str, ...] = ("fetch", "consolidate", "simulate", "aggregate", "persist")
NESTED_PHASES: tuple[str, ...] = ("admission", "decoding", "queue", "ipc")
TIMING_KINDS: dict[str, str] = {**{name: "additive" for name in ADDITIVE_PHASES}, **{name: "nested" for name in NESTED_PHASES}}

EXECUTION_MODES: tuple[str, ...] = ("serial-original", "serial-same-resource", "serial-decoded-cache")

# The engine's ``on_phase`` vocabulary mapped onto the additive spans.
_PHASE_MAP: dict[str, str] = {
    "fetching_data": "fetch",
    "consolidating_bars": "consolidate",
    "running_indicators": "simulate",
    "aggregating_results": "aggregate",
    "persisting": "persist",
}


@dataclass(frozen=True)
class CorpusIdentity:
    """One frozen evaluation: a full evaluation key over one window with one parameter group."""

    evaluation_key: str
    point_hash: str
    strategy: str
    group: str
    expectation: str
    params: dict[str, Any]
    window_start_ms: int
    window_end_ms: int
    window_kind: WindowKind
    fold_index: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluation_key": self.evaluation_key,
            "point_hash": self.point_hash,
            "strategy": self.strategy,
            "group": self.group,
            "expectation": self.expectation,
            "params": self.params,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "window_kind": self.window_kind,
            "fold_index": self.fold_index,
        }


@dataclass(frozen=True)
class BenchmarkCorpus:
    """The frozen manifest: identities, their windows, and the corpus digest that pins them."""

    identities: tuple[CorpusIdentity, ...]
    development: tuple[int, int]
    fold_windows: tuple[tuple[int, int], ...]
    context_digest: str
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "context_digest": self.context_digest,
            "development_ms": list(self.development),
            "fold_windows_ms": [list(window) for window in self.fold_windows],
            "count": len(self.identities),
            "full_development_count": sum(1 for item in self.identities if item.window_kind == "full-development"),
            "fold_count": sum(1 for item in self.identities if item.window_kind == "fold"),
            "identities": [item.as_dict() for item in self.identities],
        }


def fold_windows(development: tuple[date, date], folds: int) -> tuple[tuple[tuple[int, int], int], ...]:
    """Split the development window's calendar sessions into ``folds`` contiguous windows.

    The calendar is the only source of the split: a fold is a contiguous span
    of NYSE sessions anchored at ET midnight (ADR 0022), so the corpus's
    windows move when the calendar moves and never otherwise.
    """
    from app.lean_sidecar.trading_calendar import expected_sessions

    sessions = list(expected_sessions(*development))
    if len(sessions) < folds:
        raise ValueError(f"the development window holds {len(sessions)} sessions, fewer than the {folds} folds requested")
    bounds = [round(index * len(sessions) / folds) for index in range(folds + 1)]
    return tuple(
        ((et_midnight_ms(sessions[bounds[index]]), et_midnight_ms(sessions[bounds[index + 1] - 1]) + 24 * 60 * 60 * 1000), index)
        for index in range(folds)
    )


def vary_params(group: ParameterGroup, variation: int, *, symbol: str) -> dict[str, Any]:
    """The group's parameters with its variation knobs advanced.

    The map ``variation → params`` is injective over the corpus's ordinal
    range (no modulo wraparound: the ordinal is split across two legal
    knobs), and never changes the group's expectation — a trade-heavy
    variation still trades, a no-trade variation still cannot. Distinct
    variations are what keep same-window identities distinct evaluation
    keys.
    """
    params = {"symbol": symbol, **group.params}
    if group.strategy == "sma_crossover" and group.expectation == "trade-heavy":
        short = 2 + variation % 250
        params["short_window"] = short
        params["long_window"] = short + 1 + variation // 250
    elif group.strategy == "sma_crossover":
        params["short_window"] = 10 + variation // 100
        params["long_window"] = group.params["long_window"] + variation % 100
    elif group.expectation == "trade-heavy":
        params["gap"] = round(0.05 * (1 + variation % 20), 2)
        params["hold_bars"] = float(2 + variation // 20)
    else:
        params["gap"] = round(0.5 + 0.05 * (variation % 10), 2)
        params["hold_bars"] = float(2 + variation // 10)
    return params


def build_corpus(
    *,
    symbol: str,
    development: tuple[date, date],
    data_snapshot_digest: str,
    code_identity: CodeIdentity | None = None,
    seed: int = CORPUS_SEED,
    full_development_count: int = CORPUS_FULL_DEVELOPMENT_IDENTITIES,
    fold_count: int = CORPUS_FOLD_IDENTITIES,
    folds: int = CORPUS_FOLD_COUNT,
) -> BenchmarkCorpus:
    """Regenerate the frozen manifest from its seeds; the same inputs yield the same identities byte for byte."""
    identity = code_identity if code_identity is not None else resolve_code_identity()
    assumptions = ExecutionAssumptions()
    context = execution_context(
        strategy_key="benchmark-corpus",
        program_version=None,
        parameter_schema_version=None,
        code_identity={
            "source_digest": identity.source_digest,
            "environment_digest": identity.environment_digest,
            "digest_scheme": identity.digest_scheme,
            "tree_state": identity.tree_state,
        },
        data_snapshot_digest=data_snapshot_digest,
        execution=assumptions,
        data_policy={"source": "local-lake", "adjusted": True, "session": "regular"},
        run_up={"required_samples": 16, "bar_span_ms": 900_000, "run_up_sessions": 1},
    )
    digest = context_digest(context)

    full_window = (et_midnight_ms(development[0]), et_midnight_ms(development[1]) + 24 * 60 * 60 * 1000)
    folds_with_index = fold_windows(development, folds)
    assignments: list[tuple[tuple[int, int], WindowKind, int | None]] = [
        (full_window, "full-development", None)
    ] * full_development_count
    index = 0
    while len(assignments) < full_development_count + fold_count:
        window, fold_index = folds_with_index[index % len(folds_with_index)]
        assignments.append((window, "fold", fold_index))
        index += 1

    # The seeded draw picks groups; the ordinal-within-group varies the
    # parameters. The ordinal is structural: the n-th draw of a group varies
    # differently from every other draw of it, so two assignments of one
    # group over one window can never collapse into one evaluation key.
    draw = random.Random(seed)
    ordinal: dict[str, int] = {}
    identities: list[CorpusIdentity] = []
    for window, kind, fold_index in assignments:
        group = draw.choice(PARAMETER_GROUPS)
        variation = ordinal.get(group.label, 0)
        ordinal[group.label] = variation + 1
        params = vary_params(group, variation, symbol=symbol)
        point = {"symbol": symbol, **params}
        identities.append(
            CorpusIdentity(
                evaluation_key=evaluation_key(digest, hash_point(group.strategy, point), window, BASE_SCENARIO, False),
                point_hash=hash_point(group.strategy, point),
                strategy=group.strategy,
                group=group.label,
                expectation=group.expectation,
                params=params,
                window_start_ms=window[0],
                window_end_ms=window[1],
                window_kind=kind,
                fold_index=fold_index,
            )
        )
    corpus = BenchmarkCorpus(
        identities=tuple(identities),
        development=full_window,
        fold_windows=tuple(window for window, _ in folds_with_index),
        context_digest=digest,
        digest=hash_payload([item.as_dict() for item in identities]),
    )
    if len({item.evaluation_key for item in corpus.identities}) != len(corpus.identities):
        raise RuntimeError("the corpus generator produced duplicate evaluation keys")
    return corpus


@dataclass
class IdentityRun:
    """One identity's measured execution: timings by kind, hashes, trade accounting."""

    evaluation_key: str
    group: str
    window_kind: str
    repetition: int
    total_ms: float
    additive_ms: dict[str, float] = field(default_factory=dict)
    nested_ms: dict[str, float | None] = field(default_factory=lambda: {name: None for name in NESTED_PHASES})
    bars_scored: int = 0
    trades: int = 0
    result_hash: str = ""
    trade_log_hash: str = ""
    metrics_hash: str = ""
    expectation_held: bool | None = None


@dataclass
class BaselineReport:
    """The machine-readable baseline: environment identity, per-run records, medians, variability."""

    execution_mode: str
    timing_mode: Literal["warm", "preparation-inclusive"]
    environment: dict[str, Any]
    corpus: dict[str, Any]
    runs: list[IdentityRun] = field(default_factory=list)
    medians_ms: dict[str, float] = field(default_factory=dict)
    variability_ms: dict[str, float] = field(default_factory=dict)
    cache_hits: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "timing_mode": self.timing_mode,
            "modes_distinguished": list(EXECUTION_MODES),
            "environment": self.environment,
            "corpus": self.corpus,
            "cache_hits": self.cache_hits,
            "notes": self.notes,
            "medians_ms": self.medians_ms,
            "variability_ms": self.variability_ms,
            "runs": [
                {
                    "evaluation_key": run.evaluation_key,
                    "group": run.group,
                    "window_kind": run.window_kind,
                    "repetition": run.repetition,
                    "total_ms": run.total_ms,
                    "additive_ms": run.additive_ms,
                    "nested_ms": run.nested_ms,
                    "bars_scored": run.bars_scored,
                    "trades": run.trades,
                    "result_hash": run.result_hash,
                    "trade_log_hash": run.trade_log_hash,
                    "metrics_hash": run.metrics_hash,
                    "expectation_held": run.expectation_held,
                }
                for run in self.runs
            ],
        }


def environment_identity(snapshot: DataSnapshot, *, timing_mode: str = "warm") -> dict[str, Any]:
    """The exact code/data/environment identity a baseline run answers for."""
    identity = resolve_code_identity()
    try:
        load = os.getloadavg()
    except OSError:  # pragma: no cover - platforms without loadavg
        load = (0.0, 0.0, 0.0)
    return {
        "code_identity": {
            "source_digest": identity.source_digest,
            "environment_digest": identity.environment_digest,
            "digest_scheme": identity.digest_scheme,
            "tree_state": identity.tree_state,
        },
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "native_threads": threading.active_count(),
        "rss_bytes": _rss_bytes(),
        "competing_load_average": [round(value, 2) for value in load],
        "calendar_identity": snapshot.calendar_identity,
        "calendar_version": snapshot.calendar_version,
        "data_snapshot_digest": snapshot.digest(),
        "data_artifact_checksums": dict(sorted(snapshot.artifacts.items())),
        "timing_mode": timing_mode,
    }


def _rss_bytes() -> int | None:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except (ImportError, OSError):  # pragma: no cover - non-Unix
        return None


def _assumptions_dict() -> dict[str, Any]:
    assumptions = ExecutionAssumptions()
    return {
        "fill_mode": assumptions.fill_mode,
        "commission_per_order": assumptions.commission_per_order,
        "slippage_per_share": assumptions.slippage_per_share,
        "initial_cash": assumptions.initial_cash,
    }


def _engine_request(
    identity: CorpusIdentity, *, symbol: str, execution: Mapping[str, Any], data_policy: Mapping[str, Any] | None = None
) -> Any:
    from app.schemas.engine_backtest import EngineBacktestRequest

    start, end = window_dates(identity.window_start_ms, identity.window_end_ms)
    return EngineBacktestRequest(
        strategy_name=identity.strategy,
        params={"symbol": symbol, **identity.params},
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        fill_mode=execution["fill_mode"],
        commission_per_order=execution["commission_per_order"],
        slippage_per_share=execution["slippage_per_share"],
        initial_cash=execution["initial_cash"],
        resolution="minute",
        save_study=False,
        auto_fetch=False,
        summary_only=False,
        data_policy=dict(data_policy) if data_policy else None,
    )


def _serial_execute(request: Any, on_phase: Callable[[str], None]) -> Any:
    from app.services.engine_backtest_service import execute_engine_backtest

    return execute_engine_backtest(request=request, on_phase=on_phase, on_log=lambda message: None)


def run_identity(
    identity: CorpusIdentity,
    *,
    symbol: str,
    execution: Mapping[str, Any] | None = None,
    data_policy: Mapping[str, Any] | None = None,
    repetition: int = 0,
    execute: Callable[[Any], Any] | None = None,
) -> IdentityRun:
    """Execute one identity on the serial path and measure its additive spans.

    ``execute`` is the injection seam the worker runtime will replace; the
    default is today's ``execute_engine_backtest``. Timings ride the run's
    own phase boundaries — no timers, no sampling.
    """
    execution = _assumptions_dict() if execution is None else dict(execution)
    stamps: dict[str, float] = {}
    order: list[str] = []

    def on_phase(phase: str) -> None:
        mapped = _PHASE_MAP.get(phase)
        if mapped is not None and mapped not in stamps:
            stamps[mapped] = time.perf_counter()
            order.append(mapped)

    request = _engine_request(identity, symbol=symbol, execution=execution, data_policy=data_policy)
    start = time.perf_counter()
    response = (execute or _serial_execute)(request, on_phase)
    total_ms = (time.perf_counter() - start) * 1000

    additive: dict[str, float] = {}
    for position, name in enumerate(order):
        end = stamps[order[position + 1]] if position + 1 < len(order) else start + total_ms / 1000
        additive[name] = round((end - stamps[name]) * 1000, 3)

    trades = response.trades or []
    # The result hash pins the run's scientific projection — ordered trades,
    # the equity curve and the statistics — never the response envelope's
    # wall-clock stamps, so a projection change fails the freeze and a rerun
    # at another moment does not.
    run = IdentityRun(
        evaluation_key=identity.evaluation_key,
        group=identity.group,
        window_kind=identity.window_kind,
        repetition=repetition,
        total_ms=round(total_ms, 3),
        additive_ms=additive,
        bars_scored=len(response.equity_curve or []),
        trades=len(trades),
        result_hash=hash_payload(
            {
                "trades": [trade.model_dump(mode="json") for trade in trades],
                "equity_curve": response.equity_curve or [],
                "statistics": response.statistics,
            }
        ),
        trade_log_hash=hash_payload([trade.model_dump(mode="json") for trade in trades]),
        metrics_hash=hash_payload(response.statistics),
    )
    run.expectation_held = (run.trades > 0) is (identity.expectation == "trade-heavy")
    return run


def run_baseline(
    *,
    roots: Sequence[Path],
    symbol: str,
    development: tuple[date, date],
    execution_mode: str = "serial-original",
    timing_mode: Literal["warm", "preparation-inclusive"] = "warm",
    repetitions: int = BASELINE_REPETITIONS,
    full_development_count: int = CORPUS_FULL_DEVELOPMENT_IDENTITIES,
    fold_count: int = CORPUS_FOLD_IDENTITIES,
    folds: int = CORPUS_FOLD_COUNT,
    seed: int = CORPUS_SEED,
    data_policy: Mapping[str, Any] | None = None,
    execute: Callable[[Any], Any] | None = None,
) -> BaselineReport:
    """Freeze the snapshot, regenerate the corpus, and run it ``repetitions`` times on the serial path.

    Completed-result cache hits cannot occur here — every identity is a fresh
    evaluation key — and one showing up in a future mode is reported as a
    fault in ``notes``, never as throughput.
    """
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"unknown execution mode {execution_mode!r}")
    snapshot = capture_data_snapshot(roots=list(roots), symbol=symbol, resolution="minute", data_start=development[0], data_end=development[1])
    corpus = build_corpus(
        symbol=symbol,
        development=development,
        data_snapshot_digest=snapshot.digest(),
        seed=seed,
        full_development_count=full_development_count,
        fold_count=fold_count,
        folds=folds,
    )
    report = BaselineReport(
        execution_mode=execution_mode,
        timing_mode=timing_mode,
        environment=environment_identity(snapshot, timing_mode=timing_mode),
        corpus={"digest": corpus.digest, "count": len(corpus.identities)},
    )
    if execution_mode != "serial-original":
        report.notes.append(f"{execution_mode} is a future executor; this report carries its fields unpopulated")
    if timing_mode == "preparation-inclusive":
        report.notes.append("preparation-inclusive timings count worker startup and imports; the serial mode attributes none")
    execution = _assumptions_dict()
    for repetition in range(repetitions):
        for identity in corpus.identities:
            report.runs.append(
                run_identity(identity, symbol=symbol, execution=execution, data_policy=data_policy, repetition=repetition, execute=execute)
            )
    report.medians_ms, report.variability_ms = _summarize(report.runs)
    return report


def _summarize(runs: Sequence[IdentityRun]) -> tuple[dict[str, float], dict[str, float]]:
    """Median and variability per additive span and per total, across repetitions of the same identities."""
    spans: dict[str, list[float]] = {}
    for run in runs:
        spans.setdefault("total", []).append(run.total_ms)
        for name, value in run.additive_ms.items():
            spans.setdefault(name, []).append(value)
    medians = {name: round(sorted(values)[len(values) // 2], 3) for name, values in spans.items()}
    variability = {name: round(max(values) - min(values), 3) for name, values in spans.items()}
    return medians, variability


def round_widths(round_payload: Mapping[str, Any]) -> dict[str, int]:
    """One stored Zoom round's widths: what was sampled, testable, eligible and refused."""
    sampled = len(round_payload["values"])
    invalid = len(round_payload["invalid"])
    results = round_payload["results"]
    ineligible = sum(1 for _, code in results if code is not None)
    return {
        "sampled": sampled,
        "invalid": invalid,
        "testable": sampled - invalid,
        "evaluated": len(results),
        "eligible": len(results) - ineligible,
        "ineligible": ineligible,
    }


def protocol_width_histogram(trials: Sequence[Mapping[str, Any]], *, stage: str) -> dict[str, Any]:
    """The per-round width histogram of one stage's stored trials, with the stage's cache accounting.

    Round rows carry the stage and fold labels; the totals reconcile the
    stage's own evaluated/cached/invalid counts from its stop trial, so the
    frozen widths and the budget accounting cannot drift apart.
    """
    rounds: list[dict[str, Any]] = []
    stop: Mapping[str, Any] | None = None
    for trial in trials:
        payload = trial.get("payload") or {}
        if trial.get("kind") == "round":
            rounds.append(
                {
                    "stage": stage,
                    "fold_index": trial.get("fold_index"),
                    "pass_index": payload.get("pass_index"),
                    "knob": payload.get("knob"),
                    "round_index": payload.get("round_index"),
                    **round_widths(payload),
                }
            )
        elif trial.get("kind") == "stop":
            stop = payload
    histogram = {
        "stage": stage,
        "rounds": rounds,
        "totals": {
            "sampled": sum(row["sampled"] for row in rounds),
            "testable": sum(row["testable"] for row in rounds),
            "eligible": sum(row["eligible"] for row in rounds),
        },
    }
    if stop is not None:
        # The stop trial spreads the stage's counts into its payload's top level.
        counts = stop.get("counts") or stop
        histogram["accounting"] = {
            "newly_reserved": counts.get("evaluated"),
            "cached_or_duplicate": counts.get("cached"),
            "invalid": counts.get("invalid"),
            "ready": (counts.get("evaluated") or 0) + (counts.get("cached") or 0),
        }
        histogram["totals"]["reconciled"] = (
            histogram["totals"]["sampled"] - histogram["totals"].get("invalid", 0)
            == histogram["totals"]["testable"]
        )
    return histogram


def connection_inventory(*, research_cap: int = CONNECTION_INVENTORY_RESEARCH_CAP) -> dict[str, Any]:
    """Account for every configured pool against the server's connection budget.

    The research envelope's cap is the PRD's number (12). The research
    persistence layer shares the data-lake catalog's per-loop asyncpg pool
    (max 10 per loop; the API loop and the research writer loop are the two
    deployed). The check verifies the deployment leaves at least
    ``CONNECTION_INVENTORY_REQUIRED_FREE`` server connections free; a
    mismatch is *reported*, never repaired by raising PostgreSQL limits.
    """
    server_max = int(os.environ.get("POSTGRES_SERVER_MAX_CONNECTIONS", ASSUMED_SERVER_MAX_CONNECTIONS))
    catalog_pool_max = _catalog_pool_max()
    deployed_loops = 2  # the API loop and the research writer loop (review F15)
    pools = [
        {"name": "data-lake catalog / research persistence (asyncpg, per loop)", "max_connections": catalog_pool_max, "loops": deployed_loops},
        {"name": "job store (redis)", "max_connections": 32, "server": "redis"},
    ]
    reserved = catalog_pool_max * deployed_loops
    free = server_max - reserved
    return {
        "pools": pools,
        "postgres_server_max_connections": server_max,
        "server_max_is_assumed": "POSTGRES_SERVER_MAX_CONNECTIONS" not in os.environ,
        "research_cap": research_cap,
        "reserved_by_configured_pools": reserved,
        "free_at_server_without_research": free,
        "free_after_research_cap": server_max - reserved - research_cap,
        "required_free": CONNECTION_INVENTORY_REQUIRED_FREE,
        "meets_required_free": free >= CONNECTION_INVENTORY_REQUIRED_FREE,
        "mismatch": None if free >= CONNECTION_INVENTORY_REQUIRED_FREE
        else f"the configured pools leave {free} server connections free, below the required {CONNECTION_INVENTORY_REQUIRED_FREE}",
    }


def _catalog_pool_max() -> int:
    # The pool is created inline with max_size=10 (catalog_client's
    # init_pool); there is no module constant to read, so the inventory
    # pins the deployed number and its source here.
    return 10
