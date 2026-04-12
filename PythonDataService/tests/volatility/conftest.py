"""
Shared fixtures for volatility tests.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm


@pytest.fixture
def spot() -> float:
    return 100.0


@pytest.fixture
def rate() -> float:
    return 0.05


@pytest.fixture
def base_vol() -> float:
    return 0.25


def bs_price(
    spot: float,
    strike: float,
    ttm: float,
    rate: float,
    vol: float,
    is_call: bool,
) -> float:
    """Reference Black-Scholes price for test data generation."""
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol ** 2) * ttm) / (
        vol * math.sqrt(ttm)
    )
    d2 = d1 - vol * math.sqrt(ttm)
    df = math.exp(-rate * ttm)
    if is_call:
        return spot * norm.cdf(d1) - strike * df * norm.cdf(d2)
    return strike * df * norm.cdf(-d2) - spot * norm.cdf(-d1)


@pytest.fixture
def flat_vol_chain(spot: float, rate: float, base_vol: float) -> list[dict]:
    """Option chain priced at a flat vol (no skew) — 3 expiries, 11 strikes."""
    ttms = [30 / 365, 90 / 365, 180 / 365]
    records: list[dict] = []

    for ttm in ttms:
        forward = spot * math.exp(rate * ttm)
        strikes = np.linspace(spot * 0.85, spot * 1.15, 11)

        for k in strikes:
            is_call = k >= forward
            price = bs_price(spot, k, ttm, rate, base_vol, is_call)
            if price < 0.01:
                continue
            records.append(
                {
                    "strike": float(k),
                    "ttm": ttm,
                    "option_price": round(price, 6),
                    "is_call": is_call,
                }
            )
    return records


@pytest.fixture
def skewed_chain(spot: float, rate: float) -> list[dict]:
    """Option chain with realistic skew for testing parametric fits."""
    ttms = [30 / 365, 60 / 365, 90 / 365, 180 / 365]
    records: list[dict] = []

    for ttm in ttms:
        forward = spot * math.exp(rate * ttm)
        strikes = np.linspace(spot * 0.80, spot * 1.20, 15)

        for k in strikes:
            log_m = math.log(k / forward)
            vol = 0.25 - 0.12 * log_m + 0.03 * log_m ** 2
            vol = max(vol, 0.05)

            is_call = k >= forward
            price = bs_price(spot, k, ttm, rate, vol, is_call)
            if price < 0.01:
                continue
            records.append(
                {
                    "strike": float(k),
                    "ttm": ttm,
                    "option_price": round(price, 6),
                    "is_call": is_call,
                }
            )
    return records
