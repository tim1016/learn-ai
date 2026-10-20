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
    @pytest.mark.parametrize(
        ("make_overrides", "match"),
        [
            pytest.param(
                lambda: {"variance_contribution_synthetic": 1.5},
                "variance_contribution_synthetic",
                id="variance_contribution_outside_unit_interval_rejected",
            ),
            pytest.param(
                lambda: {"strike_coverage_score": -0.1},
                "strike_coverage_score",
                id="strike_coverage_outside_unit_interval_rejected",
            ),
            pytest.param(
                lambda: {"max_single_strike_share": 1.5},
                "max_single_strike_share",
                id="max_single_strike_share_outside_unit_interval_rejected",
            ),
            pytest.param(
                lambda: {
                    "price_source_mix": {"opra_mid": 0.4, "synthetic_close_proxy": 0.4},
                    "variance_contribution_synthetic": 0.4,
                },
                r"must sum to 1\.0",
                id="price_source_mix_must_sum_to_one",
            ),
        ],
    )
    def test_invalid_field_rejected(self, make_overrides, match):
        fields = {
            "iv_source": "internal_solver",
            "price_source_mix": {"opra_mid": 1.0},
            "variance_contribution_synthetic": 0.0,
            "strike_coverage_score": 0.5,
        }
        with pytest.raises(ValueError, match=match):
            IvProvenance(**{**fields, **make_overrides()})

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


class TestRoundTripSerialization:
    @pytest.mark.parametrize(
        "make_fields",
        [
            pytest.param(
                lambda: {
                    "price_source_mix": {"opra_mid": 0.6, "synthetic_close_proxy": 0.4},
                    "variance_contribution_synthetic": 0.35,
                    "strike_coverage_score": 0.9,
                    "per_strike_contributions": None,
                },
                id="round_trip_preserves_fields",
            ),
            pytest.param(
                lambda: {
                    "price_source_mix": {"opra_mid": 1.0},
                    "variance_contribution_synthetic": 0.0,
                    "strike_coverage_score": 0.85,
                    "per_strike_contributions": [
                        {"strike": 95.0, "kind": "put", "c_i": 0.0008},
                        {"strike": 100.0, "kind": "both", "c_i": 0.0009},
                        {"strike": 105.0, "kind": "call", "c_i": 0.0007},
                    ],
                },
                id="round_trip_with_debug_payload",
            ),
        ],
    )
    def test_round_trip(self, make_fields):
        fields = make_fields()
        original = IvProvenance(iv_source="internal_solver", **fields)
        d = asdict(original)
        restored = IvProvenance(**d)
        assert restored == original, "round trip through asdict preserves the record"
        assert restored.per_strike_contributions == fields["per_strike_contributions"], (
            "per-strike contributions survive the round trip"
        )
