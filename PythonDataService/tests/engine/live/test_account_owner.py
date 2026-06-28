"""AccountOwner single-writer submit path tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.ibkr.client import IbkrClientIdInUseError
from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    AccountInstanceBinding,
    AccountOwnerGeneration,
    bot_order_namespace_for_instance,
    read_account_events,
    read_account_owner_generation,
    write_account_freeze,
    write_account_instance_binding,
    write_account_owner_generation,
)
from app.engine.live.account_classifier import AccountClassifierDecision
from app.engine.live.account_owner import (
    AccountOwner,
    AccountOwnerSubmitIntent,
    AccountOwnerSubmitRejected,
    ClientIdInUseError,
)
from app.engine.live.order_identity import build_order_ref, mint_intent_id

ACCOUNT = "DU123456"
SID = "spy_ema_paper"
RUN_ID = "run-alpha"
NS = bot_order_namespace_for_instance(SID)
GENERATION = 7


def _binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=ACCOUNT,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_700_000_000_000,
        source="test",
    )


def _intent(intent_id: str | None = None, *, generation: int = GENERATION) -> AccountOwnerSubmitIntent:
    iid = intent_id or mint_intent_id()
    return AccountOwnerSubmitIntent(
        trace_id="trace-1",
        account_id=ACCOUNT,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        intent_id=iid,
        order_ref=build_order_ref(NS, iid),
        intent_kind="STRATEGY",
        order_spec=_spec(iid).model_dump(),
        owner_generation=generation,
        created_at_ms=1_700_000_010_000,
    )


def _spec(intent_id: str) -> IbkrOrderSpec:
    return IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=1,
        order_type="MKT",
        time_in_force="DAY",
        confirm_paper=True,
        client_order_id=f"live-{intent_id[:8]}",
        order_ref=build_order_ref(NS, intent_id),
    )


def _continue_decision() -> AccountClassifierDecision:
    return AccountClassifierDecision(
        outcome="continue",
        reason="ACCOUNT_STATE_MATCHES_REGISTRY",
        account_id=ACCOUNT,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        bot_order_namespace=NS,
        decided_at_ms=1_700_000_020_000,
    )


class _Broker:
    client_id = 42

    def __init__(self, artifacts_root: Path | None = None) -> None:
        self.calls: list[IbkrOrderSpec] = []
        self.artifacts_root = artifacts_root

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        if self.artifacts_root is not None:
            events = read_account_events(self.artifacts_root, ACCOUNT)
            assert events[-1]["event_type"] == "account_owner_submit_prepared"
        self.calls.append(spec)
        return IbkrOrderAck(
            account_id=ACCOUNT,
            is_paper=True,
            order_id=len(self.calls),
            perm_id=9000 + len(self.calls),
            client_id=self.client_id,
            con_id=756733,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="Submitted",
            placed_at_ms=1_700_000_030_000,
        )


def _owner(
    tmp_path: Path,
    broker: _Broker,
    *,
    classifier=None,
    generation: int = GENERATION,
    owner_generation_advancer=None,
) -> AccountOwner:
    write_account_instance_binding(tmp_path, _binding())
    return AccountOwner(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        owner_generation_provider=lambda: generation,
        owner_generation_advancer=owner_generation_advancer,
        classifier=classifier or (lambda _intent: _continue_decision()),
    )


def test_account_owner_generation_persists_and_rejects_stale_intent(tmp_path: Path) -> None:
    write_account_owner_generation(
        tmp_path,
        AccountOwnerGeneration(
            account_id=ACCOUNT,
            generation=GENERATION,
            phase="accepting",
            recorded_at_ms=1_700_000_000_000,
            source="test",
        ),
    )
    loaded = read_account_owner_generation(tmp_path, ACCOUNT)

    assert loaded is not None
    assert loaded.generation == GENERATION


@pytest.mark.asyncio
async def test_account_owner_writes_pre_submit_and_terminal_evidence(tmp_path: Path) -> None:
    broker = _Broker(tmp_path)
    owner = _owner(tmp_path, broker)

    result = await owner.submit(_intent())

    assert result.status == "accepted"
    assert result.order_id == 1
    assert result.perm_id == 9001
    assert broker.calls[0].symbol == "SPY"
    events = read_account_events(tmp_path, ACCOUNT)
    assert [event["event_type"] for event in events][-2:] == [
        "account_owner_submit_prepared",
        "account_owner_submit_accepted",
    ]
    assert events[-1]["diagnostics"]["trace_id"] == "trace-1"
    assert events[-1]["diagnostics"]["broker_client_id"] == 42


@pytest.mark.asyncio
async def test_account_owner_serializes_concurrent_submits(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingBroker(_Broker):
        active = 0

        async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
            self.active += 1
            assert self.active == 1
            entered.set()
            await release.wait()
            ack = await super().place_order(spec)
            self.active -= 1
            return ack

    broker = BlockingBroker()
    owner = _owner(tmp_path, broker)

    first = asyncio.create_task(owner.submit(_intent()))
    await entered.wait()
    second = asyncio.create_task(owner.submit(_intent()))
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert [first_result.order_id, second_result.order_id] == [1, 2]
    assert len(broker.calls) == 2


@pytest.mark.asyncio
async def test_account_owner_rejects_before_broker_when_account_frozen(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id=ACCOUNT,
            reason="watchdog.flatten_failed",
            source="watchdog",
            recorded_at_ms=1,
            operator_next_step="CHECK_IBKR",
        ),
    )

    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(_intent())

    assert exc.value.reason == "ACCOUNT_FROZEN"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_account_owner_rejects_stale_registry_binding(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)

    stale = _intent()
    stale = stale.model_copy(update={"run_id": "run-stale"})
    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(stale)

    assert exc.value.reason == "ACCOUNT_REGISTRY_STALE_RUN"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_account_owner_rejects_generation_mismatch(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)

    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(_intent(generation=GENERATION + 1))

    assert exc.value.reason == "OWNER_GENERATION_MISMATCH"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_account_owner_reconnect_rejects_new_intents_before_reconnect(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)
    observed_rejected = False

    async def reconnect(_client_id: int) -> None:
        nonlocal observed_rejected
        with pytest.raises(AccountOwnerSubmitRejected) as exc:
            await owner.submit(_intent())
        observed_rejected = exc.value.reason == "ACCOUNT_OWNER_RECONNECTING"

    await owner.handle_reconnect(
        reconnect=reconnect,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: _continue_decision(),
        client_id_range=(10, 11),
    )

    assert observed_rejected is True
    assert owner.accepting is True


@pytest.mark.asyncio
async def test_account_owner_reconnect_drains_prepared_intent_as_uncertain(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)
    intent = _intent()
    await owner.record_prepared_for_test(intent)

    await owner.handle_reconnect(
        reconnect=lambda _client_id: None,
        classify_inflight=lambda _event: "uncertain",
        reconcile=lambda: _continue_decision(),
        client_id_range=(10,),
    )

    events = read_account_events(tmp_path, ACCOUNT)
    assert events[-1]["event_type"] == "account_owner_reconnect_resumed"
    assert any(event["event_type"] == "account_owner_reconnect_drain_uncertain" for event in events)


@pytest.mark.asyncio
async def test_account_owner_reconnect_rotates_on_client_id_in_use(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)
    attempts: list[int] = []
    backoffs: list[int] = []

    async def reconnect(client_id: int) -> None:
        attempts.append(client_id)
        if len(attempts) == 1:
            raise ClientIdInUseError(client_id)

    await owner.handle_reconnect(
        reconnect=reconnect,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: _continue_decision(),
        client_id_range=(10, 11),
        backoff=lambda attempt: backoffs.append(attempt),
    )

    assert attempts == [10, 11]
    assert backoffs == [1]


@pytest.mark.asyncio
async def test_account_owner_reconnect_advances_generation_and_rejects_stale_intent(tmp_path: Path) -> None:
    broker = _Broker()
    generation = {"value": GENERATION}

    def provider() -> int:
        return generation["value"]

    def advancer() -> int:
        generation["value"] += 1
        return generation["value"]

    write_account_instance_binding(tmp_path, _binding())
    owner = AccountOwner(
        artifacts_root=tmp_path,
        account_id=ACCOUNT,
        broker=broker,
        owner_generation_provider=provider,
        owner_generation_advancer=advancer,
        classifier=lambda _intent: _continue_decision(),
    )

    stale = _intent(generation=GENERATION)
    await owner.handle_reconnect(
        reconnect=lambda _client_id: None,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: _continue_decision(),
        client_id_range=(10,),
    )

    loaded = read_account_owner_generation(tmp_path, ACCOUNT)
    assert loaded is not None
    assert loaded.generation == GENERATION + 1
    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(stale)
    assert exc.value.reason == "OWNER_GENERATION_MISMATCH"


@pytest.mark.asyncio
async def test_account_owner_reconnect_rotates_on_production_client_id_error(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)
    attempts: list[int] = []

    async def reconnect(client_id: int) -> None:
        attempts.append(client_id)
        if len(attempts) == 1:
            raise IbkrClientIdInUseError("client id already in use")

    await owner.handle_reconnect(
        reconnect=reconnect,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: _continue_decision(),
        client_id_range=(10, 11),
    )

    assert attempts == [10, 11]


@pytest.mark.asyncio
async def test_account_owner_reconnect_blocks_until_reconcile_passes(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)

    await owner.handle_reconnect(
        reconnect=lambda _client_id: None,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: AccountClassifierDecision(
            outcome="freeze",
            reason="BROKER_STATE_UNPROVABLE",
            account_id=ACCOUNT,
            decided_at_ms=1,
        ),
        client_id_range=(10,),
    )

    assert owner.accepting is False
    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(_intent())
    assert exc.value.reason == "ACCOUNT_OWNER_RECONNECTING"


@pytest.mark.asyncio
async def test_account_owner_reconnect_phase_projects_gate_result(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(tmp_path, broker)

    await owner.handle_reconnect(
        reconnect=lambda _client_id: None,
        classify_inflight=lambda _event: "accepted",
        reconcile=lambda: AccountClassifierDecision(
            outcome="freeze",
            reason="BROKER_STATE_UNPROVABLE",
            account_id=ACCOUNT,
            decided_at_ms=1,
        ),
        client_id_range=(10,),
    )

    gate = owner.reconnect_gate_result()
    assert gate.gate_id == "account_owner.reconnect"
    assert gate.status == "freeze"
    assert gate.operator_reason == "frozen"


@pytest.mark.asyncio
async def test_account_owner_rejects_classifier_freeze(tmp_path: Path) -> None:
    broker = _Broker()
    owner = _owner(
        tmp_path,
        broker,
        classifier=lambda _intent: AccountClassifierDecision(
            outcome="freeze",
            reason="BROKER_STATE_UNPROVABLE",
            account_id=ACCOUNT,
            decided_at_ms=1,
        ),
    )

    with pytest.raises(AccountOwnerSubmitRejected) as exc:
        await owner.submit(_intent())

    assert exc.value.reason == "BROKER_STATE_UNPROVABLE"
    assert broker.calls == []
