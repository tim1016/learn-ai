"""Edge Score — single per-bar composite signal.

Per docs/architecture/edge-feature-design.md §8:

    E_t = w1 · S_VRP + w2 · S_Regime + w3 · S_IV + w4 · S_Trend

Sign convention: +1 = long-vol attractive, −1 = short-vol attractive.
Default weights ship FIXED (anti-overfit rail #1). Bayesian calibration
is opt-in (§8.1) and lives in a separate module when needed.

All component scores are bounded to [-1, +1] via tanh squashing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_WEIGHTS = {"vrp": 0.4, "regime": 0.3, "iv_pct": 0.2, "trend": 0.1}
DEFAULT_REGIME_SCORE_MAP = {
    0: 0.0,  # state 0 (e.g. trending-low-vol) — neutral default
    1: -0.5,  # state 1 (e.g. trending-high-vol) — short-vol favored
    2: 0.5,  # state 2 (e.g. choppy-high-vol)   — long-vol favored
}


@dataclass(frozen=True)
class EdgeScoreResult:
    score: pd.Series  # composite in [-1, +1]
    components: pd.DataFrame  # per-component scores (same index)
    action: pd.Series  # -1/0/+1 from threshold gating


def s_vrp(vrp: pd.Series, lookback: int = 252) -> pd.Series:
    """High VRP = options rich → short-vol → negative score."""
    mean = vrp.rolling(lookback, min_periods=lookback).mean()
    std = vrp.rolling(lookback, min_periods=lookback).std(ddof=1)
    z = (vrp - mean) / std.replace(0, np.nan)
    return -np.tanh(z)


def s_iv_percentile(iv30: pd.Series, lookback: int = 252) -> pd.Series:
    """High IV percentile = expensive → short-vol → negative score."""
    pct = iv30.rolling(lookback, min_periods=lookback).rank(pct=True)
    return -2.0 * (pct - 0.5)


def s_trend(trend_slope: pd.Series, atr: pd.Series) -> pd.Series:
    """Strong trend penalizes long-vol; output in [-1, 0].

    Operator-precedence note: `-np.tanh(x).clip(...)` parses as
    `-(np.tanh(x).clip(...))`. Since tanh of a non-negative input is
    non-negative, that path clips first to 0 and then negates to -0.
    The intended formula negates first, *then* clips to [-1, 0].
    """
    norm = trend_slope.abs() / atr.replace(0, np.nan)
    return (-np.tanh(norm)).clip(lower=-1.0, upper=0.0)


def s_regime(labels: pd.Series, score_map: dict[int, float] | None = None) -> pd.Series:
    """User-defined per-regime score map; defaults from DEFAULT_REGIME_SCORE_MAP."""
    score_map = score_map or DEFAULT_REGIME_SCORE_MAP
    return labels.map(score_map).fillna(0.0)


def edge_score(
    *,
    vrp: pd.Series,
    iv30: pd.Series,
    trend_slope: pd.Series,
    atr: pd.Series,
    regime_labels: pd.Series,
    weights: dict[str, float] | None = None,
    regime_score_map: dict[int, float] | None = None,
    long_threshold: float = 0.5,
    short_threshold: float = -0.5,
) -> EdgeScoreResult:
    """Compute the per-bar Edge Score and discrete action."""
    w = weights or DEFAULT_WEIGHTS
    # Probability weights — strict tolerance per .claude/rules/numerical-rigor.md
    if not np.isclose(sum(w.values()), 1.0, atol=1e-10, rtol=0):
        raise ValueError(f"weights must sum to 1.0, got {sum(w.values())}")

    components = pd.DataFrame(
        {
            "vrp": s_vrp(vrp),
            "iv_pct": s_iv_percentile(iv30),
            "trend": s_trend(trend_slope, atr),
            "regime": s_regime(regime_labels, regime_score_map),
        }
    )

    score = (
        w["vrp"] * components["vrp"].fillna(0)
        + w["iv_pct"] * components["iv_pct"].fillna(0)
        + w["trend"] * components["trend"].fillna(0)
        + w["regime"] * components["regime"].fillna(0)
    )

    action = pd.Series(0, index=score.index, dtype=int)
    action[score > long_threshold] = 1
    action[score < short_threshold] = -1

    return EdgeScoreResult(score=score, components=components, action=action)
