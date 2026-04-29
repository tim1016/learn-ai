"""IV30 stability / health score (Step 6 of IV-RV alignment).

The regime-feature use case (Q3 = b in the locked plan) demands that IV30 be
**stable under perturbation**: small chain changes must not produce large IV30
jumps, otherwise the regime classifier chases noise. We surface a per-build
``iv30_health_score ∈ [0, 1]`` so downstream features can degrade gracefully
when the score drops below 0.5.

Components — each returns a [0, 1] sub-score, then averaged with weights:

1. **resampling**: drop 5% random strikes, observe |ΔIV30|. Score = exp(-|ΔIV30|/10bps).
2. **strike_grid**: half the strike resolution (drop alternates), observe |ΔIV30|.
   Score = exp(-|ΔIV30|/20bps).
3. **arb_consistency**: parametric IV30 vs VIX-replication IV30, ratio score.

The stability tests in ``tests/edge/test_iv30_stability.py`` lock the
component thresholds; this module is just the scoring helper.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from app.volatility.price_normalization import NormalizedOptionQuote
from app.volatility.vix_replication import (
    OptionQuote,
    vix_style_iv30,
    vix_style_iv30_with_provenance,
)


@dataclass(frozen=True)
class Iv30HealthBreakdown:
    score: float
    resampling_score: float
    strike_grid_score: float
    parametric_vs_replication_score: float | None
    delta_resampling_bps: float
    delta_strike_grid_bps: float


def _drop_random(quotes: list[OptionQuote], frac: float, seed: int) -> list[OptionQuote]:
    rng = random.Random(seed)
    keep = [q for q in quotes if rng.random() > frac]
    return keep if len(keep) >= 5 else quotes  # never strip below the floor


def _drop_alternates(quotes: list[OptionQuote]) -> list[OptionQuote]:
    sorted_q = sorted(quotes, key=lambda q: q.strike)
    return sorted_q[::2]


def compute_iv30_health(
    expiry1_quotes: list[OptionQuote],
    expiry2_quotes: list[OptionQuote],
    *,
    rate1: float,
    T1_calendar_days: int,
    rate2: float,
    T2_calendar_days: int,
    parametric_iv30: float | None = None,
    seed: int = 11,
) -> Iv30HealthBreakdown:
    """Per-build health score for an IV30 estimate.

    Returns the composite score plus the component sub-scores and the
    raw deltas in basis points so a UI can show provenance.
    """
    baseline = vix_style_iv30(
        expiry1_quotes, expiry2_quotes,
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
    )

    # 1. Resampling robustness
    resampled = vix_style_iv30(
        _drop_random(expiry1_quotes, 0.05, seed),
        _drop_random(expiry2_quotes, 0.05, seed + 1),
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
    )
    delta_resample_bps = abs(resampled - baseline) * 10000
    resampling_score = math.exp(-delta_resample_bps / 10.0)  # 10 bps half-life

    # 2. Strike-grid robustness
    grid_iv = vix_style_iv30(
        _drop_alternates(expiry1_quotes),
        _drop_alternates(expiry2_quotes),
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
    )
    delta_grid_bps = abs(grid_iv - baseline) * 10000
    strike_grid_score = math.exp(-delta_grid_bps / 20.0)  # 20 bps half-life

    # 3. Parametric vs replication (optional — only when caller supplies parametric IV30)
    parametric_score: float | None = None
    if parametric_iv30 is not None:
        diff_bps = abs(parametric_iv30 - baseline) * 10000
        parametric_score = math.exp(-diff_bps / 50.0)  # 50 bps half-life

    parts = [resampling_score, strike_grid_score]
    if parametric_score is not None:
        parts.append(parametric_score)
    composite = sum(parts) / len(parts)

    return Iv30HealthBreakdown(
        score=composite,
        resampling_score=resampling_score,
        strike_grid_score=strike_grid_score,
        parametric_vs_replication_score=parametric_score,
        delta_resampling_bps=delta_resample_bps,
        delta_strike_grid_bps=delta_grid_bps,
    )


def _drop_random_normalized(
    quotes: list[NormalizedOptionQuote], frac: float, seed: int
) -> list[NormalizedOptionQuote]:
    rng = random.Random(seed)
    keep = [q for q in quotes if rng.random() > frac]
    return keep if len(keep) >= 5 else quotes


def _drop_alternates_normalized(
    quotes: list[NormalizedOptionQuote],
) -> list[NormalizedOptionQuote]:
    sorted_q = sorted(quotes, key=lambda q: q.strike)
    return sorted_q[::2]


def _vix_iv30_normalized(
    expiry1_quotes: list[NormalizedOptionQuote],
    expiry2_quotes: list[NormalizedOptionQuote],
    *,
    rate1: float,
    T1_calendar_days: int,
    rate2: float,
    T2_calendar_days: int,
    target_calendar_days: int = 30,
) -> float:
    """Strip provenance and return only σ. The provenance-aware path is
    the canonical math; we discard the IvProvenance here because the
    health score is a stability test, not a provenance assertion."""
    sigma, _ = vix_style_iv30_with_provenance(
        expiry1_quotes, expiry2_quotes,
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
        target_calendar_days=target_calendar_days,
    )
    return sigma


def compute_iv30_health_normalized(
    expiry1_quotes: list[NormalizedOptionQuote],
    expiry2_quotes: list[NormalizedOptionQuote],
    *,
    rate1: float,
    T1_calendar_days: int,
    rate2: float,
    T2_calendar_days: int,
    target_calendar_days: int = 30,
    parametric_iv30: float | None = None,
    seed: int = 11,
) -> Iv30HealthBreakdown:
    """``NormalizedOptionQuote`` variant of :func:`compute_iv30_health`.

    Same math, same exponential decay constants (10 bps / 20 bps / 50 bps
    half-lives), same composite-mean averaging. Exists because the recorder
    operates exclusively on ``NormalizedOptionQuote`` and round-tripping
    through ``OptionQuote`` would discard the price-source provenance the
    rest of the recorder pipeline depends on. A parity test pins the two
    variants to the same number on a clean OPRA chain.

    ``target_calendar_days`` selects the constant-maturity horizon scored —
    defaults to 30 for parity with the legacy IV30-only path, but the
    recorder threads its caller-requested tenor through so the persisted
    score reflects the IV that was actually computed (recorder allows
    1..180 day requests; without this, non-default runs raised
    "target X not bracketed" inside the wrapped vix_style call and the
    health computation silently fell back to ``health_score=None``).
    """
    baseline = _vix_iv30_normalized(
        expiry1_quotes, expiry2_quotes,
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
        target_calendar_days=target_calendar_days,
    )

    resampled = _vix_iv30_normalized(
        _drop_random_normalized(expiry1_quotes, 0.05, seed),
        _drop_random_normalized(expiry2_quotes, 0.05, seed + 1),
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
        target_calendar_days=target_calendar_days,
    )
    delta_resample_bps = abs(resampled - baseline) * 10000
    resampling_score = math.exp(-delta_resample_bps / 10.0)

    grid_iv = _vix_iv30_normalized(
        _drop_alternates_normalized(expiry1_quotes),
        _drop_alternates_normalized(expiry2_quotes),
        rate1=rate1, T1_calendar_days=T1_calendar_days,
        rate2=rate2, T2_calendar_days=T2_calendar_days,
        target_calendar_days=target_calendar_days,
    )
    delta_grid_bps = abs(grid_iv - baseline) * 10000
    strike_grid_score = math.exp(-delta_grid_bps / 20.0)

    parametric_score: float | None = None
    if parametric_iv30 is not None:
        diff_bps = abs(parametric_iv30 - baseline) * 10000
        parametric_score = math.exp(-diff_bps / 50.0)

    parts = [resampling_score, strike_grid_score]
    if parametric_score is not None:
        parts.append(parametric_score)
    composite = sum(parts) / len(parts)

    return Iv30HealthBreakdown(
        score=composite,
        resampling_score=resampling_score,
        strike_grid_score=strike_grid_score,
        parametric_vs_replication_score=parametric_score,
        delta_resampling_bps=delta_resample_bps,
        delta_strike_grid_bps=delta_grid_bps,
    )
