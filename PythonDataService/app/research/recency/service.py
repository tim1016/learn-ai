"""Recency Chart launch lifecycle (PRD #1927): validate, create the durable launch, run, record the outcome.

The jobs router keeps only the HTTP handler; everything that decides or
writes lives here so it has a canonical, unit-testable home next to the
runner and the repository it drives.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.research.persistence.db import run_sync, with_connection
from app.research.recency import repository as repo
from app.research.recency.runner import RecencyLaunchConfig, RecencyRunSnapshot, run_recency
from app.research.recency.stats import ms_to_et_date_string
from app.research.recency.validation import RecencyRequestInvalidError, validate_recency_request
from app.research.sweep.grid import (
    RecencyGridTooLargeError,
    RunSpec,
    StrategyGridConfig,
    ValueListRange,
    expand_grid,
    grid_size,
)
from app.routers.engine import EngineBacktestRequest, execute_engine_backtest
from app.services.data_plane_health import resolved_code_revision

logger = logging.getLogger(__name__)


class RecencyLaunchRejected(ValueError):
    """The launch cannot run as requested; the message is the reason the client sees."""


class RecencyLaunchConflict(ValueError):
    """The launch id already exists with a different configuration; the first record stands and this dispatch is refused."""


@dataclass(frozen=True)
class ValidatedLaunch:
    config: RecencyLaunchConfig
    expected_runs: int


def validate_launch(
    *,
    launch_id: str,
    strategies: list[StrategyGridConfig],
    symbols: list[str],
    window_start_ms: int,
    window_end_ms: int,
    data_policy: str,
    fill_mode: str,
    commission_per_order: float,
) -> ValidatedLaunch:
    """The canonical grid plus its exact run count, or :class:`RecencyLaunchRejected` (D11 ceiling, ranges, request rules)."""
    try:
        expand_grid(strategies, symbols)
    except RecencyGridTooLargeError as exc:
        raise RecencyLaunchRejected(str(exc)) from exc
    except ValueError as exc:
        raise RecencyLaunchRejected(f"invalid parameter range: {exc}") from exc
    # A repeated value in a list is a malformed request: it would schedule two identical cells and the
    # second would read as a redelivery. (A low/high/step range cannot repeat; no expansion is needed.)
    for strategy in strategies:
        for name, spec in strategy.param_ranges.items():
            if isinstance(spec, ValueListRange) and len(set(spec.values)) != len(spec.values):
                raise RecencyLaunchRejected(f"{strategy.strategy_key}.{name} repeats a value; each parameter value may appear once")
    try:
        validate_recency_request(strategies=strategies, symbols=symbols, window_start_ms=window_start_ms, window_end_ms=window_end_ms, data_policy=data_policy)
    except RecencyRequestInvalidError as exc:
        raise RecencyLaunchRejected(str(exc)) from exc
    config = RecencyLaunchConfig(
        launch_id=launch_id,
        strategies=strategies,
        symbols=symbols,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        data_policy=data_policy,
        fill_mode=fill_mode,
        commission_per_order=commission_per_order,
    )
    return ValidatedLaunch(config=config, expected_runs=grid_size(strategies, symbols))


async def create_launch(launch: ValidatedLaunch, *, request: dict[str, Any]) -> bool:
    """Design spec D20: the durable launch exists before dispatch, so zero-success, cancellation and Redis expiry stay accountable.

    Returns whether this dispatch created the launch. An identical redelivery
    returns ``False`` (the caller decides, from the job's liveness, whether a
    worker is still needed); the same id with a different configuration
    raises ``RecencyLaunchConflict``.
    """
    try:
        return await with_connection(
            repo.create_launch, launch_id=launch.config.launch_id, config_json=json.dumps(request, sort_keys=True), expected_runs=launch.expected_runs
        )
    except repo.LaunchConflictError as exc:
        raise RecencyLaunchConflict(str(exc)) from exc


def window_date(ms: int) -> str:
    """Trading-date string for ``EngineBacktestRequest.from_date`` / ``to_date`` — ET-anchored (temporal-rigor.md), never a UTC ``strftime``."""
    return ms_to_et_date_string(ms)


def _execute_backtest(run_spec: RunSpec, config: RecencyLaunchConfig) -> Any:
    params = {**run_spec.params, "symbol": run_spec.symbol}
    request = EngineBacktestRequest(
        strategy_name=run_spec.strategy_key,
        params=params,
        from_date=window_date(config.window_start_ms),
        to_date=window_date(config.window_end_ms),
        fill_mode=config.fill_mode,
        commission_per_order=config.commission_per_order,
    )
    return execute_engine_backtest(request=request, on_phase=lambda phase: None, on_log=lambda message: None)


def _persist(snapshot: RecencyRunSnapshot) -> None:
    # Direct write on the shared writer loop; a tombstoned launch or a redelivered cell is a successful no-op.
    run_sync(with_connection(repo.persist_snapshot, snapshot))


def record_terminal_status(launch_id: str, status: str, *, succeeded_runs: int | None = None, failed_runs: int | None = None) -> None:
    """Write a launch's terminal state from the worker thread, on the shared writer loop."""
    run_sync(with_connection(repo.set_terminal_status, launch_id, status=status, succeeded_runs=succeeded_runs, failed_runs=failed_runs))


def record_abort_state(launch_id: str, terminal_status: str) -> None:
    """Move an aborted launch off RUNNING without masking why it aborted.

    The exception that ended the launch is what the operator needs; a failure
    to record the terminal state must not replace it in the traceback. Logged
    rather than raised — never swallowed silently.
    """
    try:
        record_terminal_status(launch_id, terminal_status)
    except Exception:
        logger.exception("failed to record recency launch terminal state", extra={"launch_id": launch_id, "terminal_status": terminal_status})


def run_launch(
    config: RecencyLaunchConfig,
    *,
    emit: ProgressEmitter,
    cancel: CancellationCheck,
    execute_backtest: Callable[[RunSpec, RecencyLaunchConfig], Any] = _execute_backtest,
) -> dict[str, Any]:
    """The worker body: run the grid, persist each run, record the launch's terminal state, return the summary."""
    try:
        summary = run_recency(
            config,
            execute_backtest_fn=execute_backtest,
            persist_fn=_persist,
            strategy_code_version_fn=lambda strategy_key: resolved_code_revision(),
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="runs"),
            on_run_failed=lambda run_spec, message: emit.log(
                f"run failed: {run_spec.symbol}/{run_spec.strategy_key} ({run_spec.params_hash[:8]}): {message}", level="warning"
            ),
            # Raises JobCancelled so run_in_thread emits job.cancelled instead of job.completed on a DELETE.
            cancel_check=cancel.raise_if_cancelled,
        )
    except JobCancelled:
        record_abort_state(config.launch_id, "CANCELLED")
        raise
    except Exception:
        record_abort_state(config.launch_id, "FAILED")
        raise
    record_terminal_status(
        summary.launch_id,
        "COMPLETED" if summary.failed_runs == 0 else "FAILED",
        succeeded_runs=summary.succeeded_runs,
        failed_runs=summary.failed_runs,
    )
    return {
        "launch_id": summary.launch_id,
        "expected_runs": summary.expected_runs,
        "succeeded_runs": summary.succeeded_runs,
        "failed_runs": summary.failed_runs,
    }
