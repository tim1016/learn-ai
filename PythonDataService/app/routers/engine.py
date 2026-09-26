"""LEAN-compatible backtest engine API.

POST /api/engine/backtest runs a strategy through the in-process engine at
``app.engine`` against LEAN-format minute data. Phase 1 supports a single
registered strategy (``spy_ema_crossover``) with bit-exact LEAN parity.

This endpoint is intentionally separate from the existing
``/api/backtest`` pipeline so both can coexist while the new engine is
being rolled out.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from app.engine.data.availability import (
    AvailabilityReport,
    check_availability,
)
from app.engine.data.policy_store import policy_key
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    ChartParamRef,
    public_params_schema,
)
from app.research.sweep.eligibility import sweep_eligibility
from app.schemas.engine_availability import AvailabilityResponse
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.schemas.engine_chart import EngineChartRequest, EngineChartResponse
from app.schemas.strategy_lean_source import StrategyLeanSourceResponse
from app.services.engine_backtest_service import (
    _parse_iso_date,
    _resolve_lean_data_roots,
    _serialize_chart_bar,
    execute_engine_backtest,
)
from app.services.engine_bars_service import read_consolidated_bars
from app.services.engine_chart_service import build_engine_chart
from app.services.strategy_lean_source_service import (
    StrategyLeanSourceNotFoundError,
    resolve_strategy_lean_source,
)

router = APIRouter()


def _noop_phase(_: str) -> None:
    pass


def _noop_log(_: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class StrategyBarCadenceInfo(BaseModel):
    timespan: Literal["minute", "day"]
    multiplier: int = Field(ge=1)
    parameter: str | None = None


class SweepEligibilityInfo(BaseModel):
    eligible: bool
    reason_codes: list[str] = Field(default_factory=list)
    offending_parameters: list[str] = Field(default_factory=list)


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
    strategy_bars: StrategyBarCadenceInfo
    # Sweep eligibility (PRD #1926): the one structured answer Recency Chart,
    # Grid Search and Walk-Forward all derive from — flag, reason codes, and
    # the offending PUBLIC parameters. ``recency_supported`` is its flag,
    # kept for existing consumers.
    strategy_category: str = "production_candidate"
    sweep_eligibility: SweepEligibilityInfo = Field(default_factory=lambda: SweepEligibilityInfo(eligible=False))
    recency_supported: bool = False


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
        cadence_param = reg.strategy_bars.multiplier
        defaults = reg.param_schema.model_validate({})
        eligibility = sweep_eligibility(reg)
        multiplier = (
            getattr(defaults, cadence_param.field) if isinstance(cadence_param, ChartParamRef) else cadence_param
        )
        result.append(
            StrategyInfo(
                name=name,
                display_name=reg.display_name,
                description=reg.description,
                params_schema=public_params_schema(reg),
                supported_resolutions=sorted(reg.supported_resolutions),
                algorithm_pseudocode=reg.algorithm_pseudocode,
                gotchas=list(reg.gotchas),
                pine_available=reg.pine_generator is not None,
                sizing_surface=reg.sizing_surface,
                lean_twin=reg.lean_twin,
                strategy_category=reg.strategy_category,
                sweep_eligibility=SweepEligibilityInfo(**eligibility.as_dict()),
                recency_supported=eligibility.eligible,
                strategy_bars=StrategyBarCadenceInfo(
                    timespan=reg.strategy_bars.timespan,
                    multiplier=multiplier,
                    parameter=cadence_param.field if isinstance(cadence_param, ChartParamRef) else None,
                ),
            )
        )
    return result


@router.get(
    "/strategies/{name}/lean-source",
    response_model=StrategyLeanSourceResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Strategy or registered LEAN source not found."
        }
    },
)
async def get_strategy_lean_source(name: str) -> StrategyLeanSourceResponse:
    """Return versioned QCAlgorithm source without detecting the LEAN launcher."""

    try:
        source = resolve_strategy_lean_source(name)
    except StrategyLeanSourceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return StrategyLeanSourceResponse.from_strategy_source(source)


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
# Data availability endpoint
# ---------------------------------------------------------------------------
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
    return AvailabilityResponse.from_report(report)


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


@router.post("/chart", response_model=EngineChartResponse)
async def get_engine_chart(request: EngineChartRequest) -> EngineChartResponse:
    """Render exact strategy bars and indicators from one policy-store read."""
    try:
        return await asyncio.to_thread(build_engine_chart, request)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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
