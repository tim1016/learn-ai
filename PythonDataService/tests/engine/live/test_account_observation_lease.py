"""Tests for the durable Account Truth observation lease."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountClerkLease,
    advance_account_clerk_generation,
    write_account_clerk_lease,
)
from app.engine.live.account_observation_lease import (
    AccountObservationLeaseRepo,
    account_observation_lease_gate_result,
    assess_account_observation_lease,
)

ACCOUNT_ID = "DU1234567"
OBSERVED_AT_MS = 1_780_000_000_000


def _write_accepting_clerk(root: Path, *, generation: int = 3) -> None:
    for offset in range(generation):
        advance_account_clerk_generation(
            root,
            ACCOUNT_ID,
            phase="accepting",
            recorded_at_ms=OBSERVED_AT_MS + offset,
            source="test",
        )
    write_account_clerk_lease(
        root,
        AccountClerkLease(
            account_id=ACCOUNT_ID,
            generation=generation,
            pid=123,
            ibkr_client_id=51,
            status="RUNNING",
            started_at_ms=OBSERVED_AT_MS,
            renewed_at_ms=OBSERVED_AT_MS,
            valid_until_ms=OBSERVED_AT_MS + 60_000,
        ),
    )


def _renew(root: Path, *, clerk_generation: int = 3) -> None:
    AccountObservationLeaseRepo(root).renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS,
        clerk_generation=clerk_generation,
    )


def _write_legacy_owner_keyed_lease(root: Path) -> None:
    path = AccountObservationLeaseRepo(root).path_for(ACCOUNT_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """{"account_id":"DU1234567","account_owner_generation":3,"observed_at_ms":1780000000000,"renewed_at_ms":1780000000000,"revoked_detail":null,"revoked_reason_code":null,"schema_version":1,"status":"VERIFIED","truth_watermark":"account_truth:1780000000000","valid_until_ms":1780000060000}""",
        encoding="utf-8",
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
    _write_accepting_clerk(tmp_path)
    repo = AccountObservationLeaseRepo(tmp_path)

    lease = repo.renew(
        account_id=ACCOUNT_ID,
        observed_at_ms=OBSERVED_AT_MS,
        now_ms=OBSERVED_AT_MS + 10,
        clerk_generation=3,
    )
    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 59_999,
    )

    assert lease.valid_until_ms == OBSERVED_AT_MS + 60_000
    assert lease.schema_version == 2
    assert lease.clerk_generation == 3
    assert assessment.state == "VERIFIED"
    assert account_observation_lease_gate_result(assessment).status == "pass"


def test_observation_lease_is_expired_at_valid_until_boundary(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path)
    _renew(tmp_path)

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 60_000,
    )

    assert assessment.state == "EXPIRED"
    assert assessment.reason_code == "ACCOUNT_OBSERVATION_LEASE_EXPIRED"


def test_observation_lease_revocation_wins_before_expiry(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path)
    repo = AccountObservationLeaseRepo(tmp_path)
    _renew(tmp_path)
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
    _write_accepting_clerk(tmp_path, generation=3)
    _renew(tmp_path)
    advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="accepting",
        recorded_at_ms=OBSERVED_AT_MS + 1,
        source="test.restart",
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_observation_lease_without_current_clerk_cannot_authorize(
    tmp_path: Path,
) -> None:
    _renew(tmp_path, clerk_generation=3)

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_observation_lease_rejects_expired_clerk_lease(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path, generation=3)
    _renew(tmp_path, clerk_generation=3)
    write_account_clerk_lease(
        tmp_path,
        AccountClerkLease(
            account_id=ACCOUNT_ID,
            generation=3,
            pid=123,
            ibkr_client_id=51,
            status="RUNNING",
            started_at_ms=OBSERVED_AT_MS,
            renewed_at_ms=OBSERVED_AT_MS,
            valid_until_ms=OBSERVED_AT_MS,
        ),
    )

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_observation_lease_rejects_malformed_clerk_generation(tmp_path: Path) -> None:
    _renew(tmp_path, clerk_generation=3)
    clerk_path = tmp_path / "accounts" / ACCOUNT_ID / "clerk_generation.json"
    clerk_path.write_text("{not-json", encoding="utf-8")

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "REVOKED"
    assert assessment.reason_code == "ACCOUNT_CLERK_GENERATION_CHANGED"


def test_observation_lease_rejects_legacy_owner_keyed_schema(tmp_path: Path) -> None:
    _write_legacy_owner_keyed_lease(tmp_path)

    assessment = assess_account_observation_lease(
        tmp_path,
        ACCOUNT_ID,
        now_ms=OBSERVED_AT_MS + 1,
    )

    assert assessment.state == "ABSENT"
    assert assessment.reason_code == "ACCOUNT_OBSERVATION_LEASE_ABSENT"


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
