"""Bid-ask spread models for the Edge trade simulator.

Formula: effective_spread = fixed_component + impact_component * sqrt(volume / ADV)
Reference: Madhavan, A. & Smidt, S. (1991) "A Bayesian Model of Intraday Specialist Pricing" Journal of Financial Economics 30(1)
Canonical implementation: app/engine/edge/spread_model.py
Validated against: NONE — pending

Options spread follows the Madhavan-Smidt (1991) liquidity framework:
spread reflects market-maker inventory risk and asymmetric-information cost.

Math provenance:
- Madhavan, A. & Smidt, S. (1991), "A Bayesian Model of Intraday Specialist
  Pricing", Journal of Financial Economics 30(1).
- Operational form per docs/architecture/edge-feature-design.md §7.2.
"""

from __future__ import annotations

import numpy as np

OPTION_SPREAD_FLOOR = 0.05  # dollars per share; options never trade tighter
DEFAULT_K = 0.04  # vol-time scaling coefficient
DEFAULT_ALPHA = 1.5  # moneyness penalty steepness
STOCK_DEFAULT_BPS = 1.0  # stock spread fraction in basis points


def option_spread(
    *,
    underlying_price: float | np.ndarray,
    strike: float | np.ndarray,
    time_to_expiry_years: float | np.ndarray,
    iv: float | np.ndarray,
    delta: float | np.ndarray,
    k: float = DEFAULT_K,
    alpha: float = DEFAULT_ALPHA,
    floor: float = OPTION_SPREAD_FLOOR,
) -> float | np.ndarray:
    """Modeled bid-ask spread (dollars per contract / per share) for a single option.

    spread = max(floor, k * iv * sqrt(T) * (1 + alpha * |delta - 0.5|) * S)

    All inputs are scalar or broadcastable to the same shape.
    `time_to_expiry_years` must be > 0.
    """
    if np.any(np.asarray(time_to_expiry_years) <= 0):
        raise ValueError("time_to_expiry_years must be > 0")
    sqrt_t = np.sqrt(time_to_expiry_years)
    moneyness_penalty = 1.0 + alpha * np.abs(np.asarray(delta) - 0.5)
    raw = k * iv * sqrt_t * moneyness_penalty * underlying_price
    return np.maximum(floor, raw)


def stock_spread(price: float | np.ndarray, bps: float = STOCK_DEFAULT_BPS) -> float | np.ndarray:
    """Stock bid-ask spread in dollars per share, expressed as fraction of price.

    Default 1 basis point (0.01 %) reflects S&P 500 ETF liquidity (SPY/QQQ/IWM/DIA).
    """
    if np.any(np.asarray(price) <= 0):
        raise ValueError("price must be > 0")
    return price * (bps / 10_000.0)


def is_tradable(
    *,
    spread: float,
    mid: float,
    quoted_volume: float,
    open_interest: float,
    spread_max_pct_of_mid: float = 0.25,
    min_volume: float = 50.0,
    min_open_interest: float = 100.0,
) -> bool:
    """Return True if a quoted instrument passes the liquidity floor.

    Floor (any failure → False):
    - spread / mid <= spread_max_pct_of_mid
    - quoted_volume >= min_volume
    - open_interest >= min_open_interest
    """
    if mid <= 0:
        return False
    if spread / mid > spread_max_pct_of_mid:
        return False
    if quoted_volume < min_volume:
        return False
    return open_interest >= min_open_interest
