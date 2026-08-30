"""Validation tests for app.data_lake.types Pydantic models.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.1, § 4.2
Issue: #1877 (PR D of #1861) — start_trading_date_ms/end_trading_date_ms.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.config import settings
from app.data_lake.root_identity import LEGACY_ROOT_ID
from app.data_lake.types import (
    CALENDAR_ANCHOR_UTC_HOUR,
    ArtifactIdentity,
    ArtifactRecord,
    DataRunSpec,
    calendar_anchor_ms_to_trading_date,
    trading_date_to_calendar_anchor_ms,
)

_EXPLICIT_ROOT = UUID("44444444-4444-4444-4444-444444444444")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


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


class TestCalendarAnchorHelpers:
    """The forward/inverse pair anchoring a POST-body trading date at
    12:00:00.000 UTC — a deliberate, documented exception to the
    session-open (09:30 ET) anchor the rest of the lake's wire vocabulary
    uses (trading_date_at_ms / session_open_ms_utc)."""

    def test_anchor_hour_is_noon_utc(self):
        assert CALENDAR_ANCHOR_UTC_HOUR == 12

    def test_forward_then_inverse_round_trips(self):
        d = date(2024, 5, 20)
        ms = trading_date_to_calendar_anchor_ms(d)
        assert calendar_anchor_ms_to_trading_date(ms) == d

    def test_forward_produces_the_documented_epoch_ms_example(self):
        # 2026-08-01 12:00:00 UTC — the issue's own worked example value for
        # start_trading_date_ms (int64 ms UTC, noon anchor).
        ms = trading_date_to_calendar_anchor_ms(date(2026, 8, 1))
        assert ms == 1785585600000

    def test_est_date_round_trips(self):
        """A date wholly inside EST (UTC-5) — the anchor is fixed UTC, so
        the local NY offset must not perturb it."""
        d = date(2026, 1, 15)
        assert calendar_anchor_ms_to_trading_date(trading_date_to_calendar_anchor_ms(d)) == d

    def test_edt_date_round_trips(self):
        """A date wholly inside EDT (UTC-4) — same invariant across the
        DST boundary the fixed-UTC anchor is designed to ignore."""
        d = date(2026, 7, 15)
        assert calendar_anchor_ms_to_trading_date(trading_date_to_calendar_anchor_ms(d)) == d

    def test_weekend_date_round_trips(self):
        """A calendar-range boundary may legitimately land on a weekend —
        this is a calendar anchor, not a session anchor, so there is no
        session to require."""
        d = date(2026, 8, 30)  # Sunday
        assert d.weekday() == 6
        assert calendar_anchor_ms_to_trading_date(trading_date_to_calendar_anchor_ms(d)) == d

    def test_holiday_date_round_trips(self):
        d = date(2026, 12, 25)  # Christmas (NYSE holiday)
        assert calendar_anchor_ms_to_trading_date(trading_date_to_calendar_anchor_ms(d)) == d

    def test_off_anchor_ms_is_rejected(self):
        """One millisecond off the canonical noon-UTC anchor must be
        refused, not silently snapped to the nearest date."""
        ms = trading_date_to_calendar_anchor_ms(date(2024, 5, 20)) + 1
        with pytest.raises(ValueError):
            calendar_anchor_ms_to_trading_date(ms)

    def test_midnight_utc_is_rejected(self):
        """Midnight UTC is the obvious-but-wrong anchor (would shift the
        date west of UTC) — it must be rejected like any other off-anchor
        value, not silently accepted as an alternate anchor."""
        midnight_ms = trading_date_to_calendar_anchor_ms(date(2024, 5, 20)) - CALENDAR_ANCHOR_UTC_HOUR * 3_600_000
        with pytest.raises(ValueError):
            calendar_anchor_ms_to_trading_date(midnight_ms)

    def test_session_open_anchor_is_rejected(self):
        """09:30 ET is the *other* endpoint's anchor (GET /coverage) — it
        must not be silently accepted here as if the two conventions were
        interchangeable."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        with pytest.raises(ValueError):
            calendar_anchor_ms_to_trading_date(session_open_ms_utc(date(2024, 5, 20)))

    def test_value_above_int64_max_is_rejected(self):
        with pytest.raises(ValueError):
            calendar_anchor_ms_to_trading_date(_INT64_MAX + 1)

    def test_value_below_int64_min_is_rejected(self):
        with pytest.raises(ValueError):
            calendar_anchor_ms_to_trading_date(_INT64_MIN - 1)


class TestDataRunSpec:
    def _valid_payload(self) -> dict:
        return {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "run_type": "python_lab",
            "symbols": ["SPY"],
            "start_trading_date_ms": trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
            "end_trading_date_ms": trading_date_to_calendar_anchor_ms(date(2024, 5, 24)),
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

    def test_start_and_end_trading_date_properties_derive_from_ms(self):
        spec = DataRunSpec(**self._valid_payload())
        assert spec.start_trading_date == date(2024, 5, 20)
        assert spec.end_trading_date == date(2024, 5, 24)

    def test_lowercase_symbol_is_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = ["spy"]
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_start_after_end_is_rejected(self):
        """Reversed range: start after end."""
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2024, 5, 24))
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2024, 5, 20))
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_empty_symbols_rejected(self):
        payload = self._valid_payload()
        payload["symbols"] = []
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_lean_image_digest_required(self):
        """lean_image_digest has no default; omitting it must be a ValidationError."""
        payload = self._valid_payload()
        del payload["lean_image_digest"]
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
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2018, 1, 1))
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2024, 12, 31))  # ~7 years
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    # --- #1877 boundary matrix -------------------------------------------

    def test_old_field_names_are_rejected(self):
        """The pre-#1877 ISO-date field names must be refused outright —
        no compatibility alias, no dual-model transcription."""
        payload = {
            "request_id": "12345678-1234-5678-1234-567812345678",
            "run_type": "python_lab",
            "symbols": ["SPY"],
            "start_trading_date": "2024-05-20",
            "end_trading_date": "2024-05-24",
            "lean_image_digest": "sha256:abc123",
        }
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_iso_strings_on_the_ms_fields_are_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = "2024-05-20"
        payload["end_trading_date_ms"] = "2024-05-24"
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_non_integer_values_are_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = 1716206400000.5
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_values_outside_signed_int64_are_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = _INT64_MAX + 1
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_off_anchor_milliseconds_are_rejected(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = payload["start_trading_date_ms"] + 1
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_numeric_string_value_is_rejected(self):
        """A numeric string that would parse to an on-anchor ms value must
        still be refused — the wire type is int64, not a string that
        happens to parse to the right number."""
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = str(payload["start_trading_date_ms"])
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_integral_float_value_is_rejected(self):
        """An integral float (e.g. 1716206400000.0) that would parse to an
        on-anchor ms value must still be refused — Pydantic's lax int
        coercion would otherwise silently accept it."""
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = float(payload["start_trading_date_ms"])
        with pytest.raises(ValidationError):
            DataRunSpec(**payload)

    def test_est_date_range_accepted(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 1, 12))
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 1, 16))
        spec = DataRunSpec(**payload)
        assert spec.start_trading_date == date(2026, 1, 12)

    def test_edt_date_range_accepted(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 7, 13))
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 7, 17))
        spec = DataRunSpec(**payload)
        assert spec.end_trading_date == date(2026, 7, 17)

    def test_weekend_boundary_accepted(self):
        """The window may legitimately start or end on a weekend — this is
        a calendar-range boundary, not a session requirement."""
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 8, 29))  # Saturday
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 8, 30))  # Sunday
        spec = DataRunSpec(**payload)
        assert spec.start_trading_date.weekday() == 5

    def test_holiday_boundary_accepted(self):
        payload = self._valid_payload()
        payload["start_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 12, 25))
        payload["end_trading_date_ms"] = trading_date_to_calendar_anchor_ms(date(2026, 12, 25))
        spec = DataRunSpec(**payload)
        assert spec.start_trading_date == date(2026, 12, 25)
