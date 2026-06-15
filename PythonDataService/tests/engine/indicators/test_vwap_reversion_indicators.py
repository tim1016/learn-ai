"""Golden-style parity tests for the VWAP-reversion indicators (PRD-C / PR-K).

Reference oracle is an independent numpy computation of the same closed-form
definitions (session-anchored VWAP with (H+L+C)/3 typical price; population
std ddof=0 of the distance series). Tolerance: atol=1e-9, rtol=0 per
.claude/rules/numerical-rigor.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from app.engine.indicators.rolling_distance_sigma import RollingDistanceSigma
from app.engine.indicators.vwap import SessionAnchoredVwap


def _t(minute: int) -> datetime:
    return datetime(2024, 3, 4, 14, 30, tzinfo=UTC) + timedelta(minutes=minute)


def test_session_anchored_vwap_matches_numpy_cumulative() -> None:
    bars = [
        # (h, l, c, v)
        (100.5, 99.5, 100.0, 1000.0),
        (101.0, 100.0, 100.8, 2000.0),
        (100.2, 99.0, 99.4, 1500.0),
    ]
    vwap = SessionAnchoredVwap()
    got = []
    for i, (h, l, c, v) in enumerate(bars):
        vwap.update(_t(i), high=h, low=l, close=c, volume=v)
        got.append(vwap.current_value)

    typ = np.array([(h + l + c) / 3 for h, l, c, _ in bars])
    vol = np.array([v for *_, v in bars])
    expected = np.cumsum(typ * vol) / np.cumsum(vol)
    assert np.allclose(got, expected, atol=1e-9, rtol=0)


def test_rolling_distance_sigma_matches_numpy_population_std() -> None:
    dist = [0.5, -0.3, 0.1, -0.2, 0.4, -0.1]
    sigma = RollingDistanceSigma(lookback=3)
    got = []
    for d in dist:
        sigma.update(d)
        got.append(sigma.current_value if sigma.is_ready else None)

    # Population std (ddof=0) over each trailing window of 3.
    expected = [None, None]
    for i in range(2, len(dist)):
        expected.append(float(np.std(np.array(dist[i - 2 : i + 1]), ddof=0)))
    for g, e in zip(got, expected, strict=True):
        if e is None:
            assert g is None
        else:
            assert abs(g - e) < 1e-9
