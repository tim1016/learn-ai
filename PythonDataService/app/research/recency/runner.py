"""Recency Chart launch runner.

Orchestrates one Recency Chart launch: expands the grid (lazily), executes
each (symbol, strategy, parameter-combo) backtest with bounded concurrency,
computes every trade/run statistic in Python (design spec §7.1), assembles
a fingerprinted snapshot per run, and persists each snapshot independently
so a failing child is isolated and reported ("N of M failed") rather than
aborting the batch or being silently dropped (numerical-rigor: no silent
catches). Mirrors the walk-forward runner's injected-dependency shape
(``app/research/walk_forward/runner.py``) — ``execute_backtest_fn`` and
``persist_fn`` are supplied by the caller so this module has no direct
dependency on the engine HTTP layer or the .NET persistence transport,
which keeps it testable with an in-memory double.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design spec
docs/superpowers/specs/2026-08-16-recency-chart-design.md §4, §5.3, D11.
Canonical implementation: this file.
Validated against: tests/research/recency/test_runner.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import islice
from typing import Literal, Protocol

from app.research.recency.fingerprint import RunFingerprintBase, trade_fingerprint
from app.research.recency.grid import RunSpec, StrategyGridConfig, expand_grid, grid_size
from app.research.recency.stats import (
    TradeForStats,
    holding_sessions,
    sharpe,
    total_pnl,
    trade_dollar_pnl,
)

MAX_CONCURRENT_RUNS = 8


class TradeLike(Protocol):
    entry_time: int
    exit_time: int
    pnl_pts: float
    pnl_pct: float
    quantity: int
    is_synthetic_exit: bool
    signal_reason: str


class DataPolicyLike(Protocol):
    """Duck-typed against EngineBacktestResponse.data_policy
    (_EngineDataPolicyModel) — any canonical-JSON-serializable policy
    object works, keeping this module free of the engine HTTP layer."""

    def model_dump_json(self) -> str: ...


class BacktestResultLike(Protocol):
    success: bool
    error: str | None
    trades: Sequence[TradeLike]
    study_id: int | None
    data_policy: DataPolicyLike


@dataclass(frozen=True)
class RecencyLaunchConfig:
    """One launch's configuration — the grid plus the run-level evidence context."""

    launch_id: str
    strategies: list[StrategyGridConfig]
    symbols: list[str]
    window_start_ms: int
    window_end_ms: int
    data_policy: str = "polygon-adjusted-regular-minute"
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 0.0


@dataclass(frozen=True)
class RecencyTradeSnapshot:
    """One trade as persisted into the recency-owned snapshot (design spec §4.1)."""

    fingerprint: str
    entry_ms: int
    exit_ms: int
    pnl_pts: float
    pnl_pct: float
    quantity: int
    pnl: float
    holding_sessions: int
    is_synthetic_exit: bool
    signal_reason: str


@dataclass(frozen=True)
class RecencyRunSnapshot:
    """The atomic, fingerprint-idempotent unit persisted per (symbol, combo)."""

    launch_id: str
    symbol: str
    strategy_key: str
    params: dict[str, float]
    params_hash: str
    total_pnl: float
    sharpe: float | None
    trades: list[RecencyTradeSnapshot]
    study_id: int | None


@dataclass(frozen=True)
class RecencyRunOutcome:
    run_spec: RunSpec
    status: Literal["succeeded", "failed"]
    error: str | None = None


@dataclass(frozen=True)
class RecencyRunSummary:
    launch_id: str
    expected_runs: int
    succeeded_runs: int
    failed_runs: int
    outcomes: list[RecencyRunOutcome] = field(default_factory=list)


def _snapshot_for_run(
    run_spec: RunSpec,
    config: RecencyLaunchConfig,
    result: BacktestResultLike,
    strategy_code_version: str,
) -> RecencyRunSnapshot:
    base = RunFingerprintBase(
        symbol=run_spec.symbol,
        strategy_key=run_spec.strategy_key,
        strategy_code_version=strategy_code_version,
        params_hash=run_spec.params_hash,
        # The RESOLVED policy the engine actually executed under, not the
        # caller's requested label (config.data_policy) — a stale/direct
        # client's unhonored request would otherwise mislabel evidence
        # with a policy that never governed execution.
        data_policy=result.data_policy.model_dump_json(),
        fill_mode=config.fill_mode,
        commission_per_order=config.commission_per_order,
    )
    stats_trades = [
        TradeForStats(
            entry_ms=t.entry_time,
            exit_ms=t.exit_time,
            pnl_pts=t.pnl_pts,
            pnl_pct=t.pnl_pct,
            quantity=t.quantity,
        )
        for t in result.trades
    ]
    trade_snapshots = [
        RecencyTradeSnapshot(
            fingerprint=trade_fingerprint(base, trade.entry_ms, trade.exit_ms),
            entry_ms=trade.entry_ms,
            exit_ms=trade.exit_ms,
            pnl_pts=trade.pnl_pts,
            pnl_pct=trade.pnl_pct,
            quantity=trade.quantity,
            pnl=trade_dollar_pnl(trade, config.commission_per_order),
            holding_sessions=holding_sessions(trade.entry_ms, trade.exit_ms),
            is_synthetic_exit=source.is_synthetic_exit,
            signal_reason=source.signal_reason,
        )
        # zipped against result.trades (not re-derived from stats_trades,
        # which strips is_synthetic_exit/signal_reason) — both lists are
        # built from result.trades in the same order, so positions line up.
        for trade, source in zip(stats_trades, result.trades, strict=True)
    ]
    return RecencyRunSnapshot(
        launch_id=config.launch_id,
        symbol=run_spec.symbol,
        strategy_key=run_spec.strategy_key,
        params=run_spec.params,
        params_hash=run_spec.params_hash,
        total_pnl=total_pnl(stats_trades, config.window_start_ms, config.window_end_ms, config.commission_per_order),
        sharpe=sharpe(stats_trades),
        trades=trade_snapshots,
        study_id=result.study_id,
    )


def _batches(run_specs: Iterator[RunSpec], size: int) -> Iterator[list[RunSpec]]:
    it = iter(run_specs)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def run_recency(
    config: RecencyLaunchConfig,
    *,
    execute_backtest_fn: Callable[[RunSpec, RecencyLaunchConfig], BacktestResultLike],
    persist_fn: Callable[[RecencyRunSnapshot], None],
    strategy_code_version_fn: Callable[[str], str],
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_run_failed: Callable[[RunSpec, str], None] = lambda run_spec, message: None,
    cancel_check: Callable[[], bool | None] = lambda: False,
    max_workers: int = MAX_CONCURRENT_RUNS,
) -> RecencyRunSummary:
    """Execute one launch's grid with bounded concurrency and per-run isolation.

    ``cancel_check`` mirrors app/research/walk_forward/runner.py's
    CancelCheck contract: it is called once per batch and its return value
    is IGNORED — cancellation works only by raising, and the exception
    propagates out of this function uncaught (the caller, e.g. jobs.py,
    wires a wrapper that raises JobCancelled so run_in_thread produces a
    real ``job.cancelled`` terminal state instead of ``job.completed``).
    """
    on_phase("expand")
    expected = grid_size(config.strategies, config.symbols)
    run_specs = expand_grid(config.strategies, config.symbols)
    outcomes: list[RecencyRunOutcome] = []
    done = 0

    def _execute_and_persist(run_spec: RunSpec) -> RecencyRunOutcome:
        result = execute_backtest_fn(run_spec, config)
        if not result.success:
            raise RuntimeError(result.error or "backtest did not succeed")
        version = strategy_code_version_fn(run_spec.strategy_key)
        snapshot = _snapshot_for_run(run_spec, config, result, version)
        persist_fn(snapshot)
        return RecencyRunOutcome(run_spec=run_spec, status="succeeded")

    on_phase("run")
    # Bounded batches (size == max_workers) rather than materializing the
    # full grid and submitting it all at once: at most max_workers RunSpecs
    # are pulled from the lazy expand_grid() iterator at any time, so a
    # large-but-legitimate sweep (up to MAX_GRID_SIZE) never holds its
    # entire grid in memory. A fully pipelined sliding window (submit-as-
    # you-go via concurrent.futures.wait(FIRST_COMPLETED)) would trim the
    # idle time between batches, but isn't worth the added complexity
    # given this engine's typical grid sizes; batching already bounds
    # in-flight futures, so there's no separate future-cancellation step
    # needed beyond letting the pool finish the batch already dispatched
    # when cancel_check raises.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for batch in _batches(run_specs, max_workers):
            cancel_check()
            futures = {pool.submit(_execute_and_persist, run_spec): run_spec for run_spec in batch}

            for future in as_completed(futures):
                run_spec = futures[future]
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    # Per-run isolation: a failing child is captured and reported,
                    # never silently dropped and never aborts the rest of the batch.
                    message = str(exc)
                    outcomes.append(RecencyRunOutcome(run_spec=run_spec, status="failed", error=message))
                    on_run_failed(run_spec, message)
                done += 1
                on_progress(done, expected)

    succeeded = sum(1 for outcome in outcomes if outcome.status == "succeeded")
    failed = sum(1 for outcome in outcomes if outcome.status == "failed")
    on_phase("completed")
    return RecencyRunSummary(
        launch_id=config.launch_id,
        expected_runs=expected,
        succeeded_runs=succeeded,
        failed_runs=failed,
        outcomes=outcomes,
    )
