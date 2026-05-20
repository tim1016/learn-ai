"""Unit tests for the lean_sidecar_service orchestrator dataclasses."""

from __future__ import annotations

import pytest


def _make_data_policy(*, source: str = "synthetic", strategy_multiplier: int = 15) -> DataPolicy:  # noqa: F821
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    return DataPolicy(
        source=source,  # type: ignore[arg-type]
        symbol="SPY",
        adjusted=True,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=strategy_multiplier),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )


def test_trusted_run_request_exposes_symbol_via_data_policy() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-defaults",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_policy=_make_data_policy(),
    )

    assert req.symbol == "SPY"
    assert req.data_policy.source == "synthetic"
    assert req.data_policy.session == "regular"


def test_trusted_run_request_accepts_polygon_data_source() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-polygon",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_policy=_make_data_policy(source="polygon"),
    )

    assert req.data_policy.source == "polygon"


def test_adjusted_true_with_raw_normalization_is_accepted() -> None:
    """PR B widens: pre-adjusted staging + LEAN Raw is the new default pairing."""
    from app.services.lean_sidecar_service import _assert_adjustment_vocabulary_consistent

    _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Raw")  # no raise


def test_adjusted_false_with_adjusted_normalization_is_rejected() -> None:
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_adjustment_vocabulary_consistent,
    )

    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Adjusted")


def test_adjusted_true_with_adjusted_normalization_is_rejected() -> None:
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_adjustment_vocabulary_consistent,
    )

    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Adjusted")


def test_adjusted_false_with_raw_normalization_is_accepted() -> None:
    """PR A's existing case: raw → raw."""
    from app.services.lean_sidecar_service import _assert_adjustment_vocabulary_consistent

    _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Raw")  # no raise


def test_trusted_run_request_carries_data_policy() -> None:
    """TrustedRunRequest exposes a single data_policy field; legacy top-level fields are gone."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
    from app.services.lean_sidecar_service import TrustedRunRequest

    dp = DataPolicy(
        source="polygon",
        symbol="SPY",
        adjusted=True,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    req = TrustedRunRequest(
        run_id="test-data-policy",
        algorithm_source="<source>",
        starting_cash=100_000.0,
        start_ms_utc=1736777400000,
        end_ms_utc=1737298200000,
        template="ema_crossover",
        data_policy=dp,
    )

    assert req.data_policy is dp
    assert req.symbol == "SPY"  # property accessor reads from data_policy
    # Legacy top-level dataclass fields removed:
    fields = {f.name for f in TrustedRunRequest.__dataclass_fields__.values()}
    assert "bar_minutes" not in fields
    assert "data_source" not in fields
    assert "adjustment" not in fields
    assert "session" not in fields
