"""Validation tests for app.data_lake.types Pydantic models.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.data_lake.types import DataRunSpec


class TestDataRunSpec:
    def _valid_payload(self) -> dict:
        return {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "run_type": "python_lab",
            "symbols": ["SPY"],
            "start_trading_date": "2024-05-20",
            "end_trading_date": "2024-05-24",
            "lean_image_digest": "sha256:abc123",
        }

    def test_minimal_valid_spec(self):
        spec = DataRunSpec(**self._valid_payload())
        assert spec.market == "usa"
        assert spec.symbols == ["SPY"]
        assert spec.resolution == "minute"
        assert spec.data_types == ["trade"]
        assert spec.price_adjustment_mode == "raw"
        assert spec.provider == "polygon"
        assert spec.include_factor_files is True
        assert spec.fetch_timeout_seconds == 600

    def test_lowercase_symbol_is_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = ["spy"]
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_start_after_end_is_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date"] = "2024-05-24"
        payload["end_trading_date"] = "2024-05-20"
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_empty_symbols_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = []
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_lean_image_digest_required(self):
        """lean_image_digest has no default; omitting it must be a ValidationError."""
        payload = {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "run_type": "python_lab",
            "symbols": ["SPY"],
            "start_trading_date": "2024-05-20",
            "end_trading_date": "2024-05-24",
            # lean_image_digest intentionally absent
        }
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_quote_without_trade_rejected(self):
        """data_types=['quote'] without 'trade' must be rejected.

        Quote artifacts are derived from same-day trade bytes; without a source
        trade artifact, quote synthesis cannot proceed.
        """
        payload = self._valid_payload()
        payload["data_types"] = ["quote"]
        with pytest.raises(ValidationError, match="trade"):
            DataRunSpec(**payload)

    def test_quote_with_trade_accepted(self):
        payload = self._valid_payload()
        payload["data_types"] = ["trade", "quote"]
        spec = DataRunSpec(**payload)
        assert "quote" in spec.data_types
        assert "trade" in spec.data_types

    def test_5_year_range_cap(self):
        payload = self._valid_payload()
        payload["start_trading_date"] = "2018-01-01"
        payload["end_trading_date"] = "2024-12-31"  # ~7 years
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)
