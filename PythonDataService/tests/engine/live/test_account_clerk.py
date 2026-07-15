"""Account Clerk core tests for issue #1016."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.live.account_clerk as account_clerk_module
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    advance_account_clerk_generation,
    read_account_clerk_generation,
    read_account_clerk_lease,
    read_account_events,
    read_account_freeze,
)
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkGenerationFencedError,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournalCorruptError,
    AccountClerkJournalEntry,
    AccountClerkLeaseWriter,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    AccountClerkRpcClient,
    AccountClerkRpcServer,
    account_clerk_authority_lock,
    read_account_clerk_inbox,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_reconciler import AccountClerkReconciler, namespace_expected_exposure
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.order_identity import build_order_ref

ACCOUNT = "DU123456"
START_MS = 1_784_000_000_000


def _write_active_clerk_generation(tmp_path: Path) -> int:
    return advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation


def _hold_clerk_authority_lock(
    artifacts_root: Path,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    with account_clerk_authority_lock(artifacts_root, ACCOUNT):
        acquired.set()
        release.wait(timeout=10)


class _FakeBroker:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))

    async def place_order(self, order: object) -> object:
        self.calls.append(order)
        return SimpleNamespace(order_id=101, perm_id=201, exec_id="exec-1")


class _ReconciliationBroker(_FakeBroker):
    def __init__(self, probe: str) -> None:
        super().__init__()
        self.probe = probe

    async def probe_intent_status(self, _intent_id: str, _order_ref: str) -> str:
        return self.probe


class _UncertainBroker(_ReconciliationBroker):
    async def place_order(self, order: object) -> object:
        self.calls.append(order)
        raise TimeoutError("simulated lost acknowledgement")


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


def _recovery_intent(instance_id: str, run_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    return _intent(instance_id, run_id, intent_id).model_copy(
        update={"intent_kind": "RECOVERY_FLATTEN"}
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
        "broker_submitting",
        "broker_acked",
    ]
    assert clerk.replay_recorded_receipts() == [recorded]


@pytest.mark.asyncio
async def test_fenced_bot_can_recovery_flatten_its_retired_namespace(tmp_path: Path) -> None:
    """Regression for the 2026-07-14 fenced-flatten trap."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
            lifecycle_state="RETIRED",
            recorded_at_ms=START_MS + 1,
            source="test",
        ),
    )
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    normal = _intent("bot-a", "run-a", "normal")
    recovery = _recovery_intent("bot-a", "run-a", "recovery")

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_INACTIVE_BINDING"):
        await clerk.submit_intent(normal)
    receipt = await clerk.submit_recovery_flatten(
        recovery,
        actor="bot",
        actor_strategy_instance_id="bot-a",
        actor_run_id="run-a",
        actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
    )

    assert isinstance(receipt, AccountClerkRecoveryFlattenReceipt)
    assert receipt.broker_acked.status == "broker_acked"
    assert len(broker.calls) == 1
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "recovery_cancelled",
        "broker_acked",
    ]


@pytest.mark.asyncio
async def test_recovery_flatten_rejects_bot_targeting_a_sibling_namespace(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    _write_active_binding(tmp_path, "bot-b", "run-b")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RECOVERY_ACTOR_MISMATCH"):
        await clerk.submit_recovery_flatten(
            _recovery_intent("bot-b", "run-b", "recovery-b"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )


@pytest.mark.asyncio
async def test_operator_recovery_cure_only_flattens_a_retired_namespace_and_writes_receipts(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
            lifecycle_state="RETIRED",
            recorded_at_ms=START_MS + 1,
            source="test",
        ),
    )
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)

    receipt = await clerk.submit_recovery_flatten(
        _recovery_intent("bot-a", "run-a", "operator-cure"),
        actor="operator",
    )

    assert receipt.status == "recovery_flattened"
    assert receipt.recorded.intent_id == "operator-cure"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("PRESENT", "RECOVER_ADOPT"),
        ("PROVABLY_ABSENT", "RETRY_ONCE"),
        ("NOT_PROVABLE", "HALT"),
    ],
)
async def test_clerk_reconciler_resolves_uncertain_receipt_with_state_machine(
    tmp_path: Path,
    probe: str,
    expected: str,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _ReconciliationBroker(probe)
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _intent("bot-a", "run-a", f"uncertain-{probe}")
    await clerk.record_intent(intent)

    [resolution] = await AccountClerkReconciler(clerk, now_ms=lambda: START_MS + 2).reconcile_once()

    assert resolution.verdict.value == expected
    events = read_account_events(tmp_path, ACCOUNT)
    assert events[-1]["event_type"] == "account_clerk_reconciliation_resolved"
    assert events[-1]["verdict"] == expected
    if probe == "PROVABLY_ABSENT":
        assert len(broker.calls) == 1
    if probe == "NOT_PROVABLE":
        assert read_account_freeze(tmp_path, ACCOUNT) is not None


@pytest.mark.asyncio
async def test_uncertain_broker_ack_is_durable_and_reconciled_immediately(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _UncertainBroker("PRESENT")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _intent("bot-a", "run-a", "lost-ack")

    with pytest.raises(TimeoutError, match="lost acknowledgement"):
        await clerk.submit_intent(intent)
    [resolution] = await AccountClerkReconciler(clerk).reconcile_once()

    assert resolution.verdict.value == "RECOVER_ADOPT"
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "broker_submitting",
        "broker_uncertain",
        "reconciliation",
    ]


@pytest.mark.asyncio
async def test_journal_exposure_survives_bot_crash_and_deduplicates_execution(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    intent = _intent("bot-a", "run-a", "fill-a")
    await clerk.record_intent(intent)
    fill = IbkrOrderEvent(
        account_id=ACCOUNT,
        order_id=101,
        event_type="fill",
        order_ref=intent.order_ref,
        symbol="SPY",
        side="BUY",
        fill_quantity=2,
        exec_id="exec-fill-a",
        ts_ms=START_MS,
    )
    clerk.append_broker_event(intent, fill)
    clerk.append_broker_event(intent, fill)

    restarted_entries = read_account_clerk_journal(tmp_path, ACCOUNT)
    [exposure] = namespace_expected_exposure(restarted_entries)
    assert exposure.bot_order_namespace == intent.bot_order_namespace
    assert exposure.symbol == "SPY"
    assert exposure.quantity == 2


@pytest.mark.asyncio
async def test_bot_rpc_reaches_clerk_without_a_bot_broker_adapter(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _FakeBroker()
    server = AccountClerkRpcServer(
        AccountClerk(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
            broker=broker,
            clerk_generation=generation,
        )
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


def test_two_real_processes_cannot_acquire_account_clerk_authority_together(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    first_acquired = context.Event()
    second_acquired = context.Event()
    release = context.Event()
    first = context.Process(
        target=_hold_clerk_authority_lock,
        args=(tmp_path, first_acquired, release),
    )
    second = context.Process(
        target=_hold_clerk_authority_lock,
        args=(tmp_path, second_acquired, release),
    )
    first.start()
    try:
        assert first_acquired.wait(timeout=5)
        second.start()
        assert not second_acquired.wait(timeout=0.25)
        release.set()
        assert second_acquired.wait(timeout=5)
    finally:
        release.set()
        for process in (first, second):
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0


@pytest.mark.asyncio
async def test_clerk_process_acquires_lock_before_broker_connect_and_releases_after_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _write_active_clerk_generation(tmp_path)
    events: list[str] = []

    class _StoppedEvent:
        def set(self) -> None:
            events.append("stop")

        def is_set(self) -> bool:
            return True

        async def wait(self) -> None:
            return None

    class _OrderedLock:
        def __enter__(self) -> None:
            events.append("lock_acquired")

        def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
            events.append("lock_released")

    class _Client:
        settings = SimpleNamespace(mode="paper")

        async def connect(self) -> None:
            assert "lock_acquired" in events
            events.append("broker_connect")

        async def disconnect(self) -> None:
            events.append("broker_disconnect")

    class _BrokerAdapter:
        def __init__(self, _client: object) -> None:
            pass

        def require_account_owner_write_fence(self, provider: object) -> None:
            assert callable(provider)

    class _Server:
        def __init__(self, _clerk: AccountClerk) -> None:
            pass

        async def start(self) -> None:
            events.append("socket_serve")

        async def close(self) -> None:
            events.append("socket_closed")

    class _Reconciler:
        def __init__(self, _clerk: AccountClerk) -> None:
            pass

        async def start(self) -> None:
            events.append("reconciler_start")

        async def close(self) -> None:
            events.append("reconciler_closed")

    from app.broker.ibkr import client as ibkr_client_module
    from app.engine.live import account_clerk_reconciler, account_clerk_rpc, live_portfolio

    monkeypatch.setattr(account_clerk_module, "account_clerk_authority_lock", lambda *_args: _OrderedLock())
    monkeypatch.setattr(account_clerk_module.asyncio, "Event", _StoppedEvent)
    monkeypatch.setattr(account_clerk_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ibkr_client_module, "IbkrClient", _Client)
    monkeypatch.setattr(live_portfolio, "IbkrBrokerAdapter", _BrokerAdapter)
    monkeypatch.setattr(account_clerk_rpc, "AccountClerkRpcServer", _Server)
    monkeypatch.setattr(account_clerk_reconciler, "AccountClerkReconciler", _Reconciler)

    await account_clerk_module._run_clerk_process(
        SimpleNamespace(artifacts_root=str(tmp_path), account_id=ACCOUNT, generation=generation)
    )

    assert events.index("lock_acquired") < events.index("broker_connect") < events.index("socket_serve")
    assert events.index("broker_disconnect") < events.index("lock_released")


@pytest.mark.asyncio
async def test_durable_generation_change_rejects_write_and_terminates_stale_clerk(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _FakeBroker()
    fenced = threading.Event()

    def durable_generation_provider() -> int | None:
        durable = read_account_clerk_generation(tmp_path, ACCOUNT)
        return durable.generation if durable is not None and durable.phase == "accepting" else None

    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
        durable_generation_provider=durable_generation_provider,
        on_generation_fenced=fenced.set,
    )
    await clerk.submit_intent(_intent("bot-a", "run-a", "before-takeover"))
    assert len(broker.calls) == 1

    replacement = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS + 1,
        source="test.takeover",
    )
    with pytest.raises(AccountClerkGenerationFencedError) as exc:
        await clerk.submit_intent(_intent("bot-a", "run-a", "after-takeover"))

    assert exc.value.expected_generation == generation
    assert exc.value.observed_generation == replacement.generation
    assert fenced.is_set()
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_clerk_relays_callbacks_only_to_the_originating_namespace(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    _write_active_binding(tmp_path, "bot-b", "run-b")
    generation = _write_active_clerk_generation(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, clerk_generation=generation)
    )
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
async def test_unexplained_broker_callback_is_an_observable_reconciliation_alarm(tmp_path: Path) -> None:
    generation = _write_active_clerk_generation(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, clerk_generation=generation)
    )
    await server.start()
    try:
        server._record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=404,
                event_type="fill",
                order_ref="learn-ai/unknown/v1:foreign-intent",
                fill_quantity=1,
                ts_ms=START_MS,
            )
        )
    finally:
        await server.close()

    [alarm] = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_reconciliation_alarm"
    ]
    assert alarm["event_type"] == "account_clerk_reconciliation_alarm"
    assert alarm["reason"] == "BROKER_EVENT_WITHOUT_DURABLE_CLERK_INTENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_seq", [1, 2, 4])
async def test_recover_inbox_replays_durable_intent_after_crash_before_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_seq: int,
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
    inbox_path = account_clerk_module.account_clerk_inbox_path(tmp_path, ACCOUNT)

    def crash_between_inbox_and_journal(
        path: Path,
        entry: AccountClerkInboxEntry | AccountClerkJournalEntry,
    ) -> None:
        if isinstance(entry, AccountClerkJournalEntry) and entry.seq == crash_seq:
            raise OSError("simulated process crash before journal fsync")
        original_append(path, entry)

    for sequence in range(1, crash_seq):
        await clerk.record_intent(_intent("bot-a", "run-a", f"durable-{sequence}"))

    monkeypatch.setattr(account_clerk_module, "_append_jsonl", crash_between_inbox_and_journal)
    intent = _intent("bot-a", "run-a", f"crash-intent-{crash_seq}")
    with pytest.raises(OSError, match="simulated process crash"):
        await clerk.record_intent(intent)
    monkeypatch.setattr(account_clerk_module, "_append_jsonl", original_append)

    assert inbox_path.read_text(encoding="utf-8").endswith("\n")
    assert [entry.seq for entry in read_account_clerk_inbox(tmp_path, ACCOUNT)] == [crash_seq]
    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == list(
        range(1, crash_seq)
    )

    restarted_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    first_replay = await restarted_clerk.recover_inbox()
    second_replay = await restarted_clerk.recover_inbox()

    assert read_account_clerk_inbox(tmp_path, ACCOUNT) == []
    assert inbox_path.read_text(encoding="utf-8") == ""
    assert [entry.intent.intent_id for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        *(f"durable-{sequence}" for sequence in range(1, crash_seq)),
        intent.intent_id,
    ]
    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == list(
        range(1, crash_seq + 1)
    )
    assert first_replay == second_replay
    assert first_replay[-1].intent_id == intent.intent_id
    assert first_replay[-1].recorded_at_ms == START_MS + 2
    assert broker.calls == []


@pytest.mark.asyncio
async def test_recover_inbox_discards_already_journaled_matching_row_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    inbox_path = account_clerk_module.account_clerk_inbox_path(tmp_path, ACCOUNT)
    journal_path = account_clerk_module.account_clerk_journal_path(tmp_path, ACCOUNT)
    original_rewrite = account_clerk_module._rewrite_jsonl

    def crash_after_journal_fsync(path: Path, entries: list[AccountClerkInboxEntry]) -> None:
        if path == inbox_path and journal_path.exists():
            raise OSError("simulated process crash before inbox compaction")
        original_rewrite(path, entries)

    monkeypatch.setattr(account_clerk_module, "_rewrite_jsonl", crash_after_journal_fsync)
    intent = _intent("bot-a", "run-a", "already-journaled")
    with pytest.raises(OSError, match="simulated process crash"):
        await clerk.record_intent(intent)
    monkeypatch.setattr(account_clerk_module, "_rewrite_jsonl", original_rewrite)

    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [1]
    assert [entry.seq for entry in read_account_clerk_inbox(tmp_path, ACCOUNT)] == [1]

    restarted_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    receipts = await restarted_clerk.recover_inbox()

    assert [receipt.intent_id for receipt in receipts] == [intent.intent_id]
    assert read_account_clerk_inbox(tmp_path, ACCOUNT) == []
    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [1]
    assert broker.calls == []


@pytest.mark.asyncio
async def test_recover_inbox_rejects_conflicting_journal_row_without_broker_contact(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    journal_path = account_clerk_module.account_clerk_journal_path(tmp_path, ACCOUNT)
    inbox_path = account_clerk_module.account_clerk_inbox_path(tmp_path, ACCOUNT)
    journal_intent = _intent("bot-a", "run-a", "journal-intent")
    conflicting_intent = _intent("bot-a", "run-a", "conflicting-intent")
    account_clerk_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(
            seq=1,
            recorded_at_ms=START_MS,
            intent=journal_intent,
        ),
    )
    account_clerk_module._append_jsonl(
        inbox_path,
        AccountClerkInboxEntry(
            seq=1,
            received_at_ms=START_MS,
            intent=conflicting_intent,
        ),
    )

    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    with pytest.raises(AccountClerkJournalCorruptError, match="inbox and journal intent differ"):
        await clerk.recover_inbox()

    assert [entry.intent.intent_id for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        journal_intent.intent_id
    ]
    assert [entry.intent.intent_id for entry in read_account_clerk_inbox(tmp_path, ACCOUNT)] == [
        conflicting_intent.intent_id
    ]
    assert broker.calls == []


@pytest.mark.asyncio
async def test_recover_inbox_rejects_incompatible_duplicate_rows(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    journal_path = account_clerk_module.account_clerk_journal_path(tmp_path, ACCOUNT)
    inbox_path = account_clerk_module.account_clerk_inbox_path(tmp_path, ACCOUNT)
    first_intent = _intent("bot-a", "run-a", "first-duplicate")
    second_intent = _intent("bot-a", "run-a", "second-duplicate")
    account_clerk_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(seq=1, recorded_at_ms=START_MS, intent=_intent("bot-a", "run-a", "base")),
    )
    for intent in (first_intent, second_intent):
        account_clerk_module._append_jsonl(
            inbox_path,
            AccountClerkInboxEntry(seq=2, received_at_ms=START_MS, intent=intent),
        )

    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    with pytest.raises(AccountClerkJournalCorruptError, match="duplicate incompatible inbox rows"):
        await clerk.recover_inbox()

    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [1]
    assert [entry.seq for entry in read_account_clerk_inbox(tmp_path, ACCOUNT)] == [2, 2]


@pytest.mark.asyncio
async def test_recover_inbox_rejects_genuine_journal_gap(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    journal_path = account_clerk_module.account_clerk_journal_path(tmp_path, ACCOUNT)
    inbox_path = account_clerk_module.account_clerk_inbox_path(tmp_path, ACCOUNT)
    account_clerk_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(seq=1, recorded_at_ms=START_MS, intent=_intent("bot-a", "run-a", "base")),
    )
    account_clerk_module._append_jsonl(
        inbox_path,
        AccountClerkInboxEntry(
            seq=3,
            received_at_ms=START_MS,
            intent=_intent("bot-a", "run-a", "gap"),
        ),
    )

    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    with pytest.raises(AccountClerkJournalCorruptError, match="cannot follow journal seq 1"):
        await clerk.recover_inbox()

    assert [entry.seq for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [1]
    assert [entry.seq for entry in read_account_clerk_inbox(tmp_path, ACCOUNT)] == [3]


@pytest.mark.parametrize(
    ("sequences", "error"),
    [
        ([2], "expected seq 1"),
        ([1, 3], "expected seq 2"),
    ],
)
def test_read_account_clerk_journal_rejects_non_contiguous_sequence(
    tmp_path: Path,
    sequences: list[int],
    error: str,
) -> None:
    journal_path = account_clerk_module.account_clerk_journal_path(tmp_path, ACCOUNT)
    for sequence in sequences:
        account_clerk_module._append_jsonl(
            journal_path,
            AccountClerkJournalEntry(
                seq=sequence,
                recorded_at_ms=START_MS,
                intent=_intent("bot-a", "run-a", f"journal-{sequence}"),
            ),
        )

    with pytest.raises(AccountClerkJournalCorruptError, match=error):
        read_account_clerk_journal(tmp_path, ACCOUNT)


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
