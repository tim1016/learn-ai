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
