"""Trailing-12-month dividend-yield computation from Polygon dividend events.

Computes a continuous-dividend-yield proxy ``q ≈ Σ(TTM cash dividends) / spot``
suitable for Black-Scholes pricing and IV solving. The TTM window is exactly
365 calendar days back from the observation date.

Pairs with :mod:`app.services.fred_service` to replace the legacy
``r=0.043, q=0`` defaults that were hardcoded into pricing-lab,
options-strategy-lab, and strategy-builder. See the IV-RV alignment plan
(memory: ``iv_rv_alignment_plan.md``) for context.

Cache is in-memory, per-(ticker, observation_date) with a 24-hour TTL —
matching the pattern already used by ``fred_service``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.polygon_client import PolygonClient

logger = logging.getLogger(__name__)

_yield_cache: dict[tuple[str, str], float] = {}
_cache_timestamps: dict[tuple[str, str], float] = {}
_CACHE_TTL_SECONDS = 86400  # 24 hours


def _is_cache_valid(key: tuple[str, str]) -> bool:
    ts = _cache_timestamps.get(key)
    return ts is not None and time.time() - ts < _CACHE_TTL_SECONDS


def get_trailing_12m_cash_dividends(
    ticker: str,
    polygon: PolygonClient,
    observation_date: str | None = None,
) -> float:
    """Sum cash dividends with ex-date in the 12-month window ending on observation_date.

    Returns 0.0 for non-payers or on Polygon error (logged, not raised — many
    underlyings legitimately pay no dividend).
    """
    if observation_date is None:
        observation_date = datetime.now(UTC).date().isoformat()
    end_dt = datetime.strptime(observation_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=365)
    try:
        events = polygon.list_dividends(
            ticker=ticker,
            ex_dividend_date_gte=start_dt.strftime("%Y-%m-%d"),
            ex_dividend_date_lte=observation_date,
        )
    except Exception as exc:
        logger.warning("[DIV] Failed to list dividends for %s: %s", ticker, exc)
        return 0.0
    total = 0.0
    for ev in events:
        amt = ev.get("cash_amount")
        if amt is None:
            continue
        try:
            total += float(amt)
        except (TypeError, ValueError):
            continue
    return total


def compute_dividend_yield(
    ticker: str,
    spot_price: float,
    polygon: PolygonClient,
    observation_date: str | None = None,
) -> float:
    """Continuous-dividend-yield proxy: TTM cash dividends ÷ spot.

    Cached per (ticker, observation_date) for 24 hours.
    """
    if spot_price <= 0:
        raise ValueError(f"spot_price must be positive: {spot_price}")
    if observation_date is None:
        observation_date = datetime.now(UTC).date().isoformat()
    key = (ticker.upper(), observation_date)
    if _is_cache_valid(key) and key in _yield_cache:
        return _yield_cache[key]

    ttm_dividends = get_trailing_12m_cash_dividends(ticker, polygon, observation_date)
    yld = ttm_dividends / spot_price
    _yield_cache[key] = yld
    _cache_timestamps[key] = time.time()
    return yld


def clear_cache() -> None:
    """Reset the dividend-yield cache (test helper)."""
    _yield_cache.clear()
    _cache_timestamps.clear()
