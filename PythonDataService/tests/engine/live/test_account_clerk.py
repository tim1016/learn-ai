"""Account Clerk core tests for issue #1016."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import app.engine.live.account_clerk as account_clerk_module
from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournalEntry,
    AccountClerkRecordedReceipt,
    read_account_clerk_inbox,
    read_account_clerk_journal,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.order_identity import build_order_ref

ACCOUNT = "DU123456"
START_MS = 1_784_000_000_000


class _FakeBroker:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def place_order(self, order: object) -> None:
        self.calls.append(order)


def _write_active_binding(tmp_path: Path, instance_id: str, run_id: str) -> None:
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id=instance_id,
            run_id=run_id,
            bot_order_namespace=bot_order_namespace_for_instance(instance_id),
            lifecycle_state="ACTIVE",
            recorded_at_ms=START_MS,
            source="test",
        ),
    )


def _intent(instance_id: str, run_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    namespace = bot_order_namespace_for_instance(instance_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=ACCOUNT,
        strategy_instance_id=instance_id,
        run_id=run_id,
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=build_order_ref(namespace, intent_id),
        intent_kind="STRATEGY",
        order_spec=IbkrOrderSpec(
            symbol="SPY",
            sec_type="STK",
            action="BUY",
            quantity=1,
            order_type="MKT",
            time_in_force="DAY",
            confirm_paper=True,
            client_order_id=f"client-{intent_id}",
            order_ref=build_order_ref(namespace, intent_id),
        ).model_dump(),
        owner_generation=99,
        created_at_ms=START_MS,
    )


@pytest.mark.asyncio
async def test_record_intent_writes_replayable_journal_and_receipt_before_broker_contact(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        now_ms=lambda: START_MS + 1,
    )
    intent = _intent("bot-a", "run-a", "intent-a")

    receipt = await clerk.record_intent(intent)

    assert receipt.status == "recorded"
    assert receipt.trace_id == intent.trace_id
    assert receipt.intent_id == intent.intent_id
    assert receipt.order_ref == intent.order_ref
    assert receipt.recorded_at_ms == START_MS + 1
    assert broker.calls == []
    assert read_account_clerk_inbox(tmp_path, ACCOUNT)[0].intent == intent
    journal = read_account_clerk_journal(tmp_path, ACCOUNT)
    assert journal[0].intent == intent
    assert journal[0].seq == receipt.journal_seq

    restarted_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    assert restarted_clerk.replay_recorded_receipts() == [receipt]
    assert broker.calls == []


@pytest.mark.asyncio
async def test_recover_inbox_replays_durable_intent_after_crash_before_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        now_ms=lambda: START_MS + 2,
    )
    original_append = account_clerk_module._append_jsonl

    def crash_between_inbox_and_journal(
        path: Path,
        entry: AccountClerkInboxEntry | AccountClerkJournalEntry,
    ) -> None:
        if isinstance(entry, AccountClerkJournalEntry):
            raise OSError("simulated process crash before journal fsync")
        original_append(path, entry)

    monkeypatch.setattr(account_clerk_module, "_append_jsonl", crash_between_inbox_and_journal)
    intent = _intent("bot-a", "run-a", "crash-intent")
    with pytest.raises(OSError, match="simulated process crash"):
        await clerk.record_intent(intent)
    monkeypatch.setattr(account_clerk_module, "_append_jsonl", original_append)

    restarted_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    receipts = await restarted_clerk.recover_inbox()

    assert read_account_clerk_inbox(tmp_path, ACCOUNT)[0].intent == intent
    assert [entry.intent for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [intent]
    assert receipts[0].intent_id == intent.intent_id
    assert receipts[0].recorded_at_ms == START_MS + 2
    assert broker.calls == []


@pytest.mark.asyncio
async def test_clerk_offloads_record_and_recovery_durable_work_to_a_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    worker_thread_ids: set[int] = set()
    original_record = AccountClerk._record_intent_locked
    original_recover = AccountClerk._recover_inbox_locked

    def record_in_worker(
        self: AccountClerk,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkRecordedReceipt:
        worker_thread_ids.add(threading.get_ident())
        return original_record(self, intent)

    def recover_in_worker(self: AccountClerk) -> list[AccountClerkRecordedReceipt]:
        worker_thread_ids.add(threading.get_ident())
        return original_recover(self)

    monkeypatch.setattr(AccountClerk, "_record_intent_locked", record_in_worker)
    monkeypatch.setattr(AccountClerk, "_recover_inbox_locked", recover_in_worker)

    await clerk.record_intent(_intent("bot-a", "run-a", "intent-a"))
    await clerk.recover_inbox()

    assert worker_thread_ids
    assert threading.get_ident() not in worker_thread_ids


@pytest.mark.asyncio
async def test_clerk_characterizes_three_bots_with_third_joining_without_sibling_invalidation(tmp_path: Path) -> None:
    """2026-07-14 regression: a third bot must not fence existing siblings."""

    identities = (("bot-a", "run-a"), ("bot-b", "run-b"), ("bot-c", "run-c"))
    for instance_id, run_id in identities:
        _write_active_binding(tmp_path, instance_id, run_id)
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)

    first_round = asyncio.Barrier(2)

    async def record_first_round(
        instance_id: str,
        run_id: str,
        intent_id: str,
    ) -> AccountClerkRecordedReceipt:
        await first_round.wait()
        return await clerk.record_intent(_intent(instance_id, run_id, intent_id))

    first_a = asyncio.create_task(record_first_round("bot-a", "run-a", "a-1"))
    first_b = asyncio.create_task(record_first_round("bot-b", "run-b", "b-1"))
    first_receipts = await asyncio.gather(first_a, first_b)
    third_receipt = await clerk.record_intent(_intent("bot-c", "run-c", "c-1"))

    continued_round = asyncio.Barrier(3)

    async def record_continued_round(
        instance_id: str,
        run_id: str,
        intent_id: str,
    ) -> AccountClerkRecordedReceipt:
        await continued_round.wait()
        return await clerk.record_intent(_intent(instance_id, run_id, intent_id))

    continued_receipts = await asyncio.gather(
        *[
            record_continued_round(instance_id, run_id, f"{instance_id}-2")
            for instance_id, run_id in identities
        ]
    )

    assert [receipt.status for receipt in [*first_receipts, third_receipt, *continued_receipts]] == [
        "recorded",
        "recorded",
        "recorded",
        "recorded",
        "recorded",
        "recorded",
    ]
    journal = read_account_clerk_journal(tmp_path, ACCOUNT)
    assert [entry.seq for entry in journal] == list(range(1, 7))
    assert {entry.intent.strategy_instance_id for entry in journal} == {"bot-a", "bot-b", "bot-c"}
    assert broker.calls == []


@pytest.mark.asyncio
async def test_record_intent_rejects_superseded_run_without_blocking_sibling(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a-old")
    _write_active_binding(tmp_path, "bot-b", "run-b")
    _write_active_binding(tmp_path, "bot-a", "run-a-current")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    ready = asyncio.Barrier(2)

    async def stale_submit() -> None:
        await ready.wait()
        await clerk.record_intent(_intent("bot-a", "run-a-old", "stale-a"))

    async def sibling_submit() -> AccountClerkRecordedReceipt:
        await ready.wait()
        return await clerk.record_intent(_intent("bot-b", "run-b", "live-b"))

    stale_task = asyncio.create_task(stale_submit())
    sibling_task = asyncio.create_task(sibling_submit())
    sibling_receipt = await sibling_task
    with pytest.raises(AccountClerkIntentRejected) as rejected:
        await stale_task

    assert rejected.value.reason == "CLERK_STALE_RUN"
    assert sibling_receipt.status == "recorded"
    assert [entry.intent.intent_id for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == ["live-b"]
