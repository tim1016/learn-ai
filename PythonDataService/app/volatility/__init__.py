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

from app.volatility.solver import implied_volatility, ImpliedVolResult
from app.volatility.surface import VolSurfaceBuilder, VolSurface, SurfaceMethod
from app.volatility.models import (
    OptionRecord,
    SurfaceBuildRequest,
    SurfaceBuildResponse,
    VolQuery,
    VolQueryResponse,
)
from app.volatility.conventions import SurfaceConventions, dte_to_ttm, ttm_to_dte
from app.volatility.cache import (
    SurfaceCache,
    DataFilters,
    compute_surface_id,
    SCHEMA_VERSION,
)

__all__ = [
    "implied_volatility",
    "ImpliedVolResult",
    "VolSurfaceBuilder",
    "VolSurface",
    "SurfaceMethod",
    "OptionRecord",
    "SurfaceBuildRequest",
    "SurfaceBuildResponse",
    "VolQuery",
    "VolQueryResponse",
    "SurfaceConventions",
    "dte_to_ttm",
    "ttm_to_dte",
    "SurfaceCache",
    "DataFilters",
    "compute_surface_id",
    "SCHEMA_VERSION",
]
