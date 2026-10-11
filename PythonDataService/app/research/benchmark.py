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
hardcoded) and the identity assignment
is a seeded, order-stable draw, so the same inputs regenerate the same
manifest byte for byte. Inputs are the local lake only: every artifact is
admitted and checksummed through the data snapshot before any run, the
snapshot's manifest binds every engine read to the receipted bytes, and no
provider or broker is contacted.

Modes
-----
``serial-original`` — today's path — is the only runnable execution mode;
``serial-same-resource`` and ``serial-decoded-cache`` name the executors
later slices must add, and ``run_baseline`` refuses them until one exists to
supply its own measurements. Likewise ``warm`` is the only runnable timing
mode; ``preparation-inclusive`` (worker startup and imports) exists only
when an executor that has a worker can measure it.

Timing vocabulary: an *additive* timing partitions the run (fetch,
consolidate, simulate, aggregate, persist — they sum toward the total); a
*nested* timing sits inside one additive span (admission, decoding, queue
wait and IPC live inside fetch/simulate). The serial path can only observe
the additive boundaries today; the nested fields exist so later executors
populate them against this same corpus without changing it.

The completed-evaluation cache has no entry in baseline throughput: every
corpus identity is a fresh evaluation key, so a cache hit in a baseline run
is a fault to report, never throughput.

Formula: corpus digest = hash_payload(identities' canonical dicts); per-run
  result/trade-log/metrics hashes = hash_payload over the run's ordered
  trades, equity curve and statistics projections; medians and variability =
  statistics.median over repetitions of the *same* evaluation key (never
  pooled across identities), variability = max − min per key.
Reference: #3036 (this slice's contract), PRD #3034, and the golden-search
  evaluation identity (``app/research/golden_search/evaluator.py``, pinned by
  ``tests/research/golden_search/``) the corpus keys reuse.
Canonical implementation: this file.
Validated against: tests/research/test_benchmark.py.
"""

from __future__ import annotations

import os
import platform
import random
import statistics
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from app.engine.data.decoded_cache import DecodedArtifactCache
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
from app.utils.session_anchors import et_day_end_ms, et_midnight_ms

CORPUS_FULL_DEVELOPMENT_IDENTITIES = 200
CORPUS_FOLD_IDENTITIES = 800
CORPUS_FOLD_COUNT = 8
CORPUS_SEED = 20261010
BASELINE_REPETITIONS = 3
CONNECTION_INVENTORY_RESEARCH_CAP = 12
CONNECTION_INVENTORY_REQUIRED_FREE = 20
# The data policy the corpus freezes into its identities and hands the
# engine when the operator names none: the lake's polygon-captured bars,
# adjusted, regular sessions. ``polygon`` is the capture lineage the
# engine's policy block names; the manifest still binds every read to the
# local lake. The corpus symbol completes the block per run, so one
# constant feeds both the context digest and the request and the identity
# can never disagree with the bytes the run reads.
CORPUS_DATA_POLICY: dict[str, Any] = {
    "source": "polygon",
    "adjusted": True,
    "session": "regular",
    "input_bars": {"timespan": "minute", "multiplier": 1},
    "strategy_bars": {"timespan": "minute", "multiplier": 1},
}


def _corpus_data_policy(symbol: str) -> dict[str, Any]:
    """The frozen corpus policy completed with the corpus's symbol — the engine's policy block demands one."""
    return {**CORPUS_DATA_POLICY, "symbol": symbol}
# The deployed render of the fleet topology: the committed artifact that
# names every deployed service and its VM containment.
FLEET_TOPOLOGY_PATH = Path(__file__).resolve().parents[3] / "deploy" / "fleet" / "topology.snapshot.json"
# The Backend's Npgsql pool when its deployed connection string sets no
# "Max Pool Size" — Npgsql's documented default maximum.
NPGSQL_DEFAULT_MAX_POOL_SIZE = 100

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
    # places no trade.
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

# The modes the report names. The serial originals and the decoded-artifact
# cache run today; ``serial-same-resource`` remains the contract a later
# executor must answer for, refused by ``run_baseline`` until it exists.
EXECUTION_MODES: tuple[str, ...] = ("serial-original", "serial-same-resource", "serial-decoded-cache")
RUNNABLE_EXECUTION_MODES: tuple[str, ...] = ("serial-original", "serial-decoded-cache")
TIMING_MODES: tuple[Literal["warm", "preparation-inclusive"], ...] = ("warm", "preparation-inclusive")
RUNNABLE_TIMING_MODES: tuple[str, ...] = ("warm",)

# Retention bound for the serial decoded-artifact cache (#3038): an estimate
# of the decoded bars one process may hold, sized so the qualification corpus
# (every window's artifacts decoded once) fits resident. Half the deployed
# python-service cgroup memory limit (2 GiB in compose.yaml): the cache must
# share that envelope with the interpreter, the engine and every retained
# run, so the bound alone may never claim the whole limit. One process only —
# the parallel runtime gets its own envelope in later #3034 slices.
DECODED_CACHE_DEFAULT_BYTES = 1 * 1024**3

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
        ((et_midnight_ms(sessions[bounds[index]]), et_day_end_ms(sessions[bounds[index + 1] - 1])), index)
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
    data_policy: Mapping[str, Any] | None = None,
    seed: int = CORPUS_SEED,
    full_development_count: int = CORPUS_FULL_DEVELOPMENT_IDENTITIES,
    fold_count: int = CORPUS_FOLD_IDENTITIES,
    folds: int = CORPUS_FOLD_COUNT,
) -> BenchmarkCorpus:
    """Regenerate the frozen manifest from its seeds; the same inputs yield the same identities byte for byte.

    ``data_policy`` is the policy the runs will execute under — the same
    mapping the harness hands the engine — because it is part of every
    evaluation key's context digest. It defaults to the frozen corpus
    policy (the lake's polygon-captured bars, adjusted); a raw run must
    pass its raw policy or its identities would claim adjusted bytes the
    runs never read.
    """
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
        data_policy=dict(data_policy) if data_policy is not None else _corpus_data_policy(symbol),
        # The run-up claim matches the execution exactly: the corpus
        # evaluates COLD windows. The no-trade group's mechanism is a
        # program that never readies (900 daily bars of demand, more
        # history than any window holds) and still scores a curve — an
        # execution the engine only permits over an unprimed window, since
        # a primed run refuses a program not ready at the evaluation
        # boundary. A primed claim here would key runs that never execute.
        run_up={"required_samples": 0, "bar_span_ms": 900_000, "run_up_sessions": 0},
    )
    digest = context_digest(context)

    full_window = (et_midnight_ms(development[0]), et_day_end_ms(development[1]))
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
    # The run's own projections, ordered: the baseline output carries the
    # trades and the curve themselves, not only their hashes (#3036).
    ordered_trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)


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
    throughput: dict[str, float] = field(default_factory=dict)
    # The decoded-artifact cache's accounting, present only for the mode that
    # ran one (#3038): hit/miss/eviction/uncached counts and resident/pinned
    # bytes. Distinct from ``cache_hits``, which counts completed-evaluation
    # reuse — always zero on these paths, where it would be a fault.
    decoded_cache_metrics: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "timing_mode": self.timing_mode,
            "modes_distinguished": {"execution": list(EXECUTION_MODES), "timing": list(TIMING_MODES)},
            "environment": self.environment,
            "corpus": self.corpus,
            "cache_hits": self.cache_hits,
            **({"decoded_cache": self.decoded_cache_metrics} if self.decoded_cache_metrics is not None else {}),
            "throughput": self.throughput,
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
                    "ordered_trades": run.ordered_trades,
                    "equity_curve": run.equity_curve,
                }
                for run in self.runs
            ],
        }


def _harness_digest() -> str:
    """SHA-256 over the benchmark harness's own bytes — the measurement methodology's identity.

    ``resolve_code_identity`` covers the engine and research paths this
    harness drives, not the harness itself: a timing-accounting, projection
    or summarizer change moves this digest, so a report can never present
    new methodology under the old harness identity (#3036).
    """
    hasher = sha256()
    for path in (Path(__file__), Path(__file__).resolve().parent / "runs" / "hashing.py"):
        hasher.update(path.name.encode("utf-8"))
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def environment_identity(
    snapshot: DataSnapshot, *, timing_mode: str = "warm", data_policy: Mapping[str, Any] | None = None
) -> dict[str, Any]:
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
        "harness_digest": _harness_digest(),
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
        "data_policy": dict(data_policy) if data_policy is not None else dict(CORPUS_DATA_POLICY),
        "timing_mode": timing_mode,
    }


def _rss_bytes() -> int | None:
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports ru_maxrss in KiB, macOS in bytes; the report carries bytes.
        return rss if sys.platform == "darwin" else rss * 1024
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
    identity: CorpusIdentity, *, execution: Mapping[str, Any], data_policy: Mapping[str, Any] | None = None
) -> Any:
    from app.schemas.engine_backtest import EngineBacktestRequest

    # The engine's request contract carries ET trading dates as ISO strings
    # (its own schema, predating this harness); this function is the one
    # place the corpus's int64-ms-UTC windows cross that boundary — an
    # in-function conversion under ADR 0022, never persisted or transmitted
    # further ("Left, with reasons", the engine backtest request dates).
    start, end = window_dates(identity.window_start_ms, identity.window_end_ms)
    return EngineBacktestRequest(
        strategy_name=identity.strategy,
        params=dict(identity.params),
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
        data_policy=dict(data_policy) if data_policy is not None else None,
    )


def _serial_execute(
    request: Any,
    on_phase: Callable[[str], None],
    data_manifest: Mapping[str, str] | None,
    decoded_cache: DecodedArtifactCache | None = None,
) -> Any:
    from app.services.engine_backtest_service import execute_engine_backtest

    return execute_engine_backtest(
        request=request, on_phase=on_phase, on_log=lambda message: None, data_manifest=data_manifest,
        decoded_cache=decoded_cache,
    )


class BaselineExecutionError(RuntimeError):
    """An evaluation the harness cannot record: the engine failed the run.

    A failed response (``success=False``) carries no scientific projection,
    so recording it as a zero-trade result would launder an engine fault
    into a satisfied no-trade expectation (#3036 review). Failed runs fail
    the baseline instead.
    """


def run_identity(
    identity: CorpusIdentity,
    *,
    execution: Mapping[str, Any] | None = None,
    data_policy: Mapping[str, Any] | None = None,
    data_manifest: Mapping[str, str] | None = None,
    repetition: int = 0,
    execute: Callable[..., Any] | None = None,
    decoded_cache: DecodedArtifactCache | None = None,
) -> IdentityRun:
    """Execute one identity on the serial path and measure its additive spans.

    ``execute`` is the injection seam the worker runtime will replace; the
    default is today's ``execute_engine_backtest``. The seam is invoked as
    ``execute(request=..., on_phase=..., data_manifest=...)`` — the manifest
    is the snapshot's receipts, and binding the engine's reads to it is what
    makes the report's snapshot digest the digest of the bytes actually
    consumed. With ``decoded_cache`` the invocation adds that argument, so a
    three-argument seam keeps working for every non-cache caller (#3038). A
    response with ``success=False`` raises :class:`BaselineExecutionError`
    rather than recording anything. Timings ride the run's own phase
    boundaries — no timers, no sampling.
    """
    execution = _assumptions_dict() if execution is None else dict(execution)
    stamps: dict[str, float] = {}
    order: list[str] = []

    def on_phase(phase: str) -> None:
        mapped = _PHASE_MAP.get(phase)
        if mapped is not None and mapped not in stamps:
            stamps[mapped] = time.perf_counter()
            order.append(mapped)

    request = _engine_request(identity, execution=execution, data_policy=data_policy)
    start = time.perf_counter()
    if decoded_cache is None:
        response = (execute or _serial_execute)(request, on_phase, data_manifest)
    else:
        response = (execute or _serial_execute)(request, on_phase, data_manifest, decoded_cache)
    total_ms = (time.perf_counter() - start) * 1000

    if not getattr(response, "success", False):
        raise BaselineExecutionError(
            f"evaluation {identity.evaluation_key} failed; the engine reported: {getattr(response, 'error', None)!r}"
        )

    additive: dict[str, float] = {}
    for position, name in enumerate(order):
        end = stamps[order[position + 1]] if position + 1 < len(order) else start + total_ms / 1000
        additive[name] = round((end - stamps[name]) * 1000, 3)
    if order and stamps[order[0]] > start:
        # The queue/IPC span before the first observed boundary is the part
        # a later executor attributes; the serial report names it rather
        # than letting the additive spans silently miss the total.
        additive["unattributed_prefix"] = round((stamps[order[0]] - start) * 1000, 3)

    trades = response.trades or []
    ordered_trades = [trade.model_dump(mode="json") for trade in trades]
    equity_curve = list(response.equity_curve or [])
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
        bars_scored=len(equity_curve),
        trades=len(trades),
        result_hash=hash_payload({"trades": ordered_trades, "equity_curve": equity_curve, "statistics": response.statistics}),
        trade_log_hash=hash_payload(ordered_trades),
        metrics_hash=hash_payload(response.statistics),
        ordered_trades=ordered_trades,
        equity_curve=equity_curve,
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
    decoded_cache_byte_bound: int = DECODED_CACHE_DEFAULT_BYTES,
    execute: Callable[..., Any] | None = None,
) -> BaselineReport:
    """Freeze the snapshot, regenerate the corpus, and run it ``repetitions`` times.

    The same ``data_policy`` flows into the corpus identities and every engine
    request, and the snapshot's manifest binds each run's reads to the
    receipted bytes, so the report's input identity is the identity of
    what was executed. Completed-result cache hits cannot occur here —
    every identity is a fresh evaluation key — and one showing up in a
    future mode is reported as a fault in ``notes``, never as throughput.

    ``serial-decoded-cache`` runs the same corpus with one process-shared
    decoded-artifact cache (#3038): every artifact read still re-admits, and
    the report carries the cache's own hit/miss/eviction accounting under
    ``decoded_cache``. No speedup target is claimed from that mode in this
    slice — it exists to measure the reuse.
    """
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"unknown execution mode {execution_mode!r}")
    if execution_mode not in RUNNABLE_EXECUTION_MODES:
        raise ValueError(
            f"{execution_mode!r} is a future executor; run_baseline refuses it until the executor "
            "that measures it exists (its fields are named in modes_distinguished)"
        )
    if timing_mode not in RUNNABLE_TIMING_MODES:
        raise ValueError(
            f"{timing_mode!r} timing needs an executor with a worker to start; the serial path "
            "attributes no worker startup or imports"
        )
    if decoded_cache_byte_bound <= 0:
        raise ValueError(f"decoded-cache bound must be positive, got {decoded_cache_byte_bound}")
    snapshot = capture_data_snapshot(roots=list(roots), symbol=symbol, resolution="minute", data_start=development[0], data_end=development[1])
    # One materialized policy feeds the corpus identities and every engine
    # request: passing None on to the engine would let it synthesize its own
    # legacy policy while the digest hashed the corpus default — the exact
    # identity/execution split this harness exists to prevent.
    policy = dict(data_policy) if data_policy is not None else _corpus_data_policy(symbol)
    corpus = build_corpus(
        symbol=symbol,
        development=development,
        data_snapshot_digest=snapshot.digest(),
        data_policy=policy,
        seed=seed,
        full_development_count=full_development_count,
        fold_count=fold_count,
        folds=folds,
    )
    report = BaselineReport(
        execution_mode=execution_mode,
        timing_mode=timing_mode,
        environment=environment_identity(snapshot, timing_mode=timing_mode, data_policy=policy),
        # The report carries the whole frozen manifest — windows, context
        # digest, every identity — not just its digest, so the artifact is
        # the manifest (regenerable from seeds, but complete on its face).
        corpus=corpus.as_dict(),
    )
    execution = _assumptions_dict()
    # One cache for the whole baseline when the mode asks for it: sequential
    # evaluations in this process share admitted decodes; each still runs
    # every admission check per read (#3038).
    decoded_cache = None
    if execution_mode == "serial-decoded-cache":
        decoded_cache = DecodedArtifactCache(max_bytes=decoded_cache_byte_bound)
    run_wall = time.perf_counter()
    for repetition in range(repetitions):
        for identity in corpus.identities:
            report.runs.append(
                run_identity(
                    identity,
                    execution=execution,
                    data_policy=policy,
                    data_manifest=snapshot.artifacts,
                    repetition=repetition,
                    execute=execute,
                    decoded_cache=decoded_cache,
                )
            )
    wall_seconds = time.perf_counter() - run_wall
    if decoded_cache is not None:
        report.decoded_cache_metrics = decoded_cache.metrics()
    report.medians_ms, report.variability_ms = _summarize(report.runs)
    # Valid new evaluations: every recorded run (failures raised instead of
    # being recorded, and this path holds no completed-evaluation cache), so
    # throughput is runs over the execution wall time, cache hits excluded
    # by construction and faulted in ``notes`` should one ever appear.
    report.throughput = {
        "valid_new_evaluations": len(report.runs),
        "wall_seconds": round(wall_seconds, 3),
        "evaluations_per_second": round(len(report.runs) / wall_seconds, 3) if wall_seconds > 0 else 0.0,
    }
    return report


def _summarize(runs: Sequence[IdentityRun]) -> tuple[dict[str, float], dict[str, float]]:
    """True median and repetition variability, per span, over repetitions of the same identity.

    Repetitions of *one* evaluation key are the only comparable sample —
    pooling identities would fold workload differences into what the report
    calls variability. Each identity contributes the median of its
    repetitions to the report's median and the max−min spread of its
    repetitions to its variability; the report value is the median of those
    per-identity values, so an even number of samples averages the middle
    pair instead of picking the upper one.
    """
    by_key: dict[str, list[IdentityRun]] = {}
    for run in runs:
        by_key.setdefault(run.evaluation_key, []).append(run)
    # Per key and span, the series is only the repetitions that observed the
    # span — a repetition missing one is a gap that belongs in the report,
    # not a value copied from a sibling repetition.
    spans: dict[str, list[list[float]]] = {}
    for key_runs in by_key.values():
        spans.setdefault("total", []).append([run.total_ms for run in key_runs])
        for name in {name for run in key_runs for name in run.additive_ms}:
            spans.setdefault(name, []).append(
                [run.additive_ms[name] for run in key_runs if name in run.additive_ms]
            )
    medians = {name: round(statistics.median(statistics.median(series) for series in per_key), 3) for name, per_key in spans.items()}
    variability = {
        name: round(statistics.median(max(series) - min(series) for series in per_key), 3) for name, per_key in spans.items()
    }
    return medians, variability


def round_widths(round_payload: Mapping[str, Any]) -> dict[str, Any]:
    """One stored Zoom round's widths: what was sampled, testable, eligible, refused, cached and ready.

    ``newly_reserved``/``cached_or_duplicate``/``ready`` come from the width
    accounting the procedure captures on the round itself (``submitted`` /
    ``first_evaluated``): ready is the distinct batch the round consumed,
    newly reserved its first-seen points, cached the repeats an earlier
    round had already answered. Rounds stored before that capture carry
    ``None`` — absent, never guessed as zero.
    """
    sampled = len(round_payload["values"])
    invalid = len(round_payload["invalid"])
    results = round_payload["results"]
    ineligible = sum(1 for _, code in results if code is not None)
    widths: dict[str, Any] = {
        "sampled": sampled,
        "invalid": invalid,
        "testable": sampled - invalid,
        "evaluated": len(results),
        "eligible": len(results) - ineligible,
        "ineligible": ineligible,
    }
    if "submitted" in round_payload and "first_evaluated" in round_payload:
        submitted = int(round_payload["submitted"])
        first_evaluated = int(round_payload["first_evaluated"])
        widths["newly_reserved"] = first_evaluated
        widths["cached_or_duplicate"] = submitted - first_evaluated
        widths["ready"] = submitted
    else:
        widths["newly_reserved"] = widths["cached_or_duplicate"] = widths["ready"] = None
    return widths


def protocol_width_histogram(trials: Sequence[Mapping[str, Any]], *, stage: str) -> dict[str, Any]:
    """One stage's per-fold, per-round width histogram from its stored trials.

    A stage runs one procedure per fold (the full-development window is the
    ``None`` fold), and every procedure records its own rounds and its own
    stop accounting — so the histogram keys by fold and each fold's
    accounting comes from that fold's stop trial. The totals reconcile each
    fold's invalid counts against its rounds, so frozen widths and budget
    accounting cannot drift apart; ``reconciled`` is that agreement and a
    drifted count reads False. ``trials`` may span every stage; only
    ``stage``'s trials enter the histogram.
    """
    by_fold: dict[Any, dict[str, Any]] = {}
    for trial in trials:
        if trial.get("stage") != stage:
            continue
        fold = trial.get("fold_index")
        entry = by_fold.setdefault(fold, {"rounds": [], "stop": None})
        payload = trial.get("payload") or {}
        if trial.get("kind") == "round":
            entry["rounds"].append(
                {
                    "stage": stage,
                    "fold_index": fold,
                    "pass_index": payload.get("pass_index"),
                    "knob": payload.get("knob"),
                    "round_index": payload.get("round_index"),
                    **round_widths(payload),
                }
            )
        elif trial.get("kind") == "stop":
            entry["stop"] = payload
    folds: list[dict[str, Any]] = []
    for fold in sorted(by_fold, key=lambda value: (value is not None, value)):
        entry = by_fold[fold]
        rounds = entry["rounds"]
        fold_row: dict[str, Any] = {
            "fold_index": fold,
            "rounds": rounds,
            "totals": {
                "sampled": sum(row["sampled"] for row in rounds),
                "invalid": sum(row["invalid"] for row in rounds),
                "testable": sum(row["testable"] for row in rounds),
                "eligible": sum(row["eligible"] for row in rounds),
            },
        }
        stop = entry["stop"]
        if stop is not None:
            # The stop trial's payload carries the procedure's counts at its
            # top level (``stages.run_procedure`` spreads them there).
            counts = stop
            fold_row["accounting"] = {
                "newly_reserved": counts.get("evaluated"),
                "cached_or_duplicate": counts.get("cached"),
                "invalid": counts.get("invalid"),
                "ready": (counts.get("evaluated") or 0) + (counts.get("cached") or 0),
            }
            # The rounds and the stop payload record the invalid population
            # independently — one per refinement round, one as the procedure's own
            # count — so their agreement is the reconciliation that can catch
            # drift. (The evaluated/cached counts span the whole procedure's window,
            # including points the rounds never sampled, so they reconcile as
            # ranges, not equalities; the report carries them as accounting.)
            fold_row["totals"]["reconciled"] = fold_row["totals"]["invalid"] == counts.get("invalid")
        folds.append(fold_row)
    return {
        "stage": stage,
        "folds": folds,
        "totals": {
            name: sum(fold_row["totals"][name] for fold_row in folds)
            for name in ("sampled", "invalid", "testable", "eligible")
        },
    }


def connection_inventory(*, research_cap: int = CONNECTION_INVENTORY_RESEARCH_CAP) -> dict[str, Any]:
    """Account for every deployed pool and service maximum against the server's connection budget.

    The pools come from the deployment's own facts: the data plane's two
    loops and each fleet clerk lane's one share the catalog pool's ceiling
    (``catalog_client.POOL_MAX_SIZE``), the Backend's Npgsql pool is capped
    by its deployed connection string (Npgsql's documented default when the
    string sets no maximum), and the fleet topology snapshot names the
    deployed services and their VM containment. The PostgreSQL server
    maximum is an operator input (``POSTGRES_SERVER_MAX_CONNECTIONS``):
    without it the verdict is *unverified* — ``meets_required_free`` is
    None with the reason, never a pass guessed against an assumed limit.
    A mismatch is *reported*, never repaired by raising PostgreSQL limits.
    """
    from app.data_lake.catalog_client import POOL_MAX_SIZE

    topology = _fleet_topology()
    deployed = set(topology.get("services", [])) if topology is not None else set()
    unverified: list[str] = []

    coordinator_loops = 2  # the FastAPI app loop and the background writer loop (app/research/persistence/db.py)
    pools: list[dict[str, Any]] = [
        {
            "name": "python-service: data-lake catalog / research persistence",
            "max_connections": POOL_MAX_SIZE * coordinator_loops,
            "server": "postgres",
            "source": f"catalog_client.POOL_MAX_SIZE={POOL_MAX_SIZE} × {coordinator_loops} deployed loops",
        },
        {
            "name": "backend: Npgsql pool",
            "max_connections": NPGSQL_DEFAULT_MAX_POOL_SIZE,
            "server": "postgres",
            "source": "Npgsql default maximum; compose.yaml's ConnectionStrings__DefaultConnection sets no Max Pool Size",
        },
    ]
    for clerk in sorted(name for name in deployed if "clerk" in name):
        pools.append(
            {
                "name": f"{clerk}: lane-served lake reads (asyncpg)",
                "max_connections": POOL_MAX_SIZE,
                "server": "postgres",
                "source": f"catalog_client.POOL_MAX_SIZE={POOL_MAX_SIZE} × 1 lane loop; deployed in the fleet topology",
            }
        )
    if topology is None:
        unverified.append("the fleet topology snapshot is absent; the clerk lanes and VM limits are uncounted")
    pools.append({"name": "job store (redis)", "max_connections": 32, "server": "redis", "source": "redis maxclients default, its own server"})

    reserved = sum(pool["max_connections"] for pool in pools if pool["server"] == "postgres")
    supplied = "POSTGRES_SERVER_MAX_CONNECTIONS" in os.environ
    server_max = int(os.environ["POSTGRES_SERVER_MAX_CONNECTIONS"]) if supplied else None
    if not supplied:
        unverified.append(
            "POSTGRES_SERVER_MAX_CONNECTIONS is not set; the deployed server maximum is unknown "
            "and no free-connection verdict is computed from an assumed one"
        )

    free = None if server_max is None else server_max - reserved
    # The research cap is NOT a pool above: it is the opt-in research runtime
    # process's own PostgreSQL connections (ADR 0077), a separate process
    # whose asyncpg usage no listed pool counts. Research persistence inside
    # the API process is already inside the first pool above; subtracting the
    # cap again here would double-count it — the cap reserves only the
    # runtime process, so the report names its source explicitly.
    free_after_research = None if free is None else free - research_cap
    verified = not unverified
    meets: bool | None = None if not verified else free_after_research >= CONNECTION_INVENTORY_REQUIRED_FREE
    return {
        "pools": pools,
        "postgres_server_max_connections": server_max,
        "server_max_supplied_by_operator": supplied,
        "research_cap": research_cap,
        "research_cap_source": "the opt-in research runtime process's own PostgreSQL connections (ADR 0077); no pool above counts them",
        "reserved_by_configured_pools": reserved,
        "free_at_server_without_research": free,
        "free_after_research_cap": free_after_research,
        "required_free": CONNECTION_INVENTORY_REQUIRED_FREE,
        "verified": verified,
        "unverified_reasons": unverified,
        "meets_required_free": meets,
        "mismatch": None if meets is not False else (
            f"with the research cap of {research_cap}, {free_after_research} server connections "
            f"would remain free, below the required {CONNECTION_INVENTORY_REQUIRED_FREE}"
        ),
        "vm_limits": _vm_limits(topology),
    }


def _fleet_topology() -> dict[str, Any] | None:
    """The committed fleet topology render, or None when the file is absent."""
    import json

    try:
        return json.loads(FLEET_TOPOLOGY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _vm_limits(topology: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    """Each deployed service's containment from the topology snapshot (VM resource maxima)."""
    if topology is None:
        return None
    limits: dict[str, dict[str, Any]] = {}
    for name, detail in sorted(topology.get("service_detail", {}).items()):
        containment = detail.get("containment") or {}
        limits[name] = {"cpus": containment.get("cpus"), "memory_bytes": containment.get("mem_limit")}
    return limits


def summarize_report(report: BaselineReport) -> str:
    """The readable summary an operator reads beside the machine-readable report.

    Every number here restates the report's own fields — the summary adds
    no measurement of its own.
    """
    environment = report.environment
    lines = [
        f"Backtest baseline — {report.execution_mode} ({report.timing_mode})",
        f"  corpus: {report.corpus['count']} identities, digest {report.corpus['digest'][:16]}…",
        f"  code identity: {environment['code_identity']['source_digest'][:16]}… "
        f"({environment['code_identity']['tree_state']})",
        f"  data snapshot: {environment['data_snapshot_digest'][:16]}… "
        f"over {len(environment['data_artifact_checksums'])} admitted artifacts",
        f"  data policy: {environment['data_policy']}",
        f"  environment: Python {environment['python']} on {environment['platform']}, "
        f"load average {environment['competing_load_average']}",
        f"  runs: {len(report.runs)} recorded, cache hits {report.cache_hits} "
        f"(a hit is a fault here, never throughput)",
        f"  throughput: {report.throughput.get('valid_new_evaluations', 0)} valid new evaluations "
        f"in {report.throughput.get('wall_seconds', 0.0)}s "
        f"({report.throughput.get('evaluations_per_second', 0.0)}/s)",
        f"  median total: {report.medians_ms.get('total', 0.0)} ms "
        f"(repetition variability {report.variability_ms.get('total', 0.0)} ms)",
        f"  expectations held: {sum(1 for run in report.runs if run.expectation_held)}/{len(report.runs)}",
    ]
    if report.decoded_cache_metrics is not None:
        cache = report.decoded_cache_metrics
        lines.append(
            f"  decoded cache: {cache['hits']} hits / {cache['misses']} misses, "
            f"{cache['evictions']} evictions, {cache['uncached_decodes']} uncached; "
            f"{cache['resident_bytes']} of {cache['max_bytes']} bytes resident "
            f"({cache['pinned_bytes']} pinned, {cache['duplicate_residency_bytes']} duplicated)"
        )
    inventory = connection_inventory()
    if inventory["verified"]:
        verdict = "meets" if inventory["meets_required_free"] else "FAILS"
        lines.append(
            f"  connections: {inventory['reserved_by_configured_pools']} reserved of "
            f"{inventory['postgres_server_max_connections']}; after the research cap of "
            f"{inventory['research_cap']}, {inventory['free_after_research_cap']} free — {verdict} the required "
            f"{inventory['required_free']}"
        )
    else:
        lines.append("  connections: UNVERIFIED — " + "; ".join(inventory["unverified_reasons"]))
    lines.extend(f"  note: {note}" for note in report.notes)
    return "\n".join(lines)
