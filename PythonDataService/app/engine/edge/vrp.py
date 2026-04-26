"""Variance Risk Premium series, signals, and z-scores.

Definitions:
    VRP_forward[t]     = IV30[t]² - RV_forward[t]²      (ex-post; oracle-only)
    VRP_realtime[t]    = IV30[t]² - RV_trailing[t]²     (realistic proxy)

Sign: positive = options over-priced expected RV (short-vol favored).
      negative = options under-priced (long-vol favored).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VrpSignal:
    side: pd.Series  # +1 long-vol, -1 short-vol, 0 flat
    vrp: pd.Series  # raw VRP (variance units)
    vrp_z: pd.Series  # z-score over rolling lookback


def compute_vrp(iv: pd.Series, rv: pd.Series) -> pd.Series:
    """VRP in variance units (σ_IV² − σ_RV²).

    Both inputs must already be annualized vols (not variances).
    """
    return iv**2 - rv**2


def vrp_signal(
    *,
    iv: pd.Series,
    rv: pd.Series,
    lookback: int = 252,
    threshold: float = 1.0,
) -> VrpSignal:
    """Per-bar trade-side signal driven by VRP z-score.

    Rule:
        vrp_z < -threshold → long-vol  (+1)
        vrp_z > +threshold → short-vol (-1)
        otherwise          → flat       (0)
    """
    vrp = compute_vrp(iv, rv)
    mean = vrp.rolling(lookback, min_periods=lookback).mean()
    std = vrp.rolling(lookback, min_periods=lookback).std(ddof=1)
    z = (vrp - mean) / std.replace(0, np.nan)
    side = pd.Series(0, index=vrp.index, dtype=int)
    side[z > threshold] = -1
    side[z < -threshold] = 1
    return VrpSignal(side=side, vrp=vrp, vrp_z=z)
