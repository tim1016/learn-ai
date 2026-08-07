"""Vertical strategy-to-SQLite runtime custody tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime
from app.broker.alpaca.clerk.models import ChannelHealth, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite import runtime as runtime_module
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan


class _Broker:
    broker_id = "alpaca"

    def __init__(self) -> None:
        self.submit_started = asyncio.Event()
        self.release_submit = asyncio.Event()
        self.submissions: list[str] = []
        self.cancellations: list[str] = []

    async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
        return []

    async def list_positions(self) -> list:
        return []

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
    ) -> BrokerOrder:
        self.submissions.append(client_order_id)
        self.submit_started.set()
        await self.release_submit.wait()
        return _order(client_order_id, leg)

    async def cancel(self, order_id: str) -> None:
        self.cancellations.append(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        order = _order(
            client_order_id,
            BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        )
        if f"broker-{client_order_id}" in self.cancellations:
            return order.model_copy(update={"status": "canceled", "canceled_at_ms": 11, "updated_at_ms": 11})
        return order


def _order(client_order_id: str, leg: BrokerOrderLeg) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"broker-{client_order_id}",
        client_order_id=client_order_id,
        symbol=leg.symbol,
        asset_class="us_equity",
        side=leg.side,
        order_type="market",
        time_in_force="day",
        quantity=leg.quantity,
        filled_quantity=0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="accepted",
        submitted_at_ms=10,
        created_at_ms=10,
        updated_at_ms=10,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=10,
    )


def _binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="spy-bot",
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=1,
    )


def test_durable_decision_encoding_is_injective_across_reserved_prefix() -> None:
    encoded_delimiter_id = runtime_module._durable_decision_id("a:b")

    assert encoded_delimiter_id == "encoded-YTpi"
    assert runtime_module._durable_decision_id(encoded_delimiter_id) != encoded_delimiter_id
    assert runtime_module._durable_decision_id("plain-id") == "plain-id"


async def test_registration_precedes_live_enter_and_caller_cancellation_keeps_custody(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()

    await facade.register_strategy_run(binding)
    task = asyncio.create_task(
        facade.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
    )
    await broker.submit_started.wait()

    effect = repo.effect_operation("effect:spy-bot:decision-1")
    assert effect is not None
    assert effect.state == "accepted"
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    broker.release_submit.set()
    for _ in range(10):
        await asyncio.sleep(0)
        effect = repo.effect_operation("effect:spy-bot:decision-1")
        if effect is not None and effect.state == "in_progress":
            break

    assert len(broker.submissions) == 1
    effect = repo.effect_operation("effect:spy-bot:decision-1")
    assert effect is not None
    assert effect.state == "in_progress"
    assert repo.active_run("spy-bot") is not None
    repo.close()


async def test_runtime_close_drains_shielded_effect_before_closing_repository(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    caller = asyncio.create_task(
        facade.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id="decision-shutdown",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
    )
    await broker.submit_started.wait()
    caller.cancel()
    with suppress(asyncio.CancelledError):
        await caller
    runtime = ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=facade,
        _sqlite_repository=repo,
    )

    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done()

    broker.release_submit.set()
    await closing
    assert runtime.sqlite_repository is None
    assert len(broker.submissions) == 1


async def test_cancel_verified_working_entry_records_intent_before_broker_contact(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    broker.release_submit.set()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-cancel",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    order_ref = receipt.child_order_refs[0]

    orders = await facade.cancel_verified_working_orders(
        strategy_instance_id=binding.strategy_instance_id,
        order_refs=(order_ref,),
    )

    assert orders[0].broker_state == "canceled"
    assert broker.cancellations == [f"broker-{order_ref}"]
    transitions = repo.transitions_for_order(order_ref)
    cancel_index = next(
        index
        for index, transition in enumerate(transitions)
        if transition["transition_kind"] == "ORDER_CANCEL_REQUESTED"
    )
    confirmed_index = next(
        index
        for index, transition in enumerate(transitions)
        if transition["transition_kind"] == "ENTRY_TERMINAL_CONFIRMED"
    )
    assert cancel_index < confirmed_index
    repo.close()


async def test_start_admission_fence_blocks_new_effect_intake(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)

    async with facade.start_admission_snapshot(binding.strategy_instance_id):
        effect = asyncio.create_task(
            facade.execute_for_instance(
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                decision_id="decision-fenced",
                purpose=EffectPurpose.ENTER,
                action_plan=binding.action_plan,
                quantity=binding.quantity,
            )
        )
        await asyncio.sleep(0)
        assert not broker.submit_started.is_set()

    await broker.submit_started.wait()
    broker.release_submit.set()
    await effect
    repo.close()


def _gate(*, market_data_healthy: bool, execution_healthy: bool) -> StreamHealthGate:
    return StreamHealthGate(
        market_data=lambda: ChannelHealth(
            stream="market_data",
            healthy=market_data_healthy,
            reason="" if market_data_healthy else "market_data broke for the test",
            observed_at_ms=1,
        ),
        execution=lambda: ChannelHealth(
            stream="execution",
            healthy=execution_healthy,
            reason="" if execution_healthy else "execution broke for the test",
            observed_at_ms=1,
        ),
    )


async def test_execute_for_instance_enter_fails_closed_when_execution_stream_unhealthy(
    tmp_path: Path,
) -> None:
    """#1396 P1: SQLite ENTER must consult the dual stream-health gate the
    legacy Clerk installs (S4, #1262). Losing `trade_updates` while market
    data keeps flowing must refuse ENTER and record a durable hold instead
    of silently submitting."""
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    broker.release_submit.set()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,
        trade=broker,
        stream_health=_gate(market_data_healthy=True, execution_healthy=False),
    )
    binding = _binding()
    await facade.register_strategy_run(binding)

    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-unhealthy",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )

    assert receipt.state == EffectOperationState.REJECTED
    assert receipt.child_order_refs == ()
    assert broker.submissions == []
    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code=runtime_module.STREAM_HEALTH_REASON_CODE)
    assert hold is not None
    assert "execution" in hold["evidence_refs_json"]
    repo.close()


async def test_execute_for_instance_uses_its_symbols_market_data_health(
    tmp_path: Path,
) -> None:
    """An AAPL stall cannot block a healthy SPY ENTER or authorize AAPL."""
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    broker.release_submit.set()
    gate = StreamHealthGate(
        market_data=lambda: ChannelHealth(
            stream="market_data",
            healthy=False,
            reason="A sibling symbol is stale",
            observed_at_ms=1,
        ),
        execution=lambda: ChannelHealth(
            stream="execution",
            healthy=True,
            reason="",
            observed_at_ms=1,
        ),
        market_data_for_symbol=lambda symbol: ChannelHealth(
            stream="market_data",
            healthy=symbol == "SPY",
            reason="" if symbol == "SPY" else f"{symbol} is stale",
            observed_at_ms=1,
        ),
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,
        trade=broker,
        stream_health=gate,
    )
    binding = _binding()
    await facade.register_strategy_run(binding)

    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-spy-healthy",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )

    assert receipt.state is EffectOperationState.SUBMITTED
    assert len(broker.submissions) == 1

    aapl_binding = binding.model_copy(
        update={
            "strategy_instance_id": "aapl-bot",
            "symbol": "AAPL",
            "action_plan": alpaca_v1_action_plan("AAPL"),
            "run_id": "run-aapl",
        }
    )
    await facade.register_strategy_run(aapl_binding)
    aapl_receipt = await facade.execute_for_instance(
        strategy_instance_id=aapl_binding.strategy_instance_id,
        run_id=aapl_binding.run_id,
        decision_id="decision-aapl-stale",
        purpose=EffectPurpose.ENTER,
        action_plan=aapl_binding.action_plan,
        quantity=aapl_binding.quantity,
    )

    assert aapl_receipt.state is EffectOperationState.REJECTED
    assert len(broker.submissions) == 1
    hold = repo.active_hold(
        scope="ACCOUNT_CLERK",
        reason_code=runtime_module.STREAM_HEALTH_REASON_CODE,
    )
    assert hold is not None
    assert "AAPL is stale" in hold["evidence_refs_json"]
    repo.close()


async def test_execute_for_instance_enter_resumes_and_resolves_hold_once_streams_recover(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    broker.release_submit.set()
    unhealthy_facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,
        trade=broker,
        stream_health=_gate(market_data_healthy=False, execution_healthy=True),
    )
    binding = _binding()
    await unhealthy_facade.register_strategy_run(binding)
    await unhealthy_facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-still-down",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code=runtime_module.STREAM_HEALTH_REASON_CODE) is not None

    recovered_facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,
        trade=broker,
        stream_health=_gate(market_data_healthy=True, execution_healthy=True),
    )
    receipt = await recovered_facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-recovered",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )

    assert receipt.state != EffectOperationState.REJECTED
    assert broker.submissions == [receipt.child_order_refs[0]]
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code=runtime_module.STREAM_HEALTH_REASON_CODE) is None
    repo.close()


async def test_working_order_refs_for_proof_includes_a_live_reducing_order(
    tmp_path: Path,
) -> None:
    """#1396 P1: custody proof must count a live EXIT's still-working
    REDUCING child, not just ENTRY orders. `_working_order_refs` (ENTRY-only,
    the exact STOP cancel-target set) must NOT see it, but the proof-facing
    `_working_order_refs_for_proof` must — otherwise a STOP proof can return
    `clean` with an empty working set while a reducing order is still live.
    """
    from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
    from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
    from tests.broker.alpaca.clerk.sqlite.test_exit import (
        ACCOUNT_ID,
        RUN_ID,
        SID,
        _broker_order,
        _FakeTrade,
        _make_entry,
    )

    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)

    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    # The reducing order reaches the broker and is acked, but is still
    # working (not yet filled) — exactly the state the review comment flags.
    trade = _FakeTrade(submit_result=_broker_order("placeholder", status="accepted", filled_quantity=0.0, side="sell"))
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    assert result.reducing_order_ref is not None
    reducing_order = repo.order(result.reducing_order_ref)
    assert reducing_order is not None and reducing_order.role == "REDUCING"
    assert reducing_order.broker_state == "accepted"

    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)

    assert result.reducing_order_ref not in facade._working_order_refs(SID)
    assert result.reducing_order_ref in facade._working_order_refs_for_proof(SID)
    repo.close()


async def test_custody_snapshot_uses_folds_without_full_transition_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)

    def _reject_full_replay() -> None:
        raise AssertionError("warm custody reads must not replay full transition history")

    monkeypatch.setattr(repo, "custody_transitions", _reject_full_replay)

    snapshot = await facade.custody_snapshot(binding.strategy_instance_id)

    assert snapshot.strategy_instance_id == binding.strategy_instance_id
    assert snapshot.reconciliation_state == "clean"
    repo.close()


async def test_instance_custody_proof_omits_zero_quantity_position_rows(
    tmp_path: Path,
) -> None:
    """A durable zero balance is flat, not carryover exposure."""
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    repo._conn.execute(
        "INSERT INTO positions "
        "(strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
        "VALUES (?, 'SPY', 0.0, 10)",
        (binding.strategy_instance_id,),
    )
    repo._conn.commit()

    proof = await facade.prove_instance_custody(binding.strategy_instance_id)

    assert proof.exposure == {}
    repo.close()


async def test_sqlite_trade_update_redelivery_and_regression_are_idempotent(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
    )
    broker = _Broker()
    broker.release_submit.set()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-stream",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    order_ref = receipt.child_order_refs[0]
    observed = await broker.get_order_by_client_order_id(order_ref)
    assert observed is not None
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo,
        read=broker,
        trade=broker,
        intake=facade.intake,
    )
    event = BrokerOrderEvent(
        event_type="new",
        occurred_at_ms=10,
        price=None,
        quantity=None,
    )
    transitions_before = len(repo.transitions_for_order(order_ref))

    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=event,
        event_key="broker-1|new|10",
        order=observed,
        recovery_source=None,
        recovery_window_limit=None,
    )
    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=event,
        event_key="broker-1|new|10",
        order=observed,
        recovery_source=None,
        recovery_window_limit=None,
    )
    regressed = observed.model_copy(
        update={"status": "new", "updated_at_ms": 9, "observed_at_ms": 11}
    )
    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=event.model_copy(update={"occurred_at_ms": 9}),
        event_key="broker-1|new|9",
        order=regressed,
        recovery_source=None,
        recovery_window_limit=None,
    )

    current = repo.order(order_ref)
    assert current is not None
    assert current.broker_state == "accepted"
    assert len(repo.transitions_for_order(order_ref)) == transitions_before
    repo.close()
