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

from app.volatility.cache import (
    SCHEMA_VERSION,
    DataFilters,
    SurfaceCache,
    compute_surface_id,
)
from app.volatility.conventions import SurfaceConventions, dte_to_ttm, ttm_to_dte
from app.volatility.models import (
    OptionRecord,
    SurfaceBuildRequest,
    SurfaceBuildResponse,
    VolQuery,
    VolQueryResponse,
)
from app.volatility.solver import ImpliedVolResult, implied_volatility
from app.volatility.surface import SurfaceMethod, VolSurface, VolSurfaceBuilder

__all__ = [
    "SCHEMA_VERSION",
    "DataFilters",
    "ImpliedVolResult",
    "OptionRecord",
    "SurfaceBuildRequest",
    "SurfaceBuildResponse",
    "SurfaceCache",
    "SurfaceConventions",
    "SurfaceMethod",
    "VolQuery",
    "VolQueryResponse",
    "VolSurface",
    "VolSurfaceBuilder",
    "compute_surface_id",
    "dte_to_ttm",
    "implied_volatility",
    "ttm_to_dte",
]
