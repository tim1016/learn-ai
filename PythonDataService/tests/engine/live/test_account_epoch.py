"""Durable account-epoch persistence contracts retained after Clerk retirement."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_epoch import (
    AccountEpoch,
    AccountEpochAuthority,
    AccountEpochGenerationFencedError,
    AccountEpochOutageChanges,
    AccountEpochOutageDiff,
    AccountEpochReconciliationProof,
)

ACCOUNT_ID = "DU1234567"


def _authority(
    root: Path,
    *,
    generation: int,
    boot_id: str,
    now_ms: int = 1_780_000_000_000,
) -> AccountEpochAuthority:
    return AccountEpochAuthority(
        artifacts_root=root,
        account_id=ACCOUNT_ID,
        clerk_generation=generation,
        clerk_boot_id=boot_id,
        now_ms=lambda: now_ms,
    )


def test_first_invalidation_advances_once_and_repeated_trigger_is_one_receipt(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    assert authority.initialize().current_epoch == AccountEpoch(
        clerk_boot_id="boot-a",
        epoch_seq=1,
    )

    first = authority.invalidate("IBKR_1100")
    repeated = authority.invalidate("IBKR_1100")

    assert first.observed_epoch == AccountEpoch(clerk_boot_id="boot-a", epoch_seq=2)
    assert repeated == first
    state = authority.read()
    assert state.status == "INVALID"
    assert state.invalidation_triggers == ("IBKR_1100",)
    epoch_events = [
        event
        for event in read_account_events(tmp_path, ACCOUNT_ID)
        if event["event_type"] == "account_epoch_invalidated"
    ]
    assert len(epoch_events) == 1
    assert epoch_events[0]["receipt_id"] == first.receipt_id


def test_distinct_gateway_triggers_advance_distinct_epochs(tmp_path: Path) -> None:
    authority = _authority(tmp_path, generation=4, boot_id="boot-a")
    authority.initialize()

    lost = authority.invalidate("IBKR_1101")
    maintained = authority.invalidate("IBKR_1102")

    assert lost.observed_epoch.epoch_seq == 2
    assert maintained.observed_epoch.epoch_seq == 3
    assert lost.required_reconciliation_depth == "full"
    assert maintained.required_reconciliation_depth == "incremental"


def test_new_generation_preserves_invalid_history_and_fences_old_writer(
    tmp_path: Path,
) -> None:
    original = _authority(tmp_path, generation=4, boot_id="boot-a")
    original.initialize()
    original.invalidate("SOCKET_LOSS")

    successor = _authority(tmp_path, generation=5, boot_id="boot-b")
    state = successor.initialize()

    assert state.current_epoch == AccountEpoch(clerk_boot_id="boot-b", epoch_seq=3)
    assert state.status == "INVALID"
    assert state.would_block_reason == "CLERK_DEATH"
    with pytest.raises(AccountEpochGenerationFencedError):
        original.invalidate("GENERATION_FENCED")


def test_durable_generation_fences_all_old_epoch_writes(tmp_path: Path) -> None:
    durable_generation = 4
    original = AccountEpochAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        clerk_generation=4,
        clerk_boot_id="boot-a",
        now_ms=lambda: 1_780_000_000_000,
        durable_generation_provider=lambda: durable_generation,
    )
    original.initialize()

    durable_generation = 5
    with pytest.raises(AccountEpochGenerationFencedError):
        original.invalidate("GENERATION_FENCED")
    with pytest.raises(AccountEpochGenerationFencedError):
        original.require_broker_write()
    with pytest.raises(AccountEpochGenerationFencedError):
        original.complete_reconciliation(
            AccountEpochReconciliationProof(
                account_id=ACCOUNT_ID,
                reconciliation_id="epoch:DU1234567:boot-a:2",
                candidate_epoch=AccountEpoch(clerk_boot_id="boot-a", epoch_seq=2),
                required_reconciliation_depth="full",
                journal_tail_seq=0,
                evidence_status="COMPLETE",
                outage_diff=AccountEpochOutageDiff(
                    required_reconciliation_depth="full",
                    intents=AccountEpochOutageChanges(),
                    orders=AccountEpochOutageChanges(),
                    executions=AccountEpochOutageChanges(),
                    positions=AccountEpochOutageChanges(),
                ),
            )
        )
