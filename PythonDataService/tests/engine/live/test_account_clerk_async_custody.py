"""Crash-boundary coverage for the disabled asynchronous Clerk custody path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import advance_account_clerk_generation
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkIntentRejected,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_journal import AccountClerkJournal
from app.engine.live.account_clerk_journal_models import (
    AccountClerkAsyncCustodyConfig,
    AccountClerkCustodyStatus,
)
from app.engine.live.account_clerk_reconciler import AccountClerkReconciler
from app.engine.live.account_clerk_rpc import (
    AccountClerkRpcClient,
    AccountClerkRpcRejectedError,
    AccountClerkRpcServer,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.order_identity import build_order_ref

ACCOUNT = "DUASYNC"
START_MS = 1_784_000_000_000


class _DelayedBroker:
    def __init__(self) -> None:
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))
        self.calls: list[IbkrOrderSpec] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._callback_sink = None

    def set_broker_callback_sink(self, sink) -> None:  # type: ignore[no-untyped-def]
        self._callback_sink = sink

    async def place_order(self, spec: IbkrOrderSpec) -> object:
        self.calls.append(spec)
        self.started.set()
        assert self._callback_sink is not None
        fill = IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=101,
            event_type="fill",
            status="Filled",
            order_ref=spec.order_ref,
            symbol=spec.symbol,
            side=spec.action,
            fill_quantity=spec.quantity,
            exec_id=f"exec:{spec.order_ref}",
            exec_time_ms=START_MS + 1,
            ts_ms=START_MS + 2,
        )
        # A duplicate and a later status must not move the economic lifecycle
        # backwards while the acknowledgement is still deliberately delayed.
        self._callback_sink(fill)
        self._callback_sink(fill)
        self._callback_sink(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=101,
                event_type="status",
                status="Submitted",
                order_ref=spec.order_ref,
                symbol=spec.symbol,
                side=spec.action,
                ts_ms=START_MS + 3,
            )
        )
        await self.release.wait()
        return SimpleNamespace(order_id=101, perm_id=201, exec_id=f"exec:{spec.order_ref}")

    async def fetch_positions(self) -> object:
        return SimpleNamespace(account_id=ACCOUNT, is_paper=True, positions=[], fetched_at_ms=START_MS)

    async def list_open_orders(self) -> list[object]:
        return []

    async def cancel_open_orders_for_namespace(self, _namespace: str) -> list[int]:
        return []

    async def cancel_open_orders_for_refs(self, _order_refs: tuple[str, ...]) -> list[int]:
        return []

    async def probe_intent_status(self, _intent_id: str, _order_ref: str) -> str:
        return "NOT_PROVABLE"


class _ImmediateBroker(_DelayedBroker):
    async def place_order(self, spec: IbkrOrderSpec) -> object:
        self.calls.append(spec)
        self.started.set()
        return SimpleNamespace(order_id=101, perm_id=201, exec_id=f"exec:{spec.order_ref}")


class _ConnectionLossBroker(_ImmediateBroker):
    async def place_order(self, spec: IbkrOrderSpec) -> object:
        self.calls.append(spec)
        self.started.set()
        raise ConnectionError("simulated broker socket loss")


def _bind(tmp_path: Path, instance_id: str) -> None:
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id=instance_id,
            run_id=f"run-{instance_id}",
            bot_order_namespace=bot_order_namespace_for_instance(instance_id),
            lifecycle_state="ACTIVE",
            recorded_at_ms=START_MS,
            source="test",
        ),
    )


def _intent(instance_id: str, intent_id: str) -> AccountOwnerSubmitIntent:
    namespace = bot_order_namespace_for_instance(instance_id)
    order_ref = build_order_ref(namespace, intent_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=ACCOUNT,
        strategy_instance_id=instance_id,
        run_id=f"run-{instance_id}",
        bot_order_namespace=namespace,
        intent_id=intent_id,
        order_ref=order_ref,
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
            order_ref=order_ref,
        ).model_dump(),
        owner_generation=1,
        created_at_ms=START_MS,
    )


async def _wait_for_status(
    client: AccountClerkRpcClient,
    intent: AccountOwnerSubmitIntent,
    expected: str,
) -> object:
    for _ in range(100):
        status = await client.read_custody_v2(intent)
        if status is not None and status.lifecycle_state == expected:
            return status
        await asyncio.sleep(0)
    raise AssertionError(f"custody status did not reach {expected}")


async def _wait_for_local_status(
    clerk: AccountClerk,
    intent: AccountOwnerSubmitIntent,
    expected: str,
) -> AccountClerkCustodyStatus:
    for _ in range(100):
        status = await clerk.async_custody_status(intent)
        if status is not None and status.lifecycle_state == expected:
            return status
        await asyncio.sleep(0)
    raise AssertionError(f"local custody status did not reach {expected}")


@pytest.mark.asyncio
async def test_async_custody_is_disabled_without_explicit_capacity_config(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_DelayedBroker())

    assert clerk.async_custody_health().model_dump() == {
        "enabled": False,
        "entry_depth": 0,
        "entry_capacity": 0,
        "risk_reducing_depth": 0,
        "risk_reducing_capacity": 0,
    }
    with pytest.raises(AccountClerkIntentRejected) as rejected:
        await clerk.submit_async_custody(_intent("bot-a", "disabled"))
    assert getattr(rejected.value, "reason", None) == "CLERK_ASYNC_CUSTODY_DISABLED"


@pytest.mark.asyncio
async def test_async_custody_rejects_deterministic_qualification_before_a0(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_ImmediateBroker(),
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    invalid = _intent("bot-a", "invalid-spec").model_copy(update={"order_spec": {"symbol": "SPY"}})
    await clerk.start_async_custody()
    try:
        with pytest.raises(AccountClerkIntentRejected) as rejected:
            await clerk.submit_async_custody(invalid)
        assert rejected.value.reason == "CLERK_ASYNC_CUSTODY_QUALIFICATION_FAILED:ValidationError"
        assert read_account_clerk_journal(tmp_path, ACCOUNT) == []
    finally:
        await clerk.stop_async_custody()


def test_async_custody_lane_replays_from_the_inbox_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after inbox fsync cannot erase the A0 lane's restart policy."""

    journal = AccountClerkJournal(artifacts_root=tmp_path, account_id=ACCOUNT)
    intent = _intent("bot-a", "inbox-a0-lane")
    original_append = journal._append_entry_locked

    def crash_before_journal(*_: object, **__: object) -> None:
        raise OSError("simulated journal crash after inbox fsync")

    monkeypatch.setattr(journal, "_append_entry_locked", crash_before_journal)
    with pytest.raises(OSError, match="simulated journal crash"):
        journal.record_intent(
            intent,
            validate_intent=lambda _: None,
            async_custody_lane="risk_reducing",
        )
    monkeypatch.setattr(journal, "_append_entry_locked", original_append)

    [recovered] = journal.recover_inbox()
    [recorded] = journal.snapshot()
    assert recovered.intent_id == intent.intent_id
    assert recorded.entry_kind == "recorded"
    assert recorded.async_custody_lane == "risk_reducing"


@pytest.mark.asyncio
async def test_async_custody_durably_holds_when_a_post_a0_policy_fence_closes(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    broker = _ImmediateBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    await clerk.start_async_custody()
    await clerk._broker_write_lock.acquire()
    try:
        intent = _intent("bot-a", "fenced-after-a0")
        _, initial = await clerk.submit_async_custody(intent)
        assert initial.lifecycle_state == "queued"
        clerk._normal_submit_intake_reason = "CLERK_TEST_POST_A0_FENCE"
    finally:
        clerk._broker_write_lock.release()
    try:
        held = await _wait_for_local_status(clerk, intent, "submission_hold")
        assert held.custody_stage == "A0_CUSTODY_ACCEPTED"
        assert await AccountClerkReconciler(clerk).reconcile_once() == ()
        assert not broker.calls
    finally:
        clerk._normal_submit_intake_reason = None
        await clerk.stop_async_custody()


@pytest.mark.asyncio
async def test_async_custody_shutdown_does_not_wait_for_uncooperative_observer(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_ImmediateBroker(),
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    observer_entered = asyncio.Event()
    observer_release = asyncio.Event()

    async def uncooperative_observer(_: AccountClerkCustodyStatus) -> None:
        observer_entered.set()
        while not observer_release.is_set():
            try:
                await observer_release.wait()
            except asyncio.CancelledError:
                # Models a third-party observer that ignores cancellation.
                continue

    clerk.set_custody_notification_sink(uncooperative_observer)
    await clerk.start_async_custody()
    try:
        await clerk.submit_async_custody(_intent("bot-a", "observer-timeout"))
        await observer_entered.wait()
        await asyncio.wait_for(clerk.stop_async_custody(), timeout=0.25)
    finally:
        observer_release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_async_custody_returns_a0_then_keeps_fill_before_ack_monotone(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    generation = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation
    broker = _DelayedBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    observed_lifecycles: list[str] = []

    async def observe(status: AccountClerkCustodyStatus) -> None:
        observed_lifecycles.append(status.lifecycle_state)

    clerk.set_custody_notification_sink(observe)
    server = AccountClerkRpcServer(clerk)
    await server.start()
    client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
    intent = _intent("bot-a", "intent-a")
    try:
        recorded, initial = await client.submit_custody_v2(intent)

        assert recorded.intent_id == intent.intent_id
        assert initial.custody_stage in {"A0_CUSTODY_ACCEPTED", "A1_BROKER_WRITE_STARTED"}
        await broker.started.wait()
        entry_kinds = [entry.entry_kind for entry in read_account_clerk_journal(tmp_path, ACCOUNT)]
        assert entry_kinds.index("recorded") < entry_kinds.index("broker_submitting")
        terminal = await _wait_for_status(client, intent, "economic_terminal")
        assert terminal.custody_stage == "A3_ECONOMIC_TERMINAL"
        # The delayed acknowledgement cannot regress a fill-before-ack fold.
        broker.release.set()
        after_ack = await _wait_for_status(client, intent, "economic_terminal")
        assert after_ack.custody_stage == "A3_ECONOMIC_TERMINAL"
        assert "economic_terminal" in observed_lifecycles
        assert len(broker.calls) == 1

        replay_recorded, replay = await client.submit_custody_v2(intent)
        assert replay_recorded == recorded
        assert replay.lifecycle_state == "economic_terminal"
        assert len(broker.calls) == 1
    finally:
        broker.release.set()
        await server.close()


@pytest.mark.asyncio
async def test_async_custody_refuses_full_queue_without_losing_existing_a0(tmp_path: Path) -> None:
    for instance_id in ("bot-a", "bot-b", "bot-c"):
        _bind(tmp_path, instance_id)
    generation = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation
    broker = _DelayedBroker()
    server = AccountClerkRpcServer(
        AccountClerk(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
            broker=broker,
            clerk_generation=generation,
            async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=2),
        )
    )
    await server.start()
    client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
    first = _intent("bot-a", "intent-a")
    second = _intent("bot-b", "intent-b")
    third = _intent("bot-c", "intent-c")
    try:
        await client.submit_custody_v2(first)
        await broker.started.wait()
        await client.submit_custody_v2(second)
        health = await client.custody_health_v2()
        assert health.entry_depth == 1
        assert health.entry_capacity == 1

        with pytest.raises(AccountClerkRpcRejectedError) as rejected:
            await client.submit_custody_v2(third)
        assert rejected.value.reason == "CLERK_ASYNC_ENTRY_QUEUE_FULL"
        assert await client.read_custody_v2(second) is not None
        assert await client.read_custody_v2(third) is None

        collision = _intent("bot-c", "intent-b")
        with pytest.raises(AccountClerkRpcRejectedError) as collision_rejected:
            await client.submit_custody_v2(collision)
        assert collision_rejected.value.reason == "CLERK_INTENT_ID_COLLISION"
    finally:
        broker.release.set()
        await server.close()


@pytest.mark.asyncio
async def test_async_custody_rejects_second_entry_for_one_strategy_but_keeps_risk_lane(tmp_path: Path) -> None:
    """A bot crash/restart cannot turn one durable A0 entry into two entries."""

    _bind(tmp_path, "bot-a")
    generation = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation
    broker = _DelayedBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=2, risk_reducing_capacity=1),
    )
    server = AccountClerkRpcServer(clerk)
    await server.start()
    client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
    first = _intent("bot-a", "entry-a0")
    duplicate = _intent("bot-a", "entry-after-originator-death")
    risk = _intent("bot-a", "risk-reducing")
    try:
        recorded, initial = await client.submit_custody_v2(first)
        assert recorded.intent_id == first.intent_id
        assert initial.custody_stage == "A0_CUSTODY_ACCEPTED"

        with pytest.raises(AccountClerkRpcRejectedError) as rejected:
            await client.submit_custody_v2(duplicate)
        assert rejected.value.reason == "CLERK_ASYNC_ENTRY_PENDING"
        assert await client.read_custody_v2(duplicate) is None

        risk_recorded, risk_status = await clerk.submit_async_custody(risk, lane="risk_reducing")
        assert risk_recorded.intent_id == risk.intent_id
        assert risk_status.lane == "risk_reducing"
        assert await client.read_custody_v2(first) is not None
    finally:
        broker.release.set()
        await server.close()


@pytest.mark.asyncio
async def test_async_custody_broker_socket_loss_after_a1_returns_to_reconciler(tmp_path: Path) -> None:
    """Only A0 is fenced from the generic reconciler; A1 keeps its recovery path."""

    _bind(tmp_path, "bot-a")
    generation = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation
    broker = _ConnectionLossBroker()
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        clerk_generation=generation,
        async_custody_config=AccountClerkAsyncCustodyConfig(entry_capacity=1, risk_reducing_capacity=1),
    )
    intent = _intent("bot-a", "a1-uncertain")
    await clerk.start_async_custody()
    try:
        _, queued = await clerk.submit_async_custody(intent)
        assert queued.custody_stage in {"A0_CUSTODY_ACCEPTED", "A1_BROKER_WRITE_STARTED"}
        uncertain = await _wait_for_local_status(clerk, intent, "uncertain_requires_reconciliation")
        assert uncertain.custody_stage == "A1_BROKER_WRITE_STARTED"

        [resolution] = await AccountClerkReconciler(clerk).reconcile_once()

        assert resolution.intent_id == intent.intent_id
        assert resolution.verdict.value == "HALT"
        assert "NOT_PROVABLE" in resolution.reason
        assert len(broker.calls) == 1
    finally:
        await clerk.stop_async_custody()


@pytest.mark.asyncio
async def test_async_custody_atomic_a0_survives_crash_before_volatile_enqueue(tmp_path: Path) -> None:
    _bind(tmp_path, "bot-a")
    generation = advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    ).generation
    config = AccountClerkAsyncCustodyConfig(entry_capacity=2, risk_reducing_capacity=2)
    clerk = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=_DelayedBroker(),
        clerk_generation=generation,
        async_custody_config=config,
    )
    entry = _intent("bot-a", "entry-a0")
    risk = _intent("bot-a", "risk-a0")
    # Simulate a process loss after the single durable A0 receipt and before
    # the process-local queue can receive the intent. The durable lane must
    # still determine restart policy without any second queue receipt.
    await asyncio.to_thread(clerk._record_intent_locked, entry, async_custody_lane="entry")
    await asyncio.to_thread(clerk._record_intent_locked, risk, async_custody_lane="risk_reducing")
    recorded = read_account_clerk_journal(tmp_path, ACCOUNT)
    assert [entry.entry_kind for entry in recorded] == ["recorded", "recorded"]
    assert [entry.async_custody_lane for entry in recorded] == ["entry", "risk_reducing"]
    assert await AccountClerkReconciler(clerk).reconcile_once() == ()

    restart_broker = _DelayedBroker()
    restarted = AccountClerk(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=restart_broker,
        clerk_generation=generation,
        async_custody_config=config,
    )
    await restarted.start_async_custody()
    try:
        assert (await _wait_for_local_status(restarted, entry, "expired_before_submit")).custody_stage == (
            "A3_ECONOMIC_TERMINAL"
        )
        assert (
            await _wait_for_local_status(restarted, risk, "recovery_action_required")
        ).custody_stage == "A0_CUSTODY_ACCEPTED"
        # Neither durable restart outcome can be retried by the older generic
        # reconciler after the async process boundary.
        assert await AccountClerkReconciler(restarted).reconcile_once() == ()
        assert not restart_broker.calls
    finally:
        await restarted.stop_async_custody()
