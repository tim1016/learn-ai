"""
Implied Volatility Surface Module
==================================

Production-grade implied volatility surface construction using QuantLib.

Supports three surface fitting methods:
- Variance interpolation (bilinear on variance-time grid)
- SABR parametric model (per-expiry smile fitting)
- SVI parametric model (per-expiry smile fitting)

All methods produce deterministic outputs given identical inputs.
"""

from __future__ import annotations

from app.volatility.conventions import SurfaceConventions, dte_to_ttm, ttm_to_dte
from app.volatility.solver import ImpliedVolResult, implied_volatility
from app.volatility.surface import SurfaceMethod, VolSurface, VolSurfaceBuilder

__all__ = [
    "ImpliedVolResult",
    "SurfaceConventions",
    "SurfaceMethod",
    "VolSurface",
    "VolSurfaceBuilder",
    "dte_to_ttm",
    "implied_volatility",
    "ttm_to_dte",
]
