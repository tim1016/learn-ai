"""Focused failure-injection coverage for the operator-only journal ceremony."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrPosition, IbkrPositionsSnapshot
from app.engine.live.account_artifacts import account_artifacts_root, read_account_freeze
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_clerk_journal import (
    AccountClerkJournal,
    AccountClerkJournalCorruptError,
    account_clerk_inbox_path,
    account_clerk_journal_path,
    read_account_clerk_journal,
    seed_account_clerk_broker_evidence_baseline,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkInboxEntry,
    AccountClerkPositionEvidence,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.journal_recovery_state import (
    JournalRecoveryStateCorruptError,
    assess_journal_recovery_fence,
    journal_recovery_state_path,
)
from app.schemas.artifact_io import atomic_write_pydantic_artifact
from app.schemas.journal_recovery import JournalRecoveryPosition, JournalRecoveryState
from app.services.account_start_gate import AccountStartGateError, ensure_account_start_gate
from app.services.journal_recovery import JournalRecoveryError, JournalRecoveryService


def _snapshot(account_id: str) -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id=account_id,
        is_paper=True,
        fetched_at_ms=1_780_000_000_100,
        positions=[
            IbkrPosition(
                account_id=account_id,
                con_id=101,
                symbol="SPY",
                sec_type="STK",
                quantity=2.0,
                avg_cost=500.0,
                fetched_at_ms=1_780_000_000_100,
            )
        ],
    )


def _empty_snapshot(account_id: str) -> IbkrPositionsSnapshot:
    return IbkrPositionsSnapshot(
        account_id=account_id,
        is_paper=True,
        fetched_at_ms=1_780_000_000_100,
        positions=[],
    )


def _intent(account_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    namespace = "journal-recovery-test"
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=account_id,
        strategy_instance_id="strategy-1",
        run_id="run-1",
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=f"{namespace}:{intent_id}",
        intent_kind="ORDER",
        order_spec={},
        owner_generation=1,
        created_at_ms=1_780_000_000_001,
    )


def test_quarantine_retains_torn_journal_and_rebaseline_seeds_broker_evidence_only(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    torn_bytes = b'{"seq":1\n'
    journal.write_bytes(torn_bytes)
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    quarantined = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")

    assert quarantined.phase == "REBASELINE_REQUIRED"
    assert not journal.exists()
    evidence = account_artifacts_root(tmp_path, account_id) / (quarantined.quarantined_journal_name or "")
    assert evidence.read_bytes() == torn_bytes

    completed = service.rebaseline(account_id=account_id, idempotency_key="rebaseline-1", snapshot=_snapshot(account_id))

    assert completed.phase == "COMPLETE"
    entries = read_account_clerk_journal(tmp_path, account_id)
    assert len(entries) == 1
    assert entries[0].entry_kind == "broker_evidence_baseline"
    assert entries[0].broker_evidence_baseline is not None
    assert entries[0].broker_evidence_baseline.positions[0].symbol == "SPY"
    # The recovery state is the durable, composable hold. It must never
    # overwrite a pre-existing unrelated account freeze to advertise this one.
    assert read_account_freeze(tmp_path, account_id) is None
    fence = assess_journal_recovery_fence(tmp_path, account_id)
    assert fence.reason_code == "CLERK_BROKER_EVIDENCE_ONLY_HOLD"


def test_recovery_steps_replay_without_deleting_quarantined_evidence(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    first = service.quarantine(account_id=account_id, idempotency_key="same-key")
    replay = service.quarantine(account_id=account_id, idempotency_key="same-key")

    assert replay == first
    assert (account_artifacts_root(tmp_path, account_id) / (first.quarantined_journal_name or "")).is_file()

    try:
        service.quarantine(account_id=account_id, idempotency_key="different-key")
    except JournalRecoveryError as exc:
        assert str(exc) == "JOURNAL_RECOVERY_QUARANTINE_IDEMPOTENCY_CONFLICT"
    else:
        raise AssertionError("a different quarantine key must not fabricate a receipt")


def test_quarantine_refuses_a_healthy_journal_under_the_same_writer_lock(tmp_path: Path) -> None:
    account_id = "DU1234567"
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    try:
        service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    except JournalRecoveryError as exc:
        assert str(exc) == "JOURNAL_RECOVERY_JOURNAL_NOT_CORRUPT"
    else:
        raise AssertionError("a healthy journal must never be quarantined")


def test_rebaseline_resumes_the_persisted_snapshot_after_crash_before_completion(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)
    service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    quarantined = service.state(account_id=account_id)
    snapshot = _snapshot(account_id)
    planned = JournalRecoveryState(
        account_id=account_id,
        phase="REBASELINE_PENDING",
        quarantined_journal_name=quarantined.quarantined_journal_name,
        quarantined_inbox_name=quarantined.quarantined_inbox_name,
        quarantined_at_ms=quarantined.quarantined_at_ms,
        quarantine_receipt_id=quarantined.quarantine_receipt_id,
        quarantine_idempotency_key=quarantined.quarantine_idempotency_key,
        baseline_receipt_id="journal-recovery-rebaseline:rebaseline-1",
        rebaseline_idempotency_key="rebaseline-1",
        broker_evidence_positions=(JournalRecoveryPosition(symbol="SPY", signed_quantity=2.0),),
        observed_at_ms=snapshot.fetched_at_ms,
    )
    atomic_write_pydantic_artifact(journal_recovery_state_path(tmp_path, account_id), planned)
    seed_account_clerk_broker_evidence_baseline(
        tmp_path,
        account_id,
        AccountClerkBrokerEvidenceBaseline(
            account_id=account_id,
            observed_at_ms=snapshot.fetched_at_ms,
            positions=(
                AccountClerkPositionEvidence(
                    symbol="SPY",
                    signed_quantity=2.0,
                    evidence_observed_at_ms=snapshot.fetched_at_ms,
                ),
            ),
        ),
    )

    # A cockpit reload creates a fresh request key. The durable PENDING plan,
    # not that transient key, is the authority for this irreversible retry.
    resumed = service.rebaseline(account_id=account_id, idempotency_key="after-reload", snapshot=None)

    assert resumed.phase == "COMPLETE"
    assert resumed.receipt_id == "journal-recovery-rebaseline:rebaseline-1"
    assert service.state(account_id=account_id).phase == "COMPLETE"


def test_quarantine_resumes_persisted_rename_after_cockpit_reload(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    first = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    pending = service.state(account_id=account_id).model_copy(update={"phase": "QUARANTINE_PENDING"})
    atomic_write_pydantic_artifact(journal_recovery_state_path(tmp_path, account_id), pending)

    resumed = service.quarantine(account_id=account_id, idempotency_key="after-reload")

    assert resumed == first
    assert service.state(account_id=account_id).phase == "REBASELINE_REQUIRED"


def test_second_journal_corruption_starts_a_new_recovery_epoch(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    first = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    service.rebaseline(account_id=account_id, idempotency_key="rebaseline-1", snapshot=_empty_snapshot(account_id))
    journal.write_text("new-corruption\n", encoding="utf-8")

    second = service.quarantine(account_id=account_id, idempotency_key="quarantine-2")
    state = service.state(account_id=account_id)

    assert second.phase == "REBASELINE_REQUIRED"
    assert second.receipt_id != first.receipt_id
    assert state.recovery_epoch == 2
    assert state.quarantined_journal_name != first.quarantined_journal_name
    artifacts = account_artifacts_root(tmp_path, account_id)
    assert (artifacts / (first.quarantined_journal_name or "")).is_file()
    assert (artifacts / (second.quarantined_journal_name or "")).is_file()


def test_completed_recovery_replays_its_quarantine_receipt_without_an_empty_inbox(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    first = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    service.rebaseline(account_id=account_id, idempotency_key="rebaseline-1", snapshot=_empty_snapshot(account_id))
    assert not account_clerk_inbox_path(tmp_path, account_id).exists()

    replay = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")

    assert replay == first
    assert service.state(account_id=account_id).recovery_epoch == 1


def test_live_journal_cache_reloads_after_rebaseline_replaces_its_inode(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    ledger = AccountClerkJournal(artifacts_root=tmp_path, account_id=account_id)
    first_intent = _intent(account_id, "first")
    ledger.record_intent(first_intent, validate_intent=lambda _: None)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)
    service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    service.rebaseline(account_id=account_id, idempotency_key="rebaseline-1", snapshot=_empty_snapshot(account_id))

    ledger.record_intent(_intent(account_id, "second"), validate_intent=lambda _: None)

    entries = read_account_clerk_journal(tmp_path, account_id)
    assert [entry.seq for entry in entries] == [1, 2]
    assert entries[0].entry_kind == "broker_evidence_baseline"
    assert entries[1].intent is not None
    assert entries[1].intent.intent_id == "second"


def test_live_journal_cache_cannot_mask_in_place_corruption(tmp_path: Path) -> None:
    account_id = "DU1234567"
    ledger = AccountClerkJournal(artifacts_root=tmp_path, account_id=account_id)
    ledger.record_intent(_intent(account_id, "first"), validate_intent=lambda _: None)
    ledger.snapshot()
    account_clerk_journal_path(tmp_path, account_id).write_text("not-json\n", encoding="utf-8")

    with pytest.raises(AccountClerkJournalCorruptError, match="invalid row"):
        ledger.record_intent(_intent(account_id, "second"), validate_intent=lambda _: None)


def test_pending_ceremony_blocks_an_existing_clerk_journal_writer(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)
    service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    pending = service.state(account_id=account_id).model_copy(update={"phase": "QUARANTINE_PENDING"})
    atomic_write_pydantic_artifact(journal_recovery_state_path(tmp_path, account_id), pending)

    with pytest.raises(AccountClerkJournalCorruptError, match="QUARANTINE_PENDING"):
        AccountClerkJournal(artifacts_root=tmp_path, account_id=account_id).recover_inbox()


def test_quarantine_retains_the_paired_inbox_and_never_replays_it_into_baseline(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    inbox = account_clerk_inbox_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    inbox.write_text(
        AccountClerkInboxEntry(
            seq=2,
            received_at_ms=1_780_000_000_001,
            intent=_intent(account_id, "pre-corruption"),
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    quarantined = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    state = service.state(account_id=account_id)

    assert not inbox.exists()
    quarantined_inbox = inbox.with_name(state.quarantined_inbox_name or "")
    assert "pre-corruption" in quarantined_inbox.read_text(encoding="utf-8")
    service.rebaseline(account_id=account_id, idempotency_key="rebaseline-1", snapshot=_empty_snapshot(account_id))

    entries = read_account_clerk_journal(tmp_path, account_id)
    assert [entry.entry_kind for entry in entries] == ["broker_evidence_baseline"]
    assert quarantined.phase == "REBASELINE_REQUIRED"


def test_inbox_only_corruption_enters_the_same_paired_quarantine_ceremony(tmp_path: Path) -> None:
    account_id = "DU1234567"
    inbox = account_clerk_inbox_path(tmp_path, account_id)
    inbox.parent.mkdir(parents=True)
    inbox.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)

    quarantined = service.quarantine(account_id=account_id, idempotency_key="quarantine-1")
    state = service.state(account_id=account_id)

    assert quarantined.phase == "REBASELINE_REQUIRED"
    assert quarantined.quarantined_journal_name is None
    assert state.missing_artifacts == ("journal",)
    assert not inbox.exists()
    assert (inbox.with_name(state.quarantined_inbox_name or "")).is_file()


def test_rebaseline_rejects_non_paper_or_non_finite_broker_snapshot(tmp_path: Path) -> None:
    account_id = "DU1234567"
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True)
    journal.write_text("not-json\n", encoding="utf-8")
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)
    service.quarantine(account_id=account_id, idempotency_key="quarantine-1")

    with pytest.raises(JournalRecoveryError, match="JOURNAL_RECOVERY_PAPER_BROKER_REQUIRED"):
        service.rebaseline(
            account_id=account_id,
            idempotency_key="rebaseline-1",
            snapshot=_empty_snapshot(account_id).model_copy(update={"is_paper": False}),
        )
    non_finite = _snapshot(account_id).model_copy(
        update={"positions": [_snapshot(account_id).positions[0].model_copy(update={"quantity": float("nan")})]}
    )
    with pytest.raises(JournalRecoveryError, match="JOURNAL_RECOVERY_INVALID_BROKER_SNAPSHOT"):
        service.rebaseline(account_id=account_id, idempotency_key="rebaseline-2", snapshot=non_finite)

    assert service.state(account_id=account_id).phase == "REBASELINE_REQUIRED"


@pytest.mark.asyncio
async def test_recovery_claim_waits_for_an_already_admitted_broker_write(tmp_path: Path) -> None:
    account_id = "DU1234567"
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=account_id)
    service = JournalRecoveryService(artifacts_root=tmp_path, now_ms=lambda: 1_780_000_000_000)
    entered_write = asyncio.Event()
    release_write = asyncio.Event()
    writes: list[str] = []

    async def broker_write() -> None:
        entered_write.set()
        await release_write.wait()
        writes.append("completed")

    broker_task = asyncio.create_task(clerk._run_broker_write("test.recovery_race", broker_write))
    await entered_write.wait()
    journal = account_clerk_journal_path(tmp_path, account_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not-json\n", encoding="utf-8")
    quarantine_task = asyncio.create_task(
        asyncio.to_thread(service.quarantine, account_id=account_id, idempotency_key="quarantine-1")
    )
    await asyncio.sleep(0.01)

    assert not quarantine_task.done()
    release_write.set()
    await broker_task
    quarantined = await quarantine_task

    assert writes == ["completed"]
    assert quarantined.phase == "REBASELINE_REQUIRED"


def test_malformed_recovery_state_is_an_account_write_fence(tmp_path: Path) -> None:
    account_id = "DU1234567"
    path = journal_recovery_state_path(tmp_path, account_id)
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    try:
        assess_journal_recovery_fence(tmp_path, account_id)
    except JournalRecoveryStateCorruptError:
        pass
    else:
        raise AssertionError("unreadable recovery state must fail closed")


@pytest.mark.asyncio
async def test_clerk_refuses_every_broker_write_while_broker_evidence_is_unowned(tmp_path: Path) -> None:
    account_id = "DU1234567"
    atomic_write_pydantic_artifact(
        journal_recovery_state_path(tmp_path, account_id),
        JournalRecoveryState(
            account_id=account_id,
            phase="COMPLETE",
            quarantined_journal_name="clerk_journal.jsonl.corrupt-1780000000000",
            quarantined_inbox_name="clerk_inbox.jsonl.corrupt-1780000000000",
            quarantined_at_ms=1_780_000_000_000,
            quarantine_receipt_id="journal-recovery-quarantine:quarantine-1",
            quarantine_idempotency_key="quarantine-1",
            baseline_receipt_id="journal-recovery-rebaseline:rebaseline-1",
            rebaseline_idempotency_key="rebaseline-1",
            broker_evidence_positions=(JournalRecoveryPosition(symbol="SPY", signed_quantity=2.0),),
            observed_at_ms=1_780_000_000_100,
        ),
    )
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=account_id)
    writes: list[str] = []

    async def broker_write() -> None:
        writes.append("called")

    with pytest.raises(RuntimeError, match="CLERK_BROKER_EVIDENCE_ONLY_HOLD"):
        await clerk._run_broker_write("test.journal_recovery", broker_write)

    assert writes == []


@pytest.mark.asyncio
async def test_interactive_admission_refuses_unowned_broker_evidence_before_broker_refresh(tmp_path: Path) -> None:
    account_id = "DU1234567"
    atomic_write_pydantic_artifact(
        journal_recovery_state_path(tmp_path, account_id),
        JournalRecoveryState(
            account_id=account_id,
            phase="COMPLETE",
            quarantined_journal_name="clerk_journal.jsonl.corrupt-1780000000000",
            quarantined_inbox_name="clerk_inbox.jsonl.corrupt-1780000000000",
            quarantined_at_ms=1_780_000_000_000,
            quarantine_receipt_id="journal-recovery-quarantine:quarantine-1",
            quarantine_idempotency_key="quarantine-1",
            baseline_receipt_id="journal-recovery-rebaseline:rebaseline-1",
            rebaseline_idempotency_key="rebaseline-1",
            broker_evidence_positions=(JournalRecoveryPosition(symbol="SPY", signed_quantity=2.0),),
            observed_at_ms=1_780_000_000_100,
        ),
    )

    with pytest.raises(AccountStartGateError) as error:
        await ensure_account_start_gate(
            tmp_path,
            account_id=account_id,
            daemon_url="http://daemon.invalid",
            requested_authority="account_truth",
            client=object(),  # type: ignore[arg-type]
            now_ms=1_780_000_000_200,
            current_now_ms=lambda: 1_780_000_000_200,
        )

    assert error.value.detail["reason_code"] == "CLERK_BROKER_EVIDENCE_ONLY_HOLD"
