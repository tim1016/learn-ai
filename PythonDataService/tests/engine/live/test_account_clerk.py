"""Account Clerk core tests for issue #1016."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.live.account_clerk as account_clerk_module
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import read_account_clerk_lease
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournalEntry,
    AccountClerkLeaseWriter,
    AccountClerkRecordedReceipt,
    AccountClerkRpcClient,
    AccountClerkRpcServer,
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
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))

    async def place_order(self, order: object) -> object:
        self.calls.append(order)
        return SimpleNamespace(order_id=101, perm_id=201, exec_id="exec-1")


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
    assert read_account_clerk_inbox(tmp_path, ACCOUNT) == []
    journal = read_account_clerk_journal(tmp_path, ACCOUNT)
    assert journal[0].intent == intent
    assert journal[0].seq == receipt.journal_seq

    restarted_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    assert restarted_clerk.replay_recorded_receipts() == [receipt]


@pytest.mark.asyncio
async def test_clerk_records_before_paper_broker_submit_and_deduplicates_ack(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        now_ms=lambda: START_MS + 1,
    )
    intent = _intent("bot-a", "run-a", "intent-a")

    recorded, acked = await clerk.submit_intent(intent)
    repeated_recorded, repeated_acked = await clerk.submit_intent(intent)

    assert recorded.status == "recorded"
    assert acked.status == "broker_acked"
    assert acked.order_id == 101
    assert repeated_recorded == recorded
    assert repeated_acked == acked
    assert len(broker.calls) == 1
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "broker_acked",
    ]
    assert clerk.replay_recorded_receipts() == [recorded]


@pytest.mark.asyncio
async def test_bot_rpc_reaches_clerk_without_a_bot_broker_adapter(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    )
    await server.start()
    try:
        receipt = await AccountClerkRpcClient(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
        ).submit(_intent("bot-a", "run-a", "intent-rpc"))
    finally:
        await server.close()

    assert receipt.status == "broker_acked"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_clerk_relays_callbacks_only_to_the_originating_namespace(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    _write_active_binding(tmp_path, "bot-b", "run-b")
    server = AccountClerkRpcServer(AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT))
    await server.start()
    try:
        intent_a = _intent("bot-a", "run-a", "intent-a")
        await server._clerk.record_intent(intent_a)
        server._intents_by_order_ref[intent_a.order_ref] = intent_a
        server._record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=101,
                event_type="fill",
                order_ref=intent_a.order_ref,
                fill_quantity=1,
                avg_fill_price=100,
                ts_ms=START_MS,
            )
        )
        client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
        bot_b_events = await client.drain_events(
            bot_order_namespace=bot_order_namespace_for_instance("bot-b")
        )
        bot_a_events = await client.drain_events(bot_order_namespace=intent_a.bot_order_namespace)
    finally:
        await server.close()

    assert bot_b_events == []
    assert [event.order_ref for event in bot_a_events] == [intent_a.order_ref]
    assert read_account_clerk_journal(tmp_path, ACCOUNT)[-1].entry_kind == "broker_event"


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

    assert read_account_clerk_inbox(tmp_path, ACCOUNT) == []
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


def test_clerk_lease_writer_renews_and_drains(tmp_path: Path) -> None:
    clock = iter((START_MS, START_MS + 1_000, START_MS + 2_000))
    writer = AccountClerkLeaseWriter(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        generation=2,
        pid=123,
        now_ms=lambda: next(clock),
    )

    running = writer.renew()
    draining = writer.renew(draining=True)

    assert running.status == "RUNNING"
    assert running.valid_until_ms == START_MS + 6_000
    assert draining.status == "DRAINING"
    assert draining.valid_until_ms == START_MS + 2_000
    assert read_account_clerk_lease(tmp_path, ACCOUNT) == draining
