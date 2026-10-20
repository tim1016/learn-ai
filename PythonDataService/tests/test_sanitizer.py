"""Tests for the DataSanitizer service"""

import pytest

from app.services.sanitizer import DataSanitizer


class TestSanitizeAggregates:
    def test_empty_input_returns_empty(self):
        result = DataSanitizer.sanitize_aggregates([])

        assert result["data"] == []
        assert result["summary"]["original_count"] == 0
        assert result["summary"]["cleaned_count"] == 0

    def test_valid_ohlcv_data_retained(self):
        raw = [
            {
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 155.0,
                "low": 148.0,
                "close": 153.0,
                "volume": 1000000.0,
            },
            {
                "timestamp": 1704153600000,
                "open": 153.0,
                "high": 158.0,
                "low": 151.0,
                "close": 157.0,
                "volume": 900000.0,
            },
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result["summary"]["original_count"] == 2
        assert result["summary"]["cleaned_count"] == 2
        assert result["summary"]["removed_count"] == 0
        assert len(result["data"]) == 2

    @pytest.mark.parametrize(
        ("bars", "match"),
        [
            # Duplicate timestamps must raise ValueError (fail-fast per ADR 0022 (h)).
            pytest.param(
                (
                    (1704067200000, 150.0, 155.0, 148.0, 153.0, 1000000.0),
                    (1704067200000, 150.0, 155.0, 148.0, 153.0, 1000000.0),
                ),
                "Duplicate timestamps detected",
                id="duplicates_raise_error",
            ),
            pytest.param(
                (
                    (1704153600000, 153.0, 158.0, 151.0, 157.0, 900000.0),
                    (1704067200000, 150.0, 155.0, 148.0, 153.0, 1000000.0),
                ),
                "strictly increasing",
                id="out_of_order_timestamps_raise_error",
            ),
        ],
    )
    def test_bad_timestamp_sequence_raises_error(self, bars, match):
        fields = ("timestamp", "open", "high", "low", "close", "volume")
        raw = [dict(zip(fields, bar, strict=True)) for bar in bars]

        with pytest.raises(ValueError, match=match):
            DataSanitizer.sanitize_aggregates(raw)

    @pytest.mark.parametrize(
        ("high", "low", "volume"),
        [
            # Bars where high < low should be removed by the integrity filter
            pytest.param(140.0, 155.0, 1000000.0, id="invalid_high_low_filtered"),
            pytest.param(155.0, 148.0, -100.0, id="negative_volume_filtered"),
        ],
    )
    def test_integrity_filter_drops_bar(self, high, low, volume):
        raw = [
            {"timestamp": 1704067200000, "open": 150.0, "high": high, "low": low, "close": 153.0, "volume": volume},
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result["summary"]["cleaned_count"] == 0

    def test_timestamps_returned_as_int64_ms(self):
        """Timestamps must be returned as int64 ms UTC (canonical wire format)."""
        raw = [
            {
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 155.0,
                "low": 148.0,
                "close": 153.0,
                "volume": 1000000.0,
            }
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        ts = result["data"][0]["timestamp"]
        assert isinstance(ts, (int, float)), f"Expected int64 ms, got {type(ts)}: {ts}"
        assert int(ts) == 1704067200000, f"Timestamp should round-trip exactly, got {ts}"

    def test_vwap_and_transactions_optional(self):
        """Bars with optional fields should still be processed"""
        raw = [
            {
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 155.0,
                "low": 148.0,
                "close": 153.0,
                "volume": 1000000.0,
                "vwap": 152.5,
                "transactions": 5000,
            },
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result["summary"]["cleaned_count"] == 1
