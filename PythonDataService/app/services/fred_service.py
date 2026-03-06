"""FRED Treasury rate service for dynamic risk-free rate interpolation.

Fetches daily Treasury bill/bond rates from FRED and interpolates
to arbitrary DTE for use in Black-Scholes pricing.

Tenors: DTB4WK (4-week), DTB3 (3-month), DTB6 (6-month), DTB1YR (1-year)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED series IDs → approximate days to maturity
TENOR_MAP: dict[str, int] = {
    "DTB4WK": 28,
    "DTB3": 91,
    "DTB6": 182,
    "DTB1YR": 365,
}

FALLBACK_RATE = 0.043

# Cache: {observation_date_str: {dte_days: rate}}
_rate_cache: dict[str, dict[int, float]] = {}
_cache_timestamp: float = 0.0
_CACHE_TTL_SECONDS = 86400  # 24 hours


def _is_cache_valid() -> bool:
    return time.time() - _cache_timestamp < _CACHE_TTL_SECONDS and len(_rate_cache) > 0


def _fetch_series(series_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Fetch observations for a single FRED series."""
    api_key = getattr(settings, "FRED_API_KEY", None)
    if not api_key:
        return []

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "desc",
        "limit": 10,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(FRED_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("observations", [])
    except (httpx.HTTPError, Exception) as e:
        logger.warning("[FRED] Failed to fetch %s: %s", series_id, e)
        return []


def _parse_latest_rate(observations: list[dict[str, Any]]) -> float | None:
    """Extract the most recent non-missing rate from FRED observations."""
    for obs in observations:
        val = obs.get("value", ".")
        if val != ".":
            try:
                return float(val) / 100.0  # FRED returns percentage
            except ValueError:
                continue
    return None


def _fetch_all_tenors(observation_date: str) -> dict[int, float]:
    """Fetch all tenor rates for a given date, returning {days: rate}."""
    end_date = observation_date
    # Look back 7 days to handle weekends/holidays
    start_dt = datetime.strptime(observation_date, "%Y-%m-%d") - timedelta(days=7)
    start_date = start_dt.strftime("%Y-%m-%d")

    rates: dict[int, float] = {}
    for series_id, days in TENOR_MAP.items():
        observations = _fetch_series(series_id, start_date, end_date)
        rate = _parse_latest_rate(observations)
        if rate is not None:
            rates[days] = rate

    return rates


def _interpolate_rate(rates: dict[int, float], dte_days: int) -> float:
    """Linear interpolation between adjacent Treasury tenors."""
    if not rates:
        return FALLBACK_RATE

    tenors = sorted(rates.keys())

    # Exact match
    if dte_days in rates:
        return rates[dte_days]

    # Below shortest tenor — use shortest
    if dte_days <= tenors[0]:
        return rates[tenors[0]]

    # Above longest tenor — use longest
    if dte_days >= tenors[-1]:
        return rates[tenors[-1]]

    # Find bracketing tenors and interpolate
    for i in range(len(tenors) - 1):
        if tenors[i] <= dte_days <= tenors[i + 1]:
            t_low, t_high = tenors[i], tenors[i + 1]
            r_low, r_high = rates[t_low], rates[t_high]
            weight = (dte_days - t_low) / (t_high - t_low)
            return r_low + weight * (r_high - r_low)

    return FALLBACK_RATE


def get_risk_free_rate(dte_days: int = 30, observation_date: str | None = None) -> float:
    """Get interpolated risk-free rate for a given DTE.

    Args:
        dte_days: Days to expiration for the option.
        observation_date: Date string (YYYY-MM-DD) for which to fetch rates.
                         Defaults to today.

    Returns:
        Annualized risk-free rate as a decimal (e.g. 0.043 for 4.3%).
        Falls back to FALLBACK_RATE on any error.
    """
    global _rate_cache, _cache_timestamp

    if observation_date is None:
        observation_date = datetime.now().strftime("%Y-%m-%d")

    # Check cache
    if _is_cache_valid() and observation_date in _rate_cache:
        cached_rates = _rate_cache[observation_date]
        return _interpolate_rate(cached_rates, dte_days)

    # Fetch fresh rates
    rates = _fetch_all_tenors(observation_date)

    if rates:
        _rate_cache[observation_date] = rates
        _cache_timestamp = time.time()
        result = _interpolate_rate(rates, dte_days)
        logger.info(
            "[FRED] Fetched rates for %s: %s → r(%dd) = %.4f",
            observation_date,
            {k: f"{v:.4f}" for k, v in sorted(rates.items())},
            dte_days,
            result,
        )
        return result

    logger.warning("[FRED] No rates available for %s, using fallback %.4f", observation_date, FALLBACK_RATE)
    return FALLBACK_RATE


def clear_cache() -> None:
    """Clear the rate cache (for testing)."""
    global _rate_cache, _cache_timestamp
    _rate_cache = {}
    _cache_timestamp = 0.0
