"""Unit tests for the lean_sidecar_service orchestrator dataclasses."""

from __future__ import annotations

import pytest


def test_trusted_run_request_defaults_to_synthetic_15min_regular_raw() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-defaults",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
    )

    assert req.data_source == "synthetic"
    assert req.bar_minutes == 15
    assert req.session == "regular"
    assert req.adjustment == "raw"


def test_trusted_run_request_accepts_polygon_data_source() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-polygon",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
    )

    assert req.data_source == "polygon"


def test_build_manifest_raises_when_adjusted_disagrees_with_normalization_mode() -> None:
    """data_policy.adjusted=False MUST imply data_normalization_mode='Raw'."""
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_adjustment_vocabulary_consistent,
    )

    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(
            adjusted=False,
            data_normalization_mode="Adjusted",
        )
    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(
            adjusted=True,
            data_normalization_mode="Raw",
        )

    # Happy paths return None (no exception).
    _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Raw")
    _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Adjusted")


def test_build_data_policy_reports_live_for_polygon_provider() -> None:
    """PolygonProvider (or no provider, for synthetic) → provider_kind='live'."""
    from unittest.mock import MagicMock

    from app.lean_sidecar.polygon_canonical import PolygonProvider
    from app.services.lean_sidecar_service import TrustedRunRequest, _build_data_policy

    req = TrustedRunRequest(
        run_id="test-live",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_source="polygon",
    )
    provider = PolygonProvider(polygon=MagicMock())
    dp = _build_data_policy(req, provider)

    assert dp.provider_kind == "live"
    assert dp.fixture_id is None
    assert dp.fixture_sha256 is None


def test_build_data_policy_reports_fixture_identity_when_provider_replays(tmp_path) -> None:
    """RecordedPolygonFixtureProvider → provider_kind='fixture' + dir name + sha."""
    import hashlib
    import json

    from app.lean_sidecar.polygon_canonical import RecordedPolygonFixtureProvider
    from app.services.lean_sidecar_service import TrustedRunRequest, _build_data_policy

    fixture_dir = tmp_path / "spy_minute_2025-01-13_2025-01-17"
    fixture_dir.mkdir()
    bars_bytes = json.dumps([]).encode("utf-8")
    (fixture_dir / "bars.json").write_bytes(bars_bytes)
    bars_sha = hashlib.sha256(bars_bytes).hexdigest()
    (fixture_dir / "metadata.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "symbol": "SPY",
                "from_date": "2025-01-13",
                "to_date": "2025-01-17",
                "timespan": "minute",
                "multiplier": 1,
                "adjusted": False,
                "session_prefilter": "none",
                "bar_count": 0,
                "fetched_at_ms_utc": 0,
                "polygon_sdk_version": "1.12.5",
                "bars_sha256": bars_sha,
            }
        )
    )

    req = TrustedRunRequest(
        run_id="test-fixture",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_source="polygon",
    )
    provider = RecordedPolygonFixtureProvider(fixture_dir)
    dp = _build_data_policy(req, provider)

    assert dp.provider_kind == "fixture"
    assert dp.fixture_id == fixture_dir.name
    assert dp.fixture_sha256 == bars_sha


def test_build_data_policy_no_provider_defaults_to_live() -> None:
    """Synthetic runs pass provider=None → manifest records provider_kind='live'."""
    from app.services.lean_sidecar_service import TrustedRunRequest, _build_data_policy

    req = TrustedRunRequest(
        run_id="test-synthetic",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_source="synthetic",
    )
    dp = _build_data_policy(req, None)

    assert dp.provider_kind == "live"
    assert dp.fixture_id is None
    assert dp.fixture_sha256 is None
