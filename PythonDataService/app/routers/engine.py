"""LEAN-compatible backtest engine API.

POST /api/engine/backtest runs a strategy through the in-process engine at
``app.engine`` against LEAN-format minute data. Phase 1 supports a single
registered strategy (``spy_ema_crossover``) with bit-exact LEAN parity.

This endpoint is intentionally separate from the existing
``/api/backtest`` pipeline so both can coexist while the new engine is
being rolled out.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as time_of_day
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import pandas as pd
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.data.availability import (
    AvailabilityReport,
    check_availability,
    ensure_range,
)
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.policy_store import (
    policy_key,
    record_fetch,
    resolve_data_roots,
    resolve_policy_root,
    symbol_write_lock,
)
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import FillMode
from app.engine.results.equity_downsample import from_engine_curve
from app.engine.results.statistics import summarize
from app.engine.strategy.base import Strategy
from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration
from app.models.responses import (
    LeanPortfolioStatsResponse,
    LeanRuntimeStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.schemas.engine_validation import EngineValidationAnalyticsResponse
from app.schemas.run_verdict import RunVerdict
from app.services.engine_bars_service import read_consolidated_bars
from app.services.engine_validation_analytics import (
    ValidationEquityPoint,
    ValidationTrade,
    build_validation_analytics_envelope,
    compute_engine_validation_analytics,
)
from app.services.parity_companion import dispatch_parity_companion, new_parity_group_id
from app.services.run_verdict_service import compute_run_verdict
from app.services.strategies.common import TradeRecord
from app.services.strategies.lean_statistics import compute_lean_statistics
from app.utils.timestamps import now_ms_utc, to_ms_utc

router = APIRouter()
logger = logging.getLogger(__name__)


def _public_params_schema(reg: StrategyRegistration) -> dict[str, Any]:
    schema = reg.param_schema.model_json_schema()
    if not reg.hidden_params:
        return schema
    schema = dict(schema)
    properties = dict(schema.get("properties") or {})
    for name in reg.hidden_params:
        properties.pop(name, None)
    schema["properties"] = properties
    if "required" in schema:
        schema["required"] = [name for name in schema["required"] if name not in reg.hidden_params]
    return schema


def _reject_hidden_params(reg: StrategyRegistration, params: dict[str, Any]) -> None:
    hidden = sorted(reg.hidden_params.intersection(params))
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

    Delegates to the policy-keyed bar store: reference mount first (so
    the bit-exact SPY fixture always wins), then the policy cache root
    for the requested adjustment mode. See
    :mod:`app.engine.data.policy_store` for the layout and the
    adjusted-vs-raw seam bug this keying fixes.
    """
    return resolve_data_roots(source="polygon", adjusted=adjusted)


def _policy_adjusted(data_policy: _EngineDataPolicyModel | None) -> bool:
    """Adjustment mode for root resolution; legacy requests default adjusted.

    Matches the legacy synthesizer's ``adjusted=True`` so pre-DataPolicy
    callers keep reading the tree their runs have always used.
    """
    return data_policy.adjusted if data_policy is not None else True


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class EngineBacktestRequest(BaseModel):
    """Backtest request for the in-process LEAN-compatible engine.

    Does NOT inherit ``TickerRequest`` because:
      - Dates are *optional* overrides (``None`` lets the strategy use
        its own defaults), not required.
      - The engine uses ``resolution: Literal["minute", "daily"]``
        instead of the base's ``timespan: Literal["minute","hour","day"]``
        (no "hour", and the field name differs).
      - Symbol is strategy-owned (set by the strategy's Initialize-equivalent),
        not a top-level form field.
      - No ``multiplier`` / ``session`` concepts at this layer.

    What does change in this PR: ``start_date`` / ``end_date`` are
    renamed to ``from_date`` / ``to_date`` to align with the canonical
    naming. The legacy names continue to be accepted via Pydantic
    ``AliasChoices`` during the PR (ii) → (iii) transition window;
    they're removed in PR (iii).
    """

    model_config = ConfigDict(populate_by_name=True)

    strategy_name: str = Field(..., description="Registered strategy identifier")
    fill_mode: str = Field(
        "signal_bar_close",
        description="Fill mode: signal_bar_close or next_bar_open",
    )
    commission_per_order: float = Field(1.0, ge=0)
    slippage_per_share: float = Field(
        0.0,
        ge=0,
        description=(
            "Per-share slippage applied against the trade direction at fill. "
            "Defaults to 0 to preserve LEAN-parity for bit-exact runs; pass a "
            "non-zero value (e.g. 0.02 = 2 ticks for US equities) to model a "
            "more realistic execution cost."
        ),
    )
    session_entry_cutoff: time_of_day | None = Field(
        None,
        description=(
            "After this time-of-day, entry orders (those that would grow "
            "|position|) are dropped. Exits still fill. Interpreted in the "
            "timezone of the bar data. Example: '15:55:00' for ET data."
        ),
    )
    force_flat_at: time_of_day | None = Field(
        None,
        description=(
            "At the first minute bar whose wall-clock time reaches this "
            "value, the engine cancels all queued / deferred orders, clears "
            "active TP/SL brackets, closes every open position at that "
            "minute's close, and calls strategy.on_force_flat(). Once per "
            "calendar day. Example: '15:58:00' for ET data."
        ),
    )
    limit_penetration: float = Field(
        0.0,
        ge=0,
        description=(
            "Dollar amount the bar must penetrate past a resting limit's "
            "price before the fill is recognized. Measured against the "
            "adverse extreme — low for buy limits, high for sell limits. "
            "Default 0 = TradingView-style touch fill; 0.02 for US "
            "equities is a realistic 2-tick queue-position model."
        ),
    )
    # Optional overrides — when omitted, the strategy's own defaults (set in
    # its Initialize equivalent) are used.
    from_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: start_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("from_date", "start_date"),
    )
    to_date: str | None = Field(
        None,
        description="YYYY-MM-DD override (legacy: end_date)",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        validation_alias=AliasChoices("to_date", "end_date"),
    )
    initial_cash: float | None = Field(None, ge=0)
    # Strategy-specific parameters — validated per-strategy against the
    # corresponding ``StrategyParamsBase`` subclass in the registry. Left
    # untyped here because the schema varies per strategy.
    params: dict[str, Any] = Field(default_factory=dict)
    # Data resolution the engine will read. ``"minute"`` feeds
    # ``LeanMinuteDataReader`` (the Phase 1 default, used by every
    # intraday strategy that consolidates up to 15m/1h/etc.).
    # ``"daily"`` feeds ``LeanDailyDataReader`` and is reserved for
    # strategies that declare themselves daily-native.
    resolution: Literal["minute", "daily"] = Field(
        "minute",
        description="Data resolution: 'minute' (default) or 'daily'",
    )
    # When true, the router will materialize any missing data for the
    # run's symbol + date range into the cache root before starting the
    # engine. Defaults to false so the SPY fixture path (which should
    # always hit the reference mount) is never accidentally fetched.
    auto_fetch: bool = Field(
        False,
        description="Fetch missing data from Polygon into the cache before running",
    )

    # PR B (2026-05-19) — canonical DataPolicy block. Optional on the wire
    # so legacy callers (any pre-PR-B UI build) still work; when omitted, a
    # default block is synthesized from ``params.symbol`` + ``resolution``.
    # The shape mirrors ``app.lean_sidecar.data_policy.DataPolicy`` so the
    # GraphQL/engine compare-view sees an identical schema on both engines.
    data_policy: _EngineDataPolicyModel | None = Field(
        None,
        description=(
            "Canonical DataPolicy block (PR B). When omitted, synthesized "
            "from ``params.symbol`` + ``resolution`` with ``adjusted=true`` "
            "and ``session='regular'``. Required when ``params.symbol`` is "
            "absent (no source of truth for the synthesizer)."
        ),
    )

    @model_validator(mode="after")
    def _synthesize_legacy_data_policy(self) -> EngineBacktestRequest:
        """Synthesize a default ``DataPolicy`` when the caller omits it.

        One-deprecation-cycle compat. The pre-PR-B engine wire shape
        carried ``symbol`` inside ``params`` and the resolution in the
        top-level field; we synthesize a canonical block from those two
        signals so the row written to ``StrategyExecution`` always has a
        ``DataPolicyJson``. Synthesis defaults match the engine's actual
        runtime behavior today (Polygon-sourced, pre-adjusted, regular
        session, m/1 → m/1; the strategy's own consolidator handles any
        further intra-strategy timeframe).

        When BOTH ``data_policy`` and ``params.symbol`` are absent we
        leave ``data_policy=None`` rather than raising. Legacy clients
        that POST ``params={}`` rely on the strategy registry's default
        symbol (e.g., SPY) being resolved downstream; failing
        validation here would short-circuit one-cycle compat. Downstream
        consumers (``_save_study_sync``, response serialization) already
        treat ``data_policy is None`` as "policy unknown at request
        time" and emit a null ``dataPolicyJson``; the .NET persistence
        layer then synthesizes a legacy block from ``Symbol`` in that
        case (see ``BacktestRunPersistenceService.SynthesizeLegacyDataPolicy``).
        """
        if self.data_policy is not None:
            return self
        symbol = self.params.get("symbol") if isinstance(self.params, dict) else None
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            # Legacy compat: defer synthesis to downstream code once the
            # strategy registry has resolved its default symbol.
            return self
        timespan = "day" if self.resolution == "daily" else "minute"
        self.data_policy = _EngineDataPolicyModel(
            source="polygon",
            symbol=symbol.strip().upper(),
            adjusted=True,
            session="regular",
            input_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
            strategy_bars=_EngineBarsSpecModel(timespan=timespan, multiplier=1),
        )
        return self


# ---------------------------------------------------------------------------
# DataPolicy + BarsSpec pydantic shapes (engine-side mirror)
# ---------------------------------------------------------------------------
# PR B (2026-05-19) — the engine surface accepts the canonical DataPolicy
# block on its inbound request. Mirrors ``app.lean_sidecar.data_policy.DataPolicy``
# and the leading-underscore models in ``app.routers.lean_sidecar`` (kept
# local here to avoid importing a leading-underscore name across modules).
class _EngineBarsSpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timespan: Literal["minute", "hour", "day"]
    multiplier: int = Field(..., ge=1)


class _EngineDataPolicyModel(BaseModel):
    """Pydantic shape for the canonical ``DataPolicy`` block on the engine surface.

    Identical to ``_DataPolicyModel`` in ``app.routers.lean_sidecar`` (and
    to ``app.lean_sidecar.data_policy.DataPolicy``); duplicated here only
    because the lean_sidecar module is leading-underscore private. A
    future PR can extract a shared neutral module.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["synthetic", "polygon"]
    symbol: str
    adjusted: bool = True
    session: Literal["regular", "extended"]
    input_bars: _EngineBarsSpecModel
    strategy_bars: _EngineBarsSpecModel
    timestamp_policy: Literal["bar_close_ms_utc"] = "bar_close_ms_utc"
    timezone: Literal["America/New_York"] = "America/New_York"
    provider_kind: Literal["live", "fixture"] = "live"
    fixture_id: str | None = None
    fixture_sha256: str | None = None


EngineBacktestRequest.model_rebuild()


class EngineTradeResponse(BaseModel):
    trade_number: int
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    # Filled share/contract count from the engine's fill model. Required for
    # downstream dollar-PnL persistence — without it, ``BacktestTrade.Quantity``
    # defaults to 1 on the .NET side and the persisted PnL silently diverges
    # from the actual run by a factor of ``quantity``. See
    # ``.claude/rules/numerical-rigor.md`` → ``QUANTITY_MISMATCH``.
    quantity: int
    # Per-trade indicator snapshot captured at the entry signal. Keys depend
    # on the strategy — e.g. SPY returns ``ema5``/``ema10``/``rsi``, SMA
    # crossover returns ``sma_10``/``sma_30``. The frontend renders these
    # dynamically rather than expecting a fixed shape.
    indicators: dict[str, float] = Field(default_factory=dict)
    pnl_pts: float
    pnl_pct: float
    result: str
    signal_reason: str = ""


class EngineBacktestResponse(BaseModel):
    success: bool
    strategy_name: str
    fill_mode: str
    initial_cash: float
    final_equity: float
    net_profit: float
    total_fees: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    # Extended statistics — computed from the trade log. See
    # app/engine/results/statistics.py for the full list of keys.
    statistics: dict[str, Any] = Field(default_factory=dict)
    # Full LEAN-parity statistics (portfolio + trade + runtime).
    lean_statistics: LeanStatisticsResponse | None = None
    trades: list[EngineTradeResponse] = []
    log_lines: list[str] = []
    equity_curve: list[dict] = Field(default_factory=list)
    # Consolidated OHLCV bars for the price chart (15-min or daily depending
    # on the strategy's consolidator). Much smaller than the full minute-bar
    # stream retained in BacktestResult.bars.
    chart_bars: list[dict] = Field(default_factory=list)
    # Phase 1: Insight tracking — per-prediction scoring and aggregate analytics.
    insights: list[dict] = Field(default_factory=list)
    insight_summary: dict[str, Any] = Field(default_factory=dict)
    # Auto-save study id, populated synchronously before returning so the
    # Engine Lab can immediately enable the Replay tab without polling
    # /api/studies for the latest row. Null when the save call failed
    # (the run still succeeded — the persistence is best-effort).
    study_id: int | None = None
    error: str | None = None
    # PR B (2026-05-19) — echo of the post-normalization DataPolicy so the
    # frontend can render the policy that was actually used by the engine
    # (which may differ from the request when the legacy synthesizer kicked
    # in). Never null on a successful run.
    data_policy: _EngineDataPolicyModel | None = None
    run_verdict: RunVerdict | None = None
    validation_analytics: EngineValidationAnalyticsResponse | None = None


# ---------------------------------------------------------------------------
# Phase callbacks
#
# Both the synchronous /backtest endpoint and the Jobs-system worker call
# the same ``_execute_engine_backtest_core`` to do the actual run. They
# differ only in how progress is reported: the sync path passes no-op
# callbacks (the response is the only signal); the Jobs worker forwards
# every phase/log into a ProgressEmitter that writes Redis events the
# .NET SSE layer streams to the browser. Keeping this as a callback pair
# avoids importing ProgressEmitter into the hot path.
# ---------------------------------------------------------------------------
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str], None]


def _noop_phase(_: str) -> None:
    pass


def _noop_log(_: str) -> None:
    pass


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
    if req.from_date:
        d = datetime.strptime(req.from_date, "%Y-%m-%d")
        strategy.set_start_date(d.year, d.month, d.day)
    if req.to_date:
        d = datetime.strptime(req.to_date, "%Y-%m-%d")
        strategy.set_end_date(d.year, d.month, d.day)
    if req.initial_cash is not None:
        strategy.set_cash(req.initial_cash)


def _format_trade(index: int, trade: Any) -> EngineTradeResponse:
    # ``indicators`` is a dict[str, Decimal] on ``LoggedTrade``; convert to
    # plain floats for JSON serialization.
    raw_indicators = getattr(trade, "indicators", None) or {}
    indicators = {k: float(v) for k, v in raw_indicators.items()}
    return EngineTradeResponse(
        trade_number=index,
        entry_time=_to_ms_utc(trade.entry_time),
        entry_price=float(trade.entry_price),
        exit_time=_to_ms_utc(trade.exit_time),
        exit_price=float(trade.exit_price),
        quantity=int(trade.quantity),
        indicators=indicators,
        pnl_pts=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        result=trade.result,
        signal_reason=getattr(trade, "signal_reason", "") or "",
    )


def _format_trade_record(index: int, trade: Any, cumulative_pnl_pct: float) -> TradeRecord:
    raw_indicators = getattr(trade, "indicators", None) or {}
    return TradeRecord(
        trade_number=index,
        trade_type="Buy",  # engine strategies are long-only for now
        entry_timestamp=_to_ms_utc(trade.entry_time),
        exit_timestamp=_to_ms_utc(trade.exit_time),
        entry_price=float(trade.entry_price),
        exit_price=float(trade.exit_price),
        pnl=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        cumulative_pnl_pct=cumulative_pnl_pct,
        signal_reason=getattr(trade, "signal_reason", "") or "",
        indicator_snapshot={k: float(v) for k, v in raw_indicators.items()},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class StrategyInfo(BaseModel):
    name: str
    display_name: str
    description: str
    # JSON Schema for the strategy's parameter model — the frontend renders
    # the parameter form dynamically from this.
    params_schema: dict[str, Any]
    # Data resolutions this strategy accepts. The Engine Lab uses this to
    # filter the strategy dropdown once the user picks a resolution.
    supported_resolutions: list[str] = Field(default_factory=list)
    # Short pseudocode snippet of the entry/exit rules, rendered in the
    # frontend strategy picker so users can see the rules at a glance.
    algorithm_pseudocode: str = ""
    # Parity-critical gotchas — implementation quirks, porting traps, or
    # known cross-system divergences. Rendered as a bullet list under the
    # strategy in the frontend so they're not rediscovered by trial and
    # error on the next ticker / strategy combination.
    gotchas: list[str] = Field(default_factory=list)
    # True when a Pine v6 generator is registered for this strategy.
    # The frontend uses this to show/hide the Pine-download button.
    pine_available: bool = False
    # ADR 0009 § 6 — the boundary that sizes this strategy. ``"policy"`` =
    # set_holdings via live_config.sizing; ``"explicit"`` = strategy supplies
    # its own quantity/contracts and the deploy form's sizing control is
    # disabled + labelled "self-sized".
    sizing_surface: Literal["policy", "explicit"] = "policy"
    # The LEAN trusted template that validates this strategy's execution
    # semantics. ``None`` means Engine Lab must not offer a LEAN parity run.
    lean_twin: str | None = None


@router.get("/strategies", response_model=list[StrategyInfo])
def list_engine_strategies() -> list[StrategyInfo]:
    """List strategies registered with the LEAN-compatible engine.

    Each entry carries the JSON Schema of its parameter model so the frontend
    can render a parameter form without hardcoding strategy knowledge. Sorted
    alphabetically for deterministic UI ordering.
    """
    result: list[StrategyInfo] = []
    for name in sorted(_STRATEGY_REGISTRY.keys()):
        reg = _STRATEGY_REGISTRY[name]
        if not reg.catalog_visible:
            continue
        result.append(
            StrategyInfo(
                name=name,
                display_name=reg.display_name,
                description=reg.description,
                params_schema=_public_params_schema(reg),
                supported_resolutions=sorted(reg.supported_resolutions),
                algorithm_pseudocode=reg.algorithm_pseudocode,
                gotchas=list(reg.gotchas),
                pine_available=reg.pine_generator is not None,
                sizing_surface=reg.sizing_surface,
                lean_twin=reg.lean_twin,
            )
        )
    return result


@router.post("/strategies/{name}/pine", response_class=PlainTextResponse)
def generate_pine_script(name: str, params: dict[str, Any]) -> PlainTextResponse:
    """Generate a Pine v6 script for ``name`` using the given params.

    The request body is the same ``{params: {...}}`` shape used by
    ``/backtest`` — the same Pydantic schema validates it. Response is
    the Pine source as ``text/plain`` so the browser can offer it as a
    direct download.
    """
    reg = _STRATEGY_REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy: {name}")
    if reg.pine_generator is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Pine script template available for strategy '{name}'",
        )
    try:
        validated = reg.param_schema(**params)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    pine_source = reg.pine_generator(validated)
    return PlainTextResponse(
        content=pine_source,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.pine"',
        },
    )


# ---------------------------------------------------------------------------
# Polygon → LEAN export endpoint
# ---------------------------------------------------------------------------
class LeanExportRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    from_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    to_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    adjusted: bool = Field(True, description="Apply split/dividend adjustments")
    resolution: Literal["minute", "daily"] = Field(
        "minute",
        description="Resolution to fetch: 'minute' (per-day zips) or 'daily' (one zip per symbol)",
    )


class LeanExportResponse(BaseModel):
    success: bool
    symbol: str
    data_root: str
    days_written: int
    files: list[str] = []
    error: str | None = None


@router.post("/export-lean", response_model=LeanExportResponse)
def export_polygon_to_lean(request: LeanExportRequest) -> LeanExportResponse:
    """Fetch a Polygon minute-bar range and export it to LEAN zips.

    Writes one ``{YYYYMMDD}_trade.zip`` per trading day under
    ``{LEAN_DATA_CACHE}/equity/usa/minute/{symbol}/``. The read-only
    reference mount is never touched — all fetched data lives in the
    writable cache so the SPY fixture's bit-exact guarantee is preserved.
    """
    # Imported lazily — keeps this module importable in test contexts
    # that don't provide a Polygon API key.
    from app.engine.data.polygon_export import export_polygon_range_to_lean
    from app.services.polygon_client import PolygonClientService

    cache_root = resolve_policy_root(source="polygon", adjusted=request.adjusted)
    cache_root.mkdir(parents=True, exist_ok=True)

    try:
        polygon = PolygonClientService()
        with symbol_write_lock(cache_root, request.symbol.upper()):
            files = export_polygon_range_to_lean(
                polygon=polygon,
                output_root=cache_root,
                symbol=request.symbol.upper(),
                from_date=request.from_date,
                to_date=request.to_date,
                adjusted=request.adjusted,
                resolution=request.resolution,
            )
            record_fetch(
                cache_root,
                request.symbol.upper(),
                source="polygon",
                adjusted=request.adjusted,
                resolution=request.resolution,
                from_date=request.from_date,
                to_date=request.to_date,
                fetched_at_ms=now_ms_utc(),
            )
    except Exception as exc:
        logger.exception("[ENGINE] LEAN export failed for %s", request.symbol)
        return LeanExportResponse(
            success=False,
            symbol=request.symbol.upper(),
            data_root=str(cache_root),
            days_written=0,
            error=str(exc),
        )

    return LeanExportResponse(
        success=True,
        symbol=request.symbol.upper(),
        data_root=str(cache_root),
        days_written=len(files),
        files=[str(p) for p in files],
    )


# ---------------------------------------------------------------------------
# Data availability endpoint
# ---------------------------------------------------------------------------
class AvailabilityResponse(BaseModel):
    symbol: str
    start: str
    end: str
    resolution: str
    expected_days: int
    available_days: int
    is_complete: bool
    missing_days: list[str] = []
    # Per-root breakdown (reference mount vs cache) so the UI can tell
    # the user where the data is coming from.
    sources: dict[str, list[str]] = Field(default_factory=dict)


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: expected YYYY-MM-DD, got {value!r}",
        ) from exc


@router.get("/data/availability", response_model=AvailabilityResponse)
def get_data_availability(
    symbol: str = Query(..., min_length=1, max_length=20),
    start: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    end: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    resolution: Literal["minute", "daily"] = Query(
        "minute",
        description="Resolution to check: 'minute' (per-day zips) or 'daily'",
    ),
    adjusted: bool = Query(
        True,
        description="Adjustment mode — selects the policy-keyed cache subtree",
    ),
) -> AvailabilityResponse:
    """Report how many trading days are already on disk for a symbol.

    Checks the reference mount first, then the writable cache, and
    returns both the aggregate coverage and a per-root breakdown so the
    Angular UI can show the user whether SPY is hitting the bit-exact
    reference data or an arbitrary ticker has been fetched into the
    Polygon cache.
    """
    start_date = _parse_iso_date(start, "start")
    end_date = _parse_iso_date(end, "end")
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"end ({end}) must not precede start ({start})",
        )

    roots = _resolve_lean_data_roots(adjusted=adjusted)
    report: AvailabilityReport = check_availability(
        roots=roots,
        symbol=symbol,
        start=start_date,
        end=end_date,
        resolution=resolution,
    )
    data = report.to_dict()
    return AvailabilityResponse(**data)


# ---------------------------------------------------------------------------
# Bars-by-policy endpoint (shared bar store → UI charting)
# ---------------------------------------------------------------------------
class EngineBarsCoverageResponse(BaseModel):
    expected_days: int
    available_days: int
    is_complete: bool
    missing_days: list[str] = []


class EngineBarsResponse(BaseModel):
    policy_key: str
    symbol: str
    session: str
    timespan: str
    multiplier: int
    count: int
    # Same wire shape as ``EngineBacktestResponse.chart_bars``:
    # {t: int64 ms UTC bar start, o, h, l, c, v}.
    bars: list[dict] = Field(default_factory=list)
    coverage: EngineBarsCoverageResponse


@router.get("/bars", response_model=EngineBarsResponse)
def get_engine_bars(
    symbol: str = Query(..., min_length=1, max_length=20),
    from_date: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    to_date: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    adjusted: bool = Query(True, description="Adjustment mode — selects the policy-keyed cache subtree"),
    session: Literal["regular", "extended"] = Query("regular"),
    timespan: Literal["minute", "hour", "day"] = Query(
        "minute",
        description="Strategy timeframe unit (DataPolicy strategy_bars.timespan)",
    ),
    multiplier: int = Query(1, ge=1, description="Strategy timeframe multiplier"),
) -> EngineBarsResponse:
    """Serve consolidated bars from the shared bar store for run charting.

    Reads the same roots, the same session filter, and the same
    consolidator a backtest run used, so the returned bars equal the
    run's transient ``chart_bars`` for the same DataPolicy + window.
    Display reads never mutate the cache — missing days surface in
    ``coverage``, not as a fetch and not as a 500.
    """
    from app.lean_sidecar.workspace import SymbolValidationError, validate_symbol

    try:
        safe_symbol = validate_symbol(symbol)
    except SymbolValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    start_date = _parse_iso_date(from_date, "from_date")
    end_date = _parse_iso_date(to_date, "to_date")
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"to_date ({to_date}) must not precede from_date ({from_date})",
        )

    roots = _resolve_lean_data_roots(adjusted=adjusted)
    result = read_consolidated_bars(
        roots=roots,
        symbol=safe_symbol,
        start=start_date,
        end=end_date,
        session=session,
        timespan=timespan,
        multiplier=multiplier,
    )
    return EngineBarsResponse(
        policy_key=policy_key(source="polygon", adjusted=adjusted),
        symbol=safe_symbol,
        session=session,
        timespan=timespan,
        multiplier=multiplier,
        count=len(result.bars),
        bars=[_serialize_chart_bar(b) for b in result.bars],
        coverage=EngineBarsCoverageResponse(
            expected_days=result.coverage.expected_days,
            available_days=result.coverage.available_days,
            is_complete=result.coverage.is_complete,
            missing_days=[d.isoformat() for d in result.coverage.missing_days],
        ),
    )


@router.post("/backtest", response_model=EngineBacktestResponse)
def run_engine_backtest(
    request: EngineBacktestRequest,
) -> EngineBacktestResponse:
    """Run a strategy through the LEAN-compatible backtest engine (synchronous).

    Used by tests, curl, and any caller that doesn't need streamed
    progress. The Engine Lab UI uses the Jobs system instead — see
    ``POST /api/jobs/engine_backtest`` (defined in the .NET layer) which
    forwards to ``/api/jobs-internal/engine-backtest`` here.

    The engine reads LEAN-format minute zips from the configured data
    root and produces trades that reproduce LEAN's reference log
    bit-exactly when the same strategy is run against the same data.
    """
    return execute_engine_backtest(
        request=request,
        on_phase=_noop_phase,
        on_log=_noop_log,
    )


def execute_engine_backtest(
    *,
    request: EngineBacktestRequest,
    on_phase: PhaseCallback,
    on_log: LogCallback,
) -> EngineBacktestResponse:
    """Core backtest workflow shared by the sync POST and the Jobs worker.

    Both call paths converge here. ``on_phase`` and ``on_log`` are
    callbacks the worker uses to forward to a ProgressEmitter; the sync
    path passes no-ops. Raises HTTPException for client errors; returns
    an EngineBacktestResponse with ``success=False`` for engine
    failures.
    """
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

    fill_mode = _parse_fill_mode(request.fill_mode)

    data_roots = _resolve_lean_data_roots(adjusted=_policy_adjusted(request.data_policy))
    if not data_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)",
        )

    strategy = registration.build(validated_params)

    if request.auto_fetch:
        symbol = getattr(validated_params, "symbol", None)
        start_override = request.from_date
        end_override = request.to_date
        if symbol and start_override and end_override:
            on_phase("fetching_data")
            on_log(f"Ensuring {symbol} {request.resolution} bars {start_override} → {end_override}")
            try:
                from app.services.polygon_client import PolygonClientService

                polygon = PolygonClientService()
                ensure_range(
                    reference_roots=data_roots[:-1],
                    cache_root=data_roots[-1],
                    symbol=symbol,
                    start=_parse_iso_date(start_override, "start_date"),
                    end=_parse_iso_date(end_override, "end_date"),
                    polygon=polygon,
                    adjusted=_policy_adjusted(request.data_policy),
                    resolution=request.resolution,
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
                    error=f"auto_fetch failed: {exc}",
                )

    reader: LeanMinuteDataReader | LeanDailyDataReader
    if request.resolution == "daily":
        reader = LeanDailyDataReader(data_roots)
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
        reader = LeanMinuteDataReader(data_roots, session=session_mode)
    execution_config = ExecutionConfig(
        fill_mode=fill_mode,
        commission_per_order=Decimal(str(request.commission_per_order)),
        slippage_per_share=Decimal(str(request.slippage_per_share)),
        session_entry_cutoff=request.session_entry_cutoff,
        force_flat_at=request.force_flat_at,
        limit_penetration=Decimal(str(request.limit_penetration)),
    )
    engine = BacktestEngine(
        data_source=reader,
        execution_config=execution_config,
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
        result = engine.run(strategy)
    except Exception as exc:
        logger.exception("[ENGINE] Backtest failed for %s", request.strategy_name)
        on_log(f"Engine error: {exc}")
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
            error=str(exc),
        )

    on_phase("aggregating_results")
    on_log(
        f"Engine produced {len(getattr(strategy, 'trade_log', []) or [])} trades; aggregating results and statistics"
    )

    trades = getattr(strategy, "trade_log", []) or []
    formatted = [_format_trade(i + 1, t) for i, t in enumerate(trades)]
    wins = sum(1 for t in trades if t.result == "WIN")
    losses = sum(1 for t in trades if t.result == "LOSS")
    total = len(trades)
    win_rate = (wins / total) if total else 0.0

    # Approximate calendar span (in trading days) for annualized metrics.
    # Uses the strategy's declared date range when available.
    trading_days: int | None = None
    if strategy.start_date and strategy.end_date:
        delta = (strategy.end_date.date() - strategy.start_date.date()).days
        if delta > 0:
            # Rough: 252 trading days per 365 calendar days.
            trading_days = max(1, round(delta * 252 / 365))

    from app.engine.results.statistics import EquityPoint

    equity_points = (
        [EquityPoint(timestamp=s.timestamp, equity=float(s.equity)) for s in result.equity_curve]
        if result.equity_curve
        else None
    )

    stats = summarize(
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        trades=trades,
        trading_days=trading_days,
        equity_curve=equity_points,
    )

    # ── LEAN-parity statistics ──────────────────────────────────────
    lean_stats_resp: LeanStatisticsResponse | None = None
    if result.bars and trades:
        try:
            # Convert retained TradeBar objects → DataFrame with timestamp + close
            bar_records = [
                {
                    "timestamp": b.time,
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

    equity_curve_dicts = [
        {
            "timestamp": _to_ms_utc(s.timestamp),
            "equity": float(s.equity),
            "cash": float(s.cash),
            "holdings_value": float(s.holdings_value),
        }
        for s in result.equity_curve
    ]

    # ── Serialize consolidated bars for charting ──
    chart_bars_dicts = [_serialize_chart_bar(b) for b in (strategy.ctx.consolidated_bars if strategy.ctx else [])]

    # Correct the policy's strategy_bars to the strategy's ACTUAL
    # consolidation timeframe. The legacy synthesizer writes minute/1,
    # but e.g. the EMA crossover consolidates 15-minute bars — the
    # persisted DataPolicy is the key the run report uses to re-fetch
    # chart bars from the store, so it must record the real timeframe.
    # Only single-consolidator strategies are corrected; a multi-
    # consolidator chart is a mix no single timeframe can reproduce.
    _record_actual_strategy_bars(request, strategy)

    # ── Serialize insights ──
    insights_dicts = [i.to_dict() for i in result.insights]

    validation_analytics: EngineValidationAnalyticsResponse | None = None
    try:
        validation_analytics = compute_engine_validation_analytics(
            trades=[
                ValidationTrade(
                    trade_number=trade.trade_number,
                    entry_ms_utc=trade.entry_time,
                    exit_ms_utc=trade.exit_time,
                    pnl_pct=trade.pnl_pct,
                )
                for trade in formatted
            ],
            equity_curve=[
                ValidationEquityPoint(
                    timestamp_ms_utc=point["timestamp"],
                    equity=point["equity"],
                )
                for point in equity_curve_dicts
            ],
        )
    except Exception as exc:
        logger.exception("[ENGINE] Validation analytics rejected engine output")
        on_log(f"Validation analytics unavailable: {exc}")

    run_verdict = compute_run_verdict(
        {
            "statistics": stats,
            "win_rate": win_rate,
            "total_trades": total,
            "net_profit": float(result.net_profit),
            "total_fees": float(result.total_fees),
            "lean_statistics": lean_stats_resp.model_dump(mode="json") if lean_stats_resp else None,
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
        lean_statistics=lean_stats_resp,
        trades=formatted,
        log_lines=result.log_lines,
        equity_curve=equity_curve_dicts,
        chart_bars=chart_bars_dicts,
        insights=insights_dicts,
        insight_summary=result.insight_summary,
        data_policy=request.data_policy,  # PR B — echo the normalized policy
        run_verdict=run_verdict,
        validation_analytics=validation_analytics,
    )

    # ── Auto-save to .NET backend (synchronous so we can return the id) ──
    # Used by the Engine Lab to enable the Replay tab right after a run
    # without an extra round-trip to /api/studies?latest=true. The save
    # itself is best-effort — a backend hiccup leaves study_id=None and
    # logs the failure but does not fail the backtest response.
    on_phase("persisting")
    on_log("Persisting run to history")
    # Minted BEFORE persisting so the row itself carries the group id —
    # the .NET persist step joins the LEAN companion back to this run
    # through it when computing the frozen ParityVerdict.
    parity_group_id = new_parity_group_id()
    response.study_id = _save_study_sync(
        response=response,
        symbol=strategy.ctx.symbols[0] if strategy.ctx.symbols else "SPY",
        start_date=request.from_date or "",
        end_date=request.to_date or "",
        resolution=request.resolution or "minute",
        params_json=json.dumps(request.params) if request.params else "{}",
        duration_ms=int((time.time() - _run_start) * 1000),
        commission_per_order=float(request.commission_per_order),
        parity_group_id=parity_group_id,
    )

    on_log(f"Saved study {response.study_id}")

    if response.study_id is not None:
        # Every persisted run gets a parity disposition: an async LEAN
        # validating companion when eligible, an honest "unavailable"
        # verdict row otherwise. Best-effort — never fails the run.
        on_log("Recording parity disposition")
        dispatch_parity_companion(
            registration=registration,
            request=request,
            parity_group_id=parity_group_id,
            left_execution_id=response.study_id,
        )

    return response


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------
def _to_ms_utc(dt: datetime) -> int:
    """Convert a datetime to canonical int64 ms UTC for API payloads."""
    try:
        return to_ms_utc(dt)
    except ValueError as exc:
        raise ValueError("engine timestamp must be timezone-aware before serialization") from exc


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
        "t": _to_ms_utc(b.time),
        "o": float(b.open),
        "h": float(b.high),
        "l": float(b.low),
        "c": float(b.close),
        "v": int(b.volume),
    }


# ---------------------------------------------------------------------------
# Study auto-save (fire-and-forget background task)
# ---------------------------------------------------------------------------
def _save_study_sync(
    *,
    response: EngineBacktestResponse,
    symbol: str,
    start_date: str,
    end_date: str,
    resolution: str,
    params_json: str,
    duration_ms: int,
    commission_per_order: float = 0.0,
    parity_group_id: str | None = None,
) -> int | None:
    """POST the backtest result to the .NET backend for persistence.

    Returns the saved study id so the Engine Lab can immediately enable
    the Replay tab. Returns None when the save fails — the run itself
    is unaffected; persistence is best-effort.
    """
    from app.config import settings

    backend_url = getattr(settings, "BACKEND_URL", "http://localhost:5000")
    url = f"{backend_url}/api/studies"

    # Extract LEAN portfolio stats for the top-level columns
    lp = response.lean_statistics.portfolio if response.lean_statistics else None
    lt = response.lean_statistics.trade if response.lean_statistics else None

    body: dict[str, Any] = {
        "symbol": symbol,
        "strategyName": response.strategy_name,
        "parameters": params_json,
        "startDate": start_date,
        "endDate": end_date,
        "timespan": resolution,
        "fillMode": response.fill_mode,
        "source": "engine",
        "totalTrades": response.total_trades,
        "winningTrades": response.winning_trades,
        "losingTrades": response.losing_trades,
        "totalPnL": response.net_profit,
        "maxDrawdown": lp.drawdown if lp else response.statistics.get("max_drawdown_pct", 0),
        "sharpeRatio": lp.sharpe_ratio if lp else response.statistics.get("sharpe_ratio", 0),
        "initialCash": response.initial_cash,
        "finalEquity": response.final_equity,
        "totalFees": response.total_fees,
        "winRate": response.win_rate,
        "compoundingAnnualReturn": lp.compounding_annual_return if lp else 0,
        "sortinoRatio": lp.sortino_ratio if lp else response.statistics.get("sortino_ratio", 0),
        "probabilisticSharpeRatio": lp.probabilistic_sharpe_ratio if lp else 0,
        "profitFactor": lt.profit_factor if lt else response.statistics.get("profit_factor", 0),
        "alpha": lp.alpha if lp else 0,
        "beta": lp.beta if lp else 0,
        "informationRatio": lp.information_ratio if lp else 0,
        "trackingError": lp.tracking_error if lp else 0,
        "treynorRatio": lp.treynor_ratio if lp else 0,
        "valueAtRisk95": lp.value_at_risk_95 if lp else 0,
        "valueAtRisk99": lp.value_at_risk_99 if lp else 0,
        "annualStandardDeviation": lp.annual_standard_deviation if lp else 0,
        "drawdownRecoveryDays": lp.drawdown_recovery if lp else 0,
        "leanStatisticsJson": response.lean_statistics.model_dump_json() if response.lean_statistics else None,
        "durationMs": duration_ms,
        # PR B (2026-05-19) — DataPolicy / Commission / Brokerage. Always
        # populated because the request synthesizer guarantees ``data_policy``
        # is non-null by the time we reach response construction. The .NET
        # ``SaveStudyAsync`` endpoint writes these into the new columns.
        "dataPolicyJson": response.data_policy.model_dump_json() if response.data_policy else None,
        "commissionPerOrder": commission_per_order,
        # Python engine doesn't model brokerage — record the LEAN-side
        # convention so the compare-view's soft-match treats it correctly.
        "brokeragePolicy": "algorithm_default",
        "parityGroupId": parity_group_id,
        "runVerdictJson": response.run_verdict.model_dump_json() if response.run_verdict else None,
        "verdictVersion": response.run_verdict.verdict_version if response.run_verdict else None,
        "verdictGrade": response.run_verdict.grade if response.run_verdict else None,
        "verdictSignal": response.run_verdict.signal if response.run_verdict else None,
        "equityCurveJson": json.dumps(
            from_engine_curve(
                response.equity_curve,
                trade_timestamps={t.entry_time for t in response.trades} | {t.exit_time for t in response.trades},
            )
        ),
        # Frozen at run time — the persisted run is the single render
        # source for the workbench and the run-detail page, so the atlas
        # analytics must survive the response. Null when the analytics
        # computation rejected the run's output (honest missing).
        "validationAnalyticsJson": (
            json.dumps(
                build_validation_analytics_envelope(
                    response.validation_analytics,
                    engine="python",
                    computed_at_ms=now_ms_utc(),
                )
            )
            if response.validation_analytics
            else None
        ),
        "insightSummaryJson": json.dumps(response.insight_summary) if response.insight_summary else None,
        # Dollar PnL net of commission, matching LEAN's persisted
        # ``t.pnL`` semantics. The engine charges ``commission_per_order``
        # on both entry and exit fills, so each round-trip incurs
        # ``2 × commission_per_order``. Without this scaling the
        # persisted ``BacktestTrade.PnL`` column silently disagreed with
        # the engine's own ``net_profit`` by a factor of ``quantity``
        # and the per-trade commission. See
        # ``.claude/rules/numerical-rigor.md`` → ``PNL_DRIFT``.
        "trades": [
            {
                "tradeType": "Buy",
                "entryTimestamp": t.entry_time,
                "exitTimestamp": t.exit_time,
                "entryPrice": t.entry_price,
                "exitPrice": t.exit_price,
                "quantity": t.quantity,
                "pnL": t.pnl_pts * t.quantity - 2 * commission_per_order,
                "cumulativePnL": 0,  # not tracked per-trade in engine format
                "signalReason": t.signal_reason,
            }
            for t in response.trades
        ],
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=body)
            if resp.status_code < 300:
                payload = resp.json()
                study_id = payload.get("id")
                logger.info("[ENGINE] Study saved (id=%s)", study_id)
                return int(study_id) if study_id is not None else None
            logger.warning("[ENGINE] Study save failed: %s %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.exception("[ENGINE] Study save request failed — study not persisted")
    return None
