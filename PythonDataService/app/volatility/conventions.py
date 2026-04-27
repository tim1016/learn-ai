"""
Surface Conventions Module
===========================

Immutable conventions that anchor every surface build, ensuring
deterministic forward definitions and consistent log(K/F) across builds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

TRADING_DAYS_PER_YEAR: Final[int] = 252
CALENDAR_DAYS_PER_YEAR: Final[int] = 365


@dataclass(frozen=True)
class SurfaceConventions:
    """
    Immutable conventions for surface construction.

    Defines:
    - Forward pricing (BSM: F = S * exp((r - q) * T))
    - Discount factors (continuous: df = exp(-r * T))
    - Day count conventions
    - Risk-free rate and dividend yield
    """

    day_count: str = "Actual365Fixed"
    forward_model: str = "bsm"
    discount_model: str = "continuous"
    rate: float = 0.05
    dividend_yield: float = 0.0
    calendar: str = "NullCalendar"

    def forward(self, spot: float, ttm: float) -> float:
        """
        Compute forward price under BSM conventions.

        F = S * exp((r - q) * T)
        """
        if self.forward_model != "bsm":
            raise NotImplementedError(f"forward_model={self.forward_model}")
        return spot * math.exp((self.rate - self.dividend_yield) * ttm)

    def discount_factor(self, ttm: float) -> float:
        """
        Compute discount factor under continuous conventions.

        df = exp(-r * T)
        """
        if self.discount_model != "continuous":
            raise NotImplementedError(f"discount_model={self.discount_model}")
        return math.exp(-self.rate * ttm)

    def to_hash_dict(self) -> dict[str, Any]:
        """
        Convert to dict for surface_id hash computation.

        Returns immutable dict representation of all conventions.
        """
        return {
            "day_count": self.day_count,
            "forward_model": self.forward_model,
            "discount_model": self.discount_model,
            "rate": self.rate,
            "dividend_yield": self.dividend_yield,
            "calendar": self.calendar,
        }


def dte_to_ttm(dte_days: int, day_count: str = "Actual365Fixed") -> float:
    """
    Convert DTE (calendar days) to time-to-maturity (year fraction).

    Args:
        dte_days: Days to expiration (calendar days)
        day_count: Day count convention (default: Actual365Fixed)

    Returns:
        TTM in years
    """
    if day_count == "Actual365Fixed":
        return dte_days / 365.0
    elif day_count == "Actual360":
        return dte_days / 360.0
    elif day_count == "ActualActual":
        return dte_days / 365.25
    else:
        raise ValueError(f"Unsupported day_count: {day_count}")


def ttm_to_dte(ttm: float, day_count: str = "Actual365Fixed") -> int:
    """
    Convert TTM (year fraction) to DTE (calendar days).

    Args:
        ttm: Time-to-maturity in years
        day_count: Day count convention (default: Actual365Fixed)

    Returns:
        DTE in calendar days (rounded to nearest integer)
    """
    if day_count == "Actual365Fixed":
        return round(ttm * 365.0)
    elif day_count == "Actual360":
        return round(ttm * 360.0)
    elif day_count == "ActualActual":
        return round(ttm * 365.25)
    else:
        raise ValueError(f"Unsupported day_count: {day_count}")
