"""The engine backtest workflow behind ``POST /api/engine/backtest`` and its callers.

``execute_engine_backtest`` is the one entry point: the sync endpoint, the
Strategy Lab job worker, Grid Search and Walk-Forward cells and the Recency
runner all converge on it. It holds the engine gate and runs the workflow's
three stages — :func:`_execute_engine_backtest_core` →
:func:`_aggregate_backtest_response` → :func:`_persist_and_dispatch_companion`.

Relocated from ``app/routers/engine.py`` (#1999), which keeps the route's
request validation, response shape and HTTP error translation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, BacktestResult
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.execution.sizing import LeanSetHoldingsSizing
from app.engine.results.lean_statistics import compute_lean_statistics
from app.engine.results.statistics import summarize
from app.engine.results.trade_record import TradeRecord
from app.engine.run_gate import one_backtest_in_flight
from app.engine.strategy.base import LoggedTrade, Strategy
from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    StrategyRegistration,
    hidden_params_present,
)
from app.jobs.phases import friendly
from app.models.responses import (
    LeanPortfolioStatsResponse,
    LeanRuntimeStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.research.backtest_runs.service import persist_engine_response_sync
from app.research.sweep.snapshot import ManifestBoundDailyReader, ManifestBoundMinuteReader
from app.schemas.engine_backtest import (
    EngineBacktestRequest,
    EngineBacktestResponse,
    EngineEvaluationWindowResponse,
    EngineTradeResponse,
    _EngineBarsSpecModel,
    _EngineDataPolicyModel,
)
from app.schemas.engine_validation import EngineValidationAnalyticsResponse
from app.services.engine_validation_analytics import (
    ValidationEquityPoint,
    ValidationTrade,
    build_compatibility_equity_curve,
    compute_engine_validation_analytics,
)
from app.services.parity_companion import (
    COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1,
    dispatch_parity_companion,
    new_parity_group_id,
)
from app.services.run_verdict_service import compute_run_verdict
from app.utils.session_anchors import et_day_end_ms, et_midnight_ms, persisted_execution_configuration

logger = logging.getLogger(__name__)


def _reject_hidden_params(reg: StrategyRegistration, params: dict[str, Any]) -> None:
    hidden = hidden_params_present(reg, params)
    if hidden:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "params_errors": [
                    {
                        "loc": ["params", name],
                        "msg": "Parameter is live-runtime only and is not supported by Engine Lab backtests.",
                        "type": "value_error.live_only_param",
                    }
                    for name in hidden
                ]
            },
        )


def _resolve_lean_data_roots(*, adjusted: bool) -> list[Path]:
    """Return the ordered list of roots the reader should search.

    Delegates to the policy-keyed bar store: the lake alone when the flag is
    on (its own root selected by adjustment mode), otherwise the reference
    mount first (so the bit-exact SPY fixture always wins) then the policy
    cache root for the requested adjustment mode. See
    :mod:`app.engine.data.policy_store` for the layout and the adjusted-vs-raw
    seam bug this keying fixes.

    This used to translate ``LakeAdjustmentUnsupportedError`` into a 409:
    with the lake on, an adjusted request had nowhere to go, and a default
    Strategy Lab backtest — which asks for adjusted bars — was refused
    outright. #1866 gave the lake root an adjustment segment, so an adjusted
    request now resolves a different directory and there is no refusal left
    to translate.
    """
    return resolve_data_roots(source="polygon", adjusted=adjusted)


def _policy_adjusted(data_policy: _EngineDataPolicyModel | None) -> bool:
    """Adjustment mode for root resolution; legacy requests default adjusted.

    Matches the legacy synthesizer's ``adjusted=True`` so pre-DataPolicy
    callers keep reading the tree their runs have always used.
    """
    return data_policy.adjusted if data_policy is not None else True


class ResolvedRunConfiguration(BaseModel):
    """The concrete configuration the initialized strategy actually executed.

    ``start_date`` is the evaluation start — the date the persisted study's
    figures begin — and ``warmup_start_date`` the earlier boundary the run
    read from when it was primed (``None`` for an ordinary run).
    """

    start_date: str
    end_date: str
    resolution: Literal["minute", "daily"]
    parameters: dict[str, Any]
    warmup_start_date: str | None = None


# ---------------------------------------------------------------------------
# Phase callbacks
#
# Both the synchronous /backtest endpoint and the Jobs-system worker call
# ``execute_engine_backtest`` (which gates, then runs
# ``_execute_engine_backtest_core``) to do the actual run. They
# differ only in how progress is reported: the sync path passes no-op
# callbacks (the response is the only signal); the Jobs worker forwards
# every phase/log into a ProgressEmitter that writes Redis events the
# .NET SSE layer streams to the browser. Keeping this as a callback pair
# avoids importing ProgressEmitter into the hot path.
# ---------------------------------------------------------------------------
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str], None]


def _new_parity_group_id_for(requested_engine: Literal["python", "both"]) -> str | None:
    """Mint a parity group only for an operator-requested paired run."""
    return new_parity_group_id() if requested_engine == "both" else None


def _dispatch_requested_parity_companion(
    *,
    registration: StrategyRegistration,
    request: EngineBacktestRequest,
    parity_group_id: str | None,
    study_id: int | None,
    validated_parameters: dict[str, Any] | None = None,
) -> None:
    """Dispatch LEAN only when the persisted Python run belongs to a pair."""
    if study_id is None or parity_group_id is None:
        return
    dispatch_parity_companion(
        registration=registration,
        request=request,
        parity_group_id=parity_group_id,
        left_execution_id=study_id,
        validated_parameters=validated_parameters,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_fill_mode(raw: str) -> FillMode:
    key = raw.strip().lower()
    if key in ("signal_bar_close", "signalbarclose", "close"):
        return FillMode.SIGNAL_BAR_CLOSE
    if key in ("next_bar_open", "nextbaropen", "open"):
        return FillMode.NEXT_BAR_OPEN
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown fill_mode '{raw}'. Expected signal_bar_close or next_bar_open.",
    )


def _apply_overrides(strategy: Strategy, req: EngineBacktestRequest) -> None:
    """Apply request-level overrides on top of the strategy's own defaults.

    The strategy's ``initialize`` has already run by the time this is
    called, so any override here replaces the value set by the algorithm.
    """
    data_start = req.warmup_from_date or req.from_date
    if data_start:
        # The strategy's start is where the engine begins READING. When a
        # warmup boundary is supplied that is earlier than the evaluation
        # start, which ``execute_engine_backtest`` hands to the engine
        # separately so scoring is scoped to ``from_date`` onward.
        d = datetime.strptime(data_start, "%Y-%m-%d")
        strategy.set_start_date(d.year, d.month, d.day)
    if req.to_date:
        d = datetime.strptime(req.to_date, "%Y-%m-%d")
        strategy.set_end_date(d.year, d.month, d.day)
    if req.initial_cash is not None:
        strategy.set_cash(req.initial_cash)


def _resolve_legacy_data_policy(
    request: EngineBacktestRequest,
    validated_params: Any,
) -> None:
    """Fill the legacy policy from validated defaults before any bar read."""
    if request.data_policy is not None:
        return
    symbol = getattr(validated_params, "symbol", None)
    if not isinstance(symbol, str) or not symbol.strip():
        return
    timespan: Literal["minute", "day"] = "day" if request.resolution == "daily" else "minute"
    request.data_policy = _EngineDataPolicyModel(
        source="polygon",
        symbol=symbol.strip().upper(),
        adjusted=True,
        session="regular",
        input_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
        strategy_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
    )


def _resolved_run_configuration(
    *,
    request: EngineBacktestRequest,
    registration: StrategyRegistration,
    validated_params: Any,
    strategy: Strategy,
) -> ResolvedRunConfiguration:
    """Snapshot defaults and overrides after strategy initialization."""
    if strategy.start_date is None or strategy.end_date is None:
        raise RuntimeError("initialized strategy did not resolve an execution date window")
    parameters = validated_params.model_dump(mode="json", exclude=registration.hidden_params)
    window = _evaluation_dates(request, strategy)
    return ResolvedRunConfiguration(
        start_date=window.evaluation_start.isoformat(),
        end_date=window.evaluation_end.isoformat(),
        resolution=request.resolution,
        parameters=parameters,
        warmup_start_date=window.data_start.isoformat() if window.primed else None,
    )


@dataclass(frozen=True)
class _EvaluationDates:
    """The interval table as inclusive ET trading dates, for the router's own arithmetic."""

    data_start: date
    evaluation_start: date
    evaluation_end: date
    primed: bool

    def response(self) -> EngineEvaluationWindowResponse:
        return EngineEvaluationWindowResponse(
            data_start_ms=et_midnight_ms(self.data_start),
            evaluation_start_ms=et_midnight_ms(self.evaluation_start),
            evaluation_end_ms=et_day_end_ms(self.evaluation_end),
            warmup_primed=self.primed,
        )


def _evaluation_dates(request: EngineBacktestRequest, strategy: Strategy) -> _EvaluationDates:
    """The interval table for an initialized strategy under ``request``."""
    if strategy.start_date is None or strategy.end_date is None:
        raise RuntimeError("initialized strategy did not resolve an execution date window")
    data_start = strategy.start_date.date()
    primed = request.warmup_from_date is not None
    # ``_apply_overrides`` pointed the strategy at the warmup boundary, so the
    # evaluation start is the request's own ``from_date`` — validated to be
    # present and later than the warmup start whenever one was supplied.
    evaluation_start = date.fromisoformat(request.from_date) if primed and request.from_date else data_start
    return _EvaluationDates(data_start=data_start, evaluation_start=evaluation_start, evaluation_end=strategy.end_date.date(), primed=primed)


def _evaluation_start_ms(request: EngineBacktestRequest) -> int | None:
    """ET-midnight ``int64 ms UTC`` anchor of the evaluation start, if primed."""
    if request.warmup_from_date is None or request.from_date is None:
        return None
    return et_midnight_ms(_parse_iso_date(request.from_date, "from_date"))


def _format_trade(index: int, trade: Any) -> EngineTradeResponse:
    # ``indicators`` is a dict[str, Decimal] on ``LoggedTrade``; convert to
    # plain floats for JSON serialization.
    raw_indicators = getattr(trade, "indicators", None) or {}
    indicators = {k: float(v) for k, v in raw_indicators.items()}
    return EngineTradeResponse(
        trade_number=index,
        entry_time=trade.entry_time_ms,
        entry_price=float(trade.entry_price),
        exit_time=trade.exit_time_ms,
        exit_price=float(trade.exit_price),
        quantity=int(trade.quantity),
        indicators=indicators,
        pnl_pts=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        result=trade.result,
        signal_reason=getattr(trade, "signal_reason", "") or "",
        is_synthetic_exit=bool(getattr(trade, "is_synthetic_exit", False)),
    )


def _format_trade_record(index: int, trade: Any, cumulative_pnl_pct: float) -> TradeRecord:
    raw_indicators = getattr(trade, "indicators", None) or {}
    return TradeRecord(
        trade_number=index,
        trade_type="Buy",  # engine strategies are long-only for now
        entry_timestamp=trade.entry_time_ms,
        exit_timestamp=trade.exit_time_ms,
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price),
        pnl=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        cumulative_pnl_pct=cumulative_pnl_pct,
        signal_reason=getattr(trade, "signal_reason", "") or "",
        indicator_snapshot={k: float(v) for k, v in raw_indicators.items()},
    )


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: expected YYYY-MM-DD, got {value!r}",
        ) from exc


def _build_backtest_engine(
    *,
    reader: LeanMinuteDataReader | LeanDailyDataReader,
    execution_config: ExecutionConfig,
    request: EngineBacktestRequest,
) -> BacktestEngine:
    """Apply the selected execution contract at one construction boundary."""
    if request.compatibility_profile is None:
        return BacktestEngine(
            data_source=reader,
            execution_config=execution_config,
        )

    fee_model = IbkrEquityCommissionModel()
    return BacktestEngine(
        data_source=reader,
        execution_config=execution_config,
        sizing_model=LeanSetHoldingsSizing(fee_model=fee_model),
        fill_model=FillModel(
            mode=_parse_fill_mode(request.fill_mode),
            fee_model=fee_model,
            fill_stale_signal_at_current_open=True,
        ),
    )


def _pin_compatibility_fixture(
    request: EngineBacktestRequest,
    data_roots: list[Path],
) -> None:
    """Freeze the exact minute-zip bytes consumed by a compatibility pair."""
    if request.compatibility_profile != COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1:
        return
    if request.data_policy is None or request.from_date is None or request.to_date is None:
        return

    from app.engine.data.policy_store import snapshot_minute_trade_zips

    receipt = snapshot_minute_trade_zips(
        data_roots,
        symbol=request.data_policy.symbol,
        start=_parse_iso_date(request.from_date, "from_date"),
        end=_parse_iso_date(request.to_date, "to_date"),
        adjusted=request.data_policy.adjusted,
        session=request.data_policy.session,
    )
    request.data_policy.provider_kind = "fixture"
    request.data_policy.fixture_id = str(receipt["fixture_id"])
    request.data_policy.fixture_sha256 = str(receipt["fixture_sha256"])


def _materialize_missing_bars(
    *,
    request: EngineBacktestRequest,
    symbol: str,
    start: date,
    end: date,
    data_roots: list[Path],
    on_log: LogCallback,
) -> str | None:
    """Put the run's bars on disk before the reader looks for them.

    One materializer. The lake fetches only the missing days, records every
    artifact in the catalog, and hands back the fingerprint this function
    passes on (see ``materialize_engine_run`` for what that fingerprint does
    and does not cover). #1893 retired the pre-lake policy-store export that
    used to be the other half of this decision, so there is no longer a
    branch here for ``_resolve_lean_data_roots`` to have to agree with.
    """
    # Lazy: the lake pulls in the catalog + provider stack, and this module is
    # imported by surfaces that never materialize anything.
    from app.data_lake.run_materialization import LakeMaterializationError, materialize_engine_run
    from app.data_lake.types import polygon_mode_for

    try:
        materialized = materialize_engine_run(
            symbol=symbol,
            start=start,
            end=end,
            resolution=request.resolution,
            price_adjustment_mode=polygon_mode_for(_policy_adjusted(request.data_policy)),
            requester=request.strategy_name,
        )
    except LakeMaterializationError as exc:
        # The operator reads the run log, not the service log. A refusal that
        # surfaces only as a generic "auto_fetch failed" tells them nothing
        # about which bars the lake could not produce.
        on_log(f"Lake refused this run: {exc}")
        raise
    on_log(
        f"Lake: fetched {materialized.fetched_artifact_count}, "
        f"reused {materialized.reused_artifact_count} artifact(s)"
    )
    if materialized.incomplete_summary:
        # The lake judged this harmless for the run's resolution, but the
        # operator should still see it beside the numbers rather than only in
        # the service log.
        on_log(
            f"Lake: incomplete — {materialized.incomplete_summary}; "
            f"the {request.resolution} bars this run reads did materialize"
        )
    return materialized.availability_hash


def _failed_backtest_response(request: EngineBacktestRequest, error: str) -> EngineBacktestResponse:
    """The engine's failure envelope: a *reported* failure, never a raised 500.

    Every caller — Strategy Lab, the Jobs worker, a Grid Search cell, a
    Walk-Forward fold — reads ``success``/``error``. An exception escaping
    ``execute_engine_backtest`` instead reaches them as an unhandled 500 or an
    opaque cell failure, so each failure path inside it converges here.
    """
    return EngineBacktestResponse(
        success=False,
        strategy_name=request.strategy_name,
        fill_mode=request.fill_mode,
        initial_cash=0.0,
        final_equity=0.0,
        net_profit=0.0,
        total_fees=0.0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        error=error,
    )


def execute_engine_backtest(
    *,
    request: EngineBacktestRequest,
    on_phase: PhaseCallback,
    on_log: LogCallback,
    data_manifest: Mapping[str, str] | None = None,
    while_waiting: Callable[[], None] = lambda: None,
) -> EngineBacktestResponse:
    """Core backtest workflow shared by the sync POST and the Jobs worker.

    Both call paths converge here. ``on_phase`` and ``on_log`` are
    callbacks the worker uses to forward to a ProgressEmitter; the sync
    path passes no-ops. Raises HTTPException for client errors; returns
    an EngineBacktestResponse with ``success=False`` for engine
    failures.

    ``data_manifest`` — root-relative artifact path to sha256, as captured
    by ``app.research.sweep.snapshot`` — binds every read to receipted
    bytes: a sweep cell whose lake artifact changed after the snapshot
    fails rather than consuming unreceipted data (PRD #1926 F05). Absent
    for ordinary runs, which read whatever the lake currently holds.

    This holds the *outer* of the two engine-gate holds. ``BacktestEngine.run``
    holds the gate too, which is what makes every engine run counted (#1990);
    this one is wider on purpose, because a run's ~480 MB is live through the
    auto-fetch that precedes it and the response that outlives it, not just the
    simulation (``app.engine.run_gate``, #1957). The inner acquire passes
    through this one. A caller that has to wait reports the
    ``waiting_for_engine`` phase and then queues.

    ``while_waiting`` runs about once a second for as long as the caller is
    queued, and is where a job worker puts its cancellation check; raising
    from it abandons the wait. Without it a queued run could not be
    cancelled until the run ahead of it finished.
    """
    with one_backtest_in_flight(on_wait=lambda: _report_waiting(on_phase, on_log), while_waiting=while_waiting):
        return _execute_engine_backtest_core(
            request=request,
            on_phase=on_phase,
            on_log=on_log,
            data_manifest=data_manifest,
        )


def _report_waiting(on_phase: PhaseCallback, on_log: LogCallback) -> None:
    # The copy comes from the phase vocabulary rather than being written
    # again here, so there is one label per phase id (``app.jobs.phases``).
    on_phase("waiting_for_engine")
    on_log(friendly("engine_backtest", "waiting_for_engine"))


def _execute_engine_backtest_core(
    *,
    request: EngineBacktestRequest,
    on_phase: PhaseCallback,
    on_log: LogCallback,
    data_manifest: Mapping[str, str] | None = None,
) -> EngineBacktestResponse:
    """The workflow itself, under the gate :func:`execute_engine_backtest` holds."""
    _run_start = time.time()
    registration = _STRATEGY_REGISTRY.get(request.strategy_name)
    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Unknown strategy '{request.strategy_name}'. Registered: {sorted(_STRATEGY_REGISTRY)}"),
        )

    if request.resolution not in registration.supported_resolutions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Strategy '{request.strategy_name}' does not support "
                f"resolution '{request.resolution}'. Supported: "
                f"{sorted(registration.supported_resolutions)}"
            ),
        )

    try:
        _reject_hidden_params(registration, request.params)
        validated_params = registration.param_schema.model_validate(request.params)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "strategy": request.strategy_name,
                "params_errors": exc.errors(),
            },
        )

    _resolve_legacy_data_policy(request, validated_params)

    fill_mode = _parse_fill_mode(request.fill_mode)

    data_roots = _resolve_lean_data_roots(adjusted=_policy_adjusted(request.data_policy))
    if not data_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)",
        )

    strategy = registration.build(validated_params)

    # Fingerprint of the exact lake bytes this run consumed. Stays None when
    # the lake is off, or when nothing was materialized — the run then has
    # nothing to claim about which bytes it read.
    lake_manifest: str | None = None

    if request.auto_fetch:
        symbol = getattr(validated_params, "symbol", None)
        # A primed run reads from the warmup boundary, so that is what the
        # lake must hold — not just the scored window.
        start_override = request.warmup_from_date or request.from_date
        end_override = request.to_date
        if symbol and start_override and end_override:
            on_phase("fetching_data")
            on_log(f"Ensuring {symbol} {request.resolution} bars {start_override} → {end_override}")
            try:
                lake_manifest = _materialize_missing_bars(
                    request=request,
                    symbol=symbol,
                    start=_parse_iso_date(start_override, "start_date"),
                    end=_parse_iso_date(end_override, "end_date"),
                    data_roots=data_roots,
                    on_log=on_log,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception(
                    "[ENGINE] auto_fetch failed for %s %s..%s",
                    symbol,
                    start_override,
                    end_override,
                )
                return _failed_backtest_response(request, f"auto_fetch failed: {exc}")

    try:
        _pin_compatibility_fixture(request, data_roots)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"compatibility_fixture_unavailable: {exc}",
        ) from exc

    reader: LeanMinuteDataReader | LeanDailyDataReader
    if request.resolution == "daily":
        reader = (
            LeanDailyDataReader(data_roots)
            if data_manifest is None
            else ManifestBoundDailyReader(data_roots, data_manifest)
        )
    else:
        # Honor the request's ``data_policy.session`` so the reader drops
        # extended-hours bars when the operator asked for the regular session.
        # Before this was wired, the policy value round-tripped through the
        # response but never reached the reader, and Polygon-sourced caches
        # (which retain pre/post-market by design) silently fed 04:00-20:00 ET
        # bars to the consolidator. See ``.claude/rules/numerical-rigor.md``
        # → ``DECISION_MISMATCH`` and the divergence trace at
        # ``StrategyExecutions`` rows 41/42 (run on 2026-05-21).
        session_mode = "regular"
        if request.data_policy is not None:
            session_mode = request.data_policy.session
        reader = (
            LeanMinuteDataReader(data_roots, session=session_mode)
            if data_manifest is None
            else ManifestBoundMinuteReader(data_roots, data_manifest, session=session_mode)
        )
    execution_config = ExecutionConfig(
        fill_mode=fill_mode,
        commission_per_order=Decimal(str(request.commission_per_order)),
        slippage_per_share=Decimal(str(request.slippage_per_share)),
        session_entry_cutoff=request.session_entry_cutoff,
        force_flat_at=request.force_flat_at,
        limit_penetration=Decimal(str(request.limit_penetration)),
    )
    engine = _build_backtest_engine(
        reader=reader,
        execution_config=execution_config,
        request=request,
    )

    original_initialize = strategy.initialize

    def _wrapped_initialize() -> None:
        original_initialize()
        _apply_overrides(strategy, request)

    strategy.initialize = _wrapped_initialize  # type: ignore[assignment]

    # Decompose the old monolithic "simulating" phase into the two stages
    # the engine walks through during ``engine.run``. The engine itself
    # is a single call from our side, so both phases fire back-to-back
    # immediately before invocation — they're contractually-ordered
    # markers, not progress checkpoints inside the engine loop.
    on_phase("consolidating_bars")
    on_log("Consolidating raw bars to strategy resolution")
    on_phase("running_indicators")
    on_log(f"Running {request.strategy_name} on {getattr(validated_params, 'symbol', '?')} ({request.resolution})")

    try:
        result = engine.run(
            strategy,
            evaluation_start_ms=_evaluation_start_ms(request),
            retain_bars=not request.summary_only,
        )
    except Exception as exc:
        logger.exception("[ENGINE] Backtest failed for %s", request.strategy_name)
        on_log(f"Engine error: {exc}")
        return _failed_backtest_response(request, str(exc))

    response = _aggregate_backtest_response(
        result=result,
        request=request,
        strategy=strategy,
        lake_manifest=lake_manifest,
        on_phase=on_phase,
        on_log=on_log,
    )
    if not response.success:
        # A reported failure is never written to history. Before this workflow
        # was split, the aggregation half's ``return`` left the function
        # outright and persistence was simply unreachable; the split turns that
        # into an explicit guard rather than a property of where the ``return``
        # happened to sit.
        return response

    return _persist_and_dispatch_companion(
        response=response,
        request=request,
        registration=registration,
        validated_params=validated_params,
        strategy=strategy,
        run_started_at=_run_start,
        on_phase=on_phase,
        on_log=on_log,
    )


# ---------------------------------------------------------------------------
# Backtest workflow stages, in the order a run walks them
# ---------------------------------------------------------------------------
def _lean_parity_statistics(*, result: BacktestResult, trades: list[LoggedTrade]) -> LeanStatisticsResponse | None:
    """LEAN-comparable statistics for this run, or ``None`` when they cannot be had.

    Best-effort by contract, which is the reason it is a function rather than
    a block: buried mid-aggregation its ``except Exception: log`` read as a
    shrug and there was no name for what a caller gets when it fires. ``None``
    means the comparison is unavailable for this run — no bars, no trades, or
    the computation itself refused — and the response carries that absence
    rather than a wrong number.
    """
    lean_stats_resp: LeanStatisticsResponse | None = None
    if result.bars and trades:
        try:
            # Convert retained TradeBar objects → DataFrame with timestamp + close
            bar_records = [
                {
                    "timestamp": b.start_ms,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
                for b in result.bars
            ]
            df = pd.DataFrame(bar_records)

            # Convert LoggedTrade → TradeRecord
            cum_pnl = 0.0
            trade_records: list[TradeRecord] = []
            for i, t in enumerate(trades):
                cum_pnl += float(t.pnl_pct)
                trade_records.append(_format_trade_record(i + 1, t, cum_pnl))

            lean_stats = compute_lean_statistics(
                df=df,
                trades=trade_records,
                start_capital=float(result.initial_cash),
                risk_free_rate=0.0,
                benchmark_returns=None,
            )

            from dataclasses import asdict as _dc_asdict

            lean_stats_resp = LeanStatisticsResponse(
                portfolio=LeanPortfolioStatsResponse(**_dc_asdict(lean_stats.portfolio)),
                trade=LeanTradeStatsResponse(**_dc_asdict(lean_stats.trade)),
                runtime=LeanRuntimeStatsResponse(
                    equity=lean_stats.equity,
                    fees=lean_stats.fees,
                    net_profit=lean_stats.net_profit,
                    total_return=lean_stats.total_return,
                    total_orders=lean_stats.total_orders,
                ),
            )
        except Exception:
            logger.exception("[ENGINE] LEAN statistics computation failed — returning without")

    return lean_stats_resp


def _validation_analytics(
    *,
    result: BacktestResult,
    request: EngineBacktestRequest,
    strategy: Strategy,
    formatted_trades: list[EngineTradeResponse],
    equity_curve: list[dict[str, Any]],
    on_log: LogCallback,
) -> EngineValidationAnalyticsResponse | None:
    """The Python-authored validation analytics, or ``None`` when the engine's output is not shaped for them.

    Best-effort like :func:`_lean_parity_statistics`, and absent for the same
    reason: an analytics failure is a missing panel, never a failed backtest.
    The operator hears about it through ``on_log`` rather than inferring it
    from an empty section.
    """
    validation_analytics: EngineValidationAnalyticsResponse | None = None
    try:
        validation_trades = [
            ValidationTrade(
                trade_number=trade.trade_number,
                entry_ms_utc=trade.entry_time,
                exit_ms_utc=trade.exit_time,
                pnl_pct=trade.pnl_pct,
                is_synthetic_exit=trade.is_synthetic_exit,
            )
            for trade in formatted_trades
        ]
        validation_equity = [
            ValidationEquityPoint(
                timestamp_ms_utc=point["timestamp"],
                equity=point["equity"],
            )
            for point in equity_curve
        ]
        performance_equity = validation_equity
        if (
            request.compatibility_profile == COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1
            and request.from_date is not None
            and request.to_date is not None
        ):
            start_day = date.fromisoformat(request.from_date)
            end_day = date.fromisoformat(request.to_date) + timedelta(days=1)
            validation_equity = build_compatibility_equity_curve(
                validation_trades,
                start_ms_utc=int(datetime.combine(start_day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000),
                end_ms_utc=int(datetime.combine(end_day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000),
                initial_equity=float(result.initial_cash),
            )
        validation_analytics = compute_engine_validation_analytics(
            trades=validation_trades,
            equity_curve=validation_equity,
            performance_equity_curve=performance_equity,
        )
    except Exception as exc:
        logger.exception("[ENGINE] Validation analytics rejected engine output")
        on_log(f"Validation analytics unavailable: {exc}")

    return validation_analytics


@dataclass(frozen=True)
class _PerBarArtifacts:
    """The response's per-bar evidence — everything a summary run omits (#1941).

    The equity curve, chart bars and insights serialize the run bar by bar;
    the LEAN statistics convert every retained bar into a DataFrame (the
    largest single allocation of a full minute-resolution aggregation); the
    validation analytics copy the curve again. A ``summary_only`` request
    builds none of them: the one guard in :meth:`build` replaces the flag
    test each producer would otherwise carry, and folding the curve
    construction in keeps the validation analytics' dependency on it local
    rather than spanning two producers that must silently agree.
    """

    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    chart_bars: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)
    lean_statistics: LeanStatisticsResponse | None = None
    validation_analytics: EngineValidationAnalyticsResponse | None = None

    @classmethod
    def build(
        cls,
        *,
        result: BacktestResult,
        request: EngineBacktestRequest,
        strategy: Strategy,
        trades: list[LoggedTrade],
        formatted_trades: list[EngineTradeResponse],
        on_log: LogCallback,
    ) -> _PerBarArtifacts:
        if request.summary_only:
            return cls()
        equity_curve_dicts = [
            {
                "timestamp": s.timestamp_ms,
                "equity": float(s.equity),
                "cash": float(s.cash),
                "holdings_value": float(s.holdings_value),
            }
            for s in result.equity_curve
        ]
        return cls(
            equity_curve=equity_curve_dicts,
            # ── Serialize consolidated bars for charting ──
            chart_bars=[_serialize_chart_bar(b) for b in (strategy.ctx.consolidated_bars if strategy.ctx else [])],
            # ── Serialize insights ──
            insights=[i.to_dict() for i in result.insights],
            # ── LEAN-parity statistics ──
            lean_statistics=_lean_parity_statistics(result=result, trades=trades),
            validation_analytics=_validation_analytics(
                result=result,
                request=request,
                strategy=strategy,
                formatted_trades=formatted_trades,
                equity_curve=equity_curve_dicts,
                on_log=on_log,
            ),
        )


def _aggregate_backtest_response(
    *,
    result: BacktestResult,
    request: EngineBacktestRequest,
    strategy: Strategy,
    lake_manifest: str | None,
    on_phase: PhaseCallback,
    on_log: LogCallback,
) -> EngineBacktestResponse:
    """Everything between the engine returning and the row being written.

    Statistics, LEAN-comparable statistics, the equity envelope, the chart
    bars, the run verdict and the validation analytics — the whole wire
    response, from one completed :class:`BacktestResult` and the strategy that
    produced it.

    On a ``summary_only`` request :class:`_PerBarArtifacts` comes back empty —
    the per-bar evidence is never built. The statistics are NOT skipped: they
    consume the same engine equity samples a full run consumes and come out
    byte-identical; only the copies a summary consumer never reads are not
    made (#1941).

    **It also corrects ``request.data_policy.strategy_bars`` in place**, to the
    consolidation cadence the strategy actually ran at
    (:func:`_record_actual_strategy_bars`). That is not incidental: the LEAN
    companion dispatch inside :func:`_persist_and_dispatch_companion`
    serializes ``request.data_policy``, so this half must run first. Dispatch
    the companion against the uncorrected policy and the twin is asked to
    reproduce a run at ``minute/1`` that executed at ``minute/15`` — the pair
    then grades two different data policies and reports a divergence that never
    happened, which ``parity_companion`` calls the worst possible output from a
    parity harness because it looks like a finding.
    """
    on_phase("aggregating_results")
    on_log(
        f"Engine produced {len(getattr(strategy, 'trade_log', []) or [])} trades; aggregating results and statistics"
    )

    if not result.equity_curve:
        error = "missing data: backtest evaluated zero bars for the requested window"
        on_log(error)
        return _failed_backtest_response(request, error)

    trades = getattr(strategy, "trade_log", []) or []
    formatted = [_format_trade(i + 1, t) for i, t in enumerate(trades)]
    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    total = len(trades)
    win_rate = (wins / total) if total else 0.0

    # Approximate calendar span (in trading days) for annualized metrics,
    # over the evaluation window — a primed run's warmup days are read but
    # never scored, so they must not dilute the annualization either.
    evaluation_window = _evaluation_dates(request, strategy)
    trading_days: int | None = None
    delta = (evaluation_window.evaluation_end - evaluation_window.evaluation_start).days
    if delta > 0:
        # Rough: 252 trading days per 365 calendar days.
        trading_days = max(1, round(delta * 252 / 365))

    from app.engine.results.statistics import EquityPoint

    equity_points = (
        [EquityPoint(timestamp_ms=s.timestamp_ms, equity=float(s.equity)) for s in result.equity_curve]
        if result.equity_curve
        else None
    )

    # A compatibility pair must grade the same statistical sample. LEAN's
    # native chart is sparse, so the pinned profile uses the common closed-
    # trade ledger on both sides while retaining each native curve as evidence.
    statistics_equity_points = (
        None if request.compatibility_profile == COMPATIBILITY_PROFILE_US_EQUITY_RAW_IBKR_V1 else equity_points
    )
    try:
        stats = summarize(
            initial_cash=float(result.initial_cash),
            final_equity=float(result.final_equity),
            trades=trades,
            trading_days=trading_days,
            equity_curve=statistics_equity_points,
        )
    except ValueError as exc:
        # ``validate_trade_log`` rejected the closed-trade ledger. The run is
        # genuinely unreportable and must not be graded — but that is a
        # failure to *report*, not an exception to leak: uncaught it reached
        # Strategy Lab and the sync endpoint as a 500 and handed Grid Search
        # and Walk-Forward a stack trace where a verdict belongs.
        logger.exception("[ENGINE] Trade accounting failed for %s", request.strategy_name)
        on_log(f"Trade accounting error: {exc}")
        return _failed_backtest_response(request, f"trade accounting failed: {exc}")

    # Correct the policy's strategy_bars to the strategy's ACTUAL
    # consolidation timeframe. The legacy synthesizer writes minute/1,
    # but e.g. the EMA crossover consolidates 15-minute bars — the
    # persisted DataPolicy is the key the run report uses to re-fetch
    # chart bars from the store, so it must record the real timeframe.
    # Only single-consolidator strategies are corrected; a multi-
    # consolidator chart is a mix no single timeframe can reproduce.
    # (Runs before the artifacts below: none of their producers reads
    # ``request.data_policy`` — verified against every call they make.)
    _record_actual_strategy_bars(request, strategy)

    artifacts = _PerBarArtifacts.build(
        result=result,
        request=request,
        strategy=strategy,
        trades=trades,
        formatted_trades=formatted,
        on_log=on_log,
    )

    run_verdict = compute_run_verdict(
        {
            "statistics": stats,
            "win_rate": win_rate,
            "total_trades": total,
            "net_profit": float(result.net_profit),
            "total_fees": float(result.total_fees),
            "lean_statistics": (
                artifacts.lean_statistics.model_dump(mode="json") if artifacts.lean_statistics else None
            ),
        },
        engine="python",
    )

    response = EngineBacktestResponse(
        success=True,
        strategy_name=request.strategy_name,
        fill_mode=request.fill_mode,
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        net_profit=float(result.net_profit),
        total_fees=float(result.total_fees),
        total_trades=total,
        winning_trades=wins,
        losing_trades=losses,
        win_rate=win_rate,
        statistics=stats,
        lean_statistics=artifacts.lean_statistics,
        trades=formatted,
        log_lines=result.log_lines,
        equity_curve=artifacts.equity_curve,
        bars_consumed=len(result.equity_curve),
        chart_bars=artifacts.chart_bars,
        insights=artifacts.insights,
        insight_summary=result.insight_summary,
        data_policy=request.data_policy,  # PR B — echo the normalized policy
        run_verdict=run_verdict,
        validation_analytics=artifacts.validation_analytics,
        lake_data_availability_hash=lake_manifest,
        evaluation_window=evaluation_window.response(),
    )
    return response


def _persist_and_dispatch_companion(
    *,
    response: EngineBacktestResponse,
    request: EngineBacktestRequest,
    registration: StrategyRegistration,
    validated_params: StrategyParamsBase,
    strategy: Strategy,
    run_started_at: float,
    on_phase: PhaseCallback,
    on_log: LogCallback,
) -> EngineBacktestResponse:
    """Write the run to history and dispatch its parity companion.

    Mutates ``response.study_id`` in place and hands the same object back, so
    a caller that ignores the return value still sees the id. Best-effort by
    contract: a storage failure leaves ``study_id`` None and logs, and never
    fails the backtest that produced the response.
    """
    # ── Persist the run (synchronous so we can return the id) ──
    # Used by the Engine Lab to enable the Replay tab right after a run
    # without a second round-trip. The save itself is best-effort — a
    # storage failure leaves study_id=None and logs, but does not fail the
    # backtest response.
    if not request.save_study:
        # Grid Search and Walk-Forward keep their own summary rows; a full
        # study plus a parity companion per cell would be the dominant
        # storage and round-trip cost of a sweep (PRD #1926).
        on_log("Study save suppressed by request; no parity companion dispatched")
        return response

    on_phase("persisting")
    on_log("Persisting run to history")
    # Minted BEFORE persisting so the row itself carries the group id —
    # the LEAN companion's persist step joins back to this run through it
    # when freezing the parity verdict.
    parity_group_id = _new_parity_group_id_for(request.requested_engine)
    resolved_configuration = _resolved_run_configuration(
        request=request,
        registration=registration,
        validated_params=validated_params,
        strategy=strategy,
    )
    response.study_id = persist_engine_response_sync(
        response=response,
        symbol=strategy.ctx.symbols[0] if strategy.ctx.symbols else "SPY",
        start_date=resolved_configuration.start_date,
        end_date=resolved_configuration.end_date,
        resolution=resolved_configuration.resolution,
        parameters=resolved_configuration.parameters,
        duration_ms=int((time.time() - run_started_at) * 1000),
        commission_per_order=float(request.commission_per_order),
        compatibility_profile=request.compatibility_profile,
        requested_engine=request.requested_engine,
        parity_group_id=parity_group_id,
        execution_config=_persisted_execution_config(request, evaluation_start=date.fromisoformat(resolved_configuration.start_date)),
    )

    on_log(f"Saved study {response.study_id}")

    if parity_group_id is not None:
        on_log("Recording parity disposition")
    _dispatch_requested_parity_companion(
        registration=registration,
        request=request,
        parity_group_id=parity_group_id,
        study_id=response.study_id,
        validated_parameters=resolved_configuration.parameters,
    )

    return response


def _persisted_execution_config(request: EngineBacktestRequest, *, evaluation_start: date) -> dict[str, Any]:
    """Freeze execution settings through the shared Python/LEAN receipt seam."""
    return persisted_execution_configuration(
        evaluation_start=evaluation_start,
        compatibility_profile=request.compatibility_profile,
        warmup_from_date=request.warmup_from_date,
        slippage_per_share=request.slippage_per_share,
        session_entry_cutoff=request.session_entry_cutoff,
        force_flat_at=request.force_flat_at,
        limit_penetration=request.limit_penetration,
    )


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------
def _record_actual_strategy_bars(request: EngineBacktestRequest, strategy: Strategy) -> None:
    """Overwrite ``data_policy.strategy_bars`` with the strategy's real timeframe.

    Reads the registered consolidator's period after the run. No-op when
    the request has no policy, the strategy has no context/symbols, or it
    registered more than one consolidator (no single timeframe exists).
    """
    if request.data_policy is None or strategy.ctx is None or not strategy.ctx.symbols:
        return
    consolidators = strategy.ctx.get_consolidators(strategy.ctx.symbols[0])
    if len(consolidators) != 1:
        return
    total_minutes = int(consolidators[0].period.total_seconds() // 60)
    if total_minutes <= 0:
        return
    if total_minutes % 1440 == 0:
        timespan, multiplier = "day", total_minutes // 1440
    elif total_minutes % 60 == 0:
        timespan, multiplier = "hour", total_minutes // 60
    else:
        timespan, multiplier = "minute", total_minutes
    request.data_policy.strategy_bars = _EngineBarsSpecModel(timespan=timespan, multiplier=multiplier)


def _serialize_chart_bar(b: TradeBar) -> dict[str, Any]:
    """One wire shape for consolidated chart bars — used by the live run's
    ``chart_bars`` and the ``/bars`` store endpoint, so the two can be
    equality-tested against each other."""
    return {
        "t": b.start_ms,
        "o": float(b.open),
        "h": float(b.high),
        "l": float(b.low),
        "c": float(b.close),
        "v": int(b.volume),
    }
