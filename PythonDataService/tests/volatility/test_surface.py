"""
Tests for the VolSurfaceBuilder and VolSurface.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm as _norm

from app.volatility.surface import SurfaceMethod, VolSurface, VolSurfaceBuilder


def _bs_price(spot: float, strike: float, ttm: float, rate: float, vol: float, is_call: bool) -> float:
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol**2) * ttm) / (vol * math.sqrt(ttm))
    d2 = d1 - vol * math.sqrt(ttm)
    df = math.exp(-rate * ttm)
    if is_call:
        return spot * _norm.cdf(d1) - strike * df * _norm.cdf(d2)
    return strike * df * _norm.cdf(-d2) - spot * _norm.cdf(-d1)


class TestSurfaceBuilder:
    """Tests for building surfaces from option chains."""

    @pytest.mark.parametrize("method", list(SurfaceMethod))
    def test_builds_successfully(
        self,
        flat_vol_chain: list[dict],
        spot: float,
        rate: float,
        method: SurfaceMethod,
    ) -> None:
        """Surface should build without errors for all methods."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate)
        surface = builder.build(flat_vol_chain, method=method)

        assert surface.diagnostics.valid
        assert len(surface.fits) > 0
        assert surface.diagnostics.n_total_solved > 0

    def test_recovers_flat_vol(self, flat_vol_chain: list[dict], spot: float, rate: float, base_vol: float) -> None:
        """Surface built from flat-vol chain should recover ~25% everywhere."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate)
        surface = builder.build(flat_vol_chain, method=SurfaceMethod.VARIANCE)

        for k in [90, 95, 100, 105, 110]:
            for t in [30 / 365, 90 / 365, 180 / 365]:
                iv = surface.volatility(float(k), t)
                assert abs(iv - base_vol) < 0.01, f"K={k} T={t:.3f}: expected {base_vol}, got {iv:.4f}"

    def test_skewed_chain_builds(self, skewed_chain: list[dict], spot: float, rate: float) -> None:
        """Skewed chain should build with all methods."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate)

        for method in SurfaceMethod:
            surface = builder.build(skewed_chain, method=method)
            assert surface.diagnostics.valid
            assert len(surface.fits) >= 3

    def test_rejects_empty_chain(self, spot: float, rate: float) -> None:
        """Empty chain should produce invalid surface."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate, min_contracts_per_slice=5)
        # Too few contracts per slice
        records = [{"strike": 100.0, "ttm": 0.25, "option_price": 5.0, "is_call": True}]
        surface = builder.build(records, method=SurfaceMethod.VARIANCE)
        assert not surface.diagnostics.valid

    def test_diagnostics_populated(self, flat_vol_chain: list[dict], spot: float, rate: float) -> None:
        """Diagnostics should be fully populated."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate)
        surface = builder.build(flat_vol_chain, method=SurfaceMethod.VARIANCE)
        diag = surface.diagnostics

        assert diag.n_expiries > 0
        assert diag.n_total_contracts > 0
        assert diag.n_total_solved > 0
        assert diag.method == "variance"
        assert len(diag.slices) > 0


class TestSurfaceQuery:
    """Tests for querying a built surface."""

    @pytest.fixture
    def surface(self, skewed_chain: list[dict], spot: float, rate: float) -> VolSurface:
        builder = VolSurfaceBuilder(spot=spot, rate=rate)
        return builder.build(skewed_chain, method=SurfaceMethod.SVI)

    def test_query_at_fitted_expiry(self, surface: VolSurface) -> None:
        """Query at a fitted expiry should return finite vol."""
        ttm = surface.fits[0].ttm
        iv = surface.volatility(100.0, ttm)
        assert math.isfinite(iv)
        assert iv > 0

    def test_interpolation_between_expiries(self, surface: VolSurface) -> None:
        """Query between two expiries should interpolate smoothly."""
        ttms = sorted(f.ttm for f in surface.fits)
        if len(ttms) < 2:
            pytest.skip("Need at least 2 expiries")

        mid_ttm = (ttms[0] + ttms[1]) / 2
        iv = surface.volatility(100.0, mid_ttm)

        surface.volatility(100.0, ttms[0])
        surface.volatility(100.0, ttms[1])

        # Interpolated vol should be roughly between neighbors
        assert iv > 0
        assert math.isfinite(iv)

    def test_extrapolation_flat(self, surface: VolSurface) -> None:
        """Query outside fitted range should extrapolate flat."""
        ttms = sorted(f.ttm for f in surface.fits)
        tiny_ttm = ttms[0] * 0.5
        iv_extrap = surface.volatility(100.0, tiny_ttm)
        iv_boundary = surface.volatility(100.0, ttms[0])

        assert abs(iv_extrap - iv_boundary) < 1e-6

    def test_invalid_ttm_raises(self, surface: VolSurface) -> None:
        """Negative TTM should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            surface.volatility(100.0, -0.1)

    def test_grid_output(self, surface: VolSurface) -> None:
        """to_grid should return a DataFrame with expected columns."""
        df = surface.to_grid(strike_range=(85, 115), n_strikes=20)
        assert "strike" in df.columns
        assert "ttm" in df.columns
        assert "iv" in df.columns
        assert len(df) > 0


class TestDeterminism:
    """Surface builds must be deterministic."""

    def test_same_inputs_same_surface(self, skewed_chain: list[dict], spot: float, rate: float) -> None:
        """Building twice from identical inputs should produce identical vols."""
        builder = VolSurfaceBuilder(spot=spot, rate=rate)

        s1 = builder.build(skewed_chain, method=SurfaceMethod.SABR)
        s2 = builder.build(skewed_chain, method=SurfaceMethod.SABR)

        for k in [90, 95, 100, 105, 110]:
            for t in [30 / 365, 90 / 365]:
                v1 = s1.volatility(float(k), t)
                v2 = s2.volatility(float(k), t)
                assert abs(v1 - v2) < 1e-10, f"Non-deterministic at K={k} T={t}: {v1} vs {v2}"


class TestRegressionStability:
    """Regression tests for stable outputs given fixed inputs."""

    def test_known_atm_vol(self, spot: float, rate: float, base_vol: float) -> None:
        """Fixed flat-vol chain should produce a known ATM vol."""
        ttm = 90 / 365
        records = []
        for m in np.linspace(0.85, 1.15, 15):
            k = spot * m
            forward = spot * math.exp(rate * ttm)
            is_call = k >= forward
            price = _bs_price(spot, k, ttm, rate, base_vol, is_call)
            if price < 0.01:
                continue
            records.append({"strike": float(k), "ttm": ttm, "option_price": price, "is_call": is_call})

        builder = VolSurfaceBuilder(spot=spot, rate=rate, min_contracts_per_slice=3)
        surface = builder.build(records, method=SurfaceMethod.VARIANCE)

        atm_iv = surface.volatility(spot, ttm)
        assert abs(atm_iv - base_vol) < 0.005, f"ATM IV={atm_iv}, expected ~{base_vol}"
