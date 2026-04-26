"""FastAPI router for the Edge feature.

Endpoints (per docs/architecture/edge-feature-design.md):
- POST /api/edge/realized-vs-iv/series
- POST /api/edge/realized-vs-iv/signals
- GET  /api/edge/realized-vs-iv/coverage/{symbol}
- POST /api/edge/cross-asset/run
- GET  /api/edge/cross-asset/strategies
- POST /api/edge/regimes/cluster
- POST /api/edge/regimes/strategy-fit
- POST /api/edge/trade-sim/run
- POST /api/edge/edge-score/series

v1 implementation note:
Real Polygon-backed bar fetching is delegated to the existing aggregates router.
This router wraps the engine/edge math; integration with stored DB data is
a follow-up. v1 endpoints accept inline `bars` payloads where applicable so
the frontend can drive end-to-end smoke tests immediately.
"""
from __future__ import annotations

import logging
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.engine.edge.cross_asset_runner import (
    STRATEGY_REGISTRY,
    CrossAssetRunRequest,
    run_cross_asset,
)
from app.engine.edge.edge_score import DEFAULT_WEIGHTS, edge_score
from app.engine.edge.features_realtime.realized_vol import (
    DAILY_BARS_PER_YEAR,
    close_to_close,
    garman_klass,
    parkinson,
    yang_zhang,
)
from app.engine.edge.features_realtime.regime_features import (
    build_ohlcv_features,
)
from app.engine.edge.labels_oracle.forward_rv import forward_rv
from app.engine.edge.regime_clustering import (
    fit_gaussian_hmm,
    kmeans,
    stability_filter,
)
from app.engine.edge.regime_strategy_eval import partition_by_regime
from app.engine.edge.trade_simulator import TradeSimConfig, simulate
from app.engine.edge.vrp import compute_vrp, vrp_signal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/edge", tags=["edge"])

ESTIMATOR_FNS = {
    "ctc": close_to_close,
    "parkinson": parkinson,
    "gk": garman_klass,
    "yz": yang_zhang,
}

BARS_PER_YEAR = {"1d": DAILY_BARS_PER_YEAR, "15m": DAILY_BARS_PER_YEAR * 26}


class BarPayload(BaseModel):
    """Inline OHLCV bar."""
    ts: int = Field(..., description="int64 ms UTC")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class RealizedVsIvSeriesRequest(BaseModel):
    symbol: str
    bar_size: Literal["15m", "1d"] = "1d"
    tenor_days: int = Field(30, ge=1, le=365)
    estimators: list[str] = Field(default_factory=lambda: ["yz"])
    windows: list[int] = Field(default_factory=lambda: [5, 10, 30])
    bars: list[BarPayload]
    iv_series: list[dict] | None = Field(
        None, description="Optional [{ts, iv30}] history; omitted = empty IV column."
    )


class RealizedVsIvSeriesResponse(BaseModel):
    symbol: str
    ts: list[int]
    rv_trailing: dict[str, list[float | None]]
    rv_forward: dict[str, list[float | None]]
    iv30: list[float | None]
    vrp_forward: list[float | None]
    vrp_z: list[float | None]
    coverage: dict


@router.post("/realized-vs-iv/series", response_model=RealizedVsIvSeriesResponse)
async def realized_vs_iv_series(req: RealizedVsIvSeriesRequest) -> RealizedVsIvSeriesResponse:
    """Compute trailing + forward RV for the requested estimators and windows;
    align IV30 series; emit VRP forward + z-score."""
    if not req.bars:
        raise HTTPException(400, "bars must not be empty")
    bars = _bars_payload_to_df(req.bars)
    bpy = BARS_PER_YEAR[req.bar_size]

    rv_trailing: dict[str, list] = {}
    rv_forward: dict[str, list] = {}
    for est in req.estimators:
        if est not in ESTIMATOR_FNS:
            raise HTTPException(400, f"unknown estimator: {est}")
        for w in req.windows:
            key = f"{est}_{w}"
            trailing = ESTIMATOR_FNS[est](bars, window=w, annualize=True, bars_per_year=bpy)
            fwd = forward_rv(bars, estimator=est, window=w, annualize=True, bars_per_year=bpy)
            rv_trailing[key] = _series_to_jsonable(trailing)
            rv_forward[key] = _series_to_jsonable(fwd)

    if req.iv_series:
        iv = pd.Series(
            {int(p["ts"]): float(p["iv30"]) for p in req.iv_series}
        ).reindex(bars.index)
    else:
        iv = pd.Series(index=bars.index, dtype=float)

    rv_for_vrp_key = f"{req.estimators[0]}_{req.tenor_days}" if f"{req.estimators[0]}_{req.tenor_days}" in rv_forward else next(iter(rv_forward))
    rv_for_vrp = pd.Series(rv_forward[rv_for_vrp_key], index=bars.index)
    vrp_fwd = compute_vrp(iv.fillna(np.nan), rv_for_vrp)
    sig = vrp_signal(iv=iv.ffill(), rv=rv_for_vrp.ffill(), lookback=min(252, len(bars) - 1))

    coverage = {
        "n_bars": len(bars),
        "iv_first_ts": int(iv.dropna().index.min()) if iv.notna().any() else None,
        "iv_last_ts": int(iv.dropna().index.max()) if iv.notna().any() else None,
        "forward_nan_bars": int(rv_for_vrp.isna().sum()),
    }
    return RealizedVsIvSeriesResponse(
        symbol=req.symbol,
        ts=[int(t) for t in bars.index.tolist()],
        rv_trailing=rv_trailing,
        rv_forward=rv_forward,
        iv30=_series_to_jsonable(iv),
        vrp_forward=_series_to_jsonable(vrp_fwd),
        vrp_z=_series_to_jsonable(sig.vrp_z),
        coverage=coverage,
    )


class SignalsRequest(RealizedVsIvSeriesRequest):
    rule: Literal["vrp_zscore"] = "vrp_zscore"
    threshold: float = 1.0
    lookback: int = 252


class SignalsResponse(BaseModel):
    symbol: str
    ts: list[int]
    signal_oracle: list[int]
    signal_realtime: list[int]
    vrp_z: list[float | None]


@router.post("/realized-vs-iv/signals", response_model=SignalsResponse)
async def realized_vs_iv_signals(req: SignalsRequest) -> SignalsResponse:
    series = await realized_vs_iv_series(req)
    iv = pd.Series(
        [v if v is not None else np.nan for v in series.iv30], index=series.ts,
    )
    estimator_key = next(iter(series.rv_forward))
    rv_fwd = pd.Series(series.rv_forward[estimator_key], index=series.ts)
    rv_trailing_key = next(iter(series.rv_trailing))
    rv_trailing = pd.Series(series.rv_trailing[rv_trailing_key], index=series.ts)

    sig_oracle = vrp_signal(iv=iv.ffill(), rv=rv_fwd.ffill(),
                            lookback=req.lookback, threshold=req.threshold)
    sig_real = vrp_signal(iv=iv.ffill(), rv=rv_trailing.ffill(),
                          lookback=req.lookback, threshold=req.threshold)
    return SignalsResponse(
        symbol=req.symbol,
        ts=series.ts,
        signal_oracle=[int(x) for x in sig_oracle.side.fillna(0).tolist()],
        signal_realtime=[int(x) for x in sig_real.side.fillna(0).tolist()],
        vrp_z=_series_to_jsonable(sig_oracle.vrp_z),
    )


@router.get("/realized-vs-iv/coverage/{symbol}")
async def realized_vs_iv_coverage(symbol: str) -> dict:
    """Probe how much stored IV history is available for `symbol`.

    v1 stub: returns a non-blocking placeholder so the UI can render the
    coverage banner. Wires into the real OptionIvSnapshots query in v2.
    """
    return {
        "symbol": symbol,
        "iv_first_ts": None,
        "iv_last_ts": None,
        "n_iv_bars": 0,
        "missing_pct": 1.0,
        "note": "v1 placeholder; backed by OptionIvSnapshots in v2",
    }


# ── Cross-asset ────────────────────────────────────────────────────────────

class CrossAssetBars(BaseModel):
    symbol: str
    bars: list[BarPayload]


class CrossAssetRunBody(BaseModel):
    strategy_name: str
    symbols: list[str]
    start_ms: int
    end_ms: int
    bar_size: Literal["15m", "1d"] = "1d"
    split_mode: Literal["rolling", "calendar", "walkforward", "all"] = "all"
    bars_by_symbol: list[CrossAssetBars]


@router.post("/cross-asset/run")
async def cross_asset_run(body: CrossAssetRunBody) -> dict:
    if body.strategy_name not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"unknown strategy {body.strategy_name}")
    bars_by_symbol = {p.symbol: _bars_payload_to_df(p.bars) for p in body.bars_by_symbol}
    request = CrossAssetRunRequest(
        strategy_name=body.strategy_name,
        symbols=body.symbols,
        start_ms=body.start_ms,
        end_ms=body.end_ms,
        bar_size=body.bar_size,
        split_mode=body.split_mode,
    )
    return await run_cross_asset(request, bars_by_symbol)


@router.get("/cross-asset/strategies")
async def cross_asset_strategies() -> dict:
    return {"available_strategies": [{"name": k, "params_schema": {}} for k in STRATEGY_REGISTRY]}


# ── Regimes ─────────────────────────────────────────────────────────────────

class RegimeClusterBody(BaseModel):
    symbol: str
    n_states: int = Field(3, ge=2, le=6)
    algorithms: list[Literal["hmm", "kmeans"]] = Field(default_factory=lambda: ["hmm", "kmeans"])
    p_min: float = 0.7
    min_run_length: int = 5
    bars: list[BarPayload]


@router.post("/regimes/cluster")
async def regimes_cluster(body: RegimeClusterBody) -> dict:
    bars = _bars_payload_to_df(body.bars)
    if len(bars) < 80:
        raise HTTPException(400, "need at least 80 bars to cluster regimes")

    feats = build_ohlcv_features(bars).dropna()
    X = feats.to_numpy(dtype=np.float64)
    out: dict = {"symbol": body.symbol, "ts": [int(t) for t in feats.index.tolist()]}

    if "kmeans" in body.algorithms:
        km = kmeans(X, n_clusters=body.n_states, seed=42)
        out["kmeans_labels"] = km.labels.tolist()
        out["kmeans_centroids"] = km.centroids.tolist()
    if "hmm" in body.algorithms:
        hmm = fit_gaussian_hmm(X, n_states=body.n_states, seed=42)
        active = stability_filter(
            hmm.labels, posterior=hmm.posterior,
            p_min=body.p_min, min_run_length=body.min_run_length,
        )
        out["hmm_labels"] = hmm.labels.tolist()
        out["hmm_posterior"] = hmm.posterior.tolist()
        out["hmm_transition_matrix"] = hmm.transition_matrix.tolist()
        out["hmm_means"] = hmm.means.tolist()
        out["regime_active"] = active.tolist()

    return out


class RegimeStrategyFitBody(BaseModel):
    trades: list[dict]
    regime_labels: list[dict]


@router.post("/regimes/strategy-fit")
async def regimes_strategy_fit(body: RegimeStrategyFitBody) -> dict:
    trades_df = pd.DataFrame(body.trades)
    labels = pd.Series({int(p["ts"]): int(p["label"]) for p in body.regime_labels})
    by_regime = partition_by_regime(trades=trades_df, regime_labels=labels)
    return {"by_regime": {str(k): v for k, v in by_regime.items()}}


# ── Trade sim ───────────────────────────────────────────────────────────────

class TradeSimRunBody(BaseModel):
    bars: list[BarPayload]
    signals: list[dict]
    instrument: Literal["stock", "option"] = "stock"
    time_stop_bars: int = 5
    slippage_pct: float = 0.0005
    commission_per_unit: float = 0.005


@router.post("/trade-sim/run")
async def trade_sim_run(body: TradeSimRunBody) -> dict:
    bars = _bars_payload_to_df(body.bars)
    signals = pd.Series(
        {int(p["ts"]): int(p["side"]) for p in body.signals}
    ).reindex(bars.index).fillna(0).astype(int)
    cfg = TradeSimConfig(
        instrument=body.instrument,
        time_stop_bars=body.time_stop_bars,
        slippage_pct=body.slippage_pct,
        commission_per_unit=body.commission_per_unit,
    )
    res = simulate(bars=bars, signals=signals, config=cfg)
    return {
        "trades": [t.__dict__ for t in res.trades],
        "stats": res.stats,
        "cost_attribution": res.cost_attribution,
        "equity_curve": [] if res.equity_curve is None else res.equity_curve.assign(
            ts=res.equity_curve["ts"].astype(int)
        ).to_dict(orient="records"),
    }


# ── Edge Score ──────────────────────────────────────────────────────────────

class EdgeScoreBody(BaseModel):
    symbol: str
    bars: list[BarPayload]
    iv30: list[float | None]
    regime_labels: list[int]
    weights: dict[str, float] | None = None
    regime_score_map: dict[str, float] | None = None


@router.post("/edge-score/series")
async def edge_score_series(body: EdgeScoreBody) -> dict:
    bars = _bars_payload_to_df(body.bars)
    if not (len(bars) == len(body.iv30) == len(body.regime_labels)):
        raise HTTPException(400, "bars, iv30 and regime_labels must align in length")
    iv = pd.Series(body.iv30, index=bars.index, dtype=float)
    rv = yang_zhang(bars, window=20)
    vrp = compute_vrp(iv, rv)
    high = bars["high"]
    low = bars["low"]
    close = bars["close"]
    atr = (high - low).rolling(14, min_periods=14).mean()
    trend = close.rolling(20, min_periods=20).apply(
        lambda y: float(np.polyfit(np.arange(len(y)), y, 1)[0]), raw=True,
    )
    labels = pd.Series(body.regime_labels, index=bars.index, dtype=int)

    score_map_int = (
        {int(k): float(v) for k, v in body.regime_score_map.items()}
        if body.regime_score_map else None
    )
    res = edge_score(
        vrp=vrp, iv30=iv, trend_slope=trend, atr=atr,
        regime_labels=labels, weights=body.weights or DEFAULT_WEIGHTS,
        regime_score_map=score_map_int,
    )
    return {
        "symbol": body.symbol,
        "ts": [int(t) for t in bars.index.tolist()],
        "edge_score": _series_to_jsonable(res.score),
        "components": {c: _series_to_jsonable(res.components[c]) for c in res.components.columns},
        "action": [int(x) for x in res.action.tolist()],
    }


# ── Helpers ─────────────────────────────────────────────────────────────────

def _bars_payload_to_df(bars: list[BarPayload]) -> pd.DataFrame:
    rows = sorted(bars, key=lambda b: b.ts)
    ts_ms = pd.Index([int(b.ts) for b in rows], name="ts", dtype=np.int64)
    if not ts_ms.is_unique:
        raise HTTPException(400, "bar timestamps must be unique")
    return pd.DataFrame({
        "open": [b.open for b in rows],
        "high": [b.high for b in rows],
        "low":  [b.low for b in rows],
        "close": [b.close for b in rows],
        "volume": [b.volume for b in rows],
    }, index=ts_ms)


def _series_to_jsonable(s: pd.Series) -> list:
    return [None if (isinstance(v, float) and np.isnan(v)) else float(v) for v in s.tolist()]
