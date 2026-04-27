"""Tests for VIX-style variance replication (Step 4 of IV-RV alignment).

Strategy: synthesize an option chain from a known constant Black-Scholes σ,
then verify VIX replication recovers σ within tolerance. A real Polygon-snapshot
golden fixture for SPY 2024-12-20 lives at
``tests/fixtures/golden/iv30/spy-2024-12-20-chain.parquet`` once populated;
the integration test below is skip-marked when the fixture is absent.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.services.bs_greeks import bs_european_price
from app.volatility.vix_replication import (
    OptionQuote,
    replicate_expiry_variance,
    vix_style_iv30,
)


def _bs_chain(
    *,
    spot: float,
    strikes: list[float],
    T_years: float,
    rate: float,
    sigma: float,
    half_spread: float = 0.01,
) -> list[OptionQuote]:
    """Synthetic chain at a known constant σ. Bid/ask = mid ± half_spread."""
    out: list[OptionQuote] = []
    for k in strikes:
        c = bs_european_price(spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma, is_call=True)
        p = bs_european_price(spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma, is_call=False)
        out.append(
            OptionQuote(
                strike=k,
                call_bid=max(0.0, c - half_spread),
                call_ask=c + half_spread,
                put_bid=max(0.0, p - half_spread),
                put_ask=p + half_spread,
            )
        )
    return out


class TestReplicateExpiry:
    def test_recovers_constant_vol_within_50bps(self):
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        # Dense strike grid spanning ~5σ in each direction.
        strikes = [round(k, 2) for k in [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                                         102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]]
        chain = _bs_chain(spot=spot, strikes=strikes, T_years=T_years, rate=rate, sigma=sigma_true)
        rep = replicate_expiry_variance(chain, rate=rate, T_years=T_years)
        sigma_vix = math.sqrt(rep.sigma_squared_T)
        # 50 bps tolerance — finite strike grid + strike step quantization + edge truncation.
        assert abs(sigma_vix - sigma_true) < 0.005, f"got σ={sigma_vix:.4f}, expected ~{sigma_true}"

    def test_forward_close_to_spot_at_zero_dividend(self):
        spot = 100.0
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 90, 95, 100, 105, 110, 120]
        chain = _bs_chain(spot=spot, strikes=strikes, T_years=T_years, rate=rate, sigma=0.20)
        rep = replicate_expiry_variance(chain, rate=rate, T_years=T_years)
        # F ≈ S · exp(rT) at q=0
        expected_forward = spot * math.exp(rate * T_years)
        assert abs(rep.forward - expected_forward) < 0.5

    def test_too_few_strikes_raises(self):
        spot = 100.0
        T = 30 / 365.0
        chain = _bs_chain(spot=spot, strikes=[100.0], T_years=T, rate=0.05, sigma=0.20)
        with pytest.raises(ValueError, match="at least 3"):
            replicate_expiry_variance(chain, rate=0.05, T_years=T)

    def test_zero_T_raises(self):
        chain = _bs_chain(spot=100, strikes=[90, 100, 110], T_years=30 / 365.0, rate=0.05, sigma=0.20)
        with pytest.raises(ValueError, match="T_years"):
            replicate_expiry_variance(chain, rate=0.05, T_years=0)

    def test_zero_bid_walk_truncates_wings(self):
        # 16 strikes spanning 60–140. At σ=0.20, T=30d, deep-OTM puts (K≤75) and
        # deep-OTM calls (K≥130) have BS price < half_spread → bid = 0 → walk
        # truncates after 2 consecutive zero-bids in each direction.
        spot = 100.0
        T = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 130, 135, 140]
        chain = _bs_chain(spot=spot, strikes=strikes, T_years=T, rate=rate, sigma=0.20)
        rep = replicate_expiry_variance(chain, rate=rate, T_years=T)
        # Truncation must drop at least the deep-OTM strikes from each side.
        assert rep.n_strikes_used < len(strikes)
        # And keep enough strikes near the money to compute meaningful variance.
        assert rep.n_strikes_used >= 5


class TestVixStyleIv30:
    def test_recovers_sigma_with_two_straddling_expiries(self):
        spot = 100.0
        sigma_true = 0.20
        rate = 0.05
        # T1 = 21d, T2 = 35d — straddle 30 days.
        T1_d, T2_d = 21, 35
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100, 102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]
        chain1 = _bs_chain(spot=spot, strikes=strikes, T_years=T1_d / 365.0, rate=rate, sigma=sigma_true)
        chain2 = _bs_chain(spot=spot, strikes=strikes, T_years=T2_d / 365.0, rate=rate, sigma=sigma_true)
        sigma30 = vix_style_iv30(
            chain1, chain2,
            rate1=rate, T1_calendar_days=T1_d,
            rate2=rate, T2_calendar_days=T2_d,
            target_calendar_days=30,
        )
        assert abs(sigma30 - sigma_true) < 0.005, f"σ_30={sigma30:.4f}, expected {sigma_true}"

    def test_target_outside_bracket_raises(self):
        spot = 100.0
        T1_d, T2_d = 21, 35
        strikes = [80, 90, 100, 110, 120]
        chain1 = _bs_chain(spot=spot, strikes=strikes, T_years=T1_d / 365.0, rate=0.05, sigma=0.20)
        chain2 = _bs_chain(spot=spot, strikes=strikes, T_years=T2_d / 365.0, rate=0.05, sigma=0.20)
        with pytest.raises(ValueError, match="not bracketed"):
            vix_style_iv30(
                chain1, chain2,
                rate1=0.05, T1_calendar_days=T1_d,
                rate2=0.05, T2_calendar_days=T2_d,
                target_calendar_days=60,  # outside [21, 35]
            )


_FIXTURE_CANDIDATES = [
    Path("/app/tests/fixtures/golden/iv30/spy-2024-12-20-chain.parquet"),
    Path(__file__).parent.parent / "fixtures" / "golden" / "iv30" / "spy-2024-12-20-chain.parquet",
]
GOLDEN_FIXTURE = next((p for p in _FIXTURE_CANDIDATES if p.exists()), _FIXTURE_CANDIDATES[0])


@pytest.mark.skipif(
    not GOLDEN_FIXTURE.exists(),
    reason=f"golden fixture {GOLDEN_FIXTURE} not populated — run scripts/build_iv30_golden.py",
)
class TestSpyGoldenFixture:
    """Anchor regression test against a real SPY 2024-12-20 Polygon snapshot.

    Populated by ``scripts/build_iv30_golden.py``. The test asserts that our
    VIX-replication code, run against the frozen chain, produces the value
    recorded in the meta sidecar (deterministic recomputation), and that
    sanity checks on the absolute number hold for that date.

    On 2024-12-20 the published VIX index closed at ~17.5%. Our replication
    on the SPY chain should land within a few vol points of that — confirming
    we are computing the same construct CBOE publishes, just from a single
    underlying instead of the SPX chain.

    The ATM-parametric IV30 (15.58%) lands ~170 bps below VIX-style (17.31%)
    because of SPY skew — VIX integrates OTM puts that trade higher than
    ATM. This gap is the well-known **VIX premium over ATM IV** and is part
    of the IV30 design discussion, not a bug in either method.
    """

    def test_replication_matches_meta_sidecar_deterministically(self):
        import json

        import pandas as pd

        df = pd.read_parquet(GOLDEN_FIXTURE)
        for col in ("expiry_days", "strike", "contract_type", "close"):
            assert col in df.columns

        meta = json.loads(GOLDEN_FIXTURE.with_suffix(".meta.json").read_text())
        below = int(meta["straddle"]["below_30d"])
        above = int(meta["straddle"]["above_30d"])

        chain1 = _quotes_from_meta_window(df, below)
        chain2 = _quotes_from_meta_window(df, above)
        sigma_vix = vix_style_iv30(
            chain1, chain2,
            rate1=meta["rate"], T1_calendar_days=below,
            rate2=meta["rate"], T2_calendar_days=above,
            target_calendar_days=30,
        )
        # Deterministic — within float noise of the value the build script wrote.
        assert abs(sigma_vix - float(meta["vix_style_iv30_act365"])) < 1e-9, (
            f"replication non-deterministic: {sigma_vix} vs meta {meta['vix_style_iv30_act365']}"
        )

    def test_vix_replication_in_realistic_band(self):
        """Sanity: our replicated SPY VIX-style σ on 2024-12-20 lands in the
        13–22% band (the published VIX index closed at ~17.5% that day).
        """
        import json

        meta = json.loads(GOLDEN_FIXTURE.with_suffix(".meta.json").read_text())
        sigma_vix = float(meta["vix_style_iv30_act365"])
        assert 0.13 < sigma_vix < 0.22, f"σ_VIX={sigma_vix:.4f} outside reasonable band"

    def test_skew_premium_below_300bps(self):
        """On 2024-12-20 the ATM-call parametric IV is below the VIX-style
        whole-surface integration — that's SPY skew. The gap should be
        < 300 bps; anything wider would suggest a math bug (or extreme
        skew regime that we'd want to surface as a warning, not silently).
        """
        import json

        meta = json.loads(GOLDEN_FIXTURE.with_suffix(".meta.json").read_text())
        gap_bps = float(meta["iv30_diff_bps"])
        assert gap_bps < 300.0, f"VIX–ATM gap {gap_bps:.0f} bps unusually wide"


def _quotes_from_meta_window(df, expiry_days: int):
    """Re-build the OptionQuote list from the parquet's per-contract close prices,
    using the same half-spread synthesis as the build script (max($0.05, 0.5% of close);
    contracts with close < $0.05 are zero-bid).
    """
    sub = df[df["expiry_days"] == expiry_days]
    wide = sub.pivot_table(
        index="strike", columns="contract_type", values="close", aggfunc="first"
    ).sort_index()
    quotes = []
    for strike, row in wide.iterrows():
        call = float(row.get("call", 0.0)) if "call" in wide.columns and not _isnan(row.get("call", 0.0)) else 0.0
        put = float(row.get("put", 0.0)) if "put" in wide.columns and not _isnan(row.get("put", 0.0)) else 0.0
        half = max(0.05, 0.005 * max(call, put, 1.0))
        quotes.append(
            OptionQuote(
                strike=float(strike),
                call_bid=max(0.0, call - half) if call >= 0.05 else 0.0,
                call_ask=call + half if call > 0 else 0.0,
                put_bid=max(0.0, put - half) if put >= 0.05 else 0.0,
                put_ask=put + half if put > 0 else 0.0,
            )
        )
    return quotes


def _isnan(v) -> bool:
    return isinstance(v, float) and v != v
