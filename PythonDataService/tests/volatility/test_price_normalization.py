"""Tests for the typed price-normalization contract (Step A of IV-ownership plan).

Asserts the architectural commitments:

1. You cannot construct ``NormalizedOptionPrice`` without a source tag (the
   dataclass requires ``source`` as a positional/named field — there is no
   default).
2. ``from_eod_close`` records the rule string so the synthesis is
   reproducible without reading the build script.
3. Constructors are source-explicit — no polymorphic adapter.
4. ``quality_score = 1 - half_spread / mid`` clamped to [0, 1] for any source.
5. Round-trip through dict (the JSONB shape Step D will store) preserves
   every field.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.volatility.price_normalization import (
    DEFAULT_HALF_SPREAD_RULE,
    NormalizedOptionPrice,
    NormalizedOptionQuote,
    from_eod_close,
    from_recorded_snapshot,
    from_snapshot_quote,
)


class TestNormalizedOptionPriceContract:
    def test_source_is_required_at_construction(self):
        # The dataclass has no default for source; missing it raises TypeError.
        with pytest.raises(TypeError, match="source"):
            NormalizedOptionPrice(  # type: ignore[call-arg]
                mid=1.0,
                spread_estimate=0.05,
                spread_synthetic=False,
                half_spread_rule=None,
                quality_score=0.95,
            )

    def test_negative_mid_rejected(self):
        with pytest.raises(ValueError, match="mid must be >= 0"):
            NormalizedOptionPrice(
                mid=-0.01,
                source="opra_mid",
                spread_estimate=0.05,
                spread_synthetic=False,
                half_spread_rule=None,
                quality_score=0.5,
            )

    def test_quality_score_outside_unit_interval_rejected(self):
        with pytest.raises(ValueError, match="quality_score must be in"):
            NormalizedOptionPrice(
                mid=1.0,
                source="opra_mid",
                spread_estimate=0.05,
                spread_synthetic=False,
                half_spread_rule=None,
                quality_score=1.5,
            )

    def test_synthetic_spread_requires_rule(self):
        with pytest.raises(ValueError, match="requires a half_spread_rule"):
            NormalizedOptionPrice(
                mid=1.0,
                source="synthetic_close_proxy",
                spread_estimate=0.05,
                spread_synthetic=True,
                half_spread_rule=None,
                quality_score=0.95,
            )

    def test_is_zero_bid_property(self):
        zero = NormalizedOptionPrice(
            mid=0.0,
            source="opra_mid",
            spread_estimate=None,
            spread_synthetic=False,
            half_spread_rule=None,
            quality_score=0.0,
        )
        non_zero = NormalizedOptionPrice(
            mid=0.50,
            source="opra_mid",
            spread_estimate=0.01,
            spread_synthetic=False,
            half_spread_rule=None,
            quality_score=0.98,
        )
        assert zero.is_zero_bid is True
        assert non_zero.is_zero_bid is False


class TestFromSnapshotQuote:
    def test_real_quote_records_real_spread(self):
        nop = from_snapshot_quote(bid=1.20, ask=1.30)
        assert nop.source == "opra_mid"
        assert nop.mid == pytest.approx(1.25)
        assert nop.spread_estimate == pytest.approx(0.05)
        assert nop.spread_synthetic is False
        assert nop.half_spread_rule is None
        # quality = 1 - 0.05/1.25 = 0.96
        assert nop.quality_score == pytest.approx(0.96)

    def test_zero_ask_collapses_to_quality_zero(self):
        nop = from_snapshot_quote(bid=0.0, ask=0.0)
        assert nop.mid == 0.0
        assert nop.is_zero_bid
        assert nop.quality_score == 0.0
        assert nop.spread_estimate is None

    def test_zero_bid_with_positive_ask_is_zero_bid(self):
        # Matches legacy OptionQuote semantics: a leg with bid=0 is not a
        # real quote, no matter what the ask side says. This makes the
        # NormalizedOptionPrice.is_zero_bid check the right truncation
        # trigger for VIX-style replication.
        nop = from_snapshot_quote(bid=0.0, ask=0.50)
        assert nop.is_zero_bid
        assert nop.mid == 0.0

    def test_inverted_book_treated_as_zero_bid(self):
        nop = from_snapshot_quote(bid=2.0, ask=1.0)
        assert nop.mid == 0.0
        assert nop.quality_score == 0.0


class TestFromRecordedSnapshot:
    def test_records_recorded_source_tag(self):
        nop = from_recorded_snapshot(bid=1.20, ask=1.30)
        assert nop.source == "opra_mid_recorded"
        assert nop.mid == pytest.approx(1.25)
        assert nop.spread_synthetic is False


class TestFromEodClose:
    def test_records_default_rule_string(self):
        nop = from_eod_close(close=10.0)
        assert nop.source == "synthetic_close_proxy"
        assert nop.spread_synthetic is True
        assert nop.half_spread_rule == DEFAULT_HALF_SPREAD_RULE
        # half = max(0.05, 0.005 * 10.0) = 0.05
        assert nop.spread_estimate == pytest.approx(0.05)
        # quality = 1 - 0.05/10.0 = 0.995
        assert nop.quality_score == pytest.approx(0.995)

    def test_large_close_uses_percentage_branch(self):
        nop = from_eod_close(close=100.0)
        # half = max(0.05, 0.005 * 100.0) = 0.5
        assert nop.spread_estimate == pytest.approx(0.5)
        assert nop.quality_score == pytest.approx(0.995)

    def test_small_close_uses_floor_branch(self):
        nop = from_eod_close(close=1.00)
        # half = max(0.05, 0.005 * 1.00) = 0.05
        assert nop.spread_estimate == pytest.approx(0.05)
        # quality = 1 - 0.05/1.0 = 0.95
        assert nop.quality_score == pytest.approx(0.95)

    def test_zero_bid_threshold(self):
        nop = from_eod_close(close=0.04)
        assert nop.is_zero_bid
        assert nop.mid == 0.0
        assert nop.quality_score == 0.0
        # The rule string is recorded even on zero-bid for traceability.
        assert nop.half_spread_rule == DEFAULT_HALF_SPREAD_RULE

    def test_custom_rule_string_is_preserved(self):
        nop = from_eod_close(close=10.0, rule="my-bespoke-rule")
        assert nop.half_spread_rule == "my-bespoke-rule"

    def test_negative_close_rejected(self):
        with pytest.raises(ValueError, match="close must be >= 0"):
            from_eod_close(close=-1.0)


class TestRoundTripSerialization:
    """`asdict` is the bridge to the recorder's JSONB column."""

    def test_real_quote_round_trip_preserves_all_fields(self):
        original = from_snapshot_quote(bid=1.20, ask=1.30)
        d = asdict(original)
        restored = NormalizedOptionPrice(**d)
        assert restored == original

    def test_eod_synthetic_round_trip_preserves_rule_string(self):
        original = from_eod_close(close=12.34, rule="custom-rule")
        d = asdict(original)
        assert d["half_spread_rule"] == "custom-rule"
        assert d["spread_synthetic"] is True
        restored = NormalizedOptionPrice(**d)
        assert restored == original


class TestNormalizedOptionQuote:
    def test_pair_construction(self):
        call = from_snapshot_quote(bid=1.20, ask=1.30)
        put = from_snapshot_quote(bid=0.45, ask=0.50)
        nq = NormalizedOptionQuote(strike=100.0, call=call, put=put)
        assert nq.strike == 100.0
        assert nq.call.source == "opra_mid"
        assert nq.put.source == "opra_mid"

    def test_zero_strike_rejected(self):
        call = from_snapshot_quote(1.20, 1.30)
        put = from_snapshot_quote(0.45, 0.50)
        with pytest.raises(ValueError, match="strike must be > 0"):
            NormalizedOptionQuote(strike=0.0, call=call, put=put)

    def test_mixed_sources_per_strike_allowed(self):
        # A real call leg + a synthesized put leg on the same strike is
        # legal — the variance-contribution-weighted measure handles the
        # mixed case correctly.
        nq = NormalizedOptionQuote(
            strike=100.0,
            call=from_snapshot_quote(1.20, 1.30),
            put=from_eod_close(0.50),
        )
        assert nq.call.source == "opra_mid"
        assert nq.put.source == "synthetic_close_proxy"
