"""Validation tests for app.data_lake.types Pydantic models.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from app.config import settings
from app.data_lake.root_identity import LEGACY_ROOT_ID
from app.data_lake.types import ArtifactIdentity, ArtifactRecord, DataRunSpec

_EXPLICIT_ROOT = UUID("44444444-4444-4444-4444-444444444444")


class TestArtifactIdentityDataRootId:
    """Full artifact identity = data_root_id + price_adjustment_mode +
    existing identity dimensions (issue #1876 fixed design decision)."""

    def _identity_kwargs(self) -> dict:
        return {
            "artifact_kind": "time_series_bars",
            "market": "usa",
            "symbol": "SPY",
            "resolution": "minute",
            "data_type": "trade",
            "provider": "polygon",
            "price_adjustment_mode": "raw",
        }

    def test_defaults_to_the_active_root_when_omitted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
        identity = ArtifactIdentity(**self._identity_kwargs())
        assert identity.data_root_id == LEGACY_ROOT_ID

    def test_explicit_value_is_preserved(self):
        identity = ArtifactIdentity(**self._identity_kwargs(), data_root_id=_EXPLICIT_ROOT)
        assert identity.data_root_id == _EXPLICIT_ROOT


class TestArtifactRecordDataRootId:
    def _record_kwargs(self) -> dict:
        return {
            "id": 1,
            "artifact_kind": "time_series_bars",
            "market": "usa",
            "symbol": "SPY",
            "trading_date": None,
            "resolution": "minute",
            "data_type": "trade",
            "provider": "polygon",
            "price_adjustment_mode": "raw",
            "data_contract_hash": "a" * 64,
            "file_path": "equity/usa/minute/spy/x.zip",
            "file_sha256": "b" * 64,
            "row_count": 1,
            "first_bar_start_ms": 0,
            "last_bar_start_ms": 0,
        }

    def test_defaults_to_the_active_root_when_omitted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
        record = ArtifactRecord(**self._record_kwargs())
        assert record.data_root_id == LEGACY_ROOT_ID

    def test_explicit_value_is_preserved(self):
        record = ArtifactRecord(**self._record_kwargs(), data_root_id=_EXPLICIT_ROOT)
        assert record.data_root_id == _EXPLICIT_ROOT


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

    def test_extra_fields_rejected(self):
        """Unknown keys must raise ValidationError (extra='forbid').

        Pydantic's default is to silently ignore extra fields. For DataRunSpec,
        silent drops would allow typos (e.g. 'include_lean_metadata') to fall
        back to defaults without the caller knowing.
        """
        payload = self._valid_payload()
        payload["typo_field"] = "should_be_rejected"
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_5_year_range_cap(self):
        payload = self._valid_payload()
        payload["start_trading_date"] = "2018-01-01"
        payload["end_trading_date"] = "2024-12-31"  # ~7 years
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)
