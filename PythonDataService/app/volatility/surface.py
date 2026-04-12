"""
Volatility Surface Builder
===========================

Assembles per-expiry smile fits into a full implied volatility surface
that can be queried at any (strike, expiry) pair.

The surface is built in three stages:
1. **IV solving** — Convert option prices to implied vols via ``solver.py``.
2. **Smile fitting** — Fit each expiry slice with the chosen method (``fitting.py``).
3. **Cross-expiry interpolation** — Interpolate between fitted expiry slices
   using variance-time weighting to preserve no-calendar-arbitrage.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from app.volatility.fitting import (
    ArbitrageReport,
    FitMethod,
    FitResult,
    SmileSlice,
    check_smile_arbitrage,
    fit_sabr,
    fit_svi,
    fit_variance_interp,
)
from app.volatility.solver import implied_volatility, ImpliedVolResult, SolveStatus

logger = logging.getLogger(__name__)


class SurfaceMethod(str, Enum):
    VARIANCE = "variance"
    SABR = "sabr"
    SVI = "svi"


@dataclass
class SliceDiagnostics:
    """Diagnostics for a single expiry slice."""

    ttm: float
    n_contracts: int
    n_solved: int
    n_failed: int
    fit_method: str
    fit_rmse: float
    arbitrage: Optional[ArbitrageReport] = None


@dataclass
class SurfaceDiagnostics:
    """Aggregate diagnostics for the full surface build."""

    n_expiries: int = 0
    n_total_contracts: int = 0
    n_total_solved: int = 0
    n_total_failed: int = 0
    method: str = ""
    slices: list[SliceDiagnostics] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    valid: bool = True


@dataclass
class VolSurface:
    """
    Queryable implied volatility surface.

    Supports querying vol at arbitrary (strike, ttm) points via
    cross-expiry variance-time interpolation between fitted slices.
    """

    method: SurfaceMethod
    spot: float
    rate: float
    dividend: float
    eval_date: str
    fits: list[FitResult] = field(default_factory=list)
    diagnostics: SurfaceDiagnostics = field(default_factory=SurfaceDiagnostics)

    def volatility(self, strike: float, ttm: float) -> float:
        """
        Query the surface at a given (strike, ttm) point.

        Uses variance-time interpolation between the two nearest
        expiry slices to preserve calendar-spread no-arbitrage.
        """
        if not self.fits:
            raise RuntimeError("Surface has no fitted slices")

        if ttm <= 0:
            raise ValueError(f"ttm must be positive, got {ttm}")

        # Sort fits by TTM
        sorted_fits = sorted(self.fits, key=lambda f: f.ttm)
        ttms = [f.ttm for f in sorted_fits]

        # Exact match
        for fit in sorted_fits:
            if abs(fit.ttm - ttm) < 1e-8:
                return fit.volatility(strike)

        # Extrapolation: flat outside bounds
        if ttm <= ttms[0]:
            return sorted_fits[0].volatility(strike)
        if ttm >= ttms[-1]:
            return sorted_fits[-1].volatility(strike)

        # Interpolation: variance-time weighted
        idx = 0
        for i, t in enumerate(ttms):
            if t > ttm:
                idx = i
                break

        fit_lo = sorted_fits[idx - 1]
        fit_hi = sorted_fits[idx]
        t_lo, t_hi = fit_lo.ttm, fit_hi.ttm

        vol_lo = fit_lo.volatility(strike)
        vol_hi = fit_hi.volatility(strike)

        # Linear interpolation in total variance space
        w_lo = vol_lo ** 2 * t_lo
        w_hi = vol_hi ** 2 * t_hi

        alpha = (ttm - t_lo) / (t_hi - t_lo)
        w_interp = w_lo * (1 - alpha) + w_hi * alpha

        if w_interp < 0:
            raise ValueError(
                f"Negative interpolated variance at K={strike}, T={ttm}"
            )
        return math.sqrt(w_interp / ttm)

    def to_grid(
        self,
        strike_range: tuple[float, float],
        n_strikes: int = 50,
        ttm_list: Optional[list[float]] = None,
    ) -> pd.DataFrame:
        """
        Evaluate surface on a regular grid.

        Returns a DataFrame with columns: strike, ttm, iv.
        """
        if ttm_list is None:
            ttm_list = sorted(f.ttm for f in self.fits)

        strikes = np.linspace(strike_range[0], strike_range[1], n_strikes)
        rows: list[dict[str, float]] = []

        for t in ttm_list:
            for k in strikes:
                try:
                    iv = self.volatility(float(k), t)
                    rows.append({"strike": float(k), "ttm": t, "iv": iv})
                except (ValueError, RuntimeError):
                    rows.append({"strike": float(k), "ttm": t, "iv": float("nan")})

        return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  Builder
# ═══════════════════════════════════════════════════════════════════════════

class VolSurfaceBuilder:
    """
    Builds a ``VolSurface`` from raw option chain data.

    Usage
    -----
    >>> builder = VolSurfaceBuilder(spot=100, rate=0.05)
    >>> surface = builder.build(records, method=SurfaceMethod.SABR)
    >>> vol = surface.volatility(strike=105, ttm=0.25)
    """

    def __init__(
        self,
        spot: float,
        rate: float = 0.05,
        dividend: float = 0.0,
        eval_date: str = "",
        min_contracts_per_slice: int = 5,
        sabr_beta: float = 0.5,
    ) -> None:
        self.spot = spot
        self.rate = rate
        self.dividend = dividend
        self.eval_date = eval_date
        self.min_contracts_per_slice = min_contracts_per_slice
        self.sabr_beta = sabr_beta

    def build(
        self,
        records: list[dict],
        method: SurfaceMethod = SurfaceMethod.VARIANCE,
    ) -> VolSurface:
        """
        Build a vol surface from option records.

        Each record dict must contain:
        - strike: float
        - ttm: float (time to maturity in years)
        - option_price: float (mid price)
        - is_call: bool

        Optional fields:
        - bid: float
        - ask: float
        - open_interest: int
        - volume: int

        Returns a queryable VolSurface.
        """
        logger.info(
            "[IV Surface] Building %s surface from %d records, S=%.2f",
            method.value,
            len(records),
            self.spot,
        )

        diag = SurfaceDiagnostics(method=method.value)

        # ── Step 1: Group by expiry (TTM) ────────────────────────────────
        expiry_groups = self._group_by_expiry(records)
        diag.n_expiries = len(expiry_groups)
        diag.n_total_contracts = len(records)

        # ── Step 2: Solve IV for each contract ───────────────────────────
        solved_groups: dict[float, SmileSlice] = {}
        total_solved = 0
        total_failed = 0

        for ttm, group_records in sorted(expiry_groups.items()):
            strikes: list[float] = []
            ivs: list[float] = []
            failed = 0

            for rec in group_records:
                result = implied_volatility(
                    option_price=rec["option_price"],
                    spot=self.spot,
                    strike=rec["strike"],
                    ttm=ttm,
                    rate=self.rate,
                    dividend=self.dividend,
                    is_call=rec["is_call"],
                )
                if result.iv is not None and result.status in (
                    SolveStatus.QUANTLIB_OK,
                    SolveStatus.BRENT_FALLBACK,
                ):
                    strikes.append(rec["strike"])
                    ivs.append(result.iv)
                else:
                    failed += 1

            total_solved += len(strikes)
            total_failed += failed

            if len(strikes) < self.min_contracts_per_slice:
                diag.warnings.append(
                    f"Expiry T={ttm:.4f}: only {len(strikes)} solved IVs "
                    f"(min {self.min_contracts_per_slice}), skipping"
                )
                continue

            forward = self.spot * math.exp(
                (self.rate - self.dividend) * ttm
            )
            solved_groups[ttm] = SmileSlice(
                strikes=np.array(strikes),
                ivs=np.array(ivs),
                ttm=ttm,
                forward=forward,
            )

        diag.n_total_solved = total_solved
        diag.n_total_failed = total_failed

        if not solved_groups:
            diag.valid = False
            diag.warnings.append("No valid expiry slices after IV solving")
            logger.error("[IV Surface] No valid slices — aborting")
            return VolSurface(
                method=SurfaceMethod(method),
                spot=self.spot,
                rate=self.rate,
                dividend=self.dividend,
                eval_date=self.eval_date,
                diagnostics=diag,
            )

        # ── Step 3: Fit each slice ───────────────────────────────────────
        fits: list[FitResult] = []
        for ttm, smile in sorted(solved_groups.items()):
            fit = self._fit_slice(smile, method)
            fits.append(fit)

            arb = check_smile_arbitrage(fit, smile.strikes, smile.forward)

            slice_diag = SliceDiagnostics(
                ttm=ttm,
                n_contracts=len(smile.strikes),
                n_solved=len(smile.strikes),
                n_failed=0,
                fit_method=method.value,
                fit_rmse=fit.residual_rmse,
                arbitrage=arb,
            )
            diag.slices.append(slice_diag)

            if not arb.passed:
                diag.warnings.append(
                    f"Expiry T={ttm:.4f}: {arb.butterfly_violations} butterfly violations"
                )

        logger.info(
            "[IV Surface] Built %s surface: %d expiries, %d/%d contracts solved",
            method.value,
            len(fits),
            total_solved,
            total_solved + total_failed,
        )

        return VolSurface(
            method=SurfaceMethod(method),
            spot=self.spot,
            rate=self.rate,
            dividend=self.dividend,
            eval_date=self.eval_date,
            fits=fits,
            diagnostics=diag,
        )

    def build_bid_ask(
        self,
        records: list[dict],
        method: SurfaceMethod = SurfaceMethod.VARIANCE,
    ) -> tuple[VolSurface, VolSurface]:
        """
        Build separate bid and ask vol surfaces.

        Records must contain ``bid`` and ``ask`` fields.
        Returns (bid_surface, ask_surface).
        """
        bid_records = [
            {**r, "option_price": r["bid"]}
            for r in records
            if r.get("bid", 0) > 0
        ]
        ask_records = [
            {**r, "option_price": r["ask"]}
            for r in records
            if r.get("ask", 0) > 0
        ]
        bid_surface = self.build(bid_records, method)
        ask_surface = self.build(ask_records, method)
        return bid_surface, ask_surface

    # ── Private helpers ──────────────────────────────────────────────────

    def _group_by_expiry(
        self, records: list[dict]
    ) -> dict[float, list[dict]]:
        """Group records by TTM, rounding to avoid float noise."""
        groups: dict[float, list[dict]] = {}
        for rec in records:
            ttm = round(rec["ttm"], 6)
            groups.setdefault(ttm, []).append(rec)
        return groups

    def _fit_slice(
        self, smile: SmileSlice, method: SurfaceMethod
    ) -> FitResult:
        """Dispatch to the appropriate fitter."""
        if method == SurfaceMethod.SABR:
            return fit_sabr(smile, beta=self.sabr_beta)
        elif method == SurfaceMethod.SVI:
            return fit_svi(smile)
        else:
            return fit_variance_interp(smile)
