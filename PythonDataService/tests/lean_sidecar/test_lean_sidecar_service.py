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


@pytest.mark.asyncio
async def test_trusted_run_rejects_stale_persistence_before_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import lean_sidecar_service as service
    from app.services.lean_sidecar_persistence import StaleLeanPersistenceSourceError

    def reject_stale_source() -> None:
        raise StaleLeanPersistenceSourceError(
            "restart the Python data service before starting another compatibility run"
        )

    monkeypatch.setattr(service, "assert_lean_persistence_source_current", reject_stale_source)
    request = service.TrustedRunRequest(
        run_id="stale-persistence-guard",
        start_ms_utc=1_736_175_600_000,
        end_ms_utc=1_736_607_600_000,
        starting_cash=100_000.0,
        data_policy=_make_data_policy(),
    )

    with pytest.raises(StaleLeanPersistenceSourceError, match="restart the Python data service"):
        await service.run_trusted_sample(request)


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


def test_runtime_polygon_adjustment_is_always_raw_for_adjusted_true() -> None:
    """PR B P1 (review feedback): ``data_policy.adjusted=True`` records
    staging-pipeline INTENT (spec § 4.4) and must NOT be translated to
    ``polygon_adjustment="adjusted"`` at runtime. The downstream
    ``fetch_canonical_minute_bars`` and bundled trusted templates only
    accept ``"raw"`` today, so emitting anything else would 500 every
    PR-B-default request (``adjusted=True`` is the new-shape field
    default).
    """
    from app.services.lean_sidecar_service import _runtime_polygon_adjustment

    assert _runtime_polygon_adjustment(_make_data_policy(source="polygon")) == "raw"


def test_runtime_polygon_adjustment_is_raw_for_adjusted_false() -> None:
    """PR A's existing case: ``adjusted=False`` -> ``"raw"`` as well."""
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
    from app.services.lean_sidecar_service import _runtime_polygon_adjustment

    dp = DataPolicy(
        source="polygon",
        symbol="SPY",
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    assert _runtime_polygon_adjustment(dp) == "raw"


def test_compatibility_fixture_receipt_rejects_changed_shared_bytes() -> None:
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError,
        _assert_compatibility_fixture_receipt,
    )

    with pytest.raises(LeanSidecarServiceError, match="compatibility_fixture_source_mismatch"):
        _assert_compatibility_fixture_receipt(
            expected_id="bar-store-v1-expected",
            expected_sha256="a" * 64,
            actual={
                "fixture_id": "bar-store-v1-changed",
                "fixture_sha256": "b" * 64,
            },
            phase="source",
        )


def test_compatibility_fixture_receipt_accepts_exact_replay() -> None:
    from app.services.lean_sidecar_service import _assert_compatibility_fixture_receipt

    _assert_compatibility_fixture_receipt(
        expected_id="bar-store-v1-exact",
        expected_sha256="a" * 64,
        actual={
            "fixture_id": "bar-store-v1-exact",
            "fixture_sha256": "a" * 64,
        },
        phase="staging",
    )


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


def test_effective_window_uses_algorithm_contract_not_summary_curve() -> None:
    """The reduced LEAN summary can stop before the executed algorithm."""
    from app.lean_sidecar.normalized_parser import NormalizedResult
    from app.services.lean_sidecar_service import _effective_window_from_normalized

    normalized = NormalizedResult(
        parser_version="lean-native-r1",
        algorithm_id="MyAlgorithm",
        statistics={},
        algorithm_start_ms_utc=1_699_920_000_000,
        algorithm_end_ms_utc=1_700_092_799_000,
        total_order_events=0,
        total_equity_points=2,
        first_equity_ms_utc=1_699_977_600_000,
        last_equity_ms_utc=1_700_064_000_000,
        first_summary_equity_ms_utc=1_699_977_600_000,
        last_summary_equity_ms_utc=1_700_000_000_000,
    )

    window = _effective_window_from_normalized(normalized)

    assert window is not None
    assert window.start_ms == 1_699_920_000_000
    assert window.end_ms == 1_700_092_799_000
