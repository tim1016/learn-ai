"""IV30 stability suite (Step 6 of IV-RV alignment).

These tests pin the regime-feature reproducibility contract: IV30 must not
jump on small perturbations of the chain, otherwise downstream HMM / k-means
features chase noise and refits become unstable across days.

Thresholds (per the locked plan):
- Round-trip: < 1 bp
- 5% random drop: < 10 bps
- Half-resolution strike grid: < 20 bps
"""

from __future__ import annotations

import pytest

from app.services.bs_greeks import bs_european_price
from app.volatility.iv30_health import (
    compute_iv30_health,
    compute_iv30_health_normalized,
)
from app.volatility.price_normalization import (
    NormalizedOptionQuote,
    from_snapshot_quote,
)
from app.volatility.vix_replication import OptionQuote, vix_style_iv30


def _bs_chain(
    spot: float,
    strikes: list[float],
    T_years: float,
    rate: float,
    sigma: float,
    half_spread: float = 0.01,
) -> list[OptionQuote]:
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


# Common test fixture: dense strike grid (≈ real-SPY density: 1.0-wide steps
# inside ±2σ, 5.0-wide in the wings). Two expiries straddle 30 days.
SPOT = 100.0
SIGMA_TRUE = 0.20
RATE = 0.05
STRIKES = (
    [60.0, 65.0, 70.0, 75.0]
    + [80.0 + 0.5 * i for i in range(81)]  # 80.0, 80.5, ..., 120.0
    + [125.0, 130.0, 135.0, 140.0]
)
T1_DAYS = 21
T2_DAYS = 35


@pytest.fixture(scope="module")
def chain1() -> list[OptionQuote]:
    return _bs_chain(SPOT, STRIKES, T1_DAYS / 365.0, RATE, SIGMA_TRUE)


@pytest.fixture(scope="module")
def chain2() -> list[OptionQuote]:
    return _bs_chain(SPOT, STRIKES, T2_DAYS / 365.0, RATE, SIGMA_TRUE)


@pytest.fixture(scope="module")
def baseline_iv30(chain1: list[OptionQuote], chain2: list[OptionQuote]) -> float:
    return vix_style_iv30(
        chain1, chain2,
        rate1=RATE, T1_calendar_days=T1_DAYS,
        rate2=RATE, T2_calendar_days=T2_DAYS,
    )


class TestRoundTrip:
    def test_iv30_to_chain_to_iv30_within_1bp(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote], baseline_iv30: float
    ):
        # Reprice each strike in the chain at σ=baseline_iv30, re-build IV30.
        # Because the synthetic chain was built at constant σ, this round-trip
        # is essentially testing that the replication is self-consistent.
        chain1_rt = _bs_chain(SPOT, STRIKES, T1_DAYS / 365.0, RATE, baseline_iv30)
        chain2_rt = _bs_chain(SPOT, STRIKES, T2_DAYS / 365.0, RATE, baseline_iv30)
        iv30_rt = vix_style_iv30(
            chain1_rt, chain2_rt,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )
        diff_bps = abs(iv30_rt - baseline_iv30) * 10000
        assert diff_bps < 1.0, f"round-trip diff {diff_bps:.3f} bps > 1.0"


class TestResampling:
    def test_drop_5pct_strikes_iv30_within_10bps(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote], baseline_iv30: float
    ):
        # Multiple seeds — the requirement is that the *typical* drop stays < 10 bps,
        # not that every random sample does.
        deltas = []
        for seed in [11, 17, 23, 29, 31]:
            health = compute_iv30_health(
                chain1, chain2,
                rate1=RATE, T1_calendar_days=T1_DAYS,
                rate2=RATE, T2_calendar_days=T2_DAYS,
                seed=seed,
            )
            deltas.append(health.delta_resampling_bps)
        median_delta = sorted(deltas)[len(deltas) // 2]
        assert median_delta < 10.0, f"median Δ on 5% resample {median_delta:.2f} bps > 10"


class TestStrikeGrid:
    def test_half_resolution_grid_iv30_within_20bps(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote]
    ):
        health = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )
        assert health.delta_strike_grid_bps < 20.0, (
            f"Δ on half-res grid {health.delta_strike_grid_bps:.2f} bps > 20"
        )


class TestHealthScore:
    def test_score_in_unit_interval(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote]
    ):
        health = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )
        assert 0.0 <= health.score <= 1.0

    def test_clean_chain_score_above_threshold(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote]
    ):
        # Synthetic constant-σ chain should score high (no chain pathologies).
        health = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )
        assert health.score > 0.5, f"clean-chain health {health.score:.3f} below 0.5"

    def test_score_includes_parametric_arm_when_supplied(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote], baseline_iv30: float
    ):
        # Pass parametric_iv30 == replication → arm score = 1.0; without it the
        # composite is averaged over only resampling + grid.
        with_arm = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
            parametric_iv30=baseline_iv30,
        )
        without_arm = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )
        assert with_arm.parametric_vs_replication_score == pytest.approx(1.0, abs=1e-9)
        assert without_arm.parametric_vs_replication_score is None


class TestIv30TimeOfDayStability:
    """Stability under the same-day chain shifting due to spot move + small IV drift."""

    def test_iv30_tracks_constant_sigma_across_intraday_spot_path(self):
        # Synthesize 4 intraday "snapshots" — same σ, but spot moves by ±0.5%
        # (typical 15-min spot move at 20% vol). IV30 must not jump > 50 bps
        # in absence of any vol move.
        spot_path = [99.5, 100.0, 100.4, 99.9]
        ivs: list[float] = []
        for spot in spot_path:
            c1 = _bs_chain(spot, STRIKES, T1_DAYS / 365.0, RATE, SIGMA_TRUE)
            c2 = _bs_chain(spot, STRIKES, T2_DAYS / 365.0, RATE, SIGMA_TRUE)
            ivs.append(
                vix_style_iv30(
                    c1, c2,
                    rate1=RATE, T1_calendar_days=T1_DAYS,
                    rate2=RATE, T2_calendar_days=T2_DAYS,
                )
            )
        # Max bar-to-bar move:
        max_jump_bps = max(abs(ivs[i] - ivs[i - 1]) for i in range(1, len(ivs))) * 10000
        assert max_jump_bps < 50.0, f"intraday IV30 jump {max_jump_bps:.2f} bps > 50"
        # Recovered σ stays near the truth on every snapshot.
        for iv in ivs:
            assert abs(iv - SIGMA_TRUE) < 0.005, f"snapshot σ={iv:.4f} far from {SIGMA_TRUE}"


def _to_normalized(quote: OptionQuote) -> NormalizedOptionQuote:
    return NormalizedOptionQuote(
        strike=quote.strike,
        call=from_snapshot_quote(quote.call_bid, quote.call_ask),
        put=from_snapshot_quote(quote.put_bid, quote.put_ask),
    )


class TestNormalizedHealthParity:
    """``compute_iv30_health_normalized`` and the legacy ``compute_iv30_health``
    must produce identical numbers on a clean OPRA chain. Pins the recorder
    write path (which uses the normalized variant) to the audited legacy math.
    """

    def test_scores_match_legacy_on_clean_opra_chain(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote]
    ):
        legacy = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )

        norm_chain1 = [_to_normalized(q) for q in chain1]
        norm_chain2 = [_to_normalized(q) for q in chain2]
        normalized = compute_iv30_health_normalized(
            norm_chain1, norm_chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
        )

        # Every component sub-score and the composite agree to 1e-9 — the
        # math is identical, the input wrapper is the only difference.
        assert normalized.score == pytest.approx(legacy.score, abs=1e-9)
        assert normalized.resampling_score == pytest.approx(legacy.resampling_score, abs=1e-9)
        assert normalized.strike_grid_score == pytest.approx(legacy.strike_grid_score, abs=1e-9)
        assert normalized.delta_resampling_bps == pytest.approx(legacy.delta_resampling_bps, abs=1e-9)
        assert normalized.delta_strike_grid_bps == pytest.approx(legacy.delta_strike_grid_bps, abs=1e-9)

    def test_parametric_arm_matches_legacy(
        self, chain1: list[OptionQuote], chain2: list[OptionQuote], baseline_iv30: float
    ):
        legacy = compute_iv30_health(
            chain1, chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
            parametric_iv30=baseline_iv30,
        )
        norm_chain1 = [_to_normalized(q) for q in chain1]
        norm_chain2 = [_to_normalized(q) for q in chain2]
        normalized = compute_iv30_health_normalized(
            norm_chain1, norm_chain2,
            rate1=RATE, T1_calendar_days=T1_DAYS,
            rate2=RATE, T2_calendar_days=T2_DAYS,
            parametric_iv30=baseline_iv30,
        )
        assert normalized.parametric_vs_replication_score == pytest.approx(
            legacy.parametric_vs_replication_score, abs=1e-9
        )
        assert normalized.score == pytest.approx(legacy.score, abs=1e-9)
