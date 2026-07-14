"""Sequence-parity checks for durable Account Observation Lease evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import append_account_event
from app.services.observation_lease_parity import (
    assess_observation_lease_shadow_parity,
    assess_observation_lease_shadow_parity_from_artifacts,
)


def _comparison(
    *,
    recorded_at_ms: int,
    truth_status: str = "pass",
    lease_status: str = "pass",
) -> dict[str, object]:
    return {
        "event_type": "account_observation_lease_shadow_comparison",
        "recorded_at_ms": recorded_at_ms,
        "strategy_instance_id": "bot-a",
        "run_id": "run-a",
        "truth_gate_id": "account.account_truth",
        "truth_source": "account_truth_snapshot",
        "truth_status": truth_status,
        "lease_gate_id": "account.observation_lease",
        "lease_source": "account_observation_lease",
        "lease_status": lease_status,
    }


def test_assess_observation_lease_shadow_parity_accepts_three_ny_sessions() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            _comparison(recorded_at_ms=1_704_209_400_000),  # 2024-01-02 10:30 ET
            _comparison(recorded_at_ms=1_704_295_800_000),  # 2024-01-03 10:30 ET
            _comparison(recorded_at_ms=1_704_382_200_000),  # 2024-01-04 10:30 ET
        ]
    )

    assert report.comparison_count == 3
    assert report.observed_session_dates == ("2024-01-02", "2024-01-03", "2024-01-04")
    assert report.lease_weaker_comparisons == ()
    assert report.lease_stricter_comparisons == ()
    assert report.cutover_ready is True


def test_assess_observation_lease_shadow_parity_rejects_weaker_lease() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            _comparison(recorded_at_ms=1_704_209_400_000, truth_status="block", lease_status="pass"),
            _comparison(recorded_at_ms=1_704_295_800_000),
            _comparison(recorded_at_ms=1_704_382_200_000),
        ]
    )

    assert len(report.lease_weaker_comparisons) == 1
    assert report.cutover_ready is False


def test_assess_observation_lease_shadow_parity_allows_stricter_lease() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            _comparison(recorded_at_ms=1_704_209_400_000, truth_status="pass", lease_status="block"),
            _comparison(recorded_at_ms=1_704_295_800_000),
            _comparison(recorded_at_ms=1_704_382_200_000),
        ]
    )

    assert len(report.lease_stricter_comparisons) == 1
    assert report.cutover_ready is True


def test_assess_observation_lease_shadow_parity_rejects_malformed_comparison() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            _comparison(recorded_at_ms=1_704_209_400_000),
            {
                "event_type": "account_observation_lease_shadow_comparison",
                "recorded_at_ms": "not-a-timestamp",
                "truth_status": "pass",
                "lease_status": "pass",
            },
            _comparison(recorded_at_ms=1_704_295_800_000),
            _comparison(recorded_at_ms=1_704_382_200_000),
        ]
    )

    assert report.invalid_comparison_count == 1
    assert report.cutover_ready is False


def test_assess_observation_lease_shadow_parity_rejects_non_string_status() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            {
                **_comparison(recorded_at_ms=1_704_209_400_000),
                "truth_status": {"not": "a gate status"},
            }
        ]
    )

    assert report.comparison_count == 0
    assert report.invalid_comparisons[0].reason == "truth_status must be pass or block"


def test_assess_observation_lease_shadow_parity_rejects_unknown_gate_identity() -> None:
    report = assess_observation_lease_shadow_parity(
        [
            {
                **_comparison(recorded_at_ms=1_704_209_400_000),
                "lease_gate_id": "some.other.gate",
            }
        ]
    )

    assert report.comparison_count == 0
    assert report.invalid_comparisons[0].reason == "lease gate identity is not account.observation_lease"


@pytest.mark.parametrize("field", ["strategy_instance_id", "run_id"])
def test_assess_observation_lease_shadow_parity_rejects_missing_submit_identity(
    field: str,
) -> None:
    event = _comparison(recorded_at_ms=1_704_209_400_000)
    event.pop(field)

    report = assess_observation_lease_shadow_parity([event])

    assert report.comparison_count == 0
    assert field in report.invalid_comparisons[0].reason


@pytest.mark.parametrize("field", ["strategy_instance_id", "run_id"])
def test_assess_observation_lease_shadow_parity_rejects_blank_submit_identity(
    field: str,
) -> None:
    report = assess_observation_lease_shadow_parity(
        [
            {
                **_comparison(recorded_at_ms=1_704_209_400_000),
                field: " ",
            }
        ]
    )

    assert report.comparison_count == 0
    assert field in report.invalid_comparisons[0].reason


def test_assess_observation_lease_shadow_parity_does_not_count_weekend_comparison() -> None:
    report = assess_observation_lease_shadow_parity(
        [_comparison(recorded_at_ms=1_704_555_000_000)],  # 2024-01-06 10:30 ET
        minimum_sessions=1,
    )

    assert report.comparison_count == 1
    assert report.observed_session_dates == ()
    assert report.cutover_ready is False


def test_assess_observation_lease_shadow_parity_from_artifacts_replays_canonical_journal(
    tmp_path: Path,
) -> None:
    for recorded_at_ms in (1_704_209_400_000, 1_704_295_800_000, 1_704_382_200_000):
        append_account_event(
            tmp_path,
            "DU123",
            _comparison(recorded_at_ms=recorded_at_ms),
        )

    report = assess_observation_lease_shadow_parity_from_artifacts(tmp_path, "DU123")

    assert report.cutover_ready is True
