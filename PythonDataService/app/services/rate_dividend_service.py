"""Unified ``(r, q)`` facade for option pricing inputs.

Composes :func:`app.services.fred_service.get_risk_free_rate_and_source`
and :func:`app.services.dividend_service.compute_dividend_yield`. This is
the single entry point callers should use when they need ``(r, q)`` to feed
the BS pricer or IV solver, replacing the historical hardcoded defaults
``r=0.043`` (pricing-lab UI signal) and ``q=0`` (every BS path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.dividend_service import compute_dividend_yield
from app.services.fred_service import get_risk_free_rate_and_source

if TYPE_CHECKING:
    from app.services.polygon_client import PolygonClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateAndDividend:
    """Snapshot of (r, q) at a moment, with provenance for audit.

    The rate always resolves; ``source_rate`` says whether FRED answered or the
    one default stood in. The dividend is best-effort: ``None`` (with no source)
    when its lookup fails (#2764).
    """

    rate: float
    dividend_yield: float | None
    source_rate: str
    source_dividend: str | None


def get_rate_and_dividend(
    ticker: str,
    spot_price: float,
    polygon: PolygonClient,
    dte_days: int = 30,
    observation_date: str | None = None,
) -> RateAndDividend:
    """Return (r, q) for pricing/solving on a given symbol and date.

    Parameters
    ----------
    ticker : str
        Underlying symbol — used for the dividend lookup.
    spot_price : float
        Current spot, used to convert TTM cash dividends into a continuous-yield
        proxy. A non-positive spot leaves the dividend unset.
    polygon : PolygonClient
        Polygon client instance (caller is responsible for lifecycle).
    dte_days : int
        Days to expiration of the option being priced — drives FRED interpolation
        across {DTB4WK, DTB3, DTB6, DTB1YR}. Default 30 matches IV30.
    observation_date : str | None
        ``YYYY-MM-DD`` observation date. Defaults to today.
    """
    rate, source_rate = get_risk_free_rate_and_source(dte_days=dte_days, observation_date=observation_date)
    dividend: float | None = None
    if spot_price > 0:
        try:
            dividend = compute_dividend_yield(
                ticker=ticker,
                spot_price=spot_price,
                polygon=polygon,
                observation_date=observation_date,
            )
        except Exception:
            logger.warning("[RD] %s dividend lookup failed; q left unset", ticker, exc_info=True)
    logger.debug(
        "[RD] %s @ %s: r=%.4f q=%s (spot=%.2f, dte=%d)",
        ticker,
        observation_date or "today",
        rate,
        dividend,
        spot_price,
        dte_days,
    )
    return RateAndDividend(
        rate=rate,
        dividend_yield=dividend,
        source_rate=source_rate,
        source_dividend=None if dividend is None else "Polygon TTM",
    )
