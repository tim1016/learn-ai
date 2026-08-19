"""Regression tests for retired-owner account suspension (Custody S6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event, Thread

import pytest

import app.engine.live.account_safety as account_safety_module
from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.account_artifacts import advance_account_clerk_generation
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_clerk_journal import AccountClerkJournal
from app.engine.live.account_clerk_journal_models import AccountClerkIntentRejected, AccountClerkJournalEntry
from app.engine.live.account_epoch import (
    AccountEpochAuthority,
    AccountEpochOutageChanges,
    AccountEpochOutageDiff,
    AccountEpochReconciliationProof,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    retire_unmanaged_active_bindings_on_daemon_boot,
    write_account_instance_binding,
)
from app.engine.live.account_safety import (
    AccountSafetyAdmissionError,
    AccountSafetyAdmissionParticipant,
    AccountSafetyAuthority,
    AccountSafetyCorruptError,
    AccountSafetyEntryBlockedError,
    AccountSafetyVerdict,
    RetiredOwnerCustody,
    account_safety_admission_lock,
    account_safety_admission_path,
    account_safety_admission_repair_path,
    account_safety_blocks_current_bot,
    account_safety_entry_admission_lock,
    account_safety_suspension_reservation,
    repair_account_safety_admission,
    retired_owner_nonterminal_custody,
)
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.run_ledger import LiveRunLedger, write_ledger
from app.services.bot_deletion import (
    BotRetirementBindingTarget,
    BotRetirementTransitionRecord,
    retire_bot_lifecycle_and_bindings,
    stable_bot_retirement_transition_path,
)

ACCOUNT_ID = "DU1234567"
NOW_MS = 1_780_000_000_000


def _custody(*, strategy_instance_id: str = "retired-amd") -> RetiredOwnerCustody:
    return RetiredOwnerCustody(
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        intent_id="intent-retired-1",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:intent-retired-1",
    )


def _epoch_authority(root: Path) -> AccountEpochAuthority:
    return AccountEpochAuthority(
        artifacts_root=root,
        account_id=ACCOUNT_ID,
        clerk_generation=1,
        clerk_boot_id="s6-test-boot",
        now_ms=lambda: NOW_MS,
    )


def _clean_proof(state) -> AccountEpochReconciliationProof:
    assert state.reconciliation_id is not None
    assert state.required_reconciliation_depth is not None
    return AccountEpochReconciliationProof(
        account_id=ACCOUNT_ID,
        reconciliation_id=state.reconciliation_id,
        candidate_epoch=state.current_epoch,
        required_reconciliation_depth=state.required_reconciliation_depth,
        journal_tail_seq=0,
        evidence_status="COMPLETE",
        outage_diff=AccountEpochOutageDiff(
            required_reconciliation_depth=state.required_reconciliation_depth,
            intents=AccountEpochOutageChanges(),
            orders=AccountEpochOutageChanges(),
            executions=AccountEpochOutageChanges(),
            positions=AccountEpochOutageChanges(),
        ),
    )


def test_retired_owner_suspension_lifts_only_from_bound_clean_epoch_proof(tmp_path: Path) -> None:
    epoch = _epoch_authority(tmp_path)
    epoch.initialize()
    invalid = epoch.invalidate("RETIRED_OWNER_EXPOSURE")
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    safety.suspend_retired_owner_custody((_custody(),))
    safety.bind_reconciliation(epoch.read())

    assert safety.read().verdict is AccountSafetyVerdict.SUSPENDED
    assert safety.lift_if_proven(epoch=epoch.read(), retained_custody=()).verdict is AccountSafetyVerdict.SUSPENDED

    clean = epoch.complete_reconciliation(_clean_proof(epoch.read()))
    lifted = safety.lift_if_proven(epoch=clean, retained_custody=())

    assert invalid.reconciliation_id == clean.last_reconciliation_id
    assert lifted.verdict is AccountSafetyVerdict.CLEAN
    assert lifted.last_reconciliation_id == clean.last_reconciliation_id


def test_retired_owner_suspension_never_lifts_while_nonterminal_custody_remains(tmp_path: Path) -> None:
    epoch = _epoch_authority(tmp_path)
    epoch.initialize()
    epoch.invalidate("RETIRED_OWNER_EXPOSURE")
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    custody = _custody()
    safety.suspend_retired_owner_custody((custody,))
    safety.bind_reconciliation(epoch.read())
    clean = epoch.complete_reconciliation(_clean_proof(epoch.read()))

    still_suspended = safety.lift_if_proven(epoch=clean, retained_custody=(custody,))

    assert still_suspended.verdict is AccountSafetyVerdict.SUSPENDED
    with pytest.raises(AccountSafetyEntryBlockedError):
        safety.require_entry_admission()


@pytest.mark.asyncio
async def test_clerk_refuses_new_entry_a0_while_retired_owner_suspension_is_active(tmp_path: Path) -> None:
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    safety.suspend_retired_owner_custody((_custody(),))
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        safety_authority=safety,
    )
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-s6",
        account_id=ACCOUNT_ID,
        strategy_instance_id="healthy-sibling",
        run_id="run-healthy",
        bot_order_namespace="learn-ai/healthy-sibling/v1",
        intent_id="healthy-entry-1",
        order_ref="learn-ai/healthy-sibling/v1:healthy-entry-1",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )

    with pytest.raises(AccountClerkIntentRejected) as exc:
        await clerk.record_intent(intent)

    assert exc.value.reason == "CLERK_ACCOUNT_SUSPENDED_RECONCILIATION_REQUIRED"
    assert exc.value.diagnostics["safety_verdict"] == "SUSPENDED"
    assert clerk._journal.snapshot() == []


@pytest.mark.asyncio
async def test_shared_account_safety_permit_serializes_retirement_before_sibling_a0(tmp_path: Path) -> None:
    """A sibling cannot fsync A0 after retirement has acquired the account permit."""

    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    permit_held = Event()
    release_retirement = Event()

    def suspend_under_shared_permit() -> None:
        with account_safety_admission_lock(tmp_path, ACCOUNT_ID):
            permit_held.set()
            assert release_retirement.wait(timeout=1)
            safety.suspend_retired_owner_custody((_custody(),))

    retirement = Thread(target=suspend_under_shared_permit)
    retirement.start()
    assert permit_held.wait(timeout=1)
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        safety_authority=safety,
    )
    sibling = AccountOwnerSubmitIntent(
        trace_id="trace-concurrent-sibling",
        account_id=ACCOUNT_ID,
        strategy_instance_id="healthy-sibling",
        run_id="run-healthy",
        bot_order_namespace="learn-ai/healthy-sibling/v1",
        intent_id="healthy-entry-race",
        order_ref="learn-ai/healthy-sibling/v1:healthy-entry-race",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    entry = asyncio.create_task(clerk.record_intent(sibling))
    await asyncio.sleep(0)
    assert clerk._journal.snapshot() == []

    release_retirement.set()
    await asyncio.to_thread(retirement.join, 1)
    assert not retirement.is_alive()
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_ACCOUNT_SUSPENDED"):
        await entry
    assert clerk._journal.snapshot() == []


def test_shared_entry_permits_drain_before_exclusive_suspension(tmp_path: Path) -> None:
    """Healthy entries share admission; a suspension waits for both to finish."""

    readers_entered = Event()
    second_reader_entered = Event()
    release_readers = Event()
    suspension_entered = Event()

    def reader(entered: Event) -> None:
        with account_safety_entry_admission_lock(tmp_path, ACCOUNT_ID):
            entered.set()
            assert release_readers.wait(timeout=1)

    def suspend() -> None:
        with account_safety_admission_lock(tmp_path, ACCOUNT_ID):
            suspension_entered.set()

    first_reader = Thread(target=reader, args=(readers_entered,))
    second_reader = Thread(target=reader, args=(second_reader_entered,))
    first_reader.start()
    second_reader.start()
    assert readers_entered.wait(timeout=1)
    assert second_reader_entered.wait(timeout=1)

    suspension = Thread(target=suspend)
    suspension.start()
    assert not suspension_entered.wait(timeout=0.05)

    release_readers.set()
    first_reader.join(timeout=1)
    second_reader.join(timeout=1)
    suspension.join(timeout=1)
    assert not first_reader.is_alive()
    assert not second_reader.is_alive()
    assert suspension_entered.is_set()
    assert not suspension.is_alive()


@pytest.mark.asyncio
async def test_new_a0_waiting_on_safety_writer_never_blocks_a_fill_callback_intake(
    tmp_path: Path,
) -> None:
    """The reader waits outside intake, so an old broker permit can drain."""

    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    reader_entered = Event()
    release_reader = Event()
    writer_reserved = Event()
    writer_done = Event()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT_ID, safety_authority=safety)
    sibling = AccountOwnerSubmitIntent(
        trace_id="trace-reader-writer-intake",
        account_id=ACCOUNT_ID,
        strategy_instance_id="healthy-sibling",
        run_id="run-healthy",
        bot_order_namespace="learn-ai/healthy-sibling/v1",
        intent_id="healthy-entry-writer-race",
        order_ref="learn-ai/healthy-sibling/v1:healthy-entry-writer-race",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )

    def hold_old_broker_reader() -> None:
        with account_safety_entry_admission_lock(tmp_path, ACCOUNT_ID):
            reader_entered.set()
            assert release_reader.wait(timeout=1)

    def reserve_retirement() -> None:
        with account_safety_suspension_reservation(tmp_path, ACCOUNT_ID) as reservation:
            writer_reserved.set()
            reservation.wait_for_readers()
            safety.suspend_retired_owner_custody((_custody(),))
        writer_done.set()

    reader = Thread(target=hold_old_broker_reader)
    reader.start()
    assert reader_entered.wait(timeout=1)
    writer = Thread(target=reserve_retirement)
    writer.start()
    assert writer_reserved.wait(timeout=1)

    pending_a0 = asyncio.create_task(clerk.record_intent(sibling))
    await asyncio.sleep(0)
    # This simulates the synchronous fill-before-ack callback needed by the
    # existing broker reader. It must acquire intake despite the new A0 being
    # queued behind the safety writer.
    async with clerk._intake_lock:
        callback_persisted = True
    assert callback_persisted is True

    release_reader.set()
    await asyncio.to_thread(reader.join, 1)
    await asyncio.to_thread(writer.join, 1)
    assert not reader.is_alive()
    assert writer_done.is_set()
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_ACCOUNT_SUSPENDED"):
        await pending_a0


@pytest.mark.asyncio
async def test_clerk_host_repairs_stranded_admission_markers_only_after_intake_closes(
    tmp_path: Path,
) -> None:
    """Crash recovery is explicit, capability-bound, and receipt-idempotent."""

    anchor = account_safety_admission_path(tmp_path, ACCOUNT_ID)
    coordinator = anchor.with_name(f".{anchor.name}")
    readers = coordinator / "readers"
    readers.mkdir(parents=True)
    (coordinator / "gate").write_text("stale", encoding="utf-8")
    (coordinator / "writer").write_text("stale", encoding="utf-8")
    (readers / "reader-one").write_text("stale", encoding="utf-8")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        host_binding_capability="s6-host-capability",
        now_ms=lambda: NOW_MS,
    )

    with pytest.raises(
        AccountClerkIntentRejected,
        match="CLERK_ACCOUNT_SAFETY_REPAIR_REQUIRES_CLOSED_INTAKE",
    ):
        await clerk.repair_stranded_account_safety_admission(
            operation_id="s6-admission-repair-001",
            authorized_by="clerk-host",
            host_capability="s6-host-capability",
        )

    clerk.close_normal_submit_intake()
    receipt = await clerk.repair_stranded_account_safety_admission(
        operation_id="s6-admission-repair-001",
        authorized_by="clerk-host",
        host_capability="s6-host-capability",
    )

    assert receipt.removed_markers == ("gate", "writer", "readers/reader-one")
    assert receipt.maintenance_generation == 1
    assert not (coordinator / "gate").exists()
    assert not (coordinator / "writer").exists()
    assert list(readers.iterdir()) == []
    assert (
        await clerk.repair_stranded_account_safety_admission(
            operation_id="s6-admission-repair-001",
            authorized_by="clerk-host",
            host_capability="s6-host-capability",
        )
    ) == receipt


def test_admission_repair_waits_for_cross_runtime_participants_before_removal(
    tmp_path: Path,
) -> None:
    """Maintenance fences new marker creation and drains a live remote reader."""

    advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="accepting",
        recorded_at_ms=NOW_MS,
        source="test",
    )
    reader_entered = Event()
    release_reader = Event()
    repair_finished = Event()
    receipts = []

    def hold_remote_reader() -> None:
        with account_safety_entry_admission_lock(tmp_path, ACCOUNT_ID):
            reader_entered.set()
            assert release_reader.wait(timeout=1)

    def repair() -> None:
        receipts.append(
            repair_account_safety_admission(
                tmp_path,
                ACCOUNT_ID,
                operation_id="s6-admission-repair-002",
                authorized_by="clerk-host",
                repaired_at_ms=NOW_MS,
            )
        )
        repair_finished.set()

    reader = Thread(target=hold_remote_reader)
    reader.start()
    assert reader_entered.wait(timeout=1)
    maintenance = Thread(target=repair)
    maintenance.start()
    assert not repair_finished.wait(timeout=0.05)

    release_reader.set()
    reader.join(timeout=1)
    maintenance.join(timeout=1)
    assert not reader.is_alive()
    assert not maintenance.is_alive()
    assert receipts[0].removed_markers == ()


def test_repair_fails_closed_when_a_crashed_participant_cannot_be_proven_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation alone cannot prove an old host stopped its critical section."""

    first = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="accepting",
        recorded_at_ms=NOW_MS,
        source="old-host",
    )
    anchor = account_safety_admission_path(tmp_path, ACCOUNT_ID)
    coordinator = anchor.with_name(f".{anchor.name}")
    participants = coordinator / "participants"
    readers = coordinator / "readers"
    participants.mkdir(parents=True)
    readers.mkdir()
    participant_id = "c" * 32
    (participants / participant_id).write_text(
        AccountSafetyAdmissionParticipant(
            account_id=ACCOUNT_ID,
            participant_id=participant_id,
            clerk_generation=first.generation,
            enrolled_at_ms=NOW_MS,
        ).model_dump_json(),
        encoding="utf-8",
    )
    (readers / "crashed-reader").write_text("stale", encoding="utf-8")
    fence = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT_ID,
        phase="frozen",
        recorded_at_ms=NOW_MS + 1,
        source="new-host-repair",
    )
    monkeypatch.setattr(account_safety_module, "_ACCOUNT_SAFETY_ADMISSION_TIMEOUT_S", 0.01)

    with pytest.raises(AccountSafetyAdmissionError, match="participants did not quiesce"):
        repair_account_safety_admission(
            tmp_path,
            ACCOUNT_ID,
            operation_id="s6-admission-repair-003",
            authorized_by="new-clerk-host",
            repaired_at_ms=NOW_MS + 1,
            clerk_fence_generation=fence.generation,
        )

    assert list(participants.iterdir()) == [participants / participant_id]
    assert (readers / "crashed-reader").exists()


def test_repair_replay_finalizes_maintenance_after_receipt_fsync_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry of the same operation closes the receipt-before-OPEN crash gap."""

    original_write = account_safety_module._write_admission_maintenance_locked

    def fail_before_open(path: Path, state: object) -> None:
        if getattr(state, "status", None) == "OPEN":
            raise OSError("simulated crash after repair receipt fsync")
        original_write(path, state)

    monkeypatch.setattr(
        account_safety_module,
        "_write_admission_maintenance_locked",
        fail_before_open,
    )
    with pytest.raises(OSError, match="simulated crash"):
        repair_account_safety_admission(
            tmp_path,
            ACCOUNT_ID,
            operation_id="s6-admission-repair-004",
            authorized_by="clerk-host",
            repaired_at_ms=NOW_MS,
        )
    monkeypatch.setattr(
        account_safety_module,
        "_write_admission_maintenance_locked",
        original_write,
    )

    replayed = repair_account_safety_admission(
        tmp_path,
        ACCOUNT_ID,
        operation_id="s6-admission-repair-004",
        authorized_by="clerk-host",
        repaired_at_ms=NOW_MS,
    )
    with account_safety_entry_admission_lock(tmp_path, ACCOUNT_ID):
        pass

    assert replayed.operation_id == "s6-admission-repair-004"


def test_repair_rechecks_receipt_inside_cross_runtime_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent host receipt wins instead of being duplicated after flock."""

    original_read = account_safety_module._repair_receipt_for_operation
    injected = account_safety_module.AccountSafetyAdmissionRepairReceipt(
        account_id=ACCOUNT_ID,
        operation_id="s6-admission-repair-005",
        authorized_by="other-host",
        repaired_at_ms=NOW_MS,
        maintenance_generation=1,
        clerk_fence_generation=1,
        removed_markers=(),
    )
    calls = 0

    def receipt_from_other_host(path: Path, operation_id: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text(injected.model_dump_json() + "\n", encoding="utf-8")
        return original_read(path, operation_id)

    monkeypatch.setattr(
        account_safety_module,
        "_repair_receipt_for_operation",
        receipt_from_other_host,
    )

    repaired = repair_account_safety_admission(
        tmp_path,
        ACCOUNT_ID,
        operation_id="s6-admission-repair-005",
        authorized_by="this-host",
        repaired_at_ms=NOW_MS,
    )

    assert repaired == injected
    with account_safety_entry_admission_lock(tmp_path, ACCOUNT_ID):
        pass


def test_repair_rejects_a_same_operation_receipt_from_another_fence_or_account(
    tmp_path: Path,
) -> None:
    """An idempotency key is scoped to both account path and Clerk fence."""

    receipt_path = account_safety_admission_repair_path(tmp_path, ACCOUNT_ID)
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        account_safety_module.AccountSafetyAdmissionRepairReceipt(
            account_id=ACCOUNT_ID,
            operation_id="s6-admission-repair-006",
            authorized_by="other-host",
            repaired_at_ms=NOW_MS,
            maintenance_generation=1,
            clerk_fence_generation=2,
            removed_markers=(),
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AccountSafetyCorruptError, match="different Clerk fence"):
        repair_account_safety_admission(
            tmp_path,
            ACCOUNT_ID,
            operation_id="s6-admission-repair-006",
            authorized_by="this-host",
            repaired_at_ms=NOW_MS,
            clerk_fence_generation=1,
        )


@pytest.mark.asyncio
async def test_fill_after_retirement_pending_requires_broker_clearance_before_reopening(
    tmp_path: Path,
) -> None:
    strategy_instance_id = "retired-amd"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-late-fill",
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        run_id="run-retired-amd",
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        intent_id="late-fill-intent",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:late-fill-intent",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=intent.run_id,
            bot_order_namespace=intent.bot_order_namespace,
            lifecycle_state="ACTIVE",
            recorded_at_ms=NOW_MS,
            source="test",
        ),
    )
    AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID).record_intent(
        intent,
        validate_intent=lambda _intent: None,
    )
    transition_path = stable_bot_retirement_transition_path(tmp_path, strategy_instance_id)
    transition_path.parent.mkdir(parents=True, exist_ok=True)
    transition_path.write_text(
        BotRetirementTransitionRecord(
            strategy_instance_id=strategy_instance_id,
            targets=(BotRetirementBindingTarget(account_id=ACCOUNT_ID, run_id=intent.run_id),),
            retained_custody=(),
            prepared_at_ms=NOW_MS,
            updated_by="operator",
            reason="snapshot completed before a late broker fill",
        ).model_dump_json(),
        encoding="utf-8",
    )
    epoch = _epoch_authority(tmp_path)
    epoch.initialize()
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        epoch_authority=epoch,
        safety_authority=safety,
    )

    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT_ID,
            order_ref=intent.order_ref,
            order_id=17,
            exec_id="late-fill-exec",
            event_type="fill",
            symbol="AMD",
            side="BUY",
            fill_quantity=1.0,
            ts_ms=NOW_MS + 1,
        )
    )

    state = safety.read()
    assert state.verdict is AccountSafetyVerdict.SUSPENDED
    assert state.suspension is not None
    assert state.suspension.custody[0].strategy_instance_id == strategy_instance_id
    assert state.suspension.requires_broker_clearance is True
    assert epoch.read().status == "INVALID"
    assert epoch.read().would_block_reason == "RETIRED_OWNER_EXPOSURE"

    # A reconnect replay is idempotent broker delivery, not new live custody.
    # Clear the broker-observation prerequisite, bind a fresh current epoch,
    # and prove the terminal callback did not leave journal custody behind.
    safety.observe_broker_retired_owner_custody((), observed_at_ms=NOW_MS + 2)
    safety.bind_reconciliation(epoch.read())
    clean = epoch.complete_reconciliation(_clean_proof(epoch.read()))
    assert safety.lift_if_proven(epoch=clean, retained_custody=()).verdict is AccountSafetyVerdict.CLEAN

    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT_ID,
            order_ref=intent.order_ref,
            order_id=17,
            exec_id="late-fill-exec",
            event_type="fill",
            symbol="AMD",
            side="BUY",
            fill_quantity=1.0,
            ts_ms=NOW_MS + 1,
        )
    )

    assert safety.read().verdict is AccountSafetyVerdict.CLEAN


@pytest.mark.asyncio
async def test_daemon_retirement_proposal_preserves_clerk_custody_before_writing_retired_binding(
    tmp_path: Path,
) -> None:
    strategy_instance_id = "daemon-retired-amd"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-daemon-retire",
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        run_id="run-daemon-retired-amd",
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        intent_id="daemon-retirement-a0",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:daemon-retirement-a0",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID).record_intent(
        intent,
        validate_intent=lambda _intent: None,
    )
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=intent.run_id,
            bot_order_namespace=intent.bot_order_namespace,
            lifecycle_state="ACTIVE",
            recorded_at_ms=NOW_MS,
            source="test",
        ),
    )
    retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset(),
        now_ms=NOW_MS + 1,
    )
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS + 2,
    )
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        safety_authority=safety,
    )

    folded = await clerk.fold_binding_retirement_proposals()

    assert folded.retirements_applied == 1
    suspended = safety.read()
    assert suspended.verdict is AccountSafetyVerdict.SUSPENDED
    assert suspended.suspension is not None
    assert suspended.suspension.requires_broker_clearance is True
    assert suspended.suspension.custody == (
        RetiredOwnerCustody(
            account_id=ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
        ),
    )


def test_retirement_preserves_nonterminal_clerk_custody_before_terminal_lifecycle(tmp_path: Path) -> None:
    strategy_instance_id = "retired-amd"
    run_id = "run-retired-amd"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-retire",
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        intent_id="intent-retire-pending",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:intent-retire-pending",
        intent_kind="STRATEGY",
        order_spec={"symbol": "AMD"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT_ID).record_intent(
        intent,
        validate_intent=lambda _intent: None,
    )
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT_ID,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            bot_order_namespace=intent.bot_order_namespace,
            lifecycle_state="ACTIVE",
            recorded_at_ms=NOW_MS,
            source="test",
        ),
    )
    run_dir = tmp_path / "live_runs" / run_id
    run_dir.mkdir(parents=True)
    write_ledger(
        run_dir / "run_ledger.json",
        LiveRunLedger(
            run_id=run_id,
            code_sha="abc123",
            strategy_instance_id=strategy_instance_id,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="spec-sha",
            qc_audit_copy_path="qc.py",
            qc_audit_copy_sha256="qc-sha",
            qc_cloud_backtest_id="qc-1",
            account_id=ACCOUNT_ID,
            start_date_ms=NOW_MS,
            live_config={},
        ),
    )

    retired = retire_bot_lifecycle_and_bindings(
        tmp_path,
        strategy_instance_id,
        run_ids=[run_id],
        updated_by="operator",
        reason="retire while A0 remains in Clerk custody",
        now_ms=NOW_MS,
    )

    transition = stable_bot_retirement_transition_path(tmp_path, strategy_instance_id)
    transition_payload = json.loads(transition.read_text(encoding="utf-8"))
    safety = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    ).read()
    lifecycle = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(tmp_path, strategy_instance_id)
    ).read()

    assert retired.phase is BotLifecyclePhase.RETIRED
    assert lifecycle is not None and lifecycle.phase is BotLifecyclePhase.RETIRED
    assert transition_payload["retained_custody"] == [
        {
            "account_id": ACCOUNT_ID,
            "strategy_instance_id": strategy_instance_id,
            "intent_id": intent.intent_id,
            "order_ref": intent.order_ref,
            "evidence_source": "clerk",
        }
    ]
    assert safety.verdict is AccountSafetyVerdict.SUSPENDED
    assert safety.suspension is not None
    assert safety.suspension.requires_broker_clearance is True


# ---------------------------------------------------------------------------
# account_safety_blocks_current_bot — sibling-exemption regression (BUG-11)
# ---------------------------------------------------------------------------

def test_account_safety_blocks_current_bot_blocks_own_sid(tmp_path: Path) -> None:
    """A bot whose own SID is in custody must be blocked."""
    authority = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    authority.suspend_retired_owner_custody(
        (
            RetiredOwnerCustody(
                account_id=ACCOUNT_ID,
                strategy_instance_id="dv-20260727-amd",
                intent_id="intent-amd-001",
                order_ref="learn-ai/dv-20260727-amd/v1:intent-amd-001",
            ),
        ),
    )
    safety = authority.read()

    assert account_safety_blocks_current_bot(safety, "dv-20260727-amd") is True


def test_account_safety_blocks_current_bot_exempts_sibling_sid(tmp_path: Path) -> None:
    """A bot not named in custody must NOT be blocked by a sibling's suspension.

    This is the root cause of the Jul 27 2026 cascade: AMD crashed with an
    in-flight intent, its namespace was RETIRED, Clerk detected nonterminal
    custody, suspended the account, and all 7 sibling bots were halted at
    their next bar evaluation.  After this fix each sibling's gate provider
    calls account_safety_blocks_current_bot() and sees its own SID is absent
    from custody, so it returns None (no block) instead of a block GateResult.
    """
    authority = AccountSafetyAuthority(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        now_ms=lambda: NOW_MS,
    )
    authority.suspend_retired_owner_custody(
        (
            RetiredOwnerCustody(
                account_id=ACCOUNT_ID,
                strategy_instance_id="dv-20260727-amd",
                intent_id="intent-amd-001",
                order_ref="learn-ai/dv-20260727-amd/v1:intent-amd-001",
            ),
        ),
    )
    safety = authority.read()

    # Siblings (SPY, QQQ, AAPL, …) must not be blocked
    for sibling_sid in ("dv-20260727-spy", "dv-20260727-qqq", "dv-20260727-aapl"):
        assert account_safety_blocks_current_bot(safety, sibling_sid) is False, sibling_sid


def test_cancel_confirmed_entry_resolves_custody() -> None:
    """cancel_confirmed must terminate retired-owner custody.

    Root cause of the 2026-07-29 SUSPENDED→infinite-epoch-invalidation loop:
    smoke-20260729-spy-2 crashed with SubmitUncertainHaltError; one of its
    intents had cancel_submitting + cancel_confirmed (order never reached the
    broker), but _custody_is_terminal did not check cancel_confirmed, so the
    intent was retained as non-terminal custody forever.  The Clerk's
    reconciliation loop kept re-invalidating the epoch (seq 819→835) without
    ever lifting the safety suspension.
    """
    strategy_instance_id = "crashed-spy-2"
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-cancel-terminal",
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        run_id="run-crashed-spy-2",
        bot_order_namespace=bot_order_namespace_for_instance(strategy_instance_id),
        intent_id="intent-cancel-only",
        order_ref=f"learn-ai/{strategy_instance_id}/v1:intent-cancel-only",
        intent_kind="STRATEGY",
        order_spec={"symbol": "SPY"},
        owner_generation=1,
        created_at_ms=NOW_MS,
    )
    entries = [
        AccountClerkJournalEntry(seq=1, entry_kind="recorded", recorded_at_ms=NOW_MS, intent=intent),
        AccountClerkJournalEntry(seq=2, entry_kind="cancel_submitting", recorded_at_ms=NOW_MS, intent=intent),
        AccountClerkJournalEntry(seq=3, entry_kind="cancel_confirmed", recorded_at_ms=NOW_MS, intent=intent),
    ]

    retained = retired_owner_nonterminal_custody(entries, strategy_instance_id=strategy_instance_id)

    assert retained == (), f"cancel_confirmed must terminate custody, got: {retained}"
