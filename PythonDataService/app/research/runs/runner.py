"""``run_strategy_spec`` — orchestrates one ``StrategySpec`` execution
and produces ``(RunLedger, BacktestRunResult)``.

In-memory only in v1; ``app.research.runs.storage`` adds disk persistence
in A2. The router in A3 layers on the HTTP boundary.

Wraps the same orchestration as ``app/routers/spec_strategy.py::run_spec_backtest``
but captures the engine's full result (including the equity curve) and
hashes inputs and outputs into ledger identity columns.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.engine import BacktestEngine, BacktestResult, EquitySnapshot
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.results.statistics import EquityPoint, summarize
from app.engine.strategy.base import LoggedTrade
from app.engine.strategy.spec import SpecAlgorithm, StrategySpec
from app.research.runs.hashing import hash_payload, make_data_snapshot_id
from app.research.runs.ledger import (
    RunLedger,
    _capture_git_commit,
    resolve_data_root_revision,
)
from app.research.runs.result import (
    BacktestRunResult,
    DrawdownPoint,
    EquityCurvePoint,
    RunMetrics,
    RunTrade,
)
from app.research.runs.window import summarize_window
from app.utils.timestamps import now_ms_utc, to_ms_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunRequest:
    """Validated inputs for a strategy run.

    The runner expects ``spec`` to already be a validated ``StrategySpec``
    (the FastAPI router does Pydantic validation on the wire boundary).
    Internal callers (parity tests, future fold/MC drivers) construct
    ``RunRequest`` directly.
    """

    spec: StrategySpec
    start_date: Date
    end_date: Date
    initial_cash: float = 100_000.0
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 0.0
    slippage_per_share: float = 0.0
    random_seed: int = 0
    strategy_spec_id: str = ""  # caller label; defaults to spec.name when empty
    parent_run_id: str | None = None
    parent_spec_hash: str | None = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
_VALID_FILL_MODES = {"signal_bar_close", "next_bar_open", "next_session_open"}
_NY = ZoneInfo("America/New_York")

# Default artifact root: ``PythonDataService/artifacts/`` (sibling of ``app/``).
# Resolved relative to this file so the path is stable regardless of the
# process cwd. Override with ``LEARN_AI_PREDICTION_ARTIFACTS_ROOT`` for
# tests or alternate deployments.
_DEFAULT_ARTIFACTS_ROOT = Path(__file__).resolve().parents[3] / "artifacts"


def _prediction_artifacts_root() -> Path:
    """Return the root directory for prediction-set artifacts.

    Override via ``LEARN_AI_PREDICTION_ARTIFACTS_ROOT`` env var (used by tests).
    Default: ``PythonDataService/artifacts/predictions/``.
    """
    override = os.environ.get("LEARN_AI_PREDICTION_ARTIFACTS_ROOT")
    if override:
        return Path(override)
    return _DEFAULT_ARTIFACTS_ROOT / "predictions"


def _normalize_fill_mode(s: str) -> str:
    """Lowercase + replace ``-`` → ``_``. Identity for the canonical form.

    The router accepts hyphen / case variants (``"SIGNAL-BAR-CLOSE"``);
    the runner stores and validates against the canonical form so the
    ledger doesn't carry style noise into ``data_snapshot_id`` or
    cross-run comparisons.
    """
    return s.lower().replace("-", "_")


def _parse_fill_mode(s: str) -> FillMode:
    norm = _normalize_fill_mode(s)
    if norm == "signal_bar_close":
        return FillMode.SIGNAL_BAR_CLOSE
    if norm == "next_bar_open":
        return FillMode.NEXT_BAR_OPEN
    if norm == "next_session_open":
        return FillMode.NEXT_SESSION_OPEN
    raise ValueError(f"unknown fill_mode {s!r} — expected one of {sorted(_VALID_FILL_MODES)}")


def _date_to_ny_midnight_ms(d: Date) -> int:
    """Convert a calendar date to ``int64 ms`` UTC for ``America/New_York``
    midnight on that date.

    The engine interprets ``strategy.set_start_date(...)`` /
    ``set_end_date(...)`` as NY-anchored: bars are filtered by NY-local
    date, sessions run on NY hours. Recording UTC midnight in the
    ledger would drift from the engine's actual window by 4-5 hours
    (DST-dependent) and silently misalign any ``since_ms`` filter that
    crosses midnight ET. The wire format is still ``int64 ms UTC``;
    only the *moment* the date represents shifts to NY-local midnight.
    """
    return int(datetime(d.year, d.month, d.day, tzinfo=_NY).timestamp() * 1000)


def _bars_held(entry_time: datetime, exit_time: datetime, resolution_minutes: int) -> int:
    """Approximate the number of consolidated bars an open position spanned.

    Computed from the time delta and the spec's resolution. For purely
    intraday strategies (the EMA fixture's ``BarsSinceEntry >= 5`` exit
    is exactly 5 × 15 min = 75 min) the value is exact. Cross-session
    holds inflate the count by the gap minutes — acceptable for v1
    because Phase D Monte Carlo only needs the *distribution* shape,
    not absolute consolidated-bar counts. Documented limitation.
    """
    delta_min = (exit_time - entry_time).total_seconds() / 60.0
    if delta_min <= 0:
        return 0
    return max(1, round(delta_min / resolution_minutes))


def _build_drawdown_curve(equity_curve: list[EquitySnapshot]) -> list[DrawdownPoint]:
    """Peak-to-trough drawdown fraction at every equity-curve timestamp.

    Same semantics as ``statistics._max_drawdown`` but emits the full
    series instead of the scalar maximum. Identical formula:
    ``(peak - equity) / peak`` for each point, with the running peak
    monotonically updated as the curve advances.
    """
    out: list[DrawdownPoint] = []
    peak: Decimal | None = None
    for snap in equity_curve:
        eq = snap.equity
        if peak is None or eq > peak:
            peak = eq
        if peak <= 0:
            dd = 0.0
        else:
            dd = float((peak - eq) / peak)
        out.append(DrawdownPoint(timestamp_ms=to_ms_utc(snap.timestamp), drawdown_pct=dd))
    return out


def _trade_to_run_trade(i: int, t: LoggedTrade, resolution_minutes: int) -> RunTrade:
    return RunTrade(
        trade_number=i + 1,
        entry_time_ms=to_ms_utc(t.entry_time),
        entry_price=float(t.entry_price),
        exit_time_ms=to_ms_utc(t.exit_time),
        exit_price=float(t.exit_price),
        indicators_at_entry={k: float(v) for k, v in t.indicators.items()},
        pnl_pts=float(t.pnl_pts),
        pnl_pct=float(t.pnl_pct),
        result=t.result,  # type: ignore[arg-type]
        signal_reason=t.signal_reason,
        bars_held=_bars_held(t.entry_time, t.exit_time, resolution_minutes),
    )


def _summarize_metrics(
    initial_cash: float,
    final_equity: float,
    trades: list[LoggedTrade],
    equity_curve: list[EquitySnapshot],
    bars_held_total: int,
    total_bars: int,
    resolution_minutes: int,
) -> RunMetrics:
    """Project ``statistics.summarize`` output onto the typed ``RunMetrics``.

    Formula: exposure_pct = min(1, max(0, bars_held_total * resolution_minutes / total_bars)).
    Reference: Internal run-ledger metric; see docs/references/run-ledger.md.
    Canonical implementation: this file.
    Validated against: tests/research/runs/test_runner_inmemory.py::test_exposure_uses_consolidated_bar_resolution

    ``summarize`` returns a flat ``dict[str, float | int | None]`` with
    inf/-inf/NaN already coerced to None. We fan it into the typed
    model and add two derived fields (``exposure_pct``, ``avg_trade_bars``)
    that are easy to compute here from the bar / trade data.
    """
    # statistics.summarize wants a sequence of EquityPoints if we want
    # the daily-resampled Sharpe path; build it from minute-bar snapshots.
    eq_points = [EquityPoint(timestamp=s.timestamp, equity=float(s.equity)) for s in equity_curve]

    # Trading-day count derived from distinct calendar dates in the
    # equity curve. Avoids a wall-clock-day overcount for short windows
    # that span weekends.
    trading_days = len({s.timestamp.date() for s in equity_curve}) if equity_curve else 0

    flat = summarize(
        initial_cash=initial_cash,
        final_equity=final_equity,
        trades=trades,
        trading_days=trading_days or None,
        equity_curve=eq_points,
    )

    exposure: float | None = None
    if total_bars > 0:
        held_base_bars = bars_held_total * resolution_minutes
        exposure = max(0.0, min(1.0, held_base_bars / total_bars))

    avg_bars: float | None = None
    if trades:
        avg_bars = bars_held_total / len(trades)

    return RunMetrics(
        total_trades=int(flat["total_trades"] or 0),
        winning_trades=int(flat["winning_trades"] or 0),
        losing_trades=int(flat["losing_trades"] or 0),
        win_rate=flat["win_rate"],  # type: ignore[arg-type]
        total_return_pct=float(flat["net_profit_pct"] or 0.0),
        max_drawdown_pct=flat["max_drawdown_pct"],  # type: ignore[arg-type]
        sharpe_ratio=flat["sharpe_ratio"],  # type: ignore[arg-type]
        sortino_ratio=flat["sortino_ratio"],  # type: ignore[arg-type]
        profit_factor=flat["profit_factor"],  # type: ignore[arg-type]
        expectancy_pct=flat["expectancy_pct"],  # type: ignore[arg-type]
        payoff_ratio=flat["payoff_ratio"],  # type: ignore[arg-type]
        exposure_pct=exposure,
        avg_trade_bars=avg_bars,
    )


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------
DataSourceFactory = Callable[[str, Date, Date], Any]


def run_strategy_spec(
    request: RunRequest,
    *,
    data_source_factory: DataSourceFactory,
    data_root_revision: str | None = None,
    run_id: str | None = None,
) -> tuple[RunLedger, BacktestRunResult]:
    """Run one ``StrategySpec`` and return its ledger plus result.

    ``data_source_factory`` is a callable ``(symbol, start, end) -> reader``
    that mirrors the dependency in ``app/routers/spec_strategy.py``.
    Tests inject a fake; the FastAPI router injects the real LEAN reader.

    ``data_root_revision`` is normally captured by
    ``resolve_data_root_revision()``; tests pass a deterministic value
    so the ledger identity is stable across runs.
    """
    spec = request.spec
    # Normalize the fill mode early so the ledger and the engine see the
    # same canonical form, and accept the same hyphen/case variants the
    # HTTP layer accepts.
    fill_mode_norm = _normalize_fill_mode(request.fill_mode)
    if fill_mode_norm not in _VALID_FILL_MODES:
        raise ValueError(f"unknown fill_mode {request.fill_mode!r} — expected one of {sorted(_VALID_FILL_MODES)}")
    if request.start_date >= request.end_date:
        raise ValueError(
            f"start_date must be strictly before end_date (got start={request.start_date}, end={request.end_date})"
        )
    # ``StrategySpec`` validates ``len(symbols) == 1`` at construction (Phase 1
    # boundary), so multi-symbol specs can't reach here. Still guard against
    # an empty list — that's the one shape Pydantic admits at the type level
    # but the engine can't run.
    if not spec.symbols:
        raise ValueError("StrategySpec.symbols must contain exactly one symbol")

    symbol = spec.symbols[0]
    resolution = spec.resolution.period_minutes
    start_ms = _date_to_ny_midnight_ms(request.start_date)
    end_ms = _date_to_ny_midnight_ms(request.end_date)
    # Truthy check rather than ``is not None``: an empty string would
    # produce a trailing-pipe ``data_snapshot_id`` indistinguishable
    # from a regression in ``resolve_data_root_revision``. Treat it as
    # missing and resolve the real revision.
    # TODO(phase-e): memoize ``resolve_data_root_revision()`` keyed on
    # ``LEAN_DATA_ROOT`` so 10×10 sensitivity sweeps don't pay for 100
    # ``git rev-parse`` subprocess starts.
    revision = data_root_revision if data_root_revision else resolve_data_root_revision(
        symbol=symbol,
        start_date=request.start_date,
        end_date=request.end_date,
    )

    spec_dump = spec.model_dump(mode="json")
    spec_hash = hash_payload(spec_dump)
    snapshot_id = make_data_snapshot_id(
        symbol=symbol,
        resolution_minutes=resolution,
        start_ms=start_ms,
        end_ms=end_ms,
        data_root_revision=revision,
    )

    rid = run_id or uuid.uuid4().hex
    window_summary = summarize_window(request.start_date, request.end_date)
    ledger = RunLedger(
        run_id=rid,
        parent_run_id=request.parent_run_id,
        parent_spec_hash=request.parent_spec_hash,
        strategy_spec_id=request.strategy_spec_id or spec.name,
        strategy_spec_hash=spec_hash,
        strategy_spec_json=spec_dump,
        engine_git_commit=_capture_git_commit(),
        symbol=symbol,
        resolution_minutes=resolution,
        start_ms=start_ms,
        end_ms=end_ms,
        initial_cash=request.initial_cash,
        fill_mode=fill_mode_norm,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        data_snapshot_id=snapshot_id,
        window_summary=window_summary,
    )

    # Build the data source. Failures here are infrastructure errors,
    # not strategy errors — surface as a failed-status ledger rather
    # than a thrown exception so the caller can persist the failure.
    try:
        data_source = data_source_factory(symbol, request.start_date, request.end_date)
    except Exception as exc:
        logger.exception("[RUNS] data source unavailable for %s", symbol)
        return _failed(ledger, f"data source unavailable: {exc}")

    # ML predictions-as-data (v0.5): if the spec references a prediction
    # set, load + validate it before constructing the strategy so the
    # runner can fail-fast with a ``failed`` ledger rather than letting
    # the SpecAlgorithm constructor raise. Coverage is checked here too:
    # the loader knows nothing about which bars the engine will see; the
    # runner does (data source + resolution).
    prediction_set = None
    prediction_set_hash: str | None = None
    if spec.predictions:
        from app.research.ml.coverage import (
            assert_bar_clock_coverage,
            iter_consolidated_bars,
        )
        from app.research.ml.loader import PredictionSet

        # v0.5 admits at most one prediction_set_id per spec (validated by
        # ``StrategySpec._check_phase1_boundaries``); take the first.
        set_id = spec.predictions[0].prediction_set_id
        artifact_dir = _prediction_artifacts_root() / set_id
        try:
            prediction_set = PredictionSet.load(artifact_dir)
        except Exception as exc:
            logger.exception("[RUNS] prediction set load failed: %s", set_id)
            return _failed(ledger, f"prediction set {set_id!r} failed to load: {exc}")
        try:
            prediction_set.assert_pairs_with(spec)
        except Exception as exc:
            return _failed(
                ledger,
                f"prediction set {set_id!r} does not pair with spec: {exc}",
            )
        try:
            bar_stream = iter_consolidated_bars(
                data_source,
                symbol=symbol,
                start_date=request.start_date,
                end_date=request.end_date,
                resolution_minutes=resolution,
            )
            assert_bar_clock_coverage(prediction_set, bar_stream, refs=spec.predictions)
        except Exception as exc:
            return _failed(
                ledger,
                f"prediction set {set_id!r}: bar-clock coverage failed: {exc}",
            )

        prediction_set_hash = prediction_set.manifest.prediction_set_hash
        ledger = ledger.model_copy(update={"prediction_set_hash": prediction_set_hash})

    # Construct the spec algorithm; the constructor raises NotImplementedError
    # for forward-compat spec features (FixedContracts, OPTION_TEMPLATE,
    # non-CLOSE_ALL survival actions, pyramiding != 1) and that propagates.
    try:
        strategy = SpecAlgorithm(spec, prediction_set=prediction_set)
    except NotImplementedError as exc:
        return _failed(ledger, f"spec uses unsupported feature: {exc}")

    # Patch the strategy's initialize to honor the request's date window
    # and cash override. Same trick as spec_strategy.py's router.
    orig_init = strategy.initialize

    def _patched_init() -> None:
        orig_init()
        strategy.set_start_date(request.start_date.year, request.start_date.month, request.start_date.day)
        strategy.set_end_date(request.end_date.year, request.end_date.month, request.end_date.day)
        strategy.set_cash(request.initial_cash)

    strategy.initialize = _patched_init  # type: ignore[assignment]

    fill_mode = _parse_fill_mode(fill_mode_norm)
    engine = BacktestEngine(
        data_source=data_source,
        fill_model=FillModel(
            mode=fill_mode,
            commission_per_order=Decimal(str(request.commission_per_order)),
            slippage_per_share=Decimal(str(request.slippage_per_share)),
        ),
    )

    try:
        engine_result: BacktestResult = engine.run(strategy)
    except NotImplementedError as exc:
        return _failed(ledger, f"spec uses unsupported feature at runtime: {exc}")
    except Exception as exc:
        logger.exception("[RUNS] backtest failed for run_id=%s spec=%s", rid, spec.name)
        return _failed(ledger, f"backtest run failed: {exc}")

    trades = strategy.trade_log
    bars_held_total = sum(_bars_held(t.entry_time, t.exit_time, resolution) for t in trades)
    total_bars = len(engine_result.equity_curve)
    # ``engine_result.bars`` is appended once per minute bar pulled from
    # the data source's ``iter_bars`` loop — the engine-input layer. That
    # is the count we want to surface (not consolidated-bar updates, not
    # indicator ticks): a "0 bars consumed" signal means the LEAN cache
    # was empty or the window filtered everything out, which would
    # otherwise be indistinguishable from "strategy didn't fire".
    bars_consumed = len(engine_result.bars)

    metrics = _summarize_metrics(
        initial_cash=float(engine_result.initial_cash),
        final_equity=float(engine_result.final_equity),
        trades=trades,
        equity_curve=engine_result.equity_curve,
        bars_held_total=bars_held_total,
        total_bars=total_bars,
        resolution_minutes=resolution,
    )

    warnings: list[str] = []
    if bars_consumed == 0:
        warnings.append(
            "no input bars consumed for the requested window — "
            "check LEAN data root / cache or your symbol+date filters"
        )

    result = BacktestRunResult(
        run_id=rid,
        initial_cash=float(engine_result.initial_cash),
        final_equity=float(engine_result.final_equity),
        equity_curve=[
            EquityCurvePoint(timestamp_ms=to_ms_utc(s.timestamp), equity=float(s.equity))
            for s in engine_result.equity_curve
        ],
        drawdown_curve=_build_drawdown_curve(engine_result.equity_curve),
        trades=[_trade_to_run_trade(i, t, resolution) for i, t in enumerate(trades)],
        metrics=metrics,
        log_lines=list(engine_result.log_lines),
        warnings=warnings,
        bars_consumed=bars_consumed,
    )

    # Hash the result subcomponents. ``run_id`` is excluded so two runs
    # with the same inputs but different UUIDs share a ``result_hash``;
    # ``log_lines`` is excluded because human-formatted timestamps drift
    # across replays even when the math is identical.
    result_payload = result.model_dump(mode="json", exclude={"run_id", "log_lines"})
    trade_payload = [t.model_dump(mode="json") for t in result.trades]
    metrics_payload = result.metrics.model_dump(mode="json")

    ledger = ledger.model_copy(
        update={
            "result_hash": hash_payload(result_payload),
            "trade_log_hash": hash_payload(trade_payload),
            "metrics_hash": hash_payload(metrics_payload),
            "completed_at_ms": now_ms_utc(),
            "status": "completed",
        }
    )
    return ledger, result


def _failed(ledger: RunLedger, reason: str) -> tuple[RunLedger, BacktestRunResult]:
    """Build an empty result paired with a failed-status ledger.

    Lets the caller persist failures uniformly without distinguishing
    "the run itself failed" from "the runner crashed". The result has
    an empty equity curve and trade list; metrics are zeros. Result
    hashes are computed over those zeroed payloads so two failed runs
    with the same identity columns hash identically (useful for the
    "did this fail before with the same inputs?" query in storage).
    """
    empty_metrics = RunMetrics(
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        total_return_pct=0.0,
    )
    empty_result = BacktestRunResult(
        run_id=ledger.run_id,
        initial_cash=ledger.initial_cash,
        final_equity=ledger.initial_cash,
        metrics=empty_metrics,
        warnings=[reason],
    )
    result_payload = empty_result.model_dump(mode="json", exclude={"run_id", "log_lines"})
    trade_payload: list[dict] = []
    metrics_payload = empty_metrics.model_dump(mode="json")
    failed_ledger = ledger.model_copy(
        update={
            "result_hash": hash_payload(result_payload),
            "trade_log_hash": hash_payload(trade_payload),
            "metrics_hash": hash_payload(metrics_payload),
            "completed_at_ms": now_ms_utc(),
            "status": "failed",
            "failure_reason": reason,
        }
    )
    return failed_ledger, empty_result
