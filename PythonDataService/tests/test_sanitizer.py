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

    def test_duplicates_raise_error(self):
        """Duplicate timestamps must raise ValueError (fail-fast per ADR 0022 (h))."""
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
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 155.0,
                "low": 148.0,
                "close": 153.0,
                "volume": 1000000.0,
            },
        ]

        with pytest.raises(ValueError, match="Duplicate timestamps detected"):
            DataSanitizer.sanitize_aggregates(raw)

    def test_invalid_high_low_filtered(self):
        """Bars where high < low should be removed by the integrity filter"""
        raw = [
            {
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 140.0,
                "low": 155.0,
                "close": 153.0,
                "volume": 1000000.0,
            },
        ]

        result = DataSanitizer.sanitize_aggregates(raw)

        assert result["summary"]["cleaned_count"] == 0

    def test_negative_volume_filtered(self):
        raw = [
            {"timestamp": 1704067200000, "open": 150.0, "high": 155.0, "low": 148.0, "close": 153.0, "volume": -100.0},
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

    def test_out_of_order_timestamps_raise_error(self):
        raw = [
            {
                "timestamp": 1704153600000,
                "open": 153.0,
                "high": 158.0,
                "low": 151.0,
                "close": 157.0,
                "volume": 900000.0,
            },
            {
                "timestamp": 1704067200000,
                "open": 150.0,
                "high": 155.0,
                "low": 148.0,
                "close": 153.0,
                "volume": 1000000.0,
            },
        ]

        with pytest.raises(ValueError, match="strictly increasing"):
            DataSanitizer.sanitize_aggregates(raw)

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
