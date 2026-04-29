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

from app.engine.edge.confidence import (
    DEFAULT_CONFIDENCE_FLOOR,
    confidence_with_explanation,
    regime_feature_weight,
)
from app.engine.edge.cross_asset_runner import (
    STRATEGY_REGISTRY,
    CrossAssetRunRequest,
    run_cross_asset,
)
from app.engine.edge.edge_score import DEFAULT_WEIGHTS, edge_score
from app.engine.edge.features_realtime.hf_realized_vol import (
    Session,
    hf_realized_vol_trd252,
)
from app.engine.edge.features_realtime.realized_vol import (
    DAILY_BARS_PER_YEAR,
    close_to_close,
    garman_klass,
    parkinson,
    yang_zhang,
)
from app.engine.edge.features_realtime.regime_features import (
    build_full_features,
    build_ohlcv_features,
)
from app.engine.edge.labels_oracle.forward_rv import forward_rv
from app.engine.edge.labels_oracle.hf_forward_rv import hf_forward_rv_trd252
from app.engine.edge.regime_clustering import (
    fit_gaussian_hmm,
    kmeans,
    stability_filter,
)
from app.engine.edge.regime_strategy_eval import partition_by_regime
from app.engine.edge.threshold_events import (
    log_confidence_floor_fired,
    log_imputed_prior_emitted,
)
from app.engine.edge.trade_simulator import TradeSimConfig, simulate
from app.engine.edge.vrp import compute_vrp, vrp_signal
from app.services.iv_recorder import get_iv_store
from app.volatility.basis import convert_iv_act365_to_trading252

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
    session: Session = Field(
        "ETH",
        description="Session for the HF RV estimator. ETH = 04:00-20:00 ET (default), RTH = 09:30-16:00 ET.",
    )
    estimators: list[str] = Field(default_factory=lambda: ["yz"])
    windows: list[int] = Field(default_factory=lambda: [5, 10, 30])
    bars: list[BarPayload]
    iv_series: list[dict] | None = Field(
        None,
        description=(
            "Optional [{ts, iv30, health_score?, variance_contribution_synthetic?}] history. "
            "iv30 must be ACT/365 (the solver's native basis); the router converts to TRD/252 before VRP. "
            "When health_score and variance_contribution_synthetic are supplied, the response includes "
            "per-bar confidence and the VRP signal is gated by it (Step E of IV-ownership plan)."
        ),
    )
    confidence_floor: float = Field(
        DEFAULT_CONFIDENCE_FLOOR,
        ge=0.0,
        le=1.0,
        description="Hard-gate floor: signals are forced to 0 where confidence < floor.",
    )


class RealizedVsIvSeriesResponse(BaseModel):
    symbol: str
    ts: list[int]
    rv_trailing: dict[str, list[float | None]]
    rv_forward: dict[str, list[float | None]]
    iv30: list[float | None]
    iv30_trd252: list[float | None]
    rv_hf_trailing: list[float | None]
    rv_hf_forward: list[float | None]
    vrp_forward: list[float | None]
    vrp_z: list[float | None]
    # Step E additions — populated only when caller supplies per-bar
    # health_score / variance_contribution_synthetic in iv_series.
    iv_source: str = "caller_supplied"
    confidence: list[float | None] | None = None
    vrp_z_scaled: list[float | None] | None = None
    floor_gated: list[bool] | None = None
    explanation: dict | None = None
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

    # Caller-supplied iv_series wins; otherwise fall through to the recorder
    # for the bars window. No silent forward-fill — sparse snapshots remain
    # sparse and downstream consumers handle NaN explicitly.
    if req.iv_series:
        iv_series_for_parse: list[dict] | None = req.iv_series
        iv_source_label = "caller_supplied"
    else:
        recorded = _iv_series_from_recorder(req.symbol, bars.index)
        iv_series_for_parse = recorded if recorded else None
        iv_source_label = "recorder" if recorded else "absent"
    iv, confidence, health_imputed = _parse_iv_series(iv_series_for_parse, bars.index)

    # HF RV (TRD/252) for VRP — for 15-min bars uses two-component HF; for daily
    # bars falls back to YZ at 21-day window (no intraday detail to exploit).
    rv_hf_trail, rv_hf_fwd = _compute_hf_rv_for_vrp(bars, req.bar_size, req.session)

    # Basis-convert IV from ACT/365 (solver-native) to TRD/252 before VRP.
    iv_trd252 = _convert_iv_to_trd252(iv, req.tenor_days)

    vrp_fwd = compute_vrp(iv_trd252.fillna(np.nan), rv_hf_fwd)
    sig = vrp_signal(
        iv=iv_trd252.ffill(),
        rv=rv_hf_fwd.ffill(),
        lookback=min(252, len(bars) - 1),
        confidence=confidence,
        confidence_floor=req.confidence_floor,
    )

    explanation = None
    if confidence is not None and confidence.notna().any():
        latest_idx = confidence.dropna().index.max()
        latest_h = float(confidence.loc[latest_idx])
        # Synthesize a representative breakdown from the latest non-NaN point.
        # The full per-bar history of (health, vcs) lives in iv_series; this is
        # the "what is the system telling me right now" summary for the UI banner.
        # ``health_imputed_now`` propagates the imputed-prior flag from
        # _parse_iv_series so the UI can show "confidence based on imputed
        # health_score" rather than treat the latest number as authoritative.
        if health_imputed is not None and latest_idx in health_imputed.index:
            health_imputed_now = bool(health_imputed.loc[latest_idx])
        else:
            health_imputed_now = False
        gated_now = bool(latest_h < req.confidence_floor)
        explanation = {
            "latest_confidence": latest_h,
            "floor": req.confidence_floor,
            "gated_now": gated_now,
            "health_imputed_now": health_imputed_now,
        }
        # Threshold-firing audit emitters: tagged structured logs the
        # operator can grep across the recorder burn-in to confirm gates
        # fire at the expected rate. Tied to the *latest* bar (the one
        # driving the UI banner) rather than every historical bar to
        # avoid drowning the log on long backfills.
        if gated_now:
            log_confidence_floor_fired(
                ticker=req.symbol,
                snapshot_ts_ms=int(latest_idx) if pd.notna(latest_idx) else None,
                confidence=latest_h,
                floor=req.confidence_floor,
            )
        if health_imputed_now:
            log_imputed_prior_emitted(
                ticker=req.symbol,
                snapshot_ts_ms=int(latest_idx) if pd.notna(latest_idx) else None,
                shape="missing_or_null",
            )

    coverage = {
        "n_bars": len(bars),
        "iv_first_ts": int(iv.dropna().index.min()) if iv.notna().any() else None,
        "iv_last_ts": int(iv.dropna().index.max()) if iv.notna().any() else None,
        "forward_nan_bars": int(rv_hf_fwd.isna().sum()),
        "session": req.session,
        "vrp_basis": "TRD/252 (IV converted from ACT/365 via NYSE calendar)",
        "has_confidence": confidence is not None,
    }
    return RealizedVsIvSeriesResponse(
        symbol=req.symbol,
        ts=[int(t) for t in bars.index.tolist()],
        rv_trailing=rv_trailing,
        rv_forward=rv_forward,
        iv30=_series_to_jsonable(iv),
        iv30_trd252=_series_to_jsonable(iv_trd252),
        rv_hf_trailing=_series_to_jsonable(rv_hf_trail),
        rv_hf_forward=_series_to_jsonable(rv_hf_fwd),
        vrp_forward=_series_to_jsonable(vrp_fwd),
        vrp_z=_series_to_jsonable(sig.vrp_z),
        iv_source=iv_source_label,
        confidence=_series_to_jsonable(sig.confidence) if sig.confidence is not None else None,
        vrp_z_scaled=_series_to_jsonable(sig.vrp_z_scaled) if sig.vrp_z_scaled is not None else None,
        floor_gated=[bool(v) for v in sig.floor_gated.tolist()] if sig.floor_gated is not None else None,
        explanation=explanation,
        coverage=coverage,
    )


def _iv_series_from_recorder(symbol: str, bars_index: pd.Index) -> list[dict]:
    """Read recorded IV snapshots in the bars window into ``iv_series`` shape.

    Prefers ``iv30_vix_style`` and falls back to ``iv30_parametric``; rows
    where both are None or that carry an ``error`` are skipped (no synthesis).
    Pulls ``variance_contribution_synthetic`` from ``iv_provenance`` and
    ``health_score`` directly off the row when present so downstream
    confidence gating and regime-feature weighting use real evidence. Rows
    from before the recorder persisted ``health_score`` (or rows where the
    health computation itself failed) lack the field; ``_parse_iv_series``
    takes the drop-health-factor branch (confidence = 1 − vcs) and
    surfaces the imputed-ness via ``health_imputed_now`` so the UI can
    flag the bar.

    Returns ``[]`` when the recorder has nothing in the window; the caller
    treats that as ``iv_source="absent"``.
    """
    if len(bars_index) == 0:
        return []
    rows = get_iv_store().read_series(
        symbol,
        start_ms=int(bars_index.min()),
        end_ms=int(bars_index.max()),
    )
    out: list[dict] = []
    for r in rows:
        if r.error is not None:
            continue
        iv = r.iv30_vix_style if r.iv30_vix_style is not None else r.iv30_parametric
        if iv is None:
            continue
        item: dict = {"ts": r.snapshot_ts_ms, "iv30": float(iv)}
        prov = r.iv_provenance or {}
        if "variance_contribution_synthetic" in prov:
            item["variance_contribution_synthetic"] = float(
                prov["variance_contribution_synthetic"]
            )
        if r.health_score is not None:
            item["health_score"] = float(r.health_score)
        out.append(item)
    return out


def _parse_iv_series_for_regime(
    iv_series: list[dict] | None, bars_index: pd.Index
) -> tuple[pd.Series | None, pd.Series | None]:
    """Parse iv_series for the regime route — returns (iv30, feature_weight).

    When the caller supplies ``health_score`` / ``variance_contribution_synthetic``
    alongside ``iv30``, this builds a per-bar feature weight via Step F's
    ``regime_feature_weight`` formula. When only iv30 is supplied,
    ``feature_weight`` is None (default behavior — full weight).
    """
    if not iv_series:
        return None, None

    iv_map = {int(p["ts"]): float(p["iv30"]) for p in iv_series}
    iv = pd.Series(iv_map).reindex(bars_index)

    has_health = any("health_score" in p for p in iv_series)
    has_vcs = any("variance_contribution_synthetic" in p for p in iv_series)
    if not (has_health or has_vcs):
        return iv, None

    # Imputed-prior policy: when health_score is missing (key absent OR
    # explicit null), the regime path emits feature_weight = 0 — "no
    # evidence on stability" maps to "this bar contributes no IV signal
    # to the regime classifier." The VRP path takes a different branch
    # for the same shape (drop the health factor and let (1 - vcs) carry
    # confidence) because confidence is a multiplier on a z-score, not a
    # feature weight on a regime input — see _parse_iv_series for the
    # asymmetry rationale.
    weight_map: dict[int, float] = {}
    for p in iv_series:
        h_raw = p.get("health_score")
        if "health_score" not in p or h_raw is None:
            weight_map[int(p["ts"])] = 0.0
            continue
        s_raw = p.get("variance_contribution_synthetic")
        s = 0.0 if s_raw is None else float(s_raw)
        weight_map[int(p["ts"])] = regime_feature_weight(
            health_score=float(h_raw), variance_contribution_synthetic=s
        )
    weight = pd.Series(weight_map).reindex(bars_index).fillna(0.0)
    return iv, weight


def _parse_iv_series(
    iv_series: list[dict] | None, bars_index: pd.Index
) -> tuple[pd.Series, pd.Series | None, pd.Series | None]:
    """Parse the optional iv_series payload.

    Returns ``(iv, confidence, health_imputed)``. When the caller supplies per-bar
    ``health_score`` and/or ``variance_contribution_synthetic`` alongside
    ``iv30``, the function builds a per-bar confidence series via
    ``confidence_with_explanation``. Otherwise ``confidence`` is ``None`` and
    the legacy ungated path runs.

    **Imputed-prior policy for missing ``health_score``.** When ``vcs`` is
    supplied but ``health_score`` is missing (key absent OR explicit null on
    the wire), the bar is processed as a **drop-the-health-factor** case:
    confidence collapses to ``(1 - vcs)`` rather than ``health * (1 - vcs)``.
    The previous policy multiplied by a synthetic ``0.5`` prior; we replaced
    that because a 0.5 multiplier is a real signal-attenuation choice (it
    halves every confidence with no evidence to support the cut) whereas
    "no evidence on stability, trust the data-quality side alone" is the
    honest answer when health truly is unknown. The imputed-ness is still
    surfaced via the returned ``health_imputed`` series so the UI can mark
    the bar visually rather than silently treat it as fully validated.

    See ``docs/architecture/iv-ownership-research.md`` Reviewer Feedback Log
    and ``docs/architecture/iv-research-chat-notes.md`` §5.3.
    """
    if not iv_series:
        return pd.Series(index=bars_index, dtype=float), None, None

    iv_map = {int(p["ts"]): float(p["iv30"]) for p in iv_series}
    iv = pd.Series(iv_map).reindex(bars_index)

    has_health = any("health_score" in p for p in iv_series)
    has_vcs = any("variance_contribution_synthetic" in p for p in iv_series)
    if not (has_health or has_vcs):
        return iv, None, None

    conf_map: dict[int, float] = {}
    imputed_map: dict[int, bool] = {}
    for p in iv_series:
        ts = int(p["ts"])
        # A bar is "imputed" when the key is missing OR the value is
        # explicitly None on the wire — both shapes mean "no evidence",
        # and both must surface to the UI as such.
        h_raw = p.get("health_score")
        is_imputed = "health_score" not in p or h_raw is None
        imputed_map[ts] = is_imputed
        s_raw = p.get("variance_contribution_synthetic")
        s = 0.0 if s_raw is None else float(s_raw)
        if is_imputed:
            # Drop the health factor entirely on missing-evidence bars;
            # confidence is carried by data-quality alone.
            conf_map[ts] = max(0.0, min(1.0, 1.0 - s))
        else:
            conf_map[ts] = confidence_with_explanation(
                health_score=float(h_raw),
                variance_contribution_synthetic=s,
            ).confidence
    confidence = pd.Series(conf_map).reindex(bars_index)
    health_imputed = pd.Series(imputed_map).reindex(bars_index)
    return iv, confidence, health_imputed


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
        [v if v is not None else np.nan for v in series.iv30],
        index=series.ts,
    )
    estimator_key = next(iter(series.rv_forward))
    rv_fwd = pd.Series(series.rv_forward[estimator_key], index=series.ts)
    rv_trailing_key = next(iter(series.rv_trailing))
    rv_trailing = pd.Series(series.rv_trailing[rv_trailing_key], index=series.ts)

    sig_oracle = vrp_signal(iv=iv.ffill(), rv=rv_fwd.ffill(), lookback=req.lookback, threshold=req.threshold)
    sig_real = vrp_signal(iv=iv.ffill(), rv=rv_trailing.ffill(), lookback=req.lookback, threshold=req.threshold)
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
    iv_series: list[dict] | None = Field(
        None,
        description=(
            "Optional [{ts, iv30, health_score?, variance_contribution_synthetic?}]. "
            "When supplied, IV-derived features (iv30_z, d_iv_z, iv_vol_z) are added "
            "to the regime feature matrix and weighted by regime_feature_weight (Step F)."
        ),
    )


@router.post("/regimes/cluster")
async def regimes_cluster(body: RegimeClusterBody) -> dict:
    bars = _bars_payload_to_df(body.bars)
    if len(bars) < 80:
        raise HTTPException(400, "need at least 80 bars to cluster regimes")

    # Same caller-wins-then-recorder fallback as realized-vs-iv. When neither
    # supplies iv_series, regime features fall back to OHLCV-only.
    if body.iv_series:
        iv_series_for_parse: list[dict] | None = body.iv_series
    else:
        recorded = _iv_series_from_recorder(body.symbol, bars.index)
        iv_series_for_parse = recorded if recorded else None
    iv30, weight = _parse_iv_series_for_regime(iv_series_for_parse, bars.index)
    if iv30 is not None:
        feats = build_full_features(bars, iv30=iv30, iv_feature_weight=weight).dropna()
    else:
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
            hmm.labels,
            posterior=hmm.posterior,
            p_min=body.p_min,
            min_run_length=body.min_run_length,
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
    signals = pd.Series({int(p["ts"]): int(p["side"]) for p in body.signals}).reindex(bars.index).fillna(0).astype(int)
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
        "equity_curve": []
        if res.equity_curve is None
        else res.equity_curve.assign(ts=res.equity_curve["ts"].astype(int)).to_dict(orient="records"),
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
        lambda y: float(np.polyfit(np.arange(len(y)), y, 1)[0]),
        raw=True,
    )
    labels = pd.Series(body.regime_labels, index=bars.index, dtype=int)

    score_map_int = {int(k): float(v) for k, v in body.regime_score_map.items()} if body.regime_score_map else None
    res = edge_score(
        vrp=vrp,
        iv30=iv,
        trend_slope=trend,
        atr=atr,
        regime_labels=labels,
        weights=body.weights or DEFAULT_WEIGHTS,
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
    return pd.DataFrame(
        {
            "open": [b.open for b in rows],
            "high": [b.high for b in rows],
            "low": [b.low for b in rows],
            "close": [b.close for b in rows],
            "volume": [b.volume for b in rows],
        },
        index=ts_ms,
    )


def _series_to_jsonable(s: pd.Series) -> list:
    return [None if (isinstance(v, float) and np.isnan(v)) else float(v) for v in s.tolist()]


def _compute_hf_rv_for_vrp(
    bars: pd.DataFrame,
    bar_size: str,
    session: Session,
) -> tuple[pd.Series, pd.Series]:
    """Return (trailing, forward) HF realized vol on TRD/252 basis.

    For 15-min bars, uses the two-component HF estimator from
    ``hf_realized_vol_trd252`` with the chosen session. For daily bars, falls
    back to a YZ-21 estimator (intraday detail not available).

    Both outputs are int64-ms-indexed to match ``bars``.
    """
    if bar_size == "15m":
        # HF requires tz-aware DatetimeIndex. Build a UTC-indexed copy here so
        # we don't mutate the caller's DataFrame.
        bars_utc = bars.copy()
        bars_utc.index = pd.to_datetime(bars.index, unit="ms", utc=True)
        trailing = hf_realized_vol_trd252(bars_utc, window_trading_days=21, session=session)
        forward = hf_forward_rv_trd252(bars_utc, window_trading_days=21, session=session)
        # Re-index back to int64 ms to match the rest of the pipeline.
        trailing.index = bars.index
        forward.index = bars.index
        return trailing, forward
    # Daily fallback: 21-day YZ trailing / forward. TRD/252 already.
    trailing = yang_zhang(bars, window=21, annualize=True, bars_per_year=DAILY_BARS_PER_YEAR)
    forward = forward_rv(
        bars, estimator="yz", window=21, annualize=True, bars_per_year=DAILY_BARS_PER_YEAR
    )
    return trailing, forward


def _convert_iv_to_trd252(iv_act365: pd.Series, tenor_days: int) -> pd.Series:
    """Per-timestamp basis conversion from ACT/365 to TRD/252.

    NaN inputs pass through unchanged. The dynamic NYSE-calendar factor is
    queried per timestamp via ``convert_iv_act365_to_trading252``.
    """
    out = pd.Series(index=iv_act365.index, dtype=float)
    for ts, sigma in iv_act365.items():
        if sigma is None or (isinstance(sigma, float) and np.isnan(sigma)):
            continue
        out.loc[ts] = convert_iv_act365_to_trading252(float(sigma), int(ts), tenor_days)
    return out
