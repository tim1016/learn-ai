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
from app.volatility.price_normalization import (
    NormalizedOptionPrice,
    NormalizedOptionQuote,
    from_eod_close,
    from_snapshot_quote,
)
from app.volatility.vix_replication import (
    OptionQuote,
    replicate_expiry_variance,
    replicate_expiry_variance_with_provenance,
    vix_style_iv30,
    vix_style_iv30_with_provenance,
)


def _bs_normalized_chain(
    *,
    spot: float,
    strikes: list[float],
    T_years: float,
    rate: float,
    sigma: float,
    half_spread: float = 0.01,
    source: str = "opra_mid",
) -> list[NormalizedOptionQuote]:
    """Synthetic chain of NormalizedOptionQuote at a known constant σ.

    ``source="opra_mid"`` builds bid/ask = mid ± half_spread and tags
    ``opra_mid``. ``source="synthetic_close_proxy"`` runs each leg through
    ``from_eod_close`` so the synthesis-rule provenance is preserved.
    """
    out: list[NormalizedOptionQuote] = []
    for k in strikes:
        c = bs_european_price(
            spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma, is_call=True
        )
        p = bs_european_price(
            spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma, is_call=False
        )
        if source == "opra_mid":
            call = from_snapshot_quote(max(0.0, c - half_spread), c + half_spread)
            put = from_snapshot_quote(max(0.0, p - half_spread), p + half_spread)
        elif source == "synthetic_close_proxy":
            call = from_eod_close(c)
            put = from_eod_close(p)
        else:
            raise ValueError(f"unknown source {source!r}")
        out.append(NormalizedOptionQuote(strike=k, call=call, put=put))
    return out


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


_BUILD_SCRIPT_PER_STRIKE_RULE = "max($0.05, 0.5%·max(call_close, put_close, $1.0))"


def _per_strike_synthetic_leg(close: float, half_spread: float) -> NormalizedOptionPrice:
    """Build a ``synthetic_close_proxy`` ``NormalizedOptionPrice`` using a
    pre-computed (per-strike) half-spread. Mirrors the SPY golden-fixture
    build script's rule, where the spread depends on max(call, put) at the
    same strike — different from the per-leg rule in
    ``price_normalization.from_eod_close``.

    Zero-bid the leg when ``close < 0.05`` *or* when ``close - half_spread <= 0``;
    the second condition matters for deep-OTM legs at the same strike as a
    deep-ITM leg, where the per-strike half-spread is dominated by the
    ITM side and squashes the small OTM side to zero. This matches the
    legacy build script's wing-truncation behavior exactly.

    Used only by the SPY golden fixture test for deterministic equivalence
    against the meta sidecar.
    """
    if close < 0.05 or close - half_spread <= 0.0:
        return NormalizedOptionPrice(
            mid=0.0,
            source="synthetic_close_proxy",
            spread_estimate=None,
            spread_synthetic=True,
            half_spread_rule=_BUILD_SCRIPT_PER_STRIKE_RULE,
            quality_score=0.0,
        )
    quality = max(0.0, min(1.0, 1.0 - half_spread / close))
    return NormalizedOptionPrice(
        mid=close,
        source="synthetic_close_proxy",
        spread_estimate=half_spread,
        spread_synthetic=True,
        half_spread_rule=_BUILD_SCRIPT_PER_STRIKE_RULE,
        quality_score=quality,
    )


def _normalized_quotes_from_meta_window(df, expiry_days: int) -> list[NormalizedOptionQuote]:
    """Reconstruct the chain as ``NormalizedOptionQuote`` using the same
    per-strike half-spread rule as the SPY golden-fixture build script,
    so the provenance-aware replication produces a value that's
    deterministically equivalent to the meta sidecar.
    """
    sub = df[df["expiry_days"] == expiry_days]
    wide = sub.pivot_table(
        index="strike", columns="contract_type", values="close", aggfunc="first"
    ).sort_index()
    out: list[NormalizedOptionQuote] = []
    for strike, row in wide.iterrows():
        call_close = (
            float(row.get("call", 0.0))
            if "call" in wide.columns and not _isnan(row.get("call", 0.0))
            else 0.0
        )
        put_close = (
            float(row.get("put", 0.0))
            if "put" in wide.columns and not _isnan(row.get("put", 0.0))
            else 0.0
        )
        half = max(0.05, 0.005 * max(call_close, put_close, 1.0))
        out.append(
            NormalizedOptionQuote(
                strike=float(strike),
                call=_per_strike_synthetic_leg(call_close, half),
                put=_per_strike_synthetic_leg(put_close, half),
            )
        )
    return out


# ----------------------------------------------------------------------
# Step B — provenance-aware replication
# ----------------------------------------------------------------------


class TestReplicateExpiryWithProvenance:
    """The provenance-aware path produces identical math to the legacy path
    when fed the same chain wrapped as ``opra_mid``, plus a populated
    ``IvProvenance``.
    """

    def test_matches_legacy_math_on_clean_opra_chain(self):
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                   102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]

        legacy_chain = []
        for k in strikes:
            c = bs_european_price(spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma_true, is_call=True)
            p = bs_european_price(spot=spot, strike=k, ttm_years=T_years, rate=rate, volatility=sigma_true, is_call=False)
            legacy_chain.append(
                OptionQuote(
                    strike=k,
                    call_bid=max(0.0, c - 0.01), call_ask=c + 0.01,
                    put_bid=max(0.0, p - 0.01), put_ask=p + 0.01,
                )
            )
        legacy_rep = replicate_expiry_variance(legacy_chain, rate=rate, T_years=T_years)

        normalized_chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, half_spread=0.01, source="opra_mid",
        )
        new_rep, prov = replicate_expiry_variance_with_provenance(
            normalized_chain, rate=rate, T_years=T_years
        )

        # Math is identical. K0 selection is pure index, so byte-equal;
        # forward and sigma² involve float math but should match within
        # round-off.
        assert new_rep.forward == pytest.approx(legacy_rep.forward, rel=1e-12)
        assert new_rep.K0 == legacy_rep.K0
        assert new_rep.sigma_squared_T == pytest.approx(legacy_rep.sigma_squared_T, rel=1e-12)
        assert new_rep.n_strikes_used == legacy_rep.n_strikes_used

        # Provenance is right for an all-opra chain.
        assert prov.iv_source == "internal_solver"
        assert prov.variance_contribution_synthetic == 0.0
        assert prov.price_source_mix.get("opra_mid", 0.0) == pytest.approx(1.0)
        # Strike coverage reflects post-truncation surviving wings, not the
        # input range — a BS-synthesized chain at σ=0.20 truncates deep
        # OTM aggressively, so this is < 1.0 even with 21 input strikes.
        assert 0.0 < prov.strike_coverage_score <= 1.0

    def test_synthetic_close_proxy_chain_reports_full_synthesis(self):
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]

        chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="synthetic_close_proxy",
        )
        _, prov = replicate_expiry_variance_with_provenance(
            chain, rate=rate, T_years=T_years
        )
        assert prov.variance_contribution_synthetic == pytest.approx(1.0)
        assert prov.price_source_mix.get("synthetic_close_proxy", 0.0) == pytest.approx(1.0)

    def test_mixed_atm_opra_wings_synthetic_yields_intermediate_share(self):
        """Round 3 issue #2: variance-contribution-weighted synthetic share.

        Build a chain where the inner cluster is real OPRA mid and the
        wings are synthetic. The count-based share is independent of
        which strikes are which; the variance-weighted share depends on
        which strikes contribute most to the integration.
        """
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05

        all_strikes = [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130]
        atm_window = {95, 100, 105}  # opra_mid here

        opra_chain = _bs_normalized_chain(
            spot=spot, strikes=all_strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="opra_mid",
        )
        synth_chain = _bs_normalized_chain(
            spot=spot, strikes=all_strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="synthetic_close_proxy",
        )
        mixed = [
            opra_chain[i] if all_strikes[i] in atm_window else synth_chain[i]
            for i in range(len(all_strikes))
        ]

        _, prov = replicate_expiry_variance_with_provenance(
            mixed, rate=rate, T_years=T_years
        )

        # The mix is non-trivial — neither 0 nor 1.
        assert 0.0 < prov.variance_contribution_synthetic < 1.0
        # And both sources show up in the count mix.
        assert "opra_mid" in prov.price_source_mix
        assert "synthetic_close_proxy" in prov.price_source_mix
        # Sanity: the count-based mix should sum to ~1.
        assert sum(prov.price_source_mix.values()) == pytest.approx(1.0)

    def test_debug_payload_lists_per_strike_contributions(self):
        spot = 100.0
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 90, 95, 100, 105, 110, 120]
        chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate, sigma=0.20,
            source="opra_mid",
        )
        _, prov = replicate_expiry_variance_with_provenance(
            chain, rate=rate, T_years=T_years, debug=True,
        )
        assert prov.per_strike_contributions is not None
        # Each contribution dict has the documented shape.
        for entry in prov.per_strike_contributions:
            assert {"strike", "kind", "dK", "Q", "c_i", "active_leg_sources",
                    "active_leg_synthetic"}.issubset(entry.keys())
            assert entry["c_i"] >= 0
            assert entry["kind"] in ("put", "call", "both")

    def test_debug_default_off_gives_none(self):
        spot = 100.0
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 90, 100, 110, 120]
        chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate, sigma=0.20,
            source="opra_mid",
        )
        _, prov = replicate_expiry_variance_with_provenance(
            chain, rate=rate, T_years=T_years
        )
        assert prov.per_strike_contributions is None

    def test_max_single_strike_share_in_unit_interval(self):
        """Sanity: the diagnostic always lands in [0, 1] for any valid
        chain, including the empty-integration edge case."""
        spot = 100.0
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 90, 95, 100, 105, 110, 120]
        chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=0.20, source="opra_mid",
        )
        _, prov = replicate_expiry_variance_with_provenance(
            chain, rate=rate, T_years=T_years
        )
        assert 0.0 <= prov.max_single_strike_share <= 1.0

    def test_max_single_strike_share_uplifted_by_inflated_strike(self):
        """Research-doc §8.2.5: a single strike with an anomalously large
        quote dominates the variance integral via the ``c_i ∝ Q`` term.
        Asserted as a relative uplift over the same chain without the
        inflation — sidesteps absolute-calibration brittleness from
        wing-truncation interacting with strike spacing."""
        from app.volatility.price_normalization import (
            NormalizedOptionQuote,
            from_snapshot_quote,
        )

        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                   102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]

        healthy = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="opra_mid",
        )
        _, prov_healthy = replicate_expiry_variance_with_provenance(
            healthy, rate=rate, T_years=T_years,
            dominance_gate_threshold=None,
        )

        # Inflate the K=95 put to a fixed $50 mid. K=95 is reliably in the
        # integration (just below ATM, BS-priced put is ~$0.66 at σ=0.20),
        # so a fixed dollar inflation makes the c_i term roughly 75x its
        # natural value. A multiplier on the BS-fair value would not work
        # for deep-OTM strikes whose fair value is essentially zero.
        inflated_chain = list(healthy)
        target_idx = strikes.index(95)
        original = inflated_chain[target_idx]
        inflated_put = from_snapshot_quote(bid=49.99, ask=50.01)
        inflated_chain[target_idx] = NormalizedOptionQuote(
            strike=original.strike, call=original.call, put=inflated_put,
        )

        # Gate disabled here so the metric reflects the un-mitigated chain;
        # the gate's behavior is exercised in TestSingleStrikeDominanceGate.
        _, prov_inflated = replicate_expiry_variance_with_provenance(
            inflated_chain, rate=rate, T_years=T_years,
            dominance_gate_threshold=None,
        )

        # The inflated strike should dominate; the lift over the healthy
        # baseline must be substantial. 0.30 leaves ample margin around
        # the empirical lift (typically > 0.5) without depending on the
        # exact post-truncation chain shape.
        lift = prov_inflated.max_single_strike_share - prov_healthy.max_single_strike_share
        assert lift > 0.30, (
            f"100x inflation at K=95 should dominate the integral; "
            f"healthy={prov_healthy.max_single_strike_share:.3f}, "
            f"inflated={prov_inflated.max_single_strike_share:.3f}, "
            f"lift={lift:.3f}"
        )
        # And the inflated value sits well into the "domination" regime.
        assert prov_inflated.max_single_strike_share > 0.5


class TestSingleStrikeDominanceGate:
    """The gate iteratively drops the dominant strike and recomputes when
    ``max_single_strike_share`` exceeds threshold; it hard-fails after
    ``max_iterations`` attempts or below the strike-count floor.

    See ``docs/architecture/iv-research-chat-notes.md`` §5.8.
    """

    @staticmethod
    def _inflated_chain(spot, sigma_true, T_years, rate, strikes, *, target_strike):
        """Build a chain with an inflated put at ``target_strike`` so that
        single-strike share blows up — exactly the pathological shape the
        gate is supposed to catch."""
        from app.volatility.price_normalization import (
            NormalizedOptionQuote,
            from_snapshot_quote,
        )

        chain = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="opra_mid",
        )
        target_idx = strikes.index(target_strike)
        original = chain[target_idx]
        chain[target_idx] = NormalizedOptionQuote(
            strike=original.strike,
            call=original.call,
            put=from_snapshot_quote(bid=49.99, ask=50.01),
        )
        return chain

    def test_gate_drops_dominant_strike_and_recomputes(self):
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                   102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]
        inflated = self._inflated_chain(
            spot, sigma_true, T_years, rate, strikes, target_strike=95
        )

        _, prov_gated = replicate_expiry_variance_with_provenance(
            inflated, rate=rate, T_years=T_years,
            dominance_gate_threshold=0.50,
            dominance_gate_max_iterations=2,
        )

        # Gate fired at least once — and the post-gate share is below
        # threshold (the recompute should have flattened the dominator).
        assert prov_gated.single_strike_dropped >= 1
        assert prov_gated.single_strike_hard_failed is False
        assert prov_gated.max_single_strike_share <= 0.50

    def test_gate_disabled_returns_original_share(self):
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                   102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]
        inflated = self._inflated_chain(
            spot, sigma_true, T_years, rate, strikes, target_strike=95
        )

        _, prov_ungated = replicate_expiry_variance_with_provenance(
            inflated, rate=rate, T_years=T_years,
            dominance_gate_threshold=None,
        )

        # Gate disabled — original (un-mitigated) share leaks through and
        # it exceeds threshold.
        assert prov_ungated.single_strike_dropped == 0
        assert prov_ungated.single_strike_hard_failed is False
        assert prov_ungated.max_single_strike_share > 0.50

    def test_gate_hard_fails_when_floor_blocks_more_drops(self):
        # Tiny chain: dropping below the strike floor should hard-fail
        # rather than cascade-drop to nothing. With min_strikes=8 and a
        # 9-strike chain, one drop is allowed; if the gate would need
        # a second drop, it must hard-fail instead.
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]
        inflated = self._inflated_chain(
            spot, sigma_true, T_years, rate, strikes, target_strike=95
        )

        _, prov = replicate_expiry_variance_with_provenance(
            inflated, rate=rate, T_years=T_years,
            dominance_gate_threshold=0.50,
            dominance_gate_max_iterations=5,
            dominance_gate_min_strikes=9,  # at-floor; first drop is blocked
        )

        # Floor binds immediately: gate cannot drop the dominator without
        # falling below the strike floor.
        assert prov.single_strike_dropped == 0
        assert prov.single_strike_hard_failed is True
        # The pre-gate share is preserved (no drops happened) and is
        # above threshold.
        assert prov.max_single_strike_share > 0.50

    def test_gate_default_threshold_does_not_fire_on_healthy_chain(self):
        """A dense SPY-like chain stays below the 0.50 default threshold.
        The 21-strike grid is dense enough that no single strike (incl.
        K0 with its averaged call+put treatment) crosses the threshold;
        a sparse 11-strike grid is a separate regime. Pins that the
        default-on gate is benign on the chain shape we expect from real
        Polygon snapshots."""
        spot = 100.0
        sigma_true = 0.20
        T_years = 30 / 365.0
        rate = 0.05
        strikes = [60, 65, 70, 75, 80, 85, 90, 92.5, 95, 97.5, 100,
                   102.5, 105, 107.5, 110, 115, 120, 125, 130, 135, 140]
        healthy = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T_years, rate=rate,
            sigma=sigma_true, source="opra_mid",
        )

        _, prov = replicate_expiry_variance_with_provenance(
            healthy, rate=rate, T_years=T_years,
        )

        assert prov.single_strike_dropped == 0
        assert prov.single_strike_hard_failed is False
        assert prov.max_single_strike_share <= 0.50


class TestVixStyleIv30WithProvenance:
    def test_combines_two_expiries_provenance(self):
        spot = 100.0
        sigma_true = 0.20
        rate = 0.05
        T1_d, T2_d = 21, 35
        strikes = [60, 70, 80, 90, 95, 100, 105, 110, 120, 130, 140]
        chain1 = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T1_d / 365.0, rate=rate,
            sigma=sigma_true, source="opra_mid",
        )
        chain2 = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T2_d / 365.0, rate=rate,
            sigma=sigma_true, source="synthetic_close_proxy",
        )
        sigma30, prov = vix_style_iv30_with_provenance(
            chain1, chain2,
            rate1=rate, T1_calendar_days=T1_d,
            rate2=rate, T2_calendar_days=T2_d,
            target_calendar_days=30,
        )
        # Sparse 11-strike BS chain at σ=0.20 — replication is within ~150 bps.
        # Tighter assertion lives in test_recovers_sigma_with_two_straddling_expiries
        # (21 strikes, 50 bps).
        assert abs(sigma30 - sigma_true) < 0.015
        assert prov.iv_source == "internal_solver"
        # Half opra (expiry1), half synthetic (expiry2): combined synth share
        # is between 0 and 1. The exact value depends on the variance-time
        # weights; assert the bounds.
        assert 0.1 < prov.variance_contribution_synthetic < 0.9
        # Both sources show up in the combined mix.
        assert "opra_mid" in prov.price_source_mix
        assert "synthetic_close_proxy" in prov.price_source_mix
        assert sum(prov.price_source_mix.values()) == pytest.approx(1.0)

    def test_combined_max_strike_share_is_max_of_two(self):
        """Worst-case domination across the two expiries surfaces in the
        combined provenance, mirroring ``strike_coverage_score``'s
        worst-case `min` semantics."""
        from app.volatility.price_normalization import (
            NormalizedOptionQuote,
            from_snapshot_quote,
        )

        spot = 100.0
        rate = 0.05
        T1_d, T2_d = 21, 35
        strikes = [60, 70, 80, 90, 95, 100, 105, 110, 120, 130, 140]

        # expiry1 is healthy; expiry2 has one inflated K=95 put.
        chain1 = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T1_d / 365.0, rate=rate,
            sigma=0.20, source="opra_mid",
        )
        chain2 = _bs_normalized_chain(
            spot=spot, strikes=strikes, T_years=T2_d / 365.0, rate=rate,
            sigma=0.20, source="opra_mid",
        )
        target_idx = strikes.index(95)
        original = chain2[target_idx]
        chain2[target_idx] = NormalizedOptionQuote(
            strike=original.strike,
            call=original.call,
            put=from_snapshot_quote(bid=49.99, ask=50.01),
        )

        # Gate disabled — this test pins the metric, not the gate.
        _, prov1 = replicate_expiry_variance_with_provenance(
            chain1, rate=rate, T_years=T1_d / 365.0,
            dominance_gate_threshold=None,
        )
        _, prov2 = replicate_expiry_variance_with_provenance(
            chain2, rate=rate, T_years=T2_d / 365.0,
            dominance_gate_threshold=None,
        )
        # The inflated expiry has a higher max-share than the healthy one.
        assert prov2.max_single_strike_share > prov1.max_single_strike_share

        _, combined = vix_style_iv30_with_provenance(
            chain1, chain2,
            rate1=rate, T1_calendar_days=T1_d,
            rate2=rate, T2_calendar_days=T2_d,
            dominance_gate_threshold=None,
        )
        assert combined.max_single_strike_share == pytest.approx(
            max(prov1.max_single_strike_share, prov2.max_single_strike_share)
        )

    def test_combined_strike_coverage_is_minimum_of_two(self):
        spot = 100.0
        rate = 0.05
        T1_d, T2_d = 21, 35
        # expiry1 has wide wings; expiry2 has narrow wings.
        wide = [50, 60, 70, 80, 90, 95, 100, 105, 110, 120, 130, 140, 150]
        narrow = [90, 95, 100, 105, 110]
        chain1 = _bs_normalized_chain(
            spot=spot, strikes=wide, T_years=T1_d / 365.0, rate=rate,
            sigma=0.20, source="opra_mid",
        )
        chain2 = _bs_normalized_chain(
            spot=spot, strikes=narrow, T_years=T2_d / 365.0, rate=rate,
            sigma=0.20, source="opra_mid",
        )
        _, prov_wide = replicate_expiry_variance_with_provenance(
            chain1, rate=rate, T_years=T1_d / 365.0
        )
        _, prov_narrow = replicate_expiry_variance_with_provenance(
            chain2, rate=rate, T_years=T2_d / 365.0
        )
        assert prov_wide.strike_coverage_score > prov_narrow.strike_coverage_score

        _, combined_prov = vix_style_iv30_with_provenance(
            chain1, chain2,
            rate1=rate, T1_calendar_days=T1_d,
            rate2=rate, T2_calendar_days=T2_d,
        )
        # Combined coverage = min — the narrow expiry drags the IV30 down.
        assert combined_prov.strike_coverage_score == pytest.approx(prov_narrow.strike_coverage_score)


@pytest.mark.skipif(
    not GOLDEN_FIXTURE.exists(),
    reason=f"golden fixture {GOLDEN_FIXTURE} not populated",
)
class TestSpyGoldenFixtureWithProvenance:
    """The SPY 2024-12-20 fixture is built end-to-end from EOD close prices,
    so every leg is ``synthetic_close_proxy`` and the variance-contribution-
    weighted synthetic share is exactly 1.0.
    """

    def test_replication_with_provenance_matches_meta_sidecar(self):
        import json

        import pandas as pd

        df = pd.read_parquet(GOLDEN_FIXTURE)
        meta = json.loads(GOLDEN_FIXTURE.with_suffix(".meta.json").read_text())
        below = int(meta["straddle"]["below_30d"])
        above = int(meta["straddle"]["above_30d"])

        chain1 = _normalized_quotes_from_meta_window(df, below)
        chain2 = _normalized_quotes_from_meta_window(df, above)
        sigma_vix, prov = vix_style_iv30_with_provenance(
            chain1, chain2,
            rate1=meta["rate"], T1_calendar_days=below,
            rate2=meta["rate"], T2_calendar_days=above,
            target_calendar_days=30,
        )
        # Math: deterministic recomputation of the meta sidecar value.
        assert abs(sigma_vix - float(meta["vix_style_iv30_act365"])) < 1e-9

        # Provenance: end-to-end synthetic.
        assert prov.variance_contribution_synthetic == pytest.approx(1.0)
        assert prov.price_source_mix.get("synthetic_close_proxy", 0.0) == pytest.approx(1.0)
        # SPY 2024-12-20 surviving chain extends ~2.5σ each side after the
        # per-strike-rule wing truncation kills small far-OTM legs that share
        # a strike with deep-ITM legs. Score lands ~0.50.
        assert prov.strike_coverage_score >= 0.5
