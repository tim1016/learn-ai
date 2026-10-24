"""The Realized vs IV series: trailing and forward realized vol, aligned IV, and the VRP signal over one set of bars.

Formula: per bar, trailing and forward RV for each estimator and window (annualized by sqrt(252 * bars per session * the
  per-bar variance)), the HF two-component RV over 21 trading days, IV30 converted ACT/365 to TRD/252 (NYSE session count of the tenor), and
  VRP_forward = IV30_TRD252² − RV_HF_forward² with its rolling z-score (``vrp_signal``) and, when per-bar confidence
  is supplied, the confidence-scaled z-score and hard floor.
Reference: Bollerslev, Tauchen, Zhou (2009) "Expected Stock Returns and Variance Risk Premia" RFS 22(11) for VRP;
  the estimators, the HF two-component RV, the basis conversion and the VRP signal each carry their own reference in
  ``realized_vol.py``, ``hf_realized_vol.py``, ``app/volatility/basis.py`` and ``vrp.py``. This file composes them
  and adds no estimator of its own.
Canonical implementation: this file for the composition; the pieces live in the modules named above. Until #3525 the
  composition lived in ``app/routers/edge.py`` (``POST /api/edge/realized-vs-iv/series``) and was moved here with one
  change to a step: the VRP z-score no longer forward-fills IV (below). With IV on every bar the same bars and IV give
  the same series as before.
Validated against: tests/research/test_realized_vs_iv_study.py (the composition against the estimators called
  directly, on lake bars and on the vendor's own bars, at atol 1e-9, rtol 0) and tests/edge/test_realized_vs_iv_series.py
  (the annualization follows the session the bars were folded from). The pieces keep their own tests.

Annualization. A year is 252 sessions of ``bars_per_session`` bars. The caller says how many bars a full session of the
session it folded holds (``app.engine.edge.session_bars``: 26 for regular hours and 64 for extended hours at 15 minutes,
counted from the calendar; 1 for daily bars). It used to be 26 whatever the session, which understated every trailing
Yang-Zhang line of an extended-hours run (the default) by sqrt(26 / 64), about 0.64 times (#3525).

IV in. ``iv_points`` are ``{ts, iv30, health_score?, variance_contribution_synthetic?}`` stamped at bar starts
(``app.engine.edge.iv_alignment`` puts them there). A bar with no point has no IV, and IV30, ``vrp_forward`` and the
VRP z-score are NaN there. Missing IV stays absent everywhere (PRD #3490 R25; ADR 0071 decision 9): nothing is filled,
carried or interpolated, the z-score's input included. The z-score is a rolling statistic over ``coverage.vrp_lookback``
bars that needs a VRP value on every one of them, so it exists only on a bar whose whole window has recorded IV and a
realized forward volatility. IV on a few bars gives an IV30 on those bars and a z-score on none; the z-score once
forward-filled IV, which put a dense z-score and long/short-vol signals on bars with no IV (#3525 review M1).

Warmup. Trailing Yang-Zhang over a window of ``w`` bars is NaN for the first ``w`` bars (it needs ``w`` overnight
returns and the first bar has none), so it is valid from bar index ``w``. The YZ-21 fallback on daily bars is valid from
bar index 21 for the same reason; the HF trailing vol on 15-minute bars from the first bar of the 21st session. No
warmup is fetched before the window.

Forward tail. The forward realized vol of the last bars is not realized yet. It is NaN, never filled, and so is
every VRP value built on it. ``forward_nan_bars`` counts them.

The least that can show a z-score. The z-score needs ``VRP_LOOKBACK_BARS`` bars that each have a forward value, and the
last ``FORWARD_TRADING_DAYS`` sessions have none, so a daily window needs 252 + 21 = 273 bars (the YZ-21 forward value is
valid on bar ``n - 22`` and before). The study sizes its default window from this (``resolve_window``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.engine.edge.confidence import confidence_with_explanation
from app.engine.edge.features_realtime.hf_realized_vol import Session, hf_realized_vol_trd252
from app.engine.edge.features_realtime.realized_vol import (
    DAILY_BARS_PER_YEAR,
    close_to_close,
    garman_klass,
    parkinson,
    yang_zhang,
)
from app.engine.edge.labels_oracle.forward_rv import forward_rv
from app.engine.edge.labels_oracle.hf_forward_rv import hf_forward_rv_trd252
from app.engine.edge.threshold_events import log_confidence_floor_fired, log_imputed_prior_emitted
from app.engine.edge.vrp import compute_vrp, vrp_signal
from app.volatility.basis import convert_iv_act365_to_trading252

IvSource = Literal["caller_supplied", "recorder", "absent"]

#: Bar sizes the series is defined for.
BAR_SIZES = ("1d", "15m")

#: Trading days the VRP's forward realized vol looks ahead (the HF forward RV on 15-minute bars, YZ-21 on daily bars). The
#: forward value of the last this-many sessions is not realized yet and stays NaN.
FORWARD_TRADING_DAYS = 21

#: Bars the VRP z-score rolls over, at most: a trading year of daily bars.
VRP_LOOKBACK_BARS = 252

ESTIMATOR_FNS = {
    "ctc": close_to_close,
    "parkinson": parkinson,
    "gk": garman_klass,
    "yz": yang_zhang,
}


def compute_realized_vs_iv_series(
    bars: pd.DataFrame,
    *,
    symbol: str,
    bar_size: str,
    tenor_days: int,
    hf_session: Session | None,
    estimators: Sequence[str],
    windows: Sequence[int],
    iv_points: list[dict] | None,
    iv_source: IvSource,
    confidence_floor: float,
    bars_per_session: int,
) -> dict[str, Any]:
    """The series for ``bars`` (index: ``int64 ms UTC``, columns open/high/low/close/volume), as a JSON-able dict.

    ``bars_per_session`` is the bars a full session of the session the bars were folded from holds (1 for daily bars),
    which a year of them is counted in.
    """
    if bar_size not in BAR_SIZES:
        raise ValueError(f"bar_size must be one of {sorted(BAR_SIZES)}, got {bar_size!r}")
    if bar_size == "15m" and hf_session is None:
        raise ValueError("15m bars need the HF estimator's session, ETH or RTH")
    if bars_per_session < 1 or (bar_size == "1d" and bars_per_session != 1):
        raise ValueError(f"{bar_size} bars hold {1 if bar_size == '1d' else 'at least 1'} bar a session, got {bars_per_session}")
    if len(bars) < 2:
        raise ValueError(f"the series needs at least two bars, got {len(bars)}")
    bpy = DAILY_BARS_PER_YEAR * bars_per_session

    rv_trailing: dict[str, list] = {}
    rv_forward: dict[str, list] = {}
    for est in estimators:
        if est not in ESTIMATOR_FNS:
            raise ValueError(f"unknown estimator: {est}")
        for w in windows:
            key = f"{est}_{w}"
            trailing = ESTIMATOR_FNS[est](bars, window=w, annualize=True, bars_per_year=bpy)
            fwd = forward_rv(bars, estimator=est, window=w, annualize=True, bars_per_year=bpy)
            rv_trailing[key] = _series_to_jsonable(trailing)
            rv_forward[key] = _series_to_jsonable(fwd)

    iv, confidence, health_imputed = _parse_iv_series(iv_points, bars.index)

    # HF RV (TRD/252) for VRP — for 15-min bars uses two-component HF; for daily
    # bars falls back to YZ at 21-day window (no intraday detail to exploit).
    rv_hf_trail, rv_hf_fwd = _compute_hf_rv_for_vrp(bars, bar_size, hf_session)

    # Basis-convert IV from ACT/365 (solver-native) to TRD/252 before VRP.
    iv_trd252 = _convert_iv_to_trd252(iv, tenor_days)

    vrp_fwd = compute_vrp(iv_trd252.fillna(np.nan), rv_hf_fwd)
    # Neither input is filled. The forward RV of the last window's bars is not
    # realized yet, so a z-score of an unrealized tail would read as a signal;
    # and a bar with no IV has no VRP, so a window holding one has no z-score.
    # ``vrp_z`` is NaN wherever either is, and the page shows "—".
    lookback = min(VRP_LOOKBACK_BARS, len(bars) - 1)
    sig = vrp_signal(
        iv=iv_trd252,
        rv=rv_hf_fwd,
        lookback=lookback,
        confidence=confidence,
        confidence_floor=confidence_floor,
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
        gated_now = bool(latest_h < confidence_floor)
        explanation = {
            "latest_confidence": latest_h,
            "floor": confidence_floor,
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
                ticker=symbol,
                snapshot_ts_ms=int(latest_idx) if pd.notna(latest_idx) else None,
                confidence=latest_h,
                floor=confidence_floor,
            )
        if health_imputed_now:
            log_imputed_prior_emitted(
                ticker=symbol,
                snapshot_ts_ms=int(latest_idx) if pd.notna(latest_idx) else None,
                shape="missing_or_null",
            )

    coverage = {
        "n_bars": len(bars),
        "iv_first_ts": int(iv.dropna().index.min()) if iv.notna().any() else None,
        "iv_last_ts": int(iv.dropna().index.max()) if iv.notna().any() else None,
        "forward_nan_bars": int(rv_hf_fwd.isna().sum()),
        "vrp_lookback": lookback,
        "session": hf_session,
        "vrp_basis": "TRD/252 (IV converted from ACT/365 via NYSE calendar)",
        "has_confidence": confidence is not None,
    }
    return {
        "symbol": symbol,
        "ts": [int(t) for t in bars.index.tolist()],
        "rv_trailing": rv_trailing,
        "rv_forward": rv_forward,
        "iv30": _series_to_jsonable(iv),
        "iv30_trd252": _series_to_jsonable(iv_trd252),
        "rv_hf_trailing": _series_to_jsonable(rv_hf_trail),
        "rv_hf_forward": _series_to_jsonable(rv_hf_fwd),
        "vrp_forward": _series_to_jsonable(vrp_fwd),
        "vrp_z": _series_to_jsonable(sig.vrp_z),
        "iv_source": iv_source,
        "confidence": _series_to_jsonable(sig.confidence) if sig.confidence is not None else None,
        "vrp_z_scaled": _series_to_jsonable(sig.vrp_z_scaled) if sig.vrp_z_scaled is not None else None,
        "floor_gated": [bool(v) for v in sig.floor_gated.tolist()] if sig.floor_gated is not None else None,
        "explanation": explanation,
        "coverage": coverage,
    }


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

    See ADR 0071 decision 11.
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


def _series_to_jsonable(s: pd.Series) -> list:
    return [None if (isinstance(v, float) and np.isnan(v)) else float(v) for v in s.tolist()]


def _compute_hf_rv_for_vrp(
    bars: pd.DataFrame,
    bar_size: str,
    session: Session | None,
) -> tuple[pd.Series, pd.Series]:
    """Return (trailing, forward) HF realized vol on TRD/252 basis.

    For 15-min bars, uses the two-component HF estimator from
    ``hf_realized_vol_trd252`` with the chosen session. For daily bars, falls
    back to a YZ-21 estimator (intraday detail not available) and the session
    does not apply: ``None``.

    Both outputs are int64-ms-indexed to match ``bars``.
    """
    if bar_size == "15m":
        if session is None:
            raise ValueError("15m bars need the HF estimator's session, ETH or RTH")
        # HF requires tz-aware DatetimeIndex. Build a UTC-indexed copy here so
        # we don't mutate the caller's DataFrame.
        bars_utc = bars.copy()
        bars_utc.index = pd.to_datetime(bars.index, unit="ms", utc=True)
        trailing = hf_realized_vol_trd252(bars_utc, window_trading_days=FORWARD_TRADING_DAYS, session=session)
        forward = hf_forward_rv_trd252(bars_utc, window_trading_days=FORWARD_TRADING_DAYS, session=session)
        # Re-index back to int64 ms to match the rest of the pipeline.
        trailing.index = bars.index
        forward.index = bars.index
        return trailing, forward
    # Daily fallback: 21-day YZ trailing / forward. TRD/252 already.
    trailing = yang_zhang(bars, window=FORWARD_TRADING_DAYS, annualize=True, bars_per_year=DAILY_BARS_PER_YEAR)
    forward = forward_rv(
        bars, estimator="yz", window=FORWARD_TRADING_DAYS, annualize=True, bars_per_year=DAILY_BARS_PER_YEAR
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
