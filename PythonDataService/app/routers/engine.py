"""LEAN-compatible backtest engine API.

POST /api/engine/backtest runs a strategy through the in-process engine at
``app.engine`` against LEAN-format minute data. Phase 1 supports a single
registered strategy (``spy_ema_crossover``) with bit-exact LEAN parity.

This endpoint is intentionally separate from the existing
``/api/backtest`` pipeline so both can coexist while the new engine is
being rolled out.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from app.engine.data.availability import (
    AvailabilityReport,
    Resolution,
    check_availability,
    ensure_range,
)
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.results.statistics import summarize
from app.engine.strategy.algorithms.rsi_mean_reversion import (
    RsiMeanReversionAlgorithm,
)
from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm
from app.engine.strategy.algorithms.spy_ema_crossover import (
    SpyEmaCrossoverAlgorithm,
)
from app.engine.strategy.base import Strategy

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------
# Each registered strategy declares:
#   * ``factory``  — a zero-arg callable that returns an instance when invoked
#                    with the strategy's own default parameters
#   * ``param_schema`` — a Pydantic model the router uses to validate the
#                        request body's ``params`` field and to advertise the
#                        schema via ``GET /api/engine/strategies``
#   * ``build``    — a callable that takes the validated params model and
#                    returns a fully-constructed strategy instance
#   * ``display_name`` / ``description`` — shown by the Angular strategy picker
#
# Keeping the three callables separate (factory/build + schema) lets the
# router expose a default-argument instance for metadata listing while still
# honouring request-level overrides when the user actually runs a backtest.
# ---------------------------------------------------------------------------


class StrategyParamsBase(BaseModel):
    """Base for every strategy's parameter model.

    Subclasses declare the strategy's own fields. A strategy with no
    parameters can simply reuse this class directly.
    """

    model_config = {"extra": "forbid"}


class SpyEmaCrossoverParams(StrategyParamsBase):
    """SPY EMA crossover has no tunables today.

    The algorithm's indicators (EMA5, EMA10, RSI14) and gap / RSI thresholds
    are hardcoded to match the LEAN reference exactly — changing them would
    break the bit-exact validation. This class exists so every registered
    strategy has a schema; future work can promote fields here.
    """


class EmaCrossoverSymbolParams(StrategyParamsBase):
    """Parametrized variant of the SPY EMA crossover rule set.

    Shares the exact indicator / gap / RSI logic as SpyEmaCrossoverAlgorithm
    but lets the user pick the ticker. Defaults to QQQ so the registry
    entry "qqq_ema_crossover" reads naturally; other symbols (IWM, etc.)
    can be substituted without touching code as long as the data has been
    fetched into the cache first.
    """

    symbol: str = Field("QQQ", min_length=1, max_length=20)


class SmaCrossoverParams(StrategyParamsBase):
    symbol: str = Field("SPY", min_length=1, max_length=20)
    short_window: int = Field(10, ge=2, le=500)
    long_window: int = Field(30, ge=3, le=1000)
    resolution_minutes: int = Field(15, ge=1, le=1440)


class DailySmaCrossoverParams(StrategyParamsBase):
    """Daily-resolution SMA crossover — no ``resolution_minutes`` field.

    The bar cadence is fixed to 1 day by the registry's build function
    (which sets ``resolution_minutes=1440`` on the underlying algorithm)
    because the strategy runs directly against LEAN daily zips. Window
    sizes are in *days* here: a 50/200 is the classic long-term golden
    cross.
    """

    symbol: str = Field("AAPL", min_length=1, max_length=20)
    short_window: int = Field(50, ge=2, le=500)
    long_window: int = Field(200, ge=3, le=1000)


class RsiMeanReversionParams(StrategyParamsBase):
    symbol: str = Field("SPY", min_length=1, max_length=20)
    window: int = Field(14, ge=2, le=500)
    oversold: float = Field(30.0, gt=0, lt=100)
    overbought: float = Field(70.0, gt=0, lt=100)
    resolution_minutes: int = Field(15, ge=1, le=1440)


@dataclass
class StrategyRegistration:
    display_name: str
    description: str
    param_schema: type[StrategyParamsBase]
    build: "Callable[[StrategyParamsBase], Strategy]"
    # Which data resolutions the strategy can run against. Defaults to
    # minute-only because every currently-ported strategy consolidates
    # minute bars via a ``TradeBarConsolidator``. Daily-native strategies
    # explicitly declare ``{"daily"}``.
    supported_resolutions: set[str] = dc_field(default_factory=lambda: {"minute"})


_STRATEGY_REGISTRY: dict[str, StrategyRegistration] = {
    "spy_ema_crossover": StrategyRegistration(
        display_name="SPY EMA Crossover (LEAN parity)",
        description=(
            "15-minute EMA(5)/EMA(10) crossover with Wilders RSI(14) filter. "
            "Fixed to the LEAN reference rules so the engine's output can be "
            "validated bit-exactly against the LEAN trade log."
        ),
        param_schema=SpyEmaCrossoverParams,
        build=lambda _p: SpyEmaCrossoverAlgorithm(),
    ),
    "qqq_ema_crossover": StrategyRegistration(
        display_name="QQQ EMA Crossover (SPY rules, QQQ data)",
        description=(
            "Identical 15-minute EMA(5)/EMA(10) crossover with Wilders "
            "RSI(14) filter as the LEAN-parity SPY strategy — same "
            "indicators, same gap/RSI thresholds, same 5-bar hold — "
            "but run against a configurable symbol (default QQQ). Data "
            "must already be in the reference mount or the Polygon "
            "cache; enable auto-fetch if not."
        ),
        param_schema=EmaCrossoverSymbolParams,
        build=lambda p: SpyEmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
        ),
    ),
    "sma_crossover": StrategyRegistration(
        display_name="SMA Crossover",
        description=(
            "Classic golden-cross / death-cross rule. Enters long when the "
            "short SMA crosses above the long SMA and exits on the opposite "
            "cross. Configurable symbol, window sizes, and bar resolution."
        ),
        param_schema=SmaCrossoverParams,
        build=lambda p: SmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            short_window=p.short_window,  # type: ignore[attr-defined]
            long_window=p.long_window,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
    "daily_sma_crossover": StrategyRegistration(
        display_name="Daily SMA Crossover",
        description=(
            "Long-term golden-cross / death-cross run against LEAN daily "
            "bars (one zip per symbol under equity/usa/daily/). Defaults "
            "to the classic 50/200 on AAPL. The underlying algorithm is "
            "the same SmaCrossoverAlgorithm used for intraday — only the "
            "bar cadence differs, which is handled by the data reader."
        ),
        param_schema=DailySmaCrossoverParams,
        build=lambda p: SmaCrossoverAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            short_window=p.short_window,  # type: ignore[attr-defined]
            long_window=p.long_window,  # type: ignore[attr-defined]
            # 1440 min = 1 day. TradeBarConsolidator is reference-rounded
            # to midnight ET and passes daily bars through 1:1 as long as
            # consecutive inputs are separated by >= 1 day, which is
            # always true for LEAN daily zip rows.
            resolution_minutes=1440,
        ),
        supported_resolutions={"daily"},
    ),
    "rsi_mean_reversion": StrategyRegistration(
        display_name="RSI Mean Reversion",
        description=(
            "Long-only RSI threshold strategy. Buys when RSI drops below the "
            "oversold level and sells when RSI rises above the overbought "
            "level. Configurable symbol, window, thresholds, and resolution."
        ),
        param_schema=RsiMeanReversionParams,
        build=lambda p: RsiMeanReversionAlgorithm(
            symbol=p.symbol,  # type: ignore[attr-defined]
            window=p.window,  # type: ignore[attr-defined]
            oversold=p.oversold,  # type: ignore[attr-defined]
            overbought=p.overbought,  # type: ignore[attr-defined]
            resolution_minutes=p.resolution_minutes,  # type: ignore[attr-defined]
        ),
    ),
}


def _resolve_lean_data_root() -> Path:
    """Return the LEAN reference Data directory.

    Reads the ``LEAN_DATA_ROOT`` environment variable if set; otherwise
    falls back to the standard local-development location. This root is
    expected to be read-only in containerized deployments — any
    Polygon-sourced data goes into the cache root instead.
    """
    configured = os.environ.get("LEAN_DATA_ROOT")
    if configured:
        return Path(configured)
    return Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")


def _resolve_lean_cache_root() -> Path:
    """Return the writable cache root for Polygon-sourced LEAN zips.

    Reads ``LEAN_DATA_CACHE`` if set, otherwise defaults to a sibling
    ``lean-cache`` directory next to the service. This root is writable
    and receives any data fetched on demand for symbols that aren't in
    the read-only reference mount.
    """
    configured = os.environ.get("LEAN_DATA_CACHE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "lean-cache"


def _resolve_lean_data_roots() -> list[Path]:
    """Return the ordered list of roots the reader should search.

    Reference mount comes first so the bit-exact SPY fixture always wins
    over anything that may have been materialized into the cache with the
    same date range.
    """
    roots: list[Path] = []
    ref = _resolve_lean_data_root()
    if ref.exists():
        roots.append(ref)
    cache = _resolve_lean_cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    roots.append(cache)
    return roots


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class EngineBacktestRequest(BaseModel):
    strategy_name: str = Field(..., description="Registered strategy identifier")
    fill_mode: str = Field(
        "signal_bar_close",
        description="Fill mode: signal_bar_close or next_bar_open",
    )
    commission_per_order: float = Field(1.0, ge=0)
    # Optional overrides — when omitted, the strategy's own defaults (set in
    # its Initialize equivalent) are used.
    start_date: str | None = Field(None, description="YYYY-MM-DD override")
    end_date: str | None = Field(None, description="YYYY-MM-DD override")
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


class EngineTradeResponse(BaseModel):
    trade_number: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
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
    trades: list[EngineTradeResponse] = []
    log_lines: list[str] = []
    error: str | None = None


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
    if req.start_date:
        d = datetime.strptime(req.start_date, "%Y-%m-%d")
        strategy.set_start_date(d.year, d.month, d.day)
    if req.end_date:
        d = datetime.strptime(req.end_date, "%Y-%m-%d")
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
        entry_time=trade.entry_time.strftime("%Y-%m-%d %H:%M"),
        entry_price=float(trade.entry_price),
        exit_time=trade.exit_time.strftime("%Y-%m-%d %H:%M"),
        exit_price=float(trade.exit_price),
        indicators=indicators,
        pnl_pts=float(trade.pnl_pts),
        pnl_pct=float(trade.pnl_pct),
        result=trade.result,
        signal_reason=getattr(trade, "signal_reason", "") or "",
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
        result.append(
            StrategyInfo(
                name=name,
                display_name=reg.display_name,
                description=reg.description,
                params_schema=reg.param_schema.model_json_schema(),
                supported_resolutions=sorted(reg.supported_resolutions),
            )
        )
    return result


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

    cache_root = _resolve_lean_cache_root()
    cache_root.mkdir(parents=True, exist_ok=True)

    try:
        polygon = PolygonClientService()
        files = export_polygon_range_to_lean(
            polygon=polygon,
            output_root=cache_root,
            symbol=request.symbol.upper(),
            from_date=request.from_date,
            to_date=request.to_date,
            adjusted=request.adjusted,
            resolution=request.resolution,
        )
    except Exception as exc:  # noqa: BLE001
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

    roots = _resolve_lean_data_roots()
    report: AvailabilityReport = check_availability(
        roots=roots,
        symbol=symbol,
        start=start_date,
        end=end_date,
        resolution=resolution,
    )
    data = report.to_dict()
    return AvailabilityResponse(**data)


@router.post("/backtest", response_model=EngineBacktestResponse)
def run_engine_backtest(request: EngineBacktestRequest) -> EngineBacktestResponse:
    """Run a strategy through the LEAN-compatible backtest engine.

    The engine reads LEAN-format minute zips from the configured data root
    and produces trades that reproduce LEAN's reference log bit-exactly
    when the same strategy is run against the same data.
    """
    registration = _STRATEGY_REGISTRY.get(request.strategy_name)
    if registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Unknown strategy '{request.strategy_name}'. "
                f"Registered: {sorted(_STRATEGY_REGISTRY)}"
            ),
        )

    # Strategies declare which resolutions they accept. Reject up-front so
    # the user gets a clear 400 instead of a cryptic mismatch deep inside
    # the engine when a daily-only strategy is run against minute data (or
    # vice versa).
    if request.resolution not in registration.supported_resolutions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Strategy '{request.strategy_name}' does not support "
                f"resolution '{request.resolution}'. Supported: "
                f"{sorted(registration.supported_resolutions)}"
            ),
        )

    # Validate ``request.params`` against the strategy's own schema. We do this
    # explicitly rather than making ``params`` a typed field on the request,
    # because different strategies have different parameter shapes and the
    # request has to accept all of them.
    try:
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

    data_roots = _resolve_lean_data_roots()
    if not data_roots:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)",
        )

    strategy = registration.build(validated_params)

    # Optional on-demand fetch: if the caller asked for auto_fetch and we
    # know the symbol + date range, make sure the cache has everything the
    # engine will try to read. The SPY fixture never needs this because it
    # lives in the read-only reference mount and is already complete.
    if request.auto_fetch:
        symbol = getattr(validated_params, "symbol", None)
        start_override = request.start_date
        end_override = request.end_date
        if symbol and start_override and end_override:
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
                    resolution=request.resolution,
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
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

    # Pick the reader class to match the requested resolution. Both
    # readers share the same ``iter_bars(symbol, start, end)`` contract so
    # the engine loop is unchanged — only the bar cadence differs.
    reader: LeanMinuteDataReader | LeanDailyDataReader
    if request.resolution == "daily":
        reader = LeanDailyDataReader(data_roots)
    else:
        reader = LeanMinuteDataReader(data_roots)
    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(
            mode=fill_mode,
            commission_per_order=Decimal(str(request.commission_per_order)),
        ),
    )

    # The strategy's initialize() runs inside engine.run(). We need to
    # apply overrides *after* initialize but *before* the main loop, so we
    # wrap initialize to interleave the override step.
    original_initialize = strategy.initialize

    def _wrapped_initialize() -> None:
        original_initialize()
        _apply_overrides(strategy, request)

    strategy.initialize = _wrapped_initialize  # type: ignore[assignment]

    try:
        result = engine.run(strategy)
    except Exception as exc:  # noqa: BLE001 — router must return JSON error
        logger.exception("[ENGINE] Backtest failed for %s", request.strategy_name)
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
            trading_days = max(1, int(round(delta * 252 / 365)))

    stats = summarize(
        initial_cash=float(result.initial_cash),
        final_equity=float(result.final_equity),
        trades=trades,
        trading_days=trading_days,
    )

    return EngineBacktestResponse(
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
        trades=formatted,
        log_lines=result.log_lines,
    )
