"""Account Clerk core tests for issue #1016."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.live.account_clerk as account_clerk_module
import app.engine.live.account_clerk_journal as account_clerk_journal_module
import app.engine.live.account_clerk_operations as account_clerk_operations_module
import app.engine.live.account_clerk_reconciler as account_clerk_reconciler_module
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountAuditedOverride,
    advance_account_clerk_generation,
    clear_account_freeze,
    read_account_clerk_generation,
    read_account_clerk_lease,
    read_account_events,
    read_account_freeze,
)
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkCancelNamespaceUncertainError,
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
from app.engine.live.account_clerk_cursor import (
    AccountClerkEventConsumerIdentity,
    AccountClerkEventCursorRepo,
)
from app.engine.live.account_clerk_reconciler import (
    AccountClerkReconciler,
    _unresolved_intents,
    namespace_expected_exposure,
)
from app.engine.live.account_clerk_rpc import AccountClerkCallbackPersistenceError
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.order_identity import build_order_ref
from app.services.bot_deletion import (
    BotRetirementBindingTarget,
    BotRetirementTransitionRecord,
    bot_lifecycle_operation_fence,
    stable_bot_retirement_transition_path,
)

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
        self.cancelled_namespaces: list[str] = []
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))

    async def place_order(self, order: object) -> object:
        self.calls.append(order)
        return SimpleNamespace(order_id=101, perm_id=201, exec_id="exec-1")

    async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
        self.cancelled_namespaces.append(namespace)
        return [41]


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


class _UncertainCancelBroker(_FakeBroker):
    def __init__(self, cancel_probe: str) -> None:
        super().__init__()
        self.cancel_probe = cancel_probe
        self.cancel_attempts = 0

    async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
        self.cancel_attempts += 1
        self.cancelled_namespaces.append(namespace)
        if self.cancel_attempts == 1:
            raise TimeoutError("simulated lost cancellation acknowledgement")
        return [41]

    async def probe_namespace_cancel_status(self, _namespace: str) -> str:
        return self.cancel_probe


class _RecoveryCancelTimeoutBroker(_FakeBroker):
    async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
        self.cancelled_namespaces.append(namespace)
        raise TimeoutError("simulated recovery cancel timeout")


class _MarketFillBeforeAckBroker(_FakeBroker):
    """Emits a fill through the production callback sink before its ack returns."""

    def __init__(self) -> None:
        super().__init__()
        self._callback_sink: object | None = None

    def set_broker_callback_sink(self, sink: object) -> None:
        self._callback_sink = sink

    async def place_order(self, order: object) -> object:
        self.calls.append(order)
        assert callable(self._callback_sink)
        self._callback_sink(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=101,
                event_type="fill",
                order_ref=order.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                exec_id="fast-fill-before-ack",
                ts_ms=START_MS + 1,
            )
        )
        # Let the callback worker contend for the Clerk lock while submit is
        # still awaiting the broker. Attribution must already be installed.
        await asyncio.sleep(0)
        return SimpleNamespace(order_id=101, perm_id=201, exec_id="ack-after-fill")


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
async def test_clerk_rejects_an_active_binding_while_retirement_is_pending(tmp_path: Path) -> None:
    """The retirement transaction fence closes the normal writer before replay."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    transition_path = stable_bot_retirement_transition_path(tmp_path, "bot-a")
    transition_path.parent.mkdir(parents=True)
    transition_path.write_text(
        BotRetirementTransitionRecord(
            strategy_instance_id="bot-a",
            targets=(BotRetirementBindingTarget(account_id=ACCOUNT, run_id="run-a"),),
            prepared_at_ms=START_MS,
            updated_by="test",
            reason="injected interruption",
        ).model_dump_json(),
        encoding="utf-8",
    )
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RETIREMENT_PENDING"):
        await clerk.record_intent(_intent("bot-a", "run-a", "retirement-pending"))

    assert broker.calls == []


@pytest.mark.asyncio
async def test_clerk_submit_rechecks_retirement_after_waiting_for_operation_fence(
    tmp_path: Path,
) -> None:
    """An admitted submit cannot cross a retirement fence to the broker."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    errors: list[BaseException] = []
    attempted = threading.Event()

    def submit() -> None:
        attempted.set()
        try:
            asyncio.run(clerk.submit_intent(_intent("bot-a", "run-a", "fenced-submit")))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    transition_path = stable_bot_retirement_transition_path(tmp_path, "bot-a")
    with bot_lifecycle_operation_fence(tmp_path, "bot-a"):
        worker = threading.Thread(target=submit)
        worker.start()
        assert attempted.wait(timeout=1.0)
        await asyncio.sleep(0.05)
        transition_path.parent.mkdir(parents=True, exist_ok=True)
        transition_path.write_text(
            BotRetirementTransitionRecord(
                strategy_instance_id="bot-a",
                targets=(BotRetirementBindingTarget(account_id=ACCOUNT, run_id="run-a"),),
                prepared_at_ms=START_MS,
                updated_by="operator",
                reason="retire",
            ).model_dump_json(),
            encoding="utf-8",
        )
    await asyncio.to_thread(worker.join, 2.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AccountClerkIntentRejected)
    assert "CLERK_RETIREMENT_PENDING" in str(errors[0])
    assert broker.calls == []


@pytest.mark.asyncio
async def test_clerk_retry_revalidates_retirement_before_broker_write(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _intent("bot-a", "run-a", "retired-retry")
    await clerk.record_intent(intent)
    transition_path = stable_bot_retirement_transition_path(tmp_path, "bot-a")
    transition_path.parent.mkdir(parents=True)
    transition_path.write_text(
        BotRetirementTransitionRecord(
            strategy_instance_id="bot-a",
            targets=(BotRetirementBindingTarget(account_id=ACCOUNT, run_id="run-a"),),
            prepared_at_ms=START_MS,
            updated_by="operator",
            reason="retire",
        ).model_dump_json(),
        encoding="utf-8",
    )

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RETIREMENT_PENDING"):
        await clerk.retry_recorded_intent(intent)

    assert broker.calls == []


def _recovery_intent(instance_id: str, run_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    intent = _intent(instance_id, run_id, intent_id)
    return intent.model_copy(
        update={
            "intent_kind": "RECOVERY_FLATTEN",
            "order_spec": IbkrOrderSpec(
                symbol="SPY",
                sec_type="STK",
                action="SELL",
                quantity=1,
                order_type="MKT",
                time_in_force="DAY",
                confirm_paper=True,
                client_order_id=f"recovery-{intent_id}",
                order_ref=intent.order_ref,
            ).model_dump(),
        }
    )


def _emergency_flatten_intent(intent_id: str) -> AccountOwnerSubmitIntent:
    instance_id = f"eflat-{ACCOUNT}"
    namespace = bot_order_namespace_for_instance(instance_id)
    order_ref = build_order_ref(namespace, intent_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=ACCOUNT,
        strategy_instance_id=instance_id,
        run_id="emergency-run",
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=order_ref,
        intent_kind="EMERGENCY_FLATTEN",
        order_spec=IbkrOrderSpec(
            symbol="SPY",
            sec_type="STK",
            action="SELL",
            quantity=1,
            order_type="MKT",
            time_in_force="DAY",
            confirm_paper=True,
            client_order_id=f"emergency-{intent_id}",
            order_ref=order_ref,
        ).model_dump(),
        owner_generation=99,
        created_at_ms=START_MS,
    )


async def _record_owned_fill(
    clerk: AccountClerk,
    *,
    instance_id: str,
    run_id: str,
    intent_id: str,
) -> None:
    intent = _intent(instance_id, run_id, intent_id)
    await clerk.record_intent(intent)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=100,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id=f"fill-{intent_id}",
            ts_ms=START_MS + 1,
        )
    )


def _cancel_intent(instance_id: str, run_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    return _intent(instance_id, run_id, intent_id).model_copy(
        update={"intent_kind": "CANCEL_NAMESPACE", "order_spec": {}}
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
async def test_cancel_namespace_is_durable_idempotent_and_scoped_to_its_binding(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    _write_active_binding(tmp_path, "bot-b", "run-b")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _cancel_intent("bot-a", "run-a", "cancel-a")

    first = await clerk.cancel_namespace(intent)
    second = await clerk.cancel_namespace(intent)

    assert first == second
    assert first.cancelled_order_ids == (41,)
    assert broker.cancelled_namespaces == [bot_order_namespace_for_instance("bot-a")]
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "cancel_submitting",
        "cancel_confirmed",
    ]


@pytest.mark.asyncio
async def test_submit_reconciler_ignores_terminal_cancel_receipts(tmp_path: Path) -> None:
    """#1064: a completed cancel is never a submit-retry candidate."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_ReconciliationBroker("NOT_PROVABLE"))
    await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-terminal"))

    assert await AccountClerkReconciler(clerk).reconcile_once() == ()
    assert read_account_freeze(tmp_path, ACCOUNT) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_probe", "expected_cancel_attempts"),
    [
        ("PROVABLY_ABSENT", 1),
        ("PRESENT", 2),
    ],
)
async def test_uncertain_cancel_blocks_namespace_submit_until_reconciled(
    tmp_path: Path,
    cancel_probe: str,
    expected_cancel_attempts: int,
) -> None:
    """#1064: a lost cancel acknowledgement fences later namespace submits."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _UncertainCancelBroker(cancel_probe)
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    cancel = _cancel_intent("bot-a", "run-a", "cancel-uncertain")

    with pytest.raises(AccountClerkCancelNamespaceUncertainError):
        await clerk.cancel_namespace(cancel)
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_CANCEL_NAMESPACE_UNRESOLVED"):
        await clerk.submit_intent(_intent("bot-a", "run-a", "submit-after-uncertain-cancel"))

    [resolution] = await AccountClerkReconciler(clerk, now_ms=lambda: START_MS + 2).reconcile_once()

    assert resolution.intent_id == cancel.intent_id
    assert resolution.verdict.value == "RECOVER_ADOPT"
    assert broker.cancel_attempts == expected_cancel_attempts
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)].count(
        "cancel_confirmed"
    ) == 1
    await clerk.submit_intent(_intent("bot-a", "run-a", "submit-after-cancel-resolution"))
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_cancel_namespace_records_inflight_before_awaiting_broker(tmp_path: Path) -> None:
    """#1064: a crash in the broker await leaves an explicit ambiguity marker."""

    class _BlockedCancelBroker(_FakeBroker):
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            self.started.set()
            await self.release.wait()
            return [41]

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _BlockedCancelBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    task = asyncio.create_task(clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-boundary")))
    await broker.started.wait()

    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "cancel_submitting",
    ]

    broker.release.set()
    await task


@pytest.mark.asyncio
async def test_fenced_cancel_preserves_generation_stale_error_without_uncertainty(tmp_path: Path) -> None:
    """#1064: takeover fencing is not an ambiguous broker cancellation."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_FakeBroker(),
        clerk_generation=generation,
        durable_generation_provider=lambda: read_account_clerk_generation(tmp_path, ACCOUNT).generation,
    )
    advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS + 1,
        source="test.takeover",
    )

    with pytest.raises(AccountClerkGenerationFencedError):
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-fenced"))

    # Fencing happens before the durable pre-write boundary, so a stale Clerk
    # does not leave an ambiguous cancellation marker behind.
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == ["recorded"]
    assert clerk._broker.cancelled_namespaces == []


@pytest.mark.asyncio
async def test_cancel_confirmation_waits_for_queued_broker_fill_persistence(tmp_path: Path) -> None:
    """#1064: terminal cancellation cannot race its queued fill into a stale fold."""

    class _CancelEmitsFillBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self._callback_sink: object | None = None
            self.fill_order_ref: str | None = None

        def set_broker_callback_sink(self, sink: object) -> None:
            self._callback_sink = sink

        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            assert callable(self._callback_sink)
            assert self.fill_order_ref is not None
            self._callback_sink(
                IbkrOrderEvent(
                    account_id=ACCOUNT,
                    order_id=403,
                    event_type="fill",
                    order_ref=self.fill_order_ref,
                    symbol="SPY",
                    side="BUY",
                    fill_quantity=1,
                    exec_id="cancel-terminal-fill",
                    ts_ms=START_MS + 1,
                )
            )
            return [403]

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _CancelEmitsFillBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
    )
    server = AccountClerkRpcServer(clerk)
    await server.start()
    try:
        owned = _intent("bot-a", "run-a", "open-before-cancel")
        await clerk.record_intent(owned)
        broker.fill_order_ref = owned.order_ref
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-with-fill"))
    finally:
        await server.close()

    kinds = [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)]
    assert kinds.index("broker_event") < kinds.index("cancel_confirmed")


@pytest.mark.asyncio
async def test_cancel_namespace_records_uncertainty_before_failing_closed(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")

    class _UncertainCancelBroker(_FakeBroker):
        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            raise TimeoutError("cancel confirmation lost")

    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_UncertainCancelBroker(),
    )

    with pytest.raises(AccountClerkCancelNamespaceUncertainError):
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-uncertain"))

    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "cancel_submitting",
        "cancel_uncertain",
    ]


@pytest.mark.asyncio
async def test_cancel_namespace_rejects_unsupported_broker_capability(tmp_path: Path) -> None:
    class _BrokerWithoutNamespaceCancel:
        _client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_BrokerWithoutNamespaceCancel(),
    )

    with pytest.raises(AccountClerkCancelNamespaceUncertainError):
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-unsupported"))

    [uncertain] = [
        entry
        for entry in read_account_clerk_journal(tmp_path, ACCOUNT)
        if entry.entry_kind == "cancel_uncertain"
    ]
    assert uncertain.broker_error is not None
    assert "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNSUPPORTED" in uncertain.broker_error


@pytest.mark.asyncio
async def test_cancel_namespace_bounds_callback_drain_with_broker_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    never_drained = asyncio.Event()

    async def drain() -> None:
        await never_drained.wait()

    clerk.set_callback_drain(drain)
    monkeypatch.setattr(
        account_clerk_operations_module,
        "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S",
        0.01,
    )

    with pytest.raises(AccountClerkCancelNamespaceUncertainError):
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-drain-timeout"))


@pytest.mark.asyncio
async def test_cancel_reconciler_bounds_a_hung_namespace_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingProbeBroker(_UncertainCancelBroker):
        async def probe_namespace_cancel_status(self, _namespace: str) -> str:
            await asyncio.Event().wait()
            return "PROVABLY_ABSENT"

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _HangingProbeBroker("NOT_PROVABLE")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    with pytest.raises(AccountClerkCancelNamespaceUncertainError):
        await clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "cancel-hung-probe"))

    monkeypatch.setattr(
        account_clerk_operations_module,
        "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S",
        0.01,
    )
    # The reconciler imports the public timeout once, so patch its own reference too.
    import app.engine.live.account_clerk_reconciler as reconciler_module

    monkeypatch.setattr(reconciler_module, "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S", 0.01)
    [resolution] = await AccountClerkReconciler(clerk).reconcile_once()

    assert resolution.verdict.value == "HALT"


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
async def test_order_ref_collision_is_rejected_before_durable_intake(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    first = _intent("bot-a", "run-a", "first")
    colliding = first.model_copy(update={"intent_id": "different-intent"})

    await clerk.record_intent(first)
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_ORDER_REF_COLLISION"):
        await clerk.record_intent(colliding)

    assert [entry.intent for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [first]
    assert read_account_clerk_inbox(tmp_path, ACCOUNT) == []


@pytest.mark.asyncio
async def test_bounded_broker_submit_records_uncertainty_and_releases_account_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck broker write must not park every bot behind the Clerk lock."""

    class _FirstSubmitHangsBroker(_FakeBroker):
        async def place_order(self, order: object) -> object:
            self.calls.append(order)
            if len(self.calls) == 1:
                await asyncio.Event().wait()
            return SimpleNamespace(order_id=102, perm_id=202, exec_id="exec-2")

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FirstSubmitHangsBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    monkeypatch.setattr(account_clerk_module, "_BROKER_SUBMIT_TIMEOUT_S", 0.01)

    with pytest.raises(TimeoutError):
        await clerk.submit_intent(_intent("bot-a", "run-a", "hung-submit"))
    _, acked = await clerk.submit_intent(_intent("bot-a", "run-a", "next-submit"))

    assert acked.order_id == 102
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)] == [
        "recorded",
        "broker_submitting",
        "broker_uncertain",
        "recorded",
        "broker_submitting",
        "broker_acked",
    ]


@pytest.mark.asyncio
async def test_market_fill_before_ack_is_attributed_before_broker_await(tmp_path: Path) -> None:
    """#1044: callback attribution is durable before a fast MKT ack can race it."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _MarketFillBeforeAckBroker()
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
        ).submit(_intent("bot-a", "run-a", "market-fill-race"))
        await server._flush_broker_callbacks()
    finally:
        await server.close()

    [callback] = [
        entry
        for entry in read_account_clerk_journal(tmp_path, ACCOUNT)
        if entry.entry_kind == "broker_event"
    ]
    assert receipt.status == "broker_acked"
    assert callback.intent is not None
    assert callback.intent.intent_id == "market-fill-race"
    assert not any(
        event["event_type"] == "account_clerk_unattributed_broker_event"
        for event in read_account_events(tmp_path, ACCOUNT)
    )


@pytest.mark.asyncio
async def test_restart_rebuilds_durable_callback_attribution_index(tmp_path: Path) -> None:
    """#1044: callback ownership survives the process map disappearing."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    intent = _intent("bot-a", "run-a", "restart-attribution")
    first_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, clerk_generation=generation)
    await first_clerk.record_intent(intent)

    restarted_server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, clerk_generation=generation)
    )
    await restarted_server.start()
    try:
        restarted_server._record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=202,
                event_type="fill",
                order_ref=intent.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                exec_id="restart-attribution-fill",
                ts_ms=START_MS + 1,
            )
        )
        await restarted_server._flush_broker_callbacks()
    finally:
        await restarted_server.close()

    [callback] = [
        entry
        for entry in read_account_clerk_journal(tmp_path, ACCOUNT)
        if entry.entry_kind == "broker_event"
    ]
    assert callback.intent == intent


@pytest.mark.asyncio
async def test_reconciler_adopt_and_retry_restore_callback_attribution(tmp_path: Path) -> None:
    """#1044: rescued Clerk intents remain eligible for later callbacks."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    intent = _intent("bot-a", "run-a", "reconciled-attribution")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    await clerk.record_intent(intent)

    clerk._intents_by_order_ref.clear()
    await clerk.append_reconciliation_resolution(
        intent,
        verdict="RECOVER_ADOPT",
        reason="test adoption",
    )
    adopted = await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=303,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="adopted-fill",
            ts_ms=START_MS + 1,
        )
    )

    clerk._intents_by_order_ref.clear()
    await clerk.retry_recorded_intent(intent)
    retried = await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=304,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="retried-fill",
            ts_ms=START_MS + 2,
        )
    )

    assert adopted.intent == intent
    assert retried.intent == intent


@pytest.mark.asyncio
async def test_fenced_bot_can_recovery_flatten_its_retired_namespace(tmp_path: Path) -> None:
    """Regression for the 2026-07-14 fenced-flatten trap."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")
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
        "broker_event",
        "recorded",
        "recovery_cancelling",
        "recovery_cancelled",
        "broker_submitting",
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
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")
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
    receipt = await clerk.submit_recovery_flatten(
        _recovery_intent("bot-a", "run-a", "operator-cure"),
        actor="operator",
    )

    assert receipt.status == "recovery_flattened"
    assert receipt.recorded.intent_id == "operator-cure"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_recovery_cancel_timeout_is_durable_uncertain_and_never_submits_flatten(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _RecoveryCancelTimeoutBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    position = _intent("bot-a", "run-a", "position")
    await clerk.submit_intent(position)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=position.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="position-fill",
            ts_ms=START_MS + 1,
        )
    )

    with pytest.raises(TimeoutError, match="recovery cancel timeout"):
        await clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    entries = read_account_clerk_journal(tmp_path, ACCOUNT)
    recovery_entries = [entry for entry in entries if entry.intent and entry.intent.intent_id == "recovery"]
    assert [entry.entry_kind for entry in recovery_entries] == [
        "recorded",
        "recovery_cancelling",
        "broker_uncertain",
    ]
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_recovery_bounds_hung_cancel_and_durably_records_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HangingRecoveryCancelBroker(_FakeBroker):
        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            await asyncio.Event().wait()
            return []

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _HangingRecoveryCancelBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    position = _intent("bot-a", "run-a", "position")
    await clerk.submit_intent(position)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=position.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="position-fill",
            ts_ms=START_MS + 1,
        )
    )
    monkeypatch.setattr(
        account_clerk_operations_module,
        "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S",
        0.01,
    )

    with pytest.raises(TimeoutError):
        await clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    assert not clerk.recovery_flatten_in_progress
    assert [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)[-3:]] == [
        "recorded",
        "recovery_cancelling",
        "broker_uncertain",
    ]


@pytest.mark.asyncio
async def test_recovery_pre_broker_journal_failure_releases_volatile_submit_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")

    def fail_journal(_intent: AccountOwnerSubmitIntent) -> None:
        raise OSError("journal unwritable")

    monkeypatch.setattr(clerk._journal, "append_broker_submitting", fail_journal)

    with pytest.raises(OSError, match="journal unwritable"):
        await clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    assert not clerk.recovery_flatten_in_progress


@pytest.mark.asyncio
async def test_recovery_bounds_hung_callback_drain_and_fences_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    position = _intent("bot-a", "run-a", "position")
    await clerk.submit_intent(position)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=position.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="position-fill",
            ts_ms=START_MS + 1,
        )
    )
    never_drained = asyncio.Event()

    async def drain() -> None:
        await never_drained.wait()

    clerk.set_callback_drain(drain)
    monkeypatch.setattr(
        account_clerk_operations_module,
        "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S",
        0.01,
    )

    with pytest.raises(TimeoutError):
        await clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    restarted = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_CANCEL_NAMESPACE_UNRESOLVED"):
        await restarted.submit_intent(_intent("bot-a", "run-a", "normal-after-restart"))


@pytest.mark.asyncio
async def test_recovery_accepts_fractional_exposure_within_explicit_tolerance(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    first = _intent("bot-a", "run-a", "fraction-a").model_copy(
        update={"order_spec": {**_intent("bot-a", "run-a", "fraction-a").order_spec, "quantity": 0.1}}
    )
    second = _intent("bot-a", "run-a", "fraction-b").model_copy(
        update={"order_spec": {**_intent("bot-a", "run-a", "fraction-b").order_spec, "quantity": 0.2}}
    )
    await clerk.submit_intent(first)
    await clerk.submit_intent(second)
    for order_id, intent in enumerate((first, second), start=1):
        await clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=order_id,
                event_type="fill",
                order_ref=intent.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=float(intent.order_spec["quantity"]),
                exec_id=f"fraction-fill-{order_id}",
                ts_ms=START_MS + order_id,
            )
        )
    recovery = _recovery_intent("bot-a", "run-a", "fraction-recovery").model_copy(
        update={
            "order_spec": {
                **_recovery_intent("bot-a", "run-a", "fraction-recovery").order_spec,
                "quantity": 0.3,
            }
        }
    )

    receipt = await clerk.submit_recovery_flatten(
        recovery,
        actor="bot",
        actor_strategy_instance_id="bot-a",
        actor_run_id="run-a",
        actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
    )

    assert receipt.status == "recovery_flattened"


@pytest.mark.asyncio
async def test_recovery_holds_submit_lane_but_not_intake_lock_during_cancel(tmp_path: Path) -> None:
    class _BlockedRecoveryCancelBroker(_FakeBroker):
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            self.started.set()
            await self.release.wait()
            return []

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _BlockedRecoveryCancelBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    position = _intent("bot-a", "run-a", "position")
    await clerk.submit_intent(position)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=position.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="position-fill",
            ts_ms=START_MS + 1,
        )
    )
    recovery_task = asyncio.create_task(
        clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )
    )
    await broker.started.wait()

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RECOVERY_FLATTEN_IN_PROGRESS"):
        await clerk.submit_intent(_intent("bot-a", "run-a", "normal"))
    assert await AccountClerkReconciler(clerk).reconcile_once() == ()

    broker.release.set()
    await recovery_task


@pytest.mark.asyncio
async def test_recovery_serializes_a_concurrent_namespace_cancel_until_recovery_finishes(
    tmp_path: Path,
) -> None:
    """A second cancel cannot write between recovery cancellation and liquidation."""

    class _BlockedRecoveryCancelBroker(_FakeBroker):
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            self.started.set()
            await self.release.wait()
            return []

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _BlockedRecoveryCancelBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")
    recovery_task = asyncio.create_task(
        clerk.submit_recovery_flatten(
            _recovery_intent("bot-a", "run-a", "recovery"),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )
    )
    await broker.started.wait()
    cancel_task = asyncio.create_task(
        clerk.cancel_namespace(_cancel_intent("bot-a", "run-a", "concurrent-cancel"))
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if not cancel_task.done():
            break

    assert broker.cancelled_namespaces == [bot_order_namespace_for_instance("bot-a")]
    assert not cancel_task.done()

    broker.release.set()
    await recovery_task
    await cancel_task
    assert broker.cancelled_namespaces == [
        bot_order_namespace_for_instance("bot-a"),
        bot_order_namespace_for_instance("bot-a"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_spec_update", "reason"),
    [
        ({"symbol": "QQQ"}, "CLERK_RECOVERY_SYMBOL_NOT_OWNED"),
        ({"action": "BUY"}, "CLERK_RECOVERY_DIRECTION_MISMATCH"),
        ({"quantity": 2}, "CLERK_RECOVERY_QUANTITY_MISMATCH"),
    ],
)
async def test_recovery_flatten_rejects_any_order_not_matching_journal_exposure(
    tmp_path: Path,
    order_spec_update: dict[str, object],
    reason: str,
) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")
    recovery = _recovery_intent("bot-a", "run-a", f"recovery-{reason}")
    recovery = recovery.model_copy(
        update={"order_spec": {**recovery.order_spec, **order_spec_update}}
    )

    with pytest.raises(AccountClerkIntentRejected, match=reason):
        await clerk.submit_recovery_flatten(
            recovery,
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    assert broker.calls == []


@pytest.mark.asyncio
async def test_recovery_flatten_batch_cancels_once_before_submitting_each_symbol(tmp_path: Path) -> None:
    """A multi-symbol recovery cannot cancel the first liquidation with the second."""

    class _PermWaitBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self.perm_id_waits: list[float] = []

        async def place_order(self, order: object, *, perm_id_wait_s: float = 0.0) -> object:
            self.perm_id_waits.append(perm_id_wait_s)
            return await super().place_order(order)

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _PermWaitBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    spy_owned = _intent("bot-a", "run-a", "owned-spy")
    qqq_owned = _intent("bot-a", "run-a", "owned-qqq").model_copy(
        update={"order_spec": {**_intent("bot-a", "run-a", "owned-qqq").order_spec, "symbol": "QQQ"}}
    )
    await clerk.record_intent(spy_owned)
    await clerk.record_intent(qqq_owned)
    for intent, symbol, exec_id in (
        (spy_owned, "SPY", "owned-spy-fill"),
        (qqq_owned, "QQQ", "owned-qqq-fill"),
    ):
        await clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=101,
                event_type="fill",
                order_ref=intent.order_ref,
                symbol=symbol,
                side="BUY",
                fill_quantity=1,
                exec_id=exec_id,
                ts_ms=START_MS,
            )
        )
    recovery_spy = _recovery_intent("bot-a", "run-a", "recovery-spy")
    recovery_qqq = _recovery_intent("bot-a", "run-a", "recovery-qqq").model_copy(
        update={
            "order_spec": {
                **_recovery_intent("bot-a", "run-a", "recovery-qqq").order_spec,
                "symbol": "QQQ",
            }
        }
    )

    receipts = await clerk.submit_recovery_flatten_batch(
        (recovery_spy, recovery_qqq),
        actor="bot",
        actor_strategy_instance_id="bot-a",
        actor_run_id="run-a",
        actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
    )

    assert len(receipts) == 2
    assert broker.cancelled_namespaces == [bot_order_namespace_for_instance("bot-a")]
    assert [order.symbol for order in broker.calls] == ["SPY", "QQQ"]
    assert broker.perm_id_waits == [2.0, 2.0]


@pytest.mark.asyncio
async def test_recovery_flatten_batch_rejects_a_fill_persisted_during_cancel_drain(
    tmp_path: Path,
) -> None:
    """A stale pre-cancel plan is rejected before any recovery broker write."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    owned = _intent("bot-a", "run-a", "owned-before-cancel")
    await clerk.record_intent(owned)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=101,
            event_type="fill",
            order_ref=owned.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="owned-before-cancel-fill",
            ts_ms=START_MS,
        )
    )

    async def persist_terminal_fill() -> None:
        await clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=102,
                event_type="fill",
                order_ref=owned.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                exec_id="terminal-cancel-fill",
                ts_ms=START_MS + 1,
            )
        )

    clerk.set_callback_drain(persist_terminal_fill)
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RECOVERY_QUANTITY_MISMATCH"):
        await clerk.submit_recovery_flatten_batch(
            (_recovery_intent("bot-a", "run-a", "stale-recovery-plan"),),
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    assert broker.calls == []


@pytest.mark.asyncio
async def test_recovery_flatten_drains_cancel_fill_before_validating_exact_exposure(tmp_path: Path) -> None:
    """A fill delivered by terminal cancellation changes the exact recovery size."""

    class _RecoveryCancelFillBroker(_FakeBroker):
        def __init__(self) -> None:
            super().__init__()
            self._callback_sink: object | None = None
            self.fill_order_ref: str | None = None

        def set_broker_callback_sink(self, sink: object) -> None:
            self._callback_sink = sink

        async def cancel_open_orders_for_namespace(self, namespace: str) -> list[int]:
            self.cancelled_namespaces.append(namespace)
            assert callable(self._callback_sink)
            assert self.fill_order_ref is not None
            self._callback_sink(
                IbkrOrderEvent(
                    account_id=ACCOUNT,
                    order_id=701,
                    event_type="fill",
                    order_ref=self.fill_order_ref,
                    symbol="SPY",
                    side="BUY",
                    fill_quantity=1,
                    exec_id="recovery-cancel-fill",
                    ts_ms=START_MS + 2,
                )
            )
            return [701]

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _RecoveryCancelFillBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
    )
    server = AccountClerkRpcServer(clerk)
    await server.start()
    try:
        await _record_owned_fill(clerk, instance_id="bot-a", run_id="run-a", intent_id="owned-fill")
        broker.fill_order_ref = build_order_ref(bot_order_namespace_for_instance("bot-a"), "owned-fill")
        recovery = _recovery_intent("bot-a", "run-a", "recovery-with-cancel-fill")
        recovery = recovery.model_copy(
            update={"order_spec": {**recovery.order_spec, "quantity": 2}}
        )
        receipt = await clerk.submit_recovery_flatten(
            recovery,
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )
    finally:
        await server.close()

    assert receipt.broker_acked.status == "broker_acked"
    journal_kinds = [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)]
    assert journal_kinds.index("broker_event") < journal_kinds.index("broker_submitting")


@pytest.mark.asyncio
async def test_recovery_crash_marker_fails_closed_without_reissuing_broker_writes(tmp_path: Path) -> None:
    """A retry after recovery crossed cancel's crash boundary needs operator proof."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    crashed_recovery = _recovery_intent("bot-a", "run-a", "crashed-recovery")
    await clerk.record_intent(crashed_recovery)
    await asyncio.to_thread(clerk._journal.append_recovery_cancelling, crashed_recovery)
    recovery = _recovery_intent("bot-a", "run-a", "fresh-recovery-after-crash")

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RECOVERY_REQUIRES_OPERATOR_RECONCILIATION"):
        await clerk.submit_recovery_flatten(
            recovery,
            actor="bot",
            actor_strategy_instance_id="bot-a",
            actor_run_id="run-a",
            actor_bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        )

    assert broker.cancelled_namespaces == []
    assert broker.calls == []


@pytest.mark.asyncio
async def test_submit_reconciler_never_retries_a_recovery_intent(tmp_path: Path) -> None:
    """Recovery uses its own fail-closed state machine, never submit retry."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_ReconciliationBroker("PROVABLY_ABSENT"),
    )
    await clerk.record_intent(_recovery_intent("bot-a", "run-a", "recovery-reconcile"))

    assert await AccountClerkReconciler(clerk).reconcile_once() == ()


@pytest.mark.asyncio
async def test_submit_reconciler_never_retries_an_externally_owned_emergency_intent(tmp_path: Path) -> None:
    """The Clerk receipt attributes emergency callbacks but cannot place its order."""

    _write_active_binding(tmp_path, f"eflat-{ACCOUNT}", "emergency-run")
    broker = _ReconciliationBroker("PROVABLY_ABSENT")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)

    await clerk.register_emergency_flatten_intent(_emergency_flatten_intent("emergency-reconcile"))

    assert await AccountClerkReconciler(clerk).reconcile_once() == ()
    assert broker.calls == []


@pytest.mark.asyncio
async def test_reconciler_releases_stale_submitting_rows_at_derived_ttl_boundaries(tmp_path: Path) -> None:
    """#1052: live writes retain their full RPC budget; stale writes do not hide forever."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_FakeBroker(),
        now_ms=lambda: 100,
    )
    submit = _intent("bot-a", "run-a", "stale-submit")
    recovery = _recovery_intent("bot-a", "run-a", "stale-recovery")
    await clerk.record_intent(submit)
    await clerk.record_intent(recovery)
    await asyncio.to_thread(clerk._journal.append_broker_submitting, submit)
    await asyncio.to_thread(clerk._journal.append_broker_submitting, recovery)
    entries = await clerk.reconciliation_snapshot()

    assert _unresolved_intents(entries, now_ms=60_099) == {}
    assert set(_unresolved_intents(entries, now_ms=60_100)) == {submit.intent_id}
    assert set(_unresolved_intents(entries, now_ms=150_099)) == {submit.intent_id}
    assert set(_unresolved_intents(entries, now_ms=150_100)) == {
        submit.intent_id,
        recovery.intent_id,
    }


@pytest.mark.asyncio
async def test_reconciler_handles_uncertain_recovery_and_respects_a_newer_submit_boundary(
    tmp_path: Path,
) -> None:
    """#1066: uncertainty is actionable unless a newer broker write is still fresh."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_FakeBroker(),
        now_ms=lambda: 100,
    )
    recovery = _recovery_intent("bot-a", "run-a", "uncertain-recovery")
    retried = _intent("bot-a", "run-a", "fresh-retry")
    await clerk.record_intent(recovery)
    await clerk.record_intent(retried)
    await asyncio.to_thread(clerk._journal.append_broker_uncertain, recovery, TimeoutError("lost"))
    await asyncio.to_thread(clerk._journal.append_broker_uncertain, retried, TimeoutError("lost"))
    await asyncio.to_thread(clerk._journal.append_broker_submitting, retried)

    unresolved = _unresolved_intents(await clerk.reconciliation_snapshot(), now_ms=100)

    assert set(unresolved) == {recovery.intent_id}


@pytest.mark.asyncio
async def test_reconciler_preserves_the_paper_mode_guard_before_a_retry(tmp_path: Path) -> None:
    """#1066: reconciliation cannot turn an absent probe into a live write."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _ReconciliationBroker("PROVABLY_ABSENT")
    broker._client.settings.mode = "live"
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _intent("bot-a", "run-a", "live-mode-retry")
    await clerk.record_intent(intent)

    [resolution] = await AccountClerkReconciler(clerk).reconcile_once()

    assert resolution.verdict.value == "RETRY_ONCE"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_reconciliation_retry_propagates_a_generation_fence(tmp_path: Path) -> None:
    """A stale Clerk cannot report a fenced retry as a normal resolution."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    broker = _ReconciliationBroker("PROVABLY_ABSENT")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
        durable_generation_provider=lambda: generation + 1,
    )
    intent = _intent("bot-a", "run-a", "fenced-reconciliation-retry")
    await clerk.record_intent(intent)

    with pytest.raises(AccountClerkGenerationFencedError):
        await clerk.reconcile_uncertain_intent(intent, retry_count=0)

    assert broker.calls == []


@pytest.mark.asyncio
async def test_stale_recovery_is_halted_without_automatic_broker_retry(tmp_path: Path) -> None:
    """#1052: stale recovery is probed, but ambiguity cannot create a second flatten."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _ReconciliationBroker("PROVABLY_ABSENT")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    recovery = _recovery_intent("bot-a", "run-a", "stale-recovery-halt")
    await clerk.record_intent(recovery)
    await asyncio.to_thread(clerk._journal.append_broker_submitting, recovery)

    outcome = await clerk.reconcile_uncertain_intent(recovery, retry_count=0)

    assert outcome is not None
    assert outcome.verdict == "HALT"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_reconciler_reasserts_freeze_after_halt_journal_crash_window(tmp_path: Path) -> None:
    """#1052: a durable HALT cannot lose its derived account freeze on crash."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    intent = _intent("bot-a", "run-a", "halt-crash-window")
    await clerk.record_intent(intent)
    await clerk.append_reconciliation_resolution(
        intent,
        verdict="HALT",
        reason="simulated crash before freeze write",
    )

    assert read_account_freeze(tmp_path, ACCOUNT) is None
    assert await AccountClerkReconciler(clerk).reconcile_once() == ()
    assert read_account_freeze(tmp_path, ACCOUNT) is not None


@pytest.mark.asyncio
async def test_reconciler_does_not_reassert_halt_freeze_after_audited_clear(tmp_path: Path) -> None:
    """#1066: an operator clear supersedes the HALT crash-repair path."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_FakeBroker(),
        now_ms=lambda: START_MS,
    )
    intent = _intent("bot-a", "run-a", "halt-operator-clear")
    await clerk.record_intent(intent)
    await clerk.append_reconciliation_resolution(
        intent,
        verdict="HALT",
        reason="simulated crash before freeze write",
    )
    reconciler = AccountClerkReconciler(clerk, now_ms=lambda: START_MS + 1)

    assert await reconciler.reconcile_once() == ()
    clear_account_freeze(
        tmp_path,
        audited_override=AccountAuditedOverride(
            account_id=ACCOUNT,
            override_id="override-halt-clear",
            approved_decision="poison_run",
            reason="operator verified the halted intent",
            approved_by="operator",
            approved_at_ms=START_MS + 2,
            valid_until_ms=START_MS + 60_000,
            prior_evidence={"intent_id": intent.intent_id},
            next_reconciliation_step="RECHECK_BROKER_ON_RECONNECT",
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace=intent.bot_order_namespace,
            affected_order_refs=(intent.order_ref,),
        ),
        now_ms=START_MS + 3,
    )

    assert read_account_freeze(tmp_path, ACCOUNT) is None
    assert await reconciler.reconcile_once() == ()
    assert read_account_freeze(tmp_path, ACCOUNT) is None


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
    assert len([entry for entry in restarted_entries if entry.entry_kind == "broker_event"]) == 1
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
            self._event_task: asyncio.Task[None] | None = None
            self.stream_failure: BaseException | None = None

        def require_account_owner_write_fence(self, provider: object) -> None:
            assert callable(provider)

        async def start_event_stream(self) -> None:
            events.append("stream_started")
            self._event_task = asyncio.create_task(asyncio.sleep(60))

        async def stop_event_stream(self) -> None:
            events.append("stream_stopped")
            assert self._event_task is not None
            self._event_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._event_task

    class _Server:
        def __init__(self, _clerk: AccountClerk, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            events.append("socket_serve")

        async def close(self) -> None:
            events.append("socket_closed")

    class _Reconciler:
        def __init__(self, _clerk: AccountClerk, **_kwargs: object) -> None:
            pass

        healthy = True
        unhealthy = False

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
    assert events.index("socket_serve") < events.index("stream_started") < events.index("stream_stopped")
    assert events.index("broker_disconnect") < events.index("lock_released")


@pytest.mark.asyncio
async def test_callback_persistence_failure_hook_stops_runtime_and_drains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime composition turns an RPC callback durability failure into an unhealthy exit."""

    generation = _write_active_clerk_generation(tmp_path)
    events: list[str] = []

    class _Client:
        settings = SimpleNamespace(mode="paper")

        async def connect(self) -> None:
            events.append("connected")

        async def disconnect(self) -> None:
            events.append("disconnected")

    class _BrokerAdapter:
        def __init__(self, _client: object) -> None:
            self.stream_failure: BaseException | None = None

        def require_account_owner_write_fence(self, _provider: object) -> None:
            return None

        async def start_event_stream(self) -> None:
            events.append("stream_started")

        async def stop_event_stream(self) -> None:
            events.append("stream_stopped")

    class _Server:
        def __init__(
            self,
            _clerk: AccountClerk,
            *,
            on_callback_persistence_failure: Callable[[BaseException], None],
            **_kwargs: object,
        ) -> None:
            self._on_callback_persistence_failure = on_callback_persistence_failure

        async def start(self) -> None:
            events.append("server_started")
            self._on_callback_persistence_failure(OSError("simulated callback fsync failure"))

        async def close(self) -> None:
            events.append("server_closed")

    class _Reconciler:
        healthy = True
        unhealthy = False

        def __init__(self, _clerk: AccountClerk, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            events.append("reconciler_started")

        async def close(self) -> None:
            events.append("reconciler_closed")

    from app.broker.ibkr import client as ibkr_client_module
    from app.engine.live import account_clerk_reconciler, account_clerk_rpc, live_portfolio

    monkeypatch.setattr(account_clerk_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ibkr_client_module, "IbkrClient", _Client)
    monkeypatch.setattr(live_portfolio, "IbkrBrokerAdapter", _BrokerAdapter)
    monkeypatch.setattr(account_clerk_rpc, "AccountClerkRpcServer", _Server)
    monkeypatch.setattr(account_clerk_reconciler, "AccountClerkReconciler", _Reconciler)

    exit_code = await account_clerk_module._run_clerk_process(
        SimpleNamespace(artifacts_root=str(tmp_path), account_id=ACCOUNT, generation=generation)
    )

    assert exit_code == 1
    assert events.index("server_started") < events.index("stream_stopped")
    assert events.index("stream_stopped") < events.index("server_closed") < events.index("disconnected")


@pytest.mark.asyncio
async def test_stream_task_death_alarms_rejects_normal_submits_and_exits_unhealthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1044: a dead callback stream never leaves the Clerk writing blind."""

    generation = _write_active_clerk_generation(tmp_path)

    class _Client:
        settings = SimpleNamespace(mode="paper")

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class _DyingBrokerAdapter:
        def __init__(self, _client: object) -> None:
            self._event_task: asyncio.Task[None] | None = None
            self.stream_failure: BaseException | None = None
            self._callback_sink: object | None = None

        def require_account_owner_write_fence(self, _provider: object) -> None:
            return None

        def set_broker_callback_sink(self, sink: object) -> None:
            self._callback_sink = sink

        async def start_event_stream(self) -> None:
            async def die() -> None:
                await asyncio.sleep(0)
                raise ConnectionError("simulated callback stream death")

            self._event_task = asyncio.create_task(die())

        async def stop_event_stream(self) -> None:
            assert self._event_task is not None
            if not self._event_task.done():
                self._event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._event_task

    from app.broker.ibkr import client as ibkr_client_module
    from app.engine.live import live_portfolio

    monkeypatch.setattr(ibkr_client_module, "IbkrClient", _Client)
    monkeypatch.setattr(live_portfolio, "IbkrBrokerAdapter", _DyingBrokerAdapter)
    monkeypatch.setattr(account_clerk_module.signal, "signal", lambda *_args: None)

    exit_code = await account_clerk_module._run_clerk_process(
        SimpleNamespace(artifacts_root=str(tmp_path), account_id=ACCOUNT, generation=generation)
    )

    [alarm] = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_event_stream_down"
    ]
    assert any(
        event["event_type"] == "account_clerk_event_stream_recovered"
        for event in read_account_events(tmp_path, ACCOUNT)
    )
    assert exit_code == 1
    assert alarm["reason"] == "CLERK_EVENT_STREAM_DOWN"
    assert alarm["failure_type"] == "ConnectionError"
    assert read_account_clerk_lease(tmp_path, ACCOUNT).status == "DRAINING"

    _write_active_binding(tmp_path, "bot-a", "run-a")
    fenced_clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    await fenced_clerk.mark_event_stream_down(ConnectionError("already down"))
    with pytest.raises(AccountClerkIntentRejected, match="CLERK_EVENT_STREAM_DOWN"):
        await fenced_clerk.submit_intent(_intent("bot-a", "run-a", "rejected-after-death"))


@pytest.mark.asyncio
async def test_callback_persistence_failure_closes_intake_and_balances_callback_queue(tmp_path: Path) -> None:
    """#1044 P0: a dead fsync worker neither accepts nor strands callbacks."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    generation = _write_active_clerk_generation(tmp_path)
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_FakeBroker(),
        clerk_generation=generation,
    )
    failures: list[BaseException] = []
    server = AccountClerkRpcServer(clerk, on_callback_persistence_failure=failures.append)
    await server.start()
    try:
        async def fail_callback_write(_event: IbkrOrderEvent) -> object:
            raise OSError("simulated callback journal fsync failure")

        clerk.record_broker_event = fail_callback_write  # type: ignore[method-assign]
        event = IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=101,
            event_type="fill",
            order_ref=_intent("bot-a", "run-a", "callback-failure").order_ref,
            fill_quantity=1,
            avg_fill_price=100,
            ts_ms=START_MS,
        )
        server._record_broker_event(event)
        await asyncio.wait_for(server._callback_queue.join(), timeout=1)

        assert isinstance(server._callback_failure, OSError)
        assert len(failures) == 1 and isinstance(failures[0], OSError)
        assert server._callback_queue.empty()
        with pytest.raises(AccountClerkCallbackPersistenceError):
            server._record_broker_event(event)
        await asyncio.wait_for(server._callback_queue.join(), timeout=1)
        with pytest.raises(AccountClerkIntentRejected, match="CLERK_EVENT_STREAM_DOWN"):
            await clerk.submit_intent(_intent("bot-a", "run-a", "submit-after-callback-failure"))
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_concurrent_stream_failures_write_one_durable_alarm(tmp_path: Path) -> None:
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())

    await asyncio.gather(
        clerk.mark_event_stream_down(ConnectionError("first")),
        clerk.mark_event_stream_down(OSError("second")),
    )

    alarms = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_event_stream_down"
    ]
    assert len(alarms) == 1


@pytest.mark.asyncio
async def test_rpc_shutdown_rejects_reconciliation_retry_before_broker_write(tmp_path: Path) -> None:
    _write_active_binding(tmp_path, "bot-a", "run-a")
    broker = _FakeBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker)
    intent = _intent("bot-a", "run-a", "retry-after-rpc-close")
    await clerk.record_intent(intent)
    clerk.close_normal_submit_intake()

    with pytest.raises(AccountClerkIntentRejected, match="CLERK_RPC_CLOSED"):
        await clerk.retry_recorded_intent(intent)

    assert broker.calls == []


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
        await server._flush_broker_callbacks()
        client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
        bot_b_consumer = AccountClerkEventConsumerIdentity(
            account_id=ACCOUNT,
            strategy_instance_id="bot-b",
            run_id="run-b",
            bot_order_namespace=bot_order_namespace_for_instance("bot-b"),
        )
        bot_a_consumer = AccountClerkEventConsumerIdentity(
            account_id=ACCOUNT,
            strategy_instance_id="bot-a",
            run_id="run-a",
            bot_order_namespace=intent_a.bot_order_namespace,
        )
        bot_b_events = await client.drain_events(
            after_seq=0,
            consumer=bot_b_consumer,
            cursor=AccountClerkEventCursorRepo(tmp_path / "run-b"),
        )
        bot_a_events = await client.drain_events(
            after_seq=0,
            consumer=bot_a_consumer,
            cursor=AccountClerkEventCursorRepo(tmp_path / "run-a"),
        )
    finally:
        await server.close()

    assert bot_b_events == []
    assert [delivery.event.order_ref for delivery in bot_a_events] == [intent_a.order_ref]
    assert read_account_clerk_journal(tmp_path, ACCOUNT)[-1].entry_kind == "broker_event"


@pytest.mark.asyncio
async def test_unattributed_broker_callback_is_persisted_and_blocks_new_account_starts(tmp_path: Path) -> None:
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
        if event["event_type"] == "account_clerk_unattributed_broker_event"
    ]
    [broker_event] = [
        entry
        for entry in read_account_clerk_journal(tmp_path, ACCOUNT)
        if entry.entry_kind == "broker_event"
    ]
    assert broker_event.intent is None
    assert broker_event.event_account_id == ACCOUNT
    assert alarm["reason"] == "BROKER_EVENT_WITHOUT_DURABLE_CLERK_INTENT"
    assert read_account_freeze(tmp_path, ACCOUNT) is not None


@pytest.mark.asyncio
async def test_reconciler_skips_unattributed_callback_rows(tmp_path: Path) -> None:
    """#1044 regression: unknown account flow is not an intent to reconcile."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_UncertainBroker("PRESENT"),
    )
    intent = _intent("bot-a", "run-a", "uncertain-with-foreign-callback")
    with pytest.raises(TimeoutError, match="lost acknowledgement"):
        await clerk.submit_intent(intent)
    await clerk.record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=808,
            event_type="fill",
            order_ref="learn-ai/unknown/v1:foreign-intent",
            fill_quantity=1,
            ts_ms=START_MS + 1,
        )
    )

    [resolution] = await AccountClerkReconciler(clerk).reconcile_once()

    assert resolution.intent_id == intent.intent_id
    assert resolution.verdict.value == "RECOVER_ADOPT"


@pytest.mark.asyncio
async def test_unattributed_callback_reasserts_guardrail_after_alarm_crash_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1044 regression: journal truth repairs a lost derived alarm after restart."""

    event = IbkrOrderEvent(
        account_id=ACCOUNT,
        order_id=809,
        event_type="fill",
        order_ref="learn-ai/unknown/v1:foreign-intent",
        fill_quantity=1,
        ts_ms=START_MS,
    )
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)

    def crash_after_journal_fsync(*_args: object) -> None:
        raise OSError("simulated alarm crash")

    monkeypatch.setattr(
        clerk,
        "_assert_unattributed_broker_event_guardrail",
        crash_after_journal_fsync,
    )
    with pytest.raises(OSError, match="simulated alarm crash"):
        await clerk.record_broker_event(event)

    restarted = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    await restarted.rebuild_attribution()
    duplicate = await restarted.record_broker_event(event)

    assert duplicate.newly_recorded is False
    assert read_account_freeze(tmp_path, ACCOUNT) is not None
    alarms = [
        account_event
        for account_event in read_account_events(tmp_path, ACCOUNT)
        if account_event["event_type"] == "account_clerk_unattributed_broker_event"
    ]
    assert len(alarms) == 1


@pytest.mark.asyncio
async def test_callback_fsync_is_offloaded_without_allowing_relay_before_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1044: slow journal fsync yields the loop and relay waits for it."""

    _write_active_binding(tmp_path, "bot-a", "run-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT)
    intent = _intent("bot-a", "run-a", "off-loop-fsync")
    await clerk.record_intent(intent)
    original_append = account_clerk_journal_module._append_jsonl

    def slow_append(path: Path, entry: AccountClerkInboxEntry | AccountClerkJournalEntry) -> None:
        if isinstance(entry, AccountClerkJournalEntry) and entry.entry_kind == "broker_event":
            time.sleep(0.1)
        original_append(path, entry)

    monkeypatch.setattr(account_clerk_journal_module, "_append_jsonl", slow_append)
    task = asyncio.create_task(
        clerk.record_broker_event(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=505,
                event_type="fill",
                order_ref=intent.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                exec_id="off-loop-fsync-fill",
                ts_ms=START_MS,
            )
        )
    )

    started_at = time.monotonic()
    await asyncio.sleep(0)
    assert time.monotonic() - started_at < 0.05
    assert not task.done()
    await task


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
    original_append = account_clerk_journal_module._append_jsonl
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

    monkeypatch.setattr(account_clerk_journal_module, "_append_jsonl", crash_between_inbox_and_journal)
    intent = _intent("bot-a", "run-a", f"crash-intent-{crash_seq}")
    with pytest.raises(OSError, match="simulated process crash"):
        await clerk.record_intent(intent)
    monkeypatch.setattr(account_clerk_journal_module, "_append_jsonl", original_append)

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
    original_rewrite = account_clerk_journal_module._rewrite_jsonl

    def crash_after_journal_fsync(path: Path, entries: list[AccountClerkInboxEntry]) -> None:
        if path == inbox_path and journal_path.exists():
            raise OSError("simulated process crash before inbox compaction")
        original_rewrite(path, entries)

    monkeypatch.setattr(account_clerk_journal_module, "_rewrite_jsonl", crash_after_journal_fsync)
    intent = _intent("bot-a", "run-a", "already-journaled")
    with pytest.raises(OSError, match="simulated process crash"):
        await clerk.record_intent(intent)
    monkeypatch.setattr(account_clerk_journal_module, "_rewrite_jsonl", original_rewrite)

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
    account_clerk_journal_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(
            seq=1,
            recorded_at_ms=START_MS,
            intent=journal_intent,
        ),
    )
    account_clerk_journal_module._append_jsonl(
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
    account_clerk_journal_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(seq=1, recorded_at_ms=START_MS, intent=_intent("bot-a", "run-a", "base")),
    )
    for intent in (first_intent, second_intent):
        account_clerk_journal_module._append_jsonl(
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
    account_clerk_journal_module._append_jsonl(
        journal_path,
        AccountClerkJournalEntry(seq=1, recorded_at_ms=START_MS, intent=_intent("bot-a", "run-a", "base")),
    )
    account_clerk_journal_module._append_jsonl(
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
        account_clerk_journal_module._append_jsonl(
            journal_path,
            AccountClerkJournalEntry(
                seq=sequence,
                recorded_at_ms=START_MS,
                intent=_intent("bot-a", "run-a", f"journal-{sequence}"),
            ),
        )

    with pytest.raises(AccountClerkJournalCorruptError, match=error):
        read_account_clerk_journal(tmp_path, ACCOUNT)


def test_clerk_journal_entries_reject_future_schema_versions() -> None:
    """Durable Clerk artifacts must not silently accept unknown wire formats."""

    with pytest.raises(ValueError, match="schema_version"):
        AccountClerkJournalEntry.model_validate(
            {
                "schema_version": 2,
                "seq": 1,
                "recorded_at_ms": START_MS,
                "intent": _intent("bot-a", "run-a", "future-journal"),
            }
        )
    with pytest.raises(ValueError, match="schema_version"):
        AccountClerkInboxEntry.model_validate(
            {
                "schema_version": 2,
                "seq": 1,
                "received_at_ms": START_MS,
                "intent": _intent("bot-a", "run-a", "future-inbox"),
            }
        )


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


@pytest.mark.asyncio
async def test_reconciler_resets_failures_then_exits_unhealthy_after_three_consecutive_failures(
    tmp_path: Path,
) -> None:
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    stopped = asyncio.Event()
    reconciler = AccountClerkReconciler(
        clerk,
        cadence_seconds=0,
        now_ms=lambda: START_MS,
        on_unhealthy=stopped.set,
    )
    outcomes = iter(
        [
            RuntimeError("first"),
            RuntimeError("second"),
            None,
            RuntimeError("third-1"),
            RuntimeError("third-2"),
            RuntimeError("third-3"),
        ]
    )

    async def scripted_reconcile_once() -> tuple[object, ...]:
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return ()

    reconciler.reconcile_once = scripted_reconcile_once  # type: ignore[method-assign]
    await reconciler.start()
    await asyncio.wait_for(stopped.wait(), timeout=1)
    await asyncio.sleep(0)

    failed = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_reconciliation_iteration_failed"
    ]
    assert [event["consecutive_failures"] for event in failed] == [1, 2, 1, 2, 3]
    assert reconciler.healthy is False
    assert read_account_freeze(tmp_path, ACCOUNT).reason == "ACCOUNT_CLERK_RECONCILIATION_UNHEALTHY"


@pytest.mark.asyncio
async def test_reconciler_unexpected_task_cancellation_records_terminal_alarm(tmp_path: Path) -> None:
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    stopped = asyncio.Event()
    reconciler = AccountClerkReconciler(
        clerk,
        cadence_seconds=60,
        now_ms=lambda: START_MS,
        on_unhealthy=stopped.set,
    )
    await reconciler.start()
    assert reconciler._task is not None
    reconciler._task.cancel()
    with suppress(asyncio.CancelledError):
        await reconciler._task
    await asyncio.wait_for(stopped.wait(), timeout=1)

    [alarm] = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_reconciliation_unhealthy"
    ]
    assert alarm["reason"] == "ACCOUNT_CLERK_RECONCILIATION_TASK_CANCELLED"


def test_reconciler_unhealthy_stop_hook_runs_before_terminal_artifact_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed alarm fsync cannot leave a dead reconciler renewing its lease."""

    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_FakeBroker())
    stopped = threading.Event()
    reconciler = AccountClerkReconciler(
        clerk,
        now_ms=lambda: START_MS,
        on_unhealthy=stopped.set,
    )

    def fail_terminal_event(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated full artifact volume")

    monkeypatch.setattr(account_clerk_reconciler_module, "append_account_event", fail_terminal_event)

    with pytest.raises(OSError, match="simulated full artifact volume"):
        reconciler._mark_unhealthy("CONSECUTIVE_RECONCILIATION_FAILURES")

    assert stopped.is_set()
