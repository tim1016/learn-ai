"""Tests for the IvProvenance dataclass (Step A of IV-ownership plan).

Asserts the dataclass-level invariants. The variance-contribution-weighted
synthetic share and strike-coverage scores are computed downstream by
``replicate_expiry_variance_with_provenance`` (tested in
``test_vix_replication.py``); this file pins the contract itself.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.volatility.iv_provenance import IvProvenance


class TestIvProvenanceContract:
    def test_minimal_construction_succeeds(self):
        prov = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 1.0},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.95,
        )
        assert prov.iv_source == "internal_solver"
        assert prov.per_strike_contributions is None

    def test_variance_contribution_outside_unit_interval_rejected(self):
        with pytest.raises(ValueError, match="variance_contribution_synthetic"):
            IvProvenance(
                iv_source="internal_solver",
                price_source_mix={"opra_mid": 1.0},
                variance_contribution_synthetic=1.5,
                strike_coverage_score=0.5,
            )

    def test_strike_coverage_outside_unit_interval_rejected(self):
        with pytest.raises(ValueError, match="strike_coverage_score"):
            IvProvenance(
                iv_source="internal_solver",
                price_source_mix={"opra_mid": 1.0},
                variance_contribution_synthetic=0.0,
                strike_coverage_score=-0.1,
            )

    def test_max_single_strike_share_default_is_zero(self):
        # Backwards-compat: existing constructors that don't pass this
        # diagnostic field should still build cleanly.
        prov = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 1.0},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.95,
        )
        assert prov.max_single_strike_share == 0.0

    def test_max_single_strike_share_outside_unit_interval_rejected(self):
        with pytest.raises(ValueError, match="max_single_strike_share"):
            IvProvenance(
                iv_source="internal_solver",
                price_source_mix={"opra_mid": 1.0},
                variance_contribution_synthetic=0.0,
                strike_coverage_score=0.5,
                max_single_strike_share=1.5,
            )

    def test_price_source_mix_must_sum_to_one(self):
        with pytest.raises(ValueError, match=r"must sum to 1\.0"):
            IvProvenance(
                iv_source="internal_solver",
                price_source_mix={"opra_mid": 0.4, "synthetic_close_proxy": 0.4},
                variance_contribution_synthetic=0.4,
                strike_coverage_score=0.5,
            )

    def test_empty_price_source_mix_allowed(self):
        # Sum of 0.0 represents "no legs survived wing truncation" — a
        # separate failure that the caller may want to flag, but the
        # provenance dataclass itself accepts it.
        IvProvenance(
            iv_source="internal_solver",
            price_source_mix={},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.0,
        )

    def test_per_strike_contributions_optional(self):
        prov = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 0.6, "synthetic_close_proxy": 0.4},
            variance_contribution_synthetic=0.35,
            strike_coverage_score=0.9,
            per_strike_contributions=[
                {"strike": 100.0, "kind": "both", "c_i": 0.001},
            ],
        )
        assert prov.per_strike_contributions is not None
        assert len(prov.per_strike_contributions) == 1


class TestRoundTripSerialization:
    def test_round_trip_preserves_fields(self):
        original = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 0.6, "synthetic_close_proxy": 0.4},
            variance_contribution_synthetic=0.35,
            strike_coverage_score=0.9,
            per_strike_contributions=None,
        )
        d = asdict(original)
        restored = IvProvenance(**d)
        assert restored == original

    def test_round_trip_with_debug_payload(self):
        debug = [
            {"strike": 95.0, "kind": "put", "c_i": 0.0008},
            {"strike": 100.0, "kind": "both", "c_i": 0.0009},
            {"strike": 105.0, "kind": "call", "c_i": 0.0007},
        ]
        original = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 1.0},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.85,
            per_strike_contributions=debug,
        )
        d = asdict(original)
        restored = IvProvenance(**d)
        assert restored == original
        assert restored.per_strike_contributions == debug
