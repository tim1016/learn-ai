"""Contract tests for #1040's bounded typed Account Clerk RPC protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.live.account_clerk_rpc as account_clerk_rpc
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.execution.order_sizer import FixedShares, OrderSizer
from app.engine.live.account_artifacts import advance_account_clerk_generation, read_account_events
from app.engine.live.account_clerk import (
    AccountClerk,
    AccountClerkIntentRejected,
    account_clerk_socket_path,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_cursor import (
    AccountClerkEventConsumerIdentity,
    AccountClerkEventCursorRepo,
)
from app.engine.live.account_clerk_rpc import (
    ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S,
    ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S,
    ACCOUNT_CLERK_RPC_SCHEMA_VERSION,
    AccountClerkRpcCancelNamespaceUncertainError,
    AccountClerkRpcClient,
    AccountClerkRpcGenerationHandshake,
    AccountClerkRpcGenerationMismatchError,
    AccountClerkRpcInternalError,
    AccountClerkRpcMalformedResponseError,
    AccountClerkRpcRejectedError,
    AccountClerkRpcRequestIdentity,
    AccountClerkRpcServer,
    AccountClerkRpcSuccessEnvelope,
    AccountClerkRpcTimeoutError,
    AccountClerkRpcUnavailableError,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.live_portfolio import LivePortfolio, SubmitUncertainHaltError
from app.engine.live.order_identity import build_order_ref
from app.schemas.journal_cures import JournalCureRequest
from tests.engine.live.fixtures.fake_broker import FakeBroker

ACCOUNT = "DU1040"
START_MS = 1_784_000_000_000


class _Broker:
    def __init__(self) -> None:
        self._client = SimpleNamespace(settings=SimpleNamespace(mode="paper"))

    async def place_order(self, _order: object) -> object:
        return SimpleNamespace(order_id=101, perm_id=201, exec_id="exec-1040")

    async def cancel_open_orders_for_namespace(self, _namespace: str) -> list[int]:
        return [1046]


class _FailingBroker(_Broker):
    async def place_order(self, _order: object) -> object:
        raise ValueError("broker secret must not reach the socket response")


class _UncertainCancelBroker(_Broker):
    async def cancel_open_orders_for_namespace(self, _namespace: str) -> list[int]:
        raise TimeoutError("cancel confirmation lost")
class _BlockingCallbackBroker(_Broker):
    """Emit one broker callback, then hold the accepted write at the boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._callback_sink: Callable[[IbkrOrderEvent], None] | None = None

    def set_broker_callback_sink(self, sink: Callable[[IbkrOrderEvent], None]) -> None:
        self._callback_sink = sink

    async def place_order(self, order: object) -> object:
        assert isinstance(order, IbkrOrderSpec)
        assert self._callback_sink is not None
        self._callback_sink(
            IbkrOrderEvent(
                account_id=ACCOUNT,
                order_id=101,
                event_type="fill",
                order_ref=order.order_ref,
                symbol="SPY",
                side="BUY",
                fill_quantity=1,
                exec_id="shutdown-race-fill",
                ts_ms=START_MS + 1,
            )
        )
        self.started.set()
        await self.release.wait()
        return await super().place_order(order)


class _BarrierTimeoutController:
    """A deterministic replacement for ``asyncio.timeout`` with no sleep."""

    def __init__(self) -> None:
        self.budgets: list[float] = []
        self.entered = asyncio.Event()
        self.trigger = asyncio.Event()

    def context(self, timeout_s: float) -> _BarrierTimeoutContext:
        self.budgets.append(timeout_s)
        return _BarrierTimeoutContext(self)


class _BarrierTimeoutContext:
    def __init__(self, controller: _BarrierTimeoutController) -> None:
        self._controller = controller
        self._canceller: asyncio.Task[None] | None = None
        self._task: asyncio.Task[object] | None = None

    async def __aenter__(self) -> _BarrierTimeoutContext:
        task = asyncio.current_task()
        assert task is not None
        self._task = task
        self._controller.entered.set()
        self._canceller = asyncio.create_task(self._cancel_when_triggered())
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        del exc, traceback
        if self._canceller is not None:
            self._canceller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._canceller
        if exc_type is asyncio.CancelledError:
            raise TimeoutError
        return False

    async def _cancel_when_triggered(self) -> None:
        await self._controller.trigger.wait()
        assert self._task is not None
        self._task.cancel()


def _write_active_binding(tmp_path: Path, instance_id: str = "bot-a", run_id: str = "run-a") -> None:
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


def _intent(intent_id: str = "intent-1040") -> AccountOwnerSubmitIntent:
    namespace = bot_order_namespace_for_instance("bot-a")
    order_ref = build_order_ref(namespace, intent_id)
    return AccountOwnerSubmitIntent(
        trace_id=f"trace-{intent_id}",
        account_id=ACCOUNT,
        strategy_instance_id="bot-a",
        run_id="run-a",
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
        owner_generation=99,
        created_at_ms=START_MS,
    )


def _event_consumer() -> AccountClerkEventConsumerIdentity:
    return AccountClerkEventConsumerIdentity(
        account_id=ACCOUNT,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
    )


def _drain_kwargs(tmp_path: Path) -> dict[str, object]:
    return {
        "after_seq": 0,
        "consumer": _event_consumer(),
        "cursor": AccountClerkEventCursorRepo(tmp_path / "run"),
    }


async def _read_raw_response(path: Path, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        handshake = AccountClerkRpcGenerationHandshake.model_validate_json(await reader.readline())
        assert handshake.account_id == ACCOUNT
        assert handshake.served_generation == 1
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    return json.loads(line)


async def _start_raw_server(
    tmp_path: Path,
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], object],
    *,
    served_generation: int = 1,
) -> tuple[asyncio.AbstractServer, Path]:
    path = account_clerk_socket_path(tmp_path, ACCOUNT)
    path.parent.mkdir(parents=True, exist_ok=True)

    async def generation_checked_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(
            (
                AccountClerkRpcGenerationHandshake(
                    account_id=ACCOUNT,
                    served_generation=served_generation,
                ).model_dump_json()
                + "\n"
            ).encode()
        )
        await writer.drain()
        await handler(reader, writer)

    server = await asyncio.start_unix_server(generation_checked_handler, path=str(path))
    return server, path


async def _close_raw_server(server: asyncio.AbstractServer, path: Path) -> None:
    server.close()
    await server.wait_closed()
    if path.exists():
        path.unlink()


@pytest.fixture(autouse=True)
def _write_active_clerk_generation(tmp_path: Path) -> None:
    advance_account_clerk_generation(
        tmp_path,
        ACCOUNT,
        phase="accepting",
        recorded_at_ms=START_MS,
        source="test",
    )


@pytest.mark.asyncio
async def test_success_envelope_round_trips_over_real_unix_socket(tmp_path: Path) -> None:
    _write_active_binding(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    )
    await server.start()
    try:
        response = await _read_raw_response(
            account_clerk_socket_path(tmp_path, ACCOUNT),
            {"operation": "submit", "intent": _intent().model_dump(mode="json")},
        )
    finally:
        await server.close()

    envelope = AccountClerkRpcSuccessEnvelope.model_validate(response)
    assert envelope.schema_version == ACCOUNT_CLERK_RPC_SCHEMA_VERSION
    assert envelope.outcome == "success"
    assert envelope.payload["broker_acked"]["order_id"] == 101


@pytest.mark.asyncio
async def test_cancel_namespace_round_trips_as_a_durable_terminal_receipt(tmp_path: Path) -> None:
    _write_active_binding(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    )
    await server.start()
    try:
        intent = _intent("cancel-1046").model_copy(
            update={"intent_kind": "CANCEL_NAMESPACE", "order_spec": {}}
        )
        receipt = await AccountClerkRpcClient(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
        ).cancel_namespace(intent)
    finally:
        await server.close()

    assert receipt.status == "cancel_confirmed"
    assert receipt.cancelled_order_ids == (1046,)


@pytest.mark.asyncio
async def test_operator_adjustment_round_trips_through_the_live_clerk_journal(tmp_path: Path) -> None:
    """A cure must share the Clerk's serialized journal tail, never append behind it."""

    _write_active_binding(tmp_path)
    intent = _intent("cure-1059")
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    await clerk.submit_intent(intent)
    clerk.append_broker_event(
        intent,
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=1,
            event_type="fill",
            order_ref=intent.order_ref,
            symbol="SPY",
            side="BUY",
            fill_quantity=1,
            exec_id="cure-rpc-fill",
            ts_ms=START_MS,
        ),
    )
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=ACCOUNT,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            bot_order_namespace=intent.bot_order_namespace,
            lifecycle_state="RETIRED",
            recorded_at_ms=START_MS + 1,
            source="test.retired",
        ),
    )
    server = AccountClerkRpcServer(clerk)
    await server.start()
    try:
        receipt = await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).apply_operator_adjustment(
            JournalCureRequest(
                bot_order_namespace=intent.bot_order_namespace,
                symbol="SPY",
                signed_quantity=-1,
                reason="retired namespace fill was already reconciled",
                evidence_refs=("account-reconciliation:test",),
                request_provenance="test",
                idempotency_key="cure-rpc-1059",
            )
        )
    finally:
        await server.close()

    assert receipt.journal_seq > 0


@pytest.mark.asyncio
async def test_cancel_namespace_uncertainty_is_typed_over_rpc(tmp_path: Path) -> None:
    _write_active_binding(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
            broker=_UncertainCancelBroker(),
            clerk_generation=1,
        )
    )
    await server.start()
    try:
        intent = _intent("cancel-uncertain").model_copy(
            update={"intent_kind": "CANCEL_NAMESPACE", "order_spec": {}}
        )
        with pytest.raises(AccountClerkRpcCancelNamespaceUncertainError) as exc:
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).cancel_namespace(intent)
    finally:
        await server.close()

    assert exc.value.intent_id == intent.intent_id
async def test_close_fences_normal_submit_intake_before_callback_drain(tmp_path: Path) -> None:
    """Shutdown cannot admit a write while its callback worker is draining."""

    _write_active_binding(tmp_path)
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    server = AccountClerkRpcServer(clerk)
    await server.start()

    async def assert_intake_is_fenced() -> None:
        with pytest.raises(AccountClerkIntentRejected, match="CLERK_RPC_CLOSED"):
            await clerk.submit_intent(_intent())

    server._flush_broker_callbacks = assert_intake_is_fenced  # type: ignore[method-assign]
    await server.close()


@pytest.mark.asyncio
async def test_close_waits_for_active_submit_and_persists_its_callback(tmp_path: Path) -> None:
    """A broker write accepted before shutdown drains its callback before exit."""

    _write_active_binding(tmp_path)
    broker = _BlockingCallbackBroker()
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=broker, clerk_generation=1)
    server = AccountClerkRpcServer(clerk)
    await server.start()
    try:
        submit = asyncio.create_task(
            server._dispatch({"operation": "submit", "intent": _intent().model_dump(mode="json")})
        )
        await asyncio.wait_for(broker.started.wait(), timeout=1)

        closing = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        assert not closing.done()

        broker.release.set()
        await submit
        await closing
    finally:
        if not broker.release.is_set():
            broker.release.set()
        if not server._closing:
            await server.close()

    assert any(entry.entry_kind == "broker_event" for entry in read_account_clerk_journal(tmp_path, ACCOUNT))


@pytest.mark.asyncio
async def test_close_rejects_queued_submit_before_it_records_receipt(tmp_path: Path) -> None:
    """A request queued behind shutdown is rejected without a replayable row."""

    _write_active_binding(tmp_path)
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    server = AccountClerkRpcServer(clerk)
    await server.start()
    try:
        async with clerk._intake_lock:
            queued_submit = asyncio.create_task(clerk.submit_intent(_intent()))
            await asyncio.sleep(0)
            closing = asyncio.create_task(server.close())
            await asyncio.sleep(0)

        with pytest.raises(AccountClerkIntentRejected, match="CLERK_RPC_CLOSED"):
            await queued_submit
        await closing
    finally:
        if not server._closing:
            await server.close()

    assert read_account_clerk_journal(tmp_path, ACCOUNT) == []


@pytest.mark.asyncio
async def test_close_persists_stream_alarm_when_callback_drain_fails(tmp_path: Path) -> None:
    """Shutdown must not mask a callback durability failure behind its fence."""

    _write_active_binding(tmp_path)
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    server = AccountClerkRpcServer(clerk)
    await server.start()

    async def fail_callback_write(_event: IbkrOrderEvent) -> object:
        raise OSError("simulated shutdown callback fsync failure")

    clerk.record_broker_event = fail_callback_write  # type: ignore[method-assign]
    server._record_broker_event(
        IbkrOrderEvent(
            account_id=ACCOUNT,
            order_id=101,
            event_type="fill",
            order_ref=_intent().order_ref,
            fill_quantity=1,
            ts_ms=START_MS,
        )
    )
    await server.close()

    [stream_down] = [
        event
        for event in read_account_events(tmp_path, ACCOUNT)
        if event["event_type"] == "account_clerk_event_stream_down"
    ]
    assert stream_down["failure_type"] == "OSError"


@pytest.mark.asyncio
async def test_typed_clerk_rejection_round_trips_over_real_unix_socket(tmp_path: Path) -> None:
    server = AccountClerkRpcServer(
        AccountClerk(artifacts_root=tmp_path, account_id=ACCOUNT, broker=_Broker(), clerk_generation=1)
    )
    await server.start()
    try:
        with pytest.raises(AccountClerkRpcRejectedError) as exc:
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).submit(_intent())
    finally:
        await server.close()

    assert exc.value.reason_code == "ACCOUNT_CLERK_REJECTED"
    assert exc.value.reason == "CLERK_UNKNOWN_INSTANCE"
    assert exc.value.intent_id == "intent-1040"
    assert exc.value.order_ref == _intent().order_ref


@pytest.mark.asyncio
async def test_internal_server_failure_is_logged_and_never_leaks_to_socket_client(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_active_binding(tmp_path)
    server = AccountClerkRpcServer(
        AccountClerk(
            artifacts_root=tmp_path,
            account_id=ACCOUNT,
            broker=_FailingBroker(),
            clerk_generation=1,
        )
    )
    await server.start()
    try:
        with pytest.raises(AccountClerkRpcInternalError) as exc:
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).submit(_intent())
    finally:
        await server.close()

    assert exc.value.reason_code == "ACCOUNT_CLERK_INTERNAL_ERROR"
    assert "broker secret" not in str(exc.value)
    [record] = [
        record for record in caplog.records if record.message == "Account Clerk RPC server failure"
    ]
    assert record.rpc_operation == "submit"
    assert record.account_id == ACCOUNT
    assert record.reason_code == "ACCOUNT_CLERK_INTERNAL_ERROR"
    assert record.error_type == "ValueError"


@pytest.mark.asyncio
async def test_transport_failures_are_typed_and_distinguishable(tmp_path: Path) -> None:
    client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)

    with pytest.raises(AccountClerkRpcUnavailableError) as missing:
        await client.drain_events(**_drain_kwargs(tmp_path))
    assert missing.value.reason_code == "ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING"

    async def empty_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, empty_response)
    try:
        with pytest.raises(AccountClerkRpcUnavailableError) as empty:
            await client.drain_events(**_drain_kwargs(tmp_path))
    finally:
        await _close_raw_server(server, path)
    assert empty.value.reason_code == "ACCOUNT_CLERK_UNAVAILABLE:EMPTY_RESPONSE"

    async def malformed_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b'{"schema_version": 99, "outcome": "success"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, malformed_response)
    try:
        with pytest.raises(AccountClerkRpcMalformedResponseError) as malformed:
            await client.drain_events(**_drain_kwargs(tmp_path))
    finally:
        await _close_raw_server(server, path)
    assert malformed.value.reason_code == "ACCOUNT_CLERK_PROTOCOL_ERROR:MALFORMED_RESPONSE"

    async def discard_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, discard_request)
    server.close()
    await server.wait_closed()
    assert path.exists()
    try:
        with pytest.raises(AccountClerkRpcUnavailableError) as connect_failed:
            await client.drain_events(**_drain_kwargs(tmp_path))
    finally:
        if path.exists():
            path.unlink()
    assert connect_failed.value.reason_code == "ACCOUNT_CLERK_UNAVAILABLE:SOCKET_CONNECT_FAILED"


@pytest.mark.asyncio
async def test_client_rejects_a_stale_socket_generation_before_writing_a_request(tmp_path: Path) -> None:
    request_received = asyncio.Event()

    async def stale_socket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if line:
            request_received.set()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, stale_socket, served_generation=2)
    try:
        with pytest.raises(AccountClerkRpcGenerationMismatchError) as exc:
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).drain_events(
                **_drain_kwargs(tmp_path)
            )
    finally:
        await _close_raw_server(server, path)

    assert exc.value.reason_code == "ACCOUNT_CLERK_UNAVAILABLE:GENERATION_MISMATCH"
    assert exc.value.expected_generation == 1
    assert exc.value.served_generation == 2
    assert not request_received.is_set()


@pytest.mark.asyncio
async def test_drain_events_rejects_a_broker_event_outside_the_canonical_shape(tmp_path: Path) -> None:
    async def invalid_event_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        response = AccountClerkRpcSuccessEnvelope(
            payload={"events": [{"event_type": "fill", "exec_id": "missing-required-fields"}]}
        )
        writer.write((response.model_dump_json() + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, invalid_event_response)
    try:
        with pytest.raises(AccountClerkRpcMalformedResponseError) as exc:
            await AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT).drain_events(
                **_drain_kwargs(tmp_path)
            )
    finally:
        await _close_raw_server(server, path)

    assert exc.value.reason_code == "ACCOUNT_CLERK_PROTOCOL_ERROR:MALFORMED_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc_request", "expected_budget", "expected_identity"),
    [
        (
            {"operation": "submit", "intent": _intent().model_dump(mode="json")},
            ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S,
            AccountClerkRpcRequestIdentity(intent_id="intent-1040", order_ref=_intent().order_ref),
        ),
        (
            {"operation": "drain_events", "bot_order_namespace": "learn-ai/bot-a/v1"},
            ACCOUNT_CLERK_RPC_NORMAL_TIMEOUT_S,
            AccountClerkRpcRequestIdentity(intent_id=None, order_ref=None),
        ),
        (
            {
                "operation": "recovery_flatten",
                "intent": _intent("recovery-1040").model_dump(mode="json"),
            },
            ACCOUNT_CLERK_RPC_RECOVERY_TIMEOUT_S,
            AccountClerkRpcRequestIdentity(
                intent_id="recovery-1040",
                order_ref=_intent("recovery-1040").order_ref,
            ),
        ),
    ],
)
async def test_timeout_budget_is_barrier_controlled_and_retains_request_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rpc_request: dict[str, object],
    expected_budget: float,
    expected_identity: AccountClerkRpcRequestIdentity,
) -> None:
    request_received = asyncio.Event()
    release_server = asyncio.Event()

    async def wait_for_timeout(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        request_received.set()
        await release_server.wait()
        writer.close()
        await writer.wait_closed()

    server, path = await _start_raw_server(tmp_path, wait_for_timeout)
    controller = _BarrierTimeoutController()
    monkeypatch.setattr(account_clerk_rpc, "_request_timeout", controller.context)
    client = AccountClerkRpcClient(artifacts_root=tmp_path, account_id=ACCOUNT)
    request_task = asyncio.create_task(client._request(rpc_request))
    try:
        await controller.entered.wait()
        await request_received.wait()
        controller.trigger.set()
        with pytest.raises(AccountClerkRpcTimeoutError) as exc:
            await request_task
    finally:
        release_server.set()
        await _close_raw_server(server, path)

    assert controller.budgets == [expected_budget]
    assert exc.value.reason_code == "ACCOUNT_CLERK_UNAVAILABLE:TIMEOUT"
    assert exc.value.intent_id == expected_identity.intent_id
    assert exc.value.order_ref == expected_identity.order_ref


@pytest.mark.asyncio
async def test_typed_clerk_rejection_reaches_portfolio_as_rejection() -> None:
    broker = FakeBroker()

    async def reject_from_clerk(intent: AccountOwnerSubmitIntent) -> object:
        raise AccountClerkRpcRejectedError(
            reason="CLERK_INACTIVE_BINDING",
            operation="submit",
            request_identity=AccountClerkRpcRequestIdentity(
                intent_id=intent.intent_id,
                order_ref=intent.order_ref,
            ),
        )

    portfolio = LivePortfolio(
        broker,
        account_owner_submitter=reject_from_clerk,
        account_id=ACCOUNT,
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace=bot_order_namespace_for_instance("bot-a"),
        owner_generation_provider=lambda: 99,
    )
    portfolio.order_sizer = OrderSizer(FixedShares(value=1))
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    with pytest.raises(SubmitUncertainHaltError) as exc:
        await portfolio.submit_pending_orders()

    assert exc.value.probe_result == "rejected"
    assert exc.value.reason == "CLERK_INACTIVE_BINDING"
    assert broker.orders == []
