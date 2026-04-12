"""
Example: Build and Query an Implied Volatility Surface
========================================================

Generates a synthetic option chain, builds surfaces using all three
methods (variance, SABR, SVI), prints diagnostics, and produces a
3D surface plot.

Run:
    python -m app.volatility.example
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from app.volatility.solver import implied_volatility
from app.volatility.surface import SurfaceMethod, VolSurfaceBuilder


def generate_synthetic_chain(
    spot: float = 100.0,
    rate: float = 0.05,
    base_vol: float = 0.25,
    skew: float = -0.15,
    n_strikes: int = 15,
    ttms: list[float] | None = None,
) -> list[dict]:
    """
    Generate a synthetic option chain with realistic skew.

    Uses Black-Scholes to price options from a known vol surface,
    then the solver recovers IV from those prices.
    """
    from scipy.stats import norm

    if ttms is None:
        ttms = [30 / 365, 60 / 365, 90 / 365, 180 / 365, 365 / 365]

    records: list[dict] = []

    for ttm in ttms:
        forward = spot * math.exp(rate * ttm)
        strike_lo = spot * 0.80
        strike_hi = spot * 1.20
        strikes = np.linspace(strike_lo, strike_hi, n_strikes)

        for k in strikes:
            # Synthetic vol with skew: vol = base + skew * log(K/F) / sqrt(T)
            log_m = math.log(k / forward)
            vol = base_vol + skew * log_m + 0.02 * log_m ** 2  # skew + smile
            vol = max(vol, 0.05)

            # Price via BS
            d1 = (math.log(spot / k) + (rate + 0.5 * vol ** 2) * ttm) / (
                vol * math.sqrt(ttm)
            )
            d2 = d1 - vol * math.sqrt(ttm)

            is_call = k >= forward  # OTM calls above forward
            if is_call:
                price = spot * norm.cdf(d1) - k * math.exp(-rate * ttm) * norm.cdf(d2)
            else:
                price = k * math.exp(-rate * ttm) * norm.cdf(-d2) - spot * norm.cdf(-d1)

            if price < 0.01:
                continue

            records.append(
                {
                    "strike": float(k),
                    "ttm": ttm,
                    "option_price": round(price, 4),
                    "is_call": is_call,
                    "bid": round(price * 0.98, 4),
                    "ask": round(price * 1.02, 4),
                }
            )

    return records


def main() -> None:
    spot = 100.0
    rate = 0.05

    print("=" * 70)
    print("  Implied Volatility Surface — Example")
    print("=" * 70)

    # ── Generate data ────────────────────────────────────────────────────
    records = generate_synthetic_chain(spot=spot, rate=rate)
    print(f"\nGenerated {len(records)} option records across "
          f"{len(set(r['ttm'] for r in records))} expiries\n")

    # ── Build surfaces with all three methods ────────────────────────────
    builder = VolSurfaceBuilder(spot=spot, rate=rate, eval_date="2026-04-12")

    for method in [SurfaceMethod.VARIANCE, SurfaceMethod.SABR, SurfaceMethod.SVI]:
        print(f"\n{'─' * 50}")
        print(f"  Method: {method.value.upper()}")
        print(f"{'─' * 50}")

        surface = builder.build(records, method=method)
        diag = surface.diagnostics

        print(f"  Expiries fitted:  {len(surface.fits)}")
        print(f"  Contracts solved: {diag.n_total_solved}/{diag.n_total_contracts}")
        print(f"  Warnings:         {len(diag.warnings)}")

        for fit in surface.fits:
            print(f"\n  T={fit.ttm:.4f}  RMSE={fit.residual_rmse:.6f}")
            for k, v in fit.params.items():
                print(f"    {k}: {v:.6f}" if isinstance(v, float) else f"    {k}: {v}")

        # Query at a few points
        print(f"\n  Sample queries:")
        for k in [90.0, 95.0, 100.0, 105.0, 110.0]:
            for t in [0.25, 0.5]:
                try:
                    iv = surface.volatility(k, t)
                    print(f"    K={k:6.1f}  T={t:.2f}  IV={iv:.4f}  ({iv*100:.1f}%)")
                except (ValueError, RuntimeError) as e:
                    print(f"    K={k:6.1f}  T={t:.2f}  ERROR: {e}")

    # ── Plot (if matplotlib available) ───────────────────────────────────
    try:
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D

        surface = builder.build(records, method=SurfaceMethod.SVI)
        df = surface.to_grid(strike_range=(85, 115), n_strikes=40)
        df = df.dropna(subset=["iv"])

        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_subplot(111, projection="3d")

        ax.plot_trisurf(
            df["strike"],
            df["ttm"],
            df["iv"],
            cmap="viridis",
            alpha=0.8,
        )
        ax.set_xlabel("Strike")
        ax.set_ylabel("Time to Maturity (years)")
        ax.set_zlabel("Implied Volatility")
        ax.set_title("SVI Implied Volatility Surface")

        plt.tight_layout()
        plt.savefig("iv_surface_example.png", dpi=150)
        print("\n  Surface plot saved to iv_surface_example.png")

    except ImportError:
        print("\n  (matplotlib not available — skipping plot)")

    print(f"\n{'=' * 70}")
    print("  Done.")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
