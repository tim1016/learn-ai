"""Variance Risk Premium series, signals, and z-scores.

Formula: VRP_forward[t] = IV30[t]² - RV_forward[t]²; VRP_realtime[t] = IV30[t]² - RV_trailing[t]²
Reference: Bollerslev, T., Tauchen, G., Zhou, H. (2009) "Expected Stock Returns and Variance Risk Premia" RFS 22(11)
Canonical implementation: app/engine/edge/vrp.py
Validated against: NONE — pending

Definitions:
    VRP_forward[t]     = IV30[t]² - RV_forward[t]²      (ex-post; oracle-only)
    VRP_realtime[t]    = IV30[t]² - RV_trailing[t]²     (realistic proxy)

Sign: positive = options over-priced expected RV (short-vol favored).
      negative = options under-priced (long-vol favored).

Step E of the IV-ownership plan adds optional **continuous confidence
gating**: when callers can supply per-bar ``confidence`` (derived from
``compute_iv30_health`` × ``(1 - variance_contribution_synthetic)`` —
see ``app.engine.edge.confidence``), ``vrp_signal`` scales the z-score
by confidence rather than thresholding on a binary cutoff. A hard-gate
floor still fires at ``confidence < floor`` to suppress action when the
chain is degenerate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.engine.edge.confidence import DEFAULT_CONFIDENCE_FLOOR


@dataclass(frozen=True)
class VrpSignal:
    side: pd.Series  # +1 long-vol, -1 short-vol, 0 flat
    vrp: pd.Series  # raw VRP (variance units)
    vrp_z: pd.Series  # z-score over rolling lookback (raw, before confidence scaling)
    vrp_z_scaled: pd.Series | None = None  # z-score × confidence; None when no confidence supplied
    confidence: pd.Series | None = None  # per-bar confidence ∈ [0, 1]; None when ungated
    floor_gated: pd.Series | None = None  # bool series — True where the hard floor fired


def compute_vrp(iv: pd.Series, rv: pd.Series) -> pd.Series:
    """VRP in variance units (σ_IV² − σ_RV²).

    **Both inputs must be in the same annualization basis.** The repo
    convention is **TRD/252**. Realized vol from ``realized_vol.py`` is
    already TRD/252 when ``annualize=True``. Implied vol coming out of
    ``solver.py`` is **ACT/365** by default — convert with
    :func:`app.volatility.basis.convert_iv_act365_to_trading252` (or use
    :func:`iv30_atm_50d_trading_basis`) before passing here.

    Mixing bases silently biases VRP by ~0.7% in normal weeks and up to
    ~5% in dense-holiday windows. See
    ``docs/references/iv-rv-basis-alignment.md`` for the math.
    """
    return iv**2 - rv**2


def vrp_signal(
    *,
    iv: pd.Series,
    rv: pd.Series,
    lookback: int = 252,
    threshold: float = 1.0,
    confidence: pd.Series | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> VrpSignal:
    """Per-bar trade-side signal driven by VRP z-score.

    Rule (when ``confidence`` is None — backward-compatible):
        vrp_z < -threshold → long-vol  (+1)
        vrp_z > +threshold → short-vol (-1)
        otherwise          → flat       (0)

    Rule (when ``confidence`` is supplied — Step E):
        z_scaled = vrp_z * confidence
        action  = sign(z_scaled) if abs(z_scaled) > threshold else 0
        action  = 0 (forced) where confidence < confidence_floor

    The continuous form lets a healthy-but-borderline z-score still fire
    while attenuating signals built on synthesis-heavy or unstable
    chains. The hard floor is the kill-switch for genuinely degenerate
    chains.
    """
    vrp = compute_vrp(iv, rv)
    mean = vrp.rolling(lookback, min_periods=lookback).mean()
    std = vrp.rolling(lookback, min_periods=lookback).std(ddof=1)
    z = (vrp - mean) / std.replace(0, np.nan)

    if confidence is None:
        side = pd.Series(0, index=vrp.index, dtype=int)
        side[z > threshold] = -1
        side[z < -threshold] = 1
        return VrpSignal(side=side, vrp=vrp, vrp_z=z)

    # Continuous confidence-based gating.
    conf = confidence.reindex(vrp.index).clip(lower=0.0, upper=1.0)
    z_scaled = z * conf
    side = pd.Series(0, index=vrp.index, dtype=int)
    side[z_scaled > threshold] = -1
    side[z_scaled < -threshold] = 1
    floor_gated = conf < confidence_floor
    side = side.where(~floor_gated, 0)
    return VrpSignal(
        side=side,
        vrp=vrp,
        vrp_z=z,
        vrp_z_scaled=z_scaled,
        confidence=conf,
        floor_gated=floor_gated,
    )
