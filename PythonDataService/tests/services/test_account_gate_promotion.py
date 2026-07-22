"""Regression coverage for evidence-gated Clerk account proof promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import (
    AccountClerkLease,
    advance_account_clerk_generation,
    append_account_event,
    write_account_clerk_lease,
)
from app.services.account_gate_promotion import (
    CLERK_RESTART_SMOKE_CONFIRMATION,
    AccountGatePromotionError,
    record_clerk_restart_smoke,
    resolve_account_gate_authority,
)
from app.services.observation_lease_parity import (
    OBSERVATION_LEASE_GENERATION_AUTHORITY,
    OBSERVATION_LEASE_SHADOW_COMPARISON_SCHEMA_VERSION,
)

ACCOUNT_ID = "DU1234567"
NOW_MS = 1_704_382_200_000  # 2024-01-04 10:30 ET


def _write_accepting_clerk(root: Path, *, generation: int) -> None:
    for offset in range(generation):
        advance_account_clerk_generation(
            root,
            ACCOUNT_ID,
            phase="accepting",
            recorded_at_ms=NOW_MS + offset,
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
            started_at_ms=NOW_MS,
            renewed_at_ms=NOW_MS,
            valid_until_ms=NOW_MS + 600_000,
        ),
    )


def _append_clean_shadow_row(root: Path, *, recorded_at_ms: int) -> None:
    append_account_event(
        root,
        ACCOUNT_ID,
        {
            "event_type": "account_observation_lease_shadow_comparison",
            "comparison_schema_version": OBSERVATION_LEASE_SHADOW_COMPARISON_SCHEMA_VERSION,
            "recorded_at_ms": recorded_at_ms,
            "strategy_instance_id": "bot-a",
            "run_id": "run-a",
            "truth_gate_id": "account.account_truth",
            "truth_source": "account_truth_snapshot",
            "truth_status": "pass",
            "lease_gate_id": "account.observation_lease",
            "lease_source": "account_observation_lease",
            "lease_status": "pass",
            "lease_schema_version": 2,
            "lease_generation_authority": OBSERVATION_LEASE_GENERATION_AUTHORITY,
        },
    )


def _qualify_three_sessions(root: Path) -> None:
    for recorded_at_ms in (1_704_209_400_000, 1_704_295_800_000, NOW_MS):
        _append_clean_shadow_row(root, recorded_at_ms=recorded_at_ms)


def test_requested_clerk_proof_stays_on_safe_default_without_parity(tmp_path: Path) -> None:
    resolution = resolve_account_gate_authority(
        tmp_path,
        account_id=ACCOUNT_ID,
        requested_authority="observation_lease",
        now_ms=NOW_MS,
    )

    assert resolution.effective_authority == "account_truth"
    assert resolution.state == "WAITING_FOR_SHADOW_PARITY"
    assert resolution.reason_code == "ACCOUNT_GATE_PROMOTION_EVIDENCE_INCOMPLETE"
    assert resolution.disposition == "COMPLETE_THREE_SESSION_SHADOW_PARITY"


def test_three_clean_sessions_still_require_current_clerk_restart_smoke(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path, generation=1)
    _qualify_three_sessions(tmp_path)

    resolution = resolve_account_gate_authority(
        tmp_path,
        account_id=ACCOUNT_ID,
        requested_authority="observation_lease",
        now_ms=NOW_MS + 1,
    )

    assert resolution.parity is not None and resolution.parity.cutover_ready is True
    assert resolution.effective_authority == "account_truth"
    assert resolution.state == "WAITING_FOR_CLERK_RESTART_SMOKE"
    assert resolution.reason_code == "ACCOUNT_GATE_CLERK_RESTART_SMOKE_REQUIRED"


def test_current_restart_smoke_completes_promotion_and_later_restart_invalidates_it(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path, generation=1)
    _qualify_three_sessions(tmp_path)
    smoke = record_clerk_restart_smoke(
        tmp_path,
        account_id=ACCOUNT_ID,
        confirmation=CLERK_RESTART_SMOKE_CONFIRMATION,
        recorded_at_ms=NOW_MS + 1,
    )

    promoted = resolve_account_gate_authority(
        tmp_path,
        account_id=ACCOUNT_ID,
        requested_authority="observation_lease",
        now_ms=NOW_MS + 2,
    )

    assert smoke.clerk_generation == 1
    assert promoted.effective_authority == "observation_lease"
    assert promoted.state == "CLERK_PROOF_ACTIVE"
    assert promoted.disposition is None

    advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="accepting",
        recorded_at_ms=NOW_MS + 3,
        source="restart",
    )
    write_account_clerk_lease(
        tmp_path,
        AccountClerkLease(
            account_id=ACCOUNT_ID,
            generation=2,
            pid=456,
            ibkr_client_id=52,
            status="RUNNING",
            started_at_ms=NOW_MS + 3,
            renewed_at_ms=NOW_MS + 3,
            valid_until_ms=NOW_MS + 600_000,
        ),
    )

    after_restart = resolve_account_gate_authority(
        tmp_path,
        account_id=ACCOUNT_ID,
        requested_authority="observation_lease",
        now_ms=NOW_MS + 4,
    )

    assert after_restart.effective_authority == "account_truth"
    assert after_restart.state == "WAITING_FOR_CLERK_RESTART_SMOKE"


def test_restart_smoke_requires_exact_typed_confirmation(tmp_path: Path) -> None:
    _write_accepting_clerk(tmp_path, generation=1)

    with pytest.raises(AccountGatePromotionError, match="CLERK_RESTART_SMOKE_CONFIRMATION_REQUIRED"):
        record_clerk_restart_smoke(
            tmp_path,
            account_id=ACCOUNT_ID,
            confirmation="yes",
            recorded_at_ms=NOW_MS,
        )
