"""
Implied Volatility Surface API Router
=======================================

Complete RESTful endpoints for building, querying, and analyzing
implied volatility surfaces per the IV Dashboard design plan.

Endpoints:
  POST   /surface/build               — Build from OptionRecord list
  POST   /surface/build-from-ticker   — Fetch chain via data loader
  POST   /surface/build-from-csv      — Parse CSV text
  GET    /surface/{id}/grid           — Matrix grid (x, y, z)
  GET    /surface/{id}/smiles         — Per-expiry fitted + market smiles
  GET    /surface/{id}/diagnostics    — Full diagnostics
  POST   /surface/{id}/query          — Query specific (K, T) points
  GET    /surface/{id}/export/{format} — Export as JSON/CSV/Parquet
  POST   /surface/batch-summary       — Build/load surfaces for date range
"""

from __future__ import annotations

import csv
import io
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.volatility.analytics import (
    compute_health_score,
    compute_skew_metrics,
)
from app.volatility.cache import (
    SCHEMA_VERSION,
    DataFilters,
    SurfaceCache,
    compute_surface_id,
)
from app.volatility.conventions import (
    SurfaceConventions,
    dte_to_ttm,
    ttm_to_dte,
)
from app.volatility.models import (
    ArbitrageDetail,
    BatchSummaryRequest,
    BatchSummaryResponse,
    BuildFromCsvRequest,
    BuildFromTickerRequest,
    ConventionsModel,
    DailySummary,
    DataFiltersModel,
    DiagnosticsResponse,
    FitParamsResponse,
    GridMetaModel,
    MatrixGridResponse,
    RejectionBreakdown,
    SliceDiagnosticsResponse,
    SmilePointModel,
    SmileSliceResponse,
    SmilesResponse,
    SurfaceBuildRequest,
    SurfaceBuildSummary,
    SurfaceMethodEnum,
)
from app.volatility.surface import (
    SurfaceMethod,
    VolSurface,
    VolSurfaceBuilder,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/surface", tags=["volatility-surface"])

# ── Module-level singletons ──────────────────────────────────────────────────
_cache = SurfaceCache()
_surfaces: dict[str, VolSurface] = {}
_MAX_MEMORY_CACHE = 50


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compute_surface_id_from_build(
    ticker: str,
    date: str,
    method: str,
    conventions: ConventionsModel,
    filters: DataFiltersModel,
    n_options: int,
) -> str:
    """Compute deterministic surface ID from build inputs."""
    conv = SurfaceConventions(
        rate=conventions.rate,
        dividend_yield=conventions.dividend_yield,
        day_count=conventions.day_count,
        forward_model=conventions.forward_model,
    )
    filt = DataFilters(
        min_dte=filters.min_dte,
        max_dte=filters.max_dte,
        min_open_interest=filters.min_open_interest,
        max_spread_pct=filters.max_spread_pct,
    )
    return compute_surface_id(
        ticker=ticker,
        date=date,
        method=method,
        conventions=conv,
        filters=filt,
        n_options=n_options,
    )


def _cache_surface_in_memory(surface_id: str, surface: VolSurface) -> None:
    """Store surface in-memory, evicting oldest if cache full."""
    if len(_surfaces) >= _MAX_MEMORY_CACHE:
        oldest_key = next(iter(_surfaces))
        del _surfaces[oldest_key]
    _surfaces[surface_id] = surface


def _try_load_surface_cached(surface_id: str) -> VolSurface | None:
    """Try to load surface from memory cache first, then disk cache."""
    if surface_id in _surfaces:
        return _surfaces[surface_id]
    try:
        return _cache.load_surface(surface_id)
    except Exception:
        return None


def _build_surface_from_records(
    records: list[dict],
    spot: float,
    rate: float,
    dividend: float,
    eval_date: str,
    method: SurfaceMethodEnum,
    min_contracts_per_slice: int = 5,
    sabr_beta: float = 0.5,
) -> VolSurface:
    """Build a VolSurface from option records."""
    builder = VolSurfaceBuilder(
        spot=spot,
        rate=rate,
        dividend=dividend,
        eval_date=eval_date,
        min_contracts_per_slice=min_contracts_per_slice,
        sabr_beta=sabr_beta,
    )
    method_enum = SurfaceMethod(method.value)
    return builder.build(records, method=method_enum)


def _parse_csv_to_records(
    csv_content: str,
    eval_date: str,
) -> list[dict]:
    """
    Parse CSV to option records.

    Expected columns: strike, expiration_date, bid, ask, option_type, open_interest, volume
    Computes mid price and TTM from eval_date.
    """
    records = []
    reader = csv.DictReader(io.StringIO(csv_content))

    eval_dt = datetime.strptime(eval_date, "%Y-%m-%d")

    for row in reader:
        try:
            strike = float(row["strike"])
            expiry_str = row["expiration_date"]
            bid = float(row["bid"])
            ask = float(row["ask"])
            option_type = row.get("option_type", "call").lower()
            oi = int(row.get("open_interest", 0))
            vol = int(row.get("volume", 0))

            mid = (bid + ask) / 2.0
            expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d")
            dte = (expiry_dt - eval_dt).days
            ttm = dte_to_ttm(dte)

            records.append(
                {
                    "strike": strike,
                    "ttm": ttm,
                    "option_price": mid,
                    "is_call": option_type == "call",
                    "bid": bid,
                    "ask": ask,
                    "open_interest": oi,
                    "volume": vol,
                }
            )
        except (ValueError, KeyError) as e:
            logger.warning("[IV Surface] Skipped CSV row: %s", e)
            continue

    return records


def _build_summary_from_surface(
    surface_id: str,
    surface: VolSurface,
    ticker: str,
    date: str,
    build_time_ms: int,
    cached: bool = False,
) -> SurfaceBuildSummary:
    """Create SurfaceBuildSummary from built surface."""
    diag = surface.diagnostics
    health = compute_health_score(surface)

    n_accepted = diag.n_total_solved
    n_rejected = diag.n_total_failed

    return SurfaceBuildSummary(
        surface_id=surface_id,
        ticker=ticker,
        spot=surface.spot,
        method=surface.method.value,
        date=date,
        cached=cached,
        n_expiries=diag.n_expiries,
        n_contracts_accepted=n_accepted,
        n_contracts_rejected=n_rejected,
        build_time_ms=build_time_ms,
        health_score=health.total,
        valid=diag.valid,
        schema_version=SCHEMA_VERSION,
    )


def _extract_fit_params(surface: VolSurface) -> list[FitParamsResponse]:
    """Extract fitted parameters from surface."""
    return [
        FitParamsResponse(
            ttm=fit.ttm,
            method=fit.method.value,
            params=fit.params,
            rmse=fit.residual_rmse,
        )
        for fit in surface.fits
    ]


def _extract_slice_diagnostics(surface: VolSurface) -> list[SliceDiagnosticsResponse]:
    """Extract per-slice diagnostics from surface."""
    diag = surface.diagnostics
    return [
        SliceDiagnosticsResponse(
            ttm=s.ttm,
            n_contracts=s.n_contracts,
            n_solved=s.n_solved,
            n_failed=s.n_failed,
            fit_method=s.fit_method,
            fit_rmse=s.fit_rmse,
            butterfly_violations=s.arbitrage.butterfly_violations if s.arbitrage else 0,
            arbitrage_passed=s.arbitrage.passed if s.arbitrage else True,
        )
        for s in diag.slices
    ]


def _build_diagnostics_response(
    surface: VolSurface,
    summary: SurfaceBuildSummary,
) -> DiagnosticsResponse:
    """Build full DiagnosticsResponse from surface."""
    diag = surface.diagnostics
    health = compute_health_score(surface)

    n_accepted = diag.n_total_solved
    n_rejected = diag.n_total_failed
    n_total = n_accepted + n_rejected

    rejection_by_reason: dict[str, int] = {}
    for w in diag.warnings:
        if "rejection" in w.lower():
            rejection_by_reason[w] = rejection_by_reason.get(w, 0) + 1

    arbitrage_detail = ArbitrageDetail(
        calendar_violations=sum(s.arbitrage.calendar_violations if s.arbitrage else 0 for s in diag.slices),
        butterfly_violations=sum(s.arbitrage.butterfly_violations if s.arbitrage else 0 for s in diag.slices),
        severity="none" if health.arbitrage_score >= 80 else ("high" if health.arbitrage_score < 40 else "moderate"),
        worst_slices=[],
    )

    return DiagnosticsResponse(
        summary=summary,
        rejections=RejectionBreakdown(
            total_quotes=n_total,
            accepted=n_accepted,
            rejected=n_rejected,
            by_reason=rejection_by_reason,
        ),
        arbitrage=arbitrage_detail,
        fitted_params=_extract_fit_params(surface),
        slices=_extract_slice_diagnostics(surface),
        health_score=health.total,
        warnings=diag.warnings,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/build", response_model=SurfaceBuildSummary)
async def build_surface(request: SurfaceBuildRequest) -> SurfaceBuildSummary:
    """
    Build an implied volatility surface from an option chain.

    Accepts a list of option records (strike, ttm, price, is_call) and
    returns a cached surface_id for subsequent queries.
    """
    t0 = time.perf_counter()
    logger.info(
        "[IV Surface] POST /build ticker=%s method=%s n_options=%d",
        request.ticker,
        request.method.value,
        len(request.options),
    )

    records = [rec.model_dump() for rec in request.options]

    try:
        surface = _build_surface_from_records(
            records=records,
            spot=request.spot,
            rate=request.rate,
            dividend=request.dividend,
            eval_date=request.eval_date,
            method=request.method,
            min_contracts_per_slice=request.min_contracts_per_slice,
            sabr_beta=request.sabr_beta,
        )
    except Exception as e:
        logger.error("[IV Surface] Build failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail=f"Surface build failed: {e!s}",
        )

    if not surface.diagnostics.valid:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Surface build failed",
                "warnings": surface.diagnostics.warnings,
            },
        )

    surface_id = _compute_surface_id_from_build(
        ticker=request.ticker,
        date=request.eval_date,
        method=request.method.value,
        conventions=ConventionsModel(
            rate=request.rate,
            dividend_yield=request.dividend,
        ),
        filters=DataFiltersModel(),
        n_options=len(request.options),
    )

    _cache_surface_in_memory(surface_id, surface)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    logger.info(
        "[IV Surface] Built in %dms — %d expiries, %d/%d solved",
        elapsed_ms,
        surface.diagnostics.n_expiries,
        surface.diagnostics.n_total_solved,
        surface.diagnostics.n_total_contracts,
    )

    return _build_summary_from_surface(
        surface_id=surface_id,
        surface=surface,
        ticker=request.ticker,
        date=request.eval_date,
        build_time_ms=elapsed_ms,
    )


@router.post("/build-from-ticker", response_model=SurfaceBuildSummary)
async def build_from_ticker(request: BuildFromTickerRequest) -> SurfaceBuildSummary:
    """
    Build IV surface by fetching option chain from data source.

    Loads chains via OptionChainLoader, applies filters, and builds surface.
    """
    t0 = time.perf_counter()
    logger.info(
        "[IV Surface] POST /build-from-ticker ticker=%s date=%s method=%s",
        request.ticker,
        request.date,
        request.method.value,
    )

    surface_id = _compute_surface_id_from_build(
        ticker=request.ticker,
        date=request.date,
        method=request.method.value,
        conventions=request.conventions,
        filters=request.filters,
        n_options=0,
    )

    if request.mode in ("auto", "cached"):
        cached = _try_load_surface_cached(surface_id)
        if cached is not None:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "[IV Surface] Loaded from cache in %dms",
                elapsed_ms,
            )
            _cache_surface_in_memory(surface_id, cached)
            return _build_summary_from_surface(
                surface_id=surface_id,
                surface=cached,
                ticker=request.ticker,
                date=request.date,
                build_time_ms=elapsed_ms,
                cached=True,
            )

    if request.mode == "cached":
        raise HTTPException(
            status_code=409,
            detail=f"Surface {surface_id} not cached and mode=cached",
        )

    try:
        from app.volatility.data_loader import OptionChainLoader

        loader = OptionChainLoader()
        records = loader.fetch_and_filter(
            ticker=request.ticker,
            date=request.date,
            min_dte=request.filters.min_dte,
            max_dte=request.filters.max_dte,
            min_open_interest=request.filters.min_open_interest,
            max_spread_pct=request.filters.max_spread_pct,
        )
    except ImportError:
        logger.warning("[IV Surface] OptionChainLoader not available, skipping")
        raise HTTPException(
            status_code=503,
            detail="Data loader not available",
        )
    except Exception as e:
        logger.error("[IV Surface] Failed to fetch chain: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch chain: {e!s}",
        )

    if not records:
        raise HTTPException(
            status_code=422,
            detail="No option contracts after filtering",
        )

    SurfaceConventions(
        rate=request.conventions.rate,
        dividend_yield=request.conventions.dividend_yield,
        day_count=request.conventions.day_count,
        forward_model=request.conventions.forward_model,
    )

    try:
        surface = _build_surface_from_records(
            records=records,
            spot=records[0].get("spot", 100.0),
            rate=request.conventions.rate,
            dividend=request.conventions.dividend_yield,
            eval_date=request.date,
            method=request.method,
        )
    except Exception as e:
        logger.error("[IV Surface] Build failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail=f"Surface build failed: {e!s}",
        )

    if not surface.diagnostics.valid:
        raise HTTPException(
            status_code=422,
            detail=f"Surface invalid: {surface.diagnostics.warnings}",
        )

    _cache_surface_in_memory(surface_id, surface)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "[IV Surface] Built from ticker in %dms — %d expiries",
        elapsed_ms,
        surface.diagnostics.n_expiries,
    )

    return _build_summary_from_surface(
        surface_id=surface_id,
        surface=surface,
        ticker=request.ticker,
        date=request.date,
        build_time_ms=elapsed_ms,
    )


@router.post("/build-from-csv", response_model=SurfaceBuildSummary)
async def build_from_csv(request: BuildFromCsvRequest) -> SurfaceBuildSummary:
    """
    Build IV surface from raw CSV text.

    CSV expected columns: strike, expiration_date, bid, ask, option_type, open_interest, volume
    """
    t0 = time.perf_counter()
    ticker = request.ticker or "UNKNOWN"

    logger.info(
        "[IV Surface] POST /build-from-csv ticker=%s method=%s",
        ticker,
        request.method.value,
    )

    try:
        records = _parse_csv_to_records(
            csv_content=request.csv_content,
            eval_date=datetime.now(UTC).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.error("[IV Surface] CSV parsing failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail=f"CSV parsing failed: {e!s}",
        )

    if not records:
        raise HTTPException(
            status_code=422,
            detail="No valid records in CSV",
        )

    spot = request.spot
    eval_date = datetime.now(UTC).strftime("%Y-%m-%d")

    try:
        surface = _build_surface_from_records(
            records=records,
            spot=spot,
            rate=request.conventions.rate,
            dividend=request.conventions.dividend_yield,
            eval_date=eval_date,
            method=request.method,
        )
    except Exception as e:
        logger.error("[IV Surface] Build failed: %s", e)
        raise HTTPException(
            status_code=422,
            detail=f"Surface build failed: {e!s}",
        )

    if not surface.diagnostics.valid:
        raise HTTPException(
            status_code=422,
            detail=f"Surface invalid: {surface.diagnostics.warnings}",
        )

    surface_id = _compute_surface_id_from_build(
        ticker=ticker,
        date=eval_date,
        method=request.method.value,
        conventions=request.conventions,
        filters=DataFiltersModel(),
        n_options=len(records),
    )

    _cache_surface_in_memory(surface_id, surface)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "[IV Surface] Built from CSV in %dms",
        elapsed_ms,
    )

    return _build_summary_from_surface(
        surface_id=surface_id,
        surface=surface,
        ticker=ticker,
        date=eval_date,
        build_time_ms=elapsed_ms,
    )


@router.get("/{surface_id}/grid", response_model=MatrixGridResponse)
async def get_grid(
    surface_id: str,
    axis: str = Query("log_moneyness", pattern="^(log_moneyness|moneyness|strike)$"),
    n_strikes: int = Query(50, ge=10, le=500),
    dte_days: str | None = Query(None, description="Comma-separated DTE days"),
) -> MatrixGridResponse:
    """
    Retrieve IV grid for a surface as a matrix (x, y, z).

    axis: log_moneyness, moneyness, or strike
    dte_days: Optional comma-separated list of DTE days to include
    """
    surface = _try_load_surface_cached(surface_id)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail=f"Surface {surface_id} not found",
        )

    logger.info(
        "[IV Surface] GET /grid/%s axis=%s n_strikes=%d",
        surface_id,
        axis,
        n_strikes,
    )

    ttm_list = [fit.ttm for fit in surface.fits]

    if dte_days:
        try:
            dte_list = [int(d.strip()) for d in dte_days.split(",")]
            ttm_list = [dte_to_ttm(d) for d in dte_list]
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid dte_days format",
            )

    min_strike = surface.spot * 0.5
    max_strike = surface.spot * 1.5

    try:
        df = surface.to_grid(
            strike_range=(min_strike, max_strike),
            n_strikes=n_strikes,
            ttm_list=ttm_list,
        )
    except Exception as e:
        logger.error("[IV Surface] Grid evaluation failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Grid evaluation failed: {e!s}",
        )

    df = df.sort_values(["ttm", "strike"])

    ttm_unique = sorted(df["ttm"].unique())
    strike_unique = sorted(df["strike"].unique())

    x_vals = []
    for k in strike_unique:
        if axis == "log_moneyness":
            forward = surface.spot * math.exp((surface.rate - surface.dividend) * ttm_unique[0])
            x_vals.append(math.log(k / forward))
        elif axis == "moneyness":
            x_vals.append(k / surface.spot)
        else:
            x_vals.append(k)

    y_vals = [ttm_to_dte(t) for t in ttm_unique]

    z_matrix: list[list[float | None]] = []
    for t in ttm_unique:
        row = []
        for k in strike_unique:
            subset = df[(df["ttm"] == t) & (df["strike"] == k)]
            if not subset.empty:
                iv = subset.iloc[0]["iv"]
                row.append(None if (iv != iv) else float(iv))
            else:
                row.append(None)
        z_matrix.append(row)

    forwards = [surface.spot * math.exp((surface.rate - surface.dividend) * t) for t in ttm_unique]

    expiry_dates = [(datetime.now(UTC) + timedelta(days=dte)).strftime("%Y-%m-%d") for dte in y_vals]

    meta = GridMetaModel(
        spot=surface.spot,
        forwards=forwards,
        n_strikes=n_strikes,
        n_expiries=len(ttm_unique),
        expiry_dates=expiry_dates,
    )

    return MatrixGridResponse(
        x=x_vals,
        y=y_vals,
        z=z_matrix,
        x_label=axis,
        y_label="dte_days",
        z_label="implied_vol",
        meta=meta,
    )


@router.get("/{surface_id}/smiles", response_model=SmilesResponse)
async def get_smiles(
    surface_id: str,
    axis: str = Query("log_moneyness", pattern="^(log_moneyness|moneyness|strike)$"),
) -> SmilesResponse:
    """
    Retrieve fitted and market smile curves per expiry.

    axis: log_moneyness, moneyness, or strike
    """
    surface = _try_load_surface_cached(surface_id)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail=f"Surface {surface_id} not found",
        )

    logger.info(
        "[IV Surface] GET /smiles/%s axis=%s",
        surface_id,
        axis,
    )

    slices = []
    for fit in surface.fits:
        ttm = fit.ttm
        dte = ttm_to_dte(ttm)
        forward = surface.spot * math.exp((surface.rate - surface.dividend) * ttm)

        expiry_date = (datetime.now(UTC) + timedelta(days=dte)).strftime("%Y-%m-%d")

        strike_min = forward * 0.7
        strike_max = forward * 1.3
        strikes = list(linspace(strike_min, strike_max, 50))

        fitted_points = []
        for k in strikes:
            try:
                iv = surface.volatility(k, ttm)
                if iv == iv:
                    x = _get_x_coord(k, forward, surface.spot, axis)
                    fitted_points.append(SmilePointModel(x=x, iv=iv))
            except (ValueError, RuntimeError):
                pass

        market_points = []
        diag_slice = next((s for s in surface.diagnostics.slices if abs(s.ttm - ttm) < 1e-6), None)
        if diag_slice:
            for k in surface.fits:
                if abs(k.ttm - ttm) < 1e-6:
                    pass

        slices.append(
            SmileSliceResponse(
                ttm=ttm,
                dte_days=dte,
                expiry_date=expiry_date,
                forward=forward,
                fitted=fitted_points,
                market=market_points,
            )
        )

    return SmilesResponse(
        x_label=axis,
        slices=slices,
    )


@router.get("/{surface_id}/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(surface_id: str) -> DiagnosticsResponse:
    """
    Retrieve full diagnostics for a surface.
    """
    surface = _try_load_surface_cached(surface_id)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail=f"Surface {surface_id} not found",
        )

    logger.info("[IV Surface] GET /diagnostics/%s", surface_id)

    summary = _build_summary_from_surface(
        surface_id=surface_id,
        surface=surface,
        ticker="",
        date=surface.eval_date,
        build_time_ms=0,
    )

    return _build_diagnostics_response(surface, summary)


@router.post("/{surface_id}/query")
async def query_surface(surface_id: str, queries: list[dict]) -> dict:
    """
    Query IV at specific (strike, ttm) points.

    Body: [{"strike": 100, "ttm": 0.25}, ...]
    """
    surface = _try_load_surface_cached(surface_id)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail=f"Surface {surface_id} not found",
        )

    results = []
    for q in queries:
        try:
            strike = float(q["strike"])
            ttm = float(q["ttm"])
            iv = surface.volatility(strike, ttm)
            results.append(
                {
                    "strike": strike,
                    "ttm": ttm,
                    "iv": float(iv) if iv == iv else None,
                }
            )
        except (ValueError, RuntimeError, KeyError) as e:
            results.append(
                {
                    "strike": q.get("strike"),
                    "ttm": q.get("ttm"),
                    "iv": None,
                    "error": str(e),
                }
            )

    return {"results": results}


@router.get("/{surface_id}/export/{format}")
async def export_surface(
    surface_id: str,
    format: str = "json",
) -> StreamingResponse:
    """
    Export surface as JSON, CSV, or Parquet.

    format: json, csv, or parquet
    """
    surface = _try_load_surface_cached(surface_id)
    if surface is None:
        raise HTTPException(
            status_code=404,
            detail=f"Surface {surface_id} not found",
        )

    if format not in ("json", "csv", "parquet"):
        raise HTTPException(
            status_code=400,
            detail="format must be json, csv, or parquet",
        )

    logger.info("[IV Surface] GET /export/%s as %s", surface_id, format)

    df = surface.to_grid(
        strike_range=(surface.spot * 0.5, surface.spot * 1.5),
        n_strikes=50,
    )

    if format == "json":
        content = df.to_json(orient="records")
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={surface_id}.json"},
        )
    elif format == "csv":
        content = df.to_csv(index=False)
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={surface_id}.csv"},
        )
    elif format == "parquet":
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={surface_id}.parquet"},
        )


@router.post("/batch-summary", response_model=BatchSummaryResponse)
async def batch_summary(request: BatchSummaryRequest) -> BatchSummaryResponse:
    """
    Build or load surfaces for a date range, return daily summaries.

    Returns ATM IV, 25D RR/BF, skew slope per day.
    """
    logger.info(
        "[IV Surface] POST /batch-summary ticker=%s %s to %s",
        request.ticker,
        request.start_date,
        request.end_date,
    )

    start_dt = datetime.strptime(request.start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(request.end_date, "%Y-%m-%d")

    summaries = []
    current_dt = start_dt

    while current_dt <= end_dt:
        date_str = current_dt.strftime("%Y-%m-%d")

        surface_id = _compute_surface_id_from_build(
            ticker=request.ticker,
            date=date_str,
            method=request.method.value,
            conventions=request.conventions,
            filters=request.filters,
            n_options=0,
        )

        cached = _try_load_surface_cached(surface_id)
        if cached is not None:
            _cache_surface_in_memory(surface_id, cached)
            atm_iv = None
            rr_25d = None
            bf_25d = None
            skew_slope = None

            if cached.fits:
                metrics = compute_skew_metrics(cached, cached.fits[0].ttm)
                atm_iv = metrics.atm_iv
                rr_25d = metrics.rr_25d
                bf_25d = metrics.bf_25d
                skew_slope = metrics.skew_slope

            health = compute_health_score(cached)

            summaries.append(
                DailySummary(
                    date=date_str,
                    surface_id=surface_id,
                    atm_iv=atm_iv,
                    rr_25d=rr_25d,
                    bf_25d=bf_25d,
                    skew_slope=skew_slope,
                    n_contracts=cached.diagnostics.n_total_solved,
                    health_score=health.total,
                    cached=True,
                )
            )
        elif request.mode != "cached":
            logger.info("[IV Surface] Batch summary: skipping %s (no cache)", date_str)

        current_dt += timedelta(days=1)

    return BatchSummaryResponse(
        ticker=request.ticker,
        daily_summaries=summaries,
    )


# ── Helpers for smiles ───────────────────────────────────────────────────────


def _get_x_coord(strike: float, forward: float, spot: float, axis: str) -> float:
    """Convert strike to x-axis coordinate per axis type."""
    if axis == "log_moneyness":
        return math.log(strike / forward)
    elif axis == "moneyness":
        return strike / spot
    else:
        return strike


def linspace(start: float, stop: float, num: int) -> list[float]:
    """Simple linspace implementation."""
    if num == 1:
        return [start]
    step = (stop - start) / (num - 1)
    return [start + i * step for i in range(num)]
