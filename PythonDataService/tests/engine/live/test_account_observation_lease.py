"""Tests for the durable Account Truth observation lease."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountOwnerGeneration,
    write_account_owner_generation,
)
from app.engine.live.account_observation_lease import (
    AccountObservationLeaseRepo,
    account_observation_lease_gate_result,
    assess_account_observation_lease,
)

ACCOUNT_ID = "DU1234567"
OBSERVED_AT_MS = 1_780_000_000_000


def _write_accepting_owner(root: Path, *, generation: int = 3) -> None:
    write_account_owner_generation(
        root,
        AccountOwnerGeneration(
            account_id=ACCOUNT_ID,
            generation=generation,
            phase="accepting",
            recorded_at_ms=OBSERVED_AT_MS,
            source="test",
        ),
    )


def test_observation_lease_is_absent_when_no_artifact_exists(tmp_path: Path) -> None:
    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS,
    )

    assert assessment.state == "ABSENT"
    assert assessment.reason_code == "ACCOUNT_OBSERVATION_LEASE_ABSENT"
    assert account_observation_lease_gate_result(assessment).status == "block"


def test_observation_lease_renews_with_existing_truth_ttl(tmp_path: Path) -> None:
    _write_accepting_owner(tmp_path)
    repo = AccountObservationLeaseRepo(tmp_path)

    lease = repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS + 10,
        account_owner_generation=3,
    )
    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 59_999,
    )

    assert lease.valid_until_ms == OBSERVED_AT_MS + 60_000
    assert assessment.state == "VERIFIED"
    assert account_observation_lease_gate_result(assessment).status == "pass"


def test_observation_lease_is_expired_at_valid_until_boundary(tmp_path: Path) -> None:
    _write_accepting_owner(tmp_path)
    repo = AccountObservationLeaseRepo(tmp_path)
    repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS,
        account_owner_generation=3,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 60_000,
    )

    assert assessment.state == "EXPIRED"
    assert assessment.reason_code == "ACCOUNT_OBSERVATION_LEASE_EXPIRED"


def test_observation_lease_revocation_wins_before_expiry(tmp_path: Path) -> None:
    _write_accepting_owner(tmp_path)
    repo = AccountObservationLeaseRepo(tmp_path)
    repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS,
        account_owner_generation=3,
    )
    repo.revoke(
        account_id=ACCOUNT_ID,
        reason_code="ACCOUNT_TRUTH_NOT_PROVEN",
        detail="Foreign broker activity is not attributable.",
        now_ms=OBSERVED_AT_MS + 1,
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 2,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_TRUTH_NOT_PROVEN"


def test_observation_lease_rejects_generation_change(tmp_path: Path) -> None:
    _write_accepting_owner(tmp_path, generation=3)
    repo = AccountObservationLeaseRepo(tmp_path)
    repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS,
        account_owner_generation=3,
    )
    _write_accepting_owner(tmp_path, generation=4)

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_OWNER_GENERATION_CHANGED"


def test_observation_lease_without_owner_generation_cannot_authorize_new_owner(
    tmp_path: Path,
) -> None:
    repo = AccountObservationLeaseRepo(tmp_path)
    repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS,
        account_owner_generation=None,
    )
    _write_accepting_owner(tmp_path, generation=3)

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_OWNER_GENERATION_CHANGED"


def test_observation_lease_reader_fails_closed_for_malformed_artifact(tmp_path: Path) -> None:
    repo = AccountObservationLeaseRepo(tmp_path)
    repo.path_for(ACCOUNT_ID).parent.mkdir(parents=True)
    repo.path_for(ACCOUNT_ID).write_text("{not-json", encoding="utf-8")

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS,
    )

    assert assessment.state == "ABSENT"
