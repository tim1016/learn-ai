"""No ENTER may stay working at the broker once its run is no longer ACTIVE (#2362).

Regression tests for #2347 (a crashed run never cancels its working ENTERs),
#2358 (Stop during an in-flight ENTER POST reports ``STOPPED_FLAT`` and sends
no cancel) and #2361 (Stop colliding with a held operation claim cancels
nothing and never retries). The cure for all three is one durable step of the
account reconciliation pass that cancels every working ENTRY whose run is no
longer ACTIVE -- operator Stop reaches it through the reconciliation inside
its custody proof -- plus one "unresolved" definition that counts an ENTER
whose POST has not folded yet.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite.exit import accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.manual_orders import submit_manual_order
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.stopped_run_entries import entries_owed_a_cancel
from app.broker.alpaca.clerk.sqlite.uncertainty import ORDER_OUTCOME_UNKNOWN_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
)
from app.services.bot_carryover import prove_stop_outcome, read_checkpoint
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding, _Broker, _order


class _OpenOrdersBroker(_Broker):
    """Alpaca's view of submitted orders: listed while open, looked up exactly.

    The exact lookup can be parked -- a slow ``GET
    /v2/orders:by_client_order_id`` inside the sweep, which holds the ENTER's
    operation claim -- and one cancel can fail with ``BrokerUnavailable``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open: dict[str, BrokerOrder] = {}
        self.park_lookup = False
        self.lookup_started = asyncio.Event()
        self.release_lookup = asyncio.Event()
        self.cancel_unavailable_once = False

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        order = await super().submit(leg, client_order_id=client_order_id)
        self.open[client_order_id] = order
        return order

    async def cancel(self, order_id: str) -> None:
        if self.cancel_unavailable_once:
            self.cancel_unavailable_once = False
            raise BrokerUnavailable("synthetic cancel response lost", broker="alpaca")
        await super().cancel(order_id)

    async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
        return [
            order
            for coid, order in self.open.items()
            if f"broker-{coid}" not in self.cancellations
        ]

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if self.park_lookup:
            self.lookup_started.set()
            await self.release_lookup.wait()
        order = self.open.get(client_order_id)
        if order is None:
            return await super().get_order_by_client_order_id(client_order_id)
        if f"broker-{client_order_id}" in self.cancellations:
            return order.model_copy(
                update={"status": "canceled", "canceled_at_ms": 30, "updated_at_ms": 30}
            )
        return order


def _facade(tmp_path: Path, broker: _Broker) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade]:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    return repo, facade


def _claim_held(repo: ClerkSqliteRepository, order_ref: str) -> bool:
    order = repo.order(order_ref)
    assert order is not None
    row = repo._conn.execute(
        "SELECT claim_token FROM effect_operations WHERE effect_operation_id = ?",
        (order.effect_operation_id,),
    ).fetchone()
    return row[0] is not None


async def _working_enter(facade: SqliteAlpacaClerkFacade, *, quantity: int = 1) -> str:
    binding = _binding().model_copy(update={"quantity": quantity})
    await facade.register_strategy_run(binding)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-1",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    return receipt.child_order_refs[0]


def _start_enter(facade: SqliteAlpacaClerkFacade) -> asyncio.Task:
    binding = _binding()
    return asyncio.create_task(
        facade.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
    )


async def _stop(facade: SqliteAlpacaClerkFacade) -> None:
    binding = _binding()
    await facade.stop_strategy_run(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        reason="operator_stop",
    )


async def _stop_task(bot_task: asyncio.Task) -> None:
    bot_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task


async def test_sweep_cancels_working_enter_of_a_run_that_ended_without_stop_proof(
    tmp_path: Path,
) -> None:
    """#2347: a crash commits RUN_STOPPED only; the next sweep cancels the ENTER."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    assert repo.order(order_ref).broker_state == "accepted"
    # The crash path (finalize_crash) commits STOP and never proves custody.
    await _stop(facade)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_sweep_never_cancels_a_working_enter_of_the_active_run(tmp_path: Path) -> None:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == []
    assert repo.order(order_ref).broker_state == "accepted"
    assert entries_owed_a_cancel(repo) == []
    repo.close()


async def test_enter_with_an_active_exit_is_left_to_that_exit(tmp_path: Path) -> None:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    await _stop(facade)
    assert [order.order_ref for order in entries_owed_a_cancel(repo)] == [order_ref]

    accept_recovery_exit(
        repo,
        account_id="PA-TEST",
        strategy_instance_id=_binding().strategy_instance_id,
        decision_id="recovery-flatten-0123456789abcdef",
        entry_order_ref=order_ref,
    )

    assert entries_owed_a_cancel(repo) == []
    repo.close()


async def test_sweep_never_cancels_a_working_manual_order(tmp_path: Path) -> None:
    """Manual custody belongs to no run, so no run's end can owe it a cancel."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    submitted = await submit_manual_order(
        repo,
        account_id="PA-TEST",
        operator_id="operator-1",
        ticket_id="ticket-1",
        leg_id="leg-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        trade=broker,
    )
    manual_ref = submitted.leg.order_ref
    assert repo.order(manual_ref).broker_state == "accepted"

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == []
    assert repo.order(manual_ref).broker_state == "accepted"
    assert entries_owed_a_cancel(repo) == []
    repo.close()


async def test_stopped_run_partial_fill_cancels_only_the_remainder(tmp_path: Path) -> None:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=facade.intake, reconciler=facade)
    order_ref = await _working_enter(facade, quantity=2)
    partial = broker.open[order_ref].model_copy(
        update={
            "status": "partially_filled",
            "filled_quantity": 1,
            "filled_avg_price": 500.0,
            "updated_at_ms": 20,
            "observed_at_ms": 20,
        }
    )
    broker.open[order_ref] = partial
    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=BrokerOrderEvent(
            event_type="partial_fill",
            occurred_at_ms=20,
            price=500.0,
            quantity=1,
            execution_id="exec-1",
        ),
        event_key=f"broker-{order_ref}|partial_fill|20",
        order=partial,
        recovery_source=None,
        recovery_window_limit=None,
    )
    assert repo.attributed_positions_for_strategy("spy-bot") == {"SPY": 1.0}
    await _stop(facade)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    assert repo.attributed_positions_for_strategy("spy-bot") == {"SPY": 1.0}
    assert broker.submissions == [order_ref]  # no reducing sell
    repo.close()


async def test_unavailable_cancel_is_uncertain_then_a_later_pass_cancels(tmp_path: Path) -> None:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    await _stop(facade)
    broker.cancel_unavailable_once = True

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.has_order_transition(order_ref=order_ref, transition_kind="ORDER_CANCEL_UNCERTAIN")
    assert repo.order(order_ref).broker_state == "accepted"
    assert broker.cancellations == []

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    assert (
        repo.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
            strategy_instance_id="spy-bot",
        )
        is None
    )
    repo.close()


async def test_stop_during_inflight_enter_post_is_not_flat_and_sweep_cancels_after_post(
    tmp_path: Path,
) -> None:
    """#2358: the POST is in flight, the order row has no broker state yet."""
    broker = _OpenOrdersBroker()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = _start_enter(facade)
    await broker.submit_started.wait()
    order_ref = broker.submissions[0]
    await _stop(facade)
    await _stop_task(bot_task)

    outcome = await prove_stop_outcome(
        binding, clerk=facade, checkpoint_path=tmp_path / "cp.json", now_ms=lambda: 1
    )

    assert outcome == "STOPPED_CUSTODY_UNPROVABLE"
    assert broker.cancellations == []

    broker.release_submit.set()
    for _ in range(200):
        if repo.order(order_ref).broker_state is not None:
            break
        await asyncio.sleep(0.01)
    assert repo.order(order_ref).broker_state == "accepted"

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_custody_snapshot_counts_an_inflight_enter_post_as_pending(
    tmp_path: Path,
) -> None:
    """#2358: the snapshot and the STOP proof share one "unresolved" definition."""
    broker = _OpenOrdersBroker()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = _start_enter(facade)
    await broker.submit_started.wait()
    order_ref = broker.submissions[0]
    await _stop(facade)
    await _stop_task(bot_task)

    snapshot = await facade.custody_snapshot(binding.strategy_instance_id)
    proof = await facade.prove_instance_custody(binding.strategy_instance_id)

    assert snapshot.pending_orders.state == "non_zero"
    assert snapshot.pending_orders.count == 1
    assert snapshot.unresolved_effects.count == 1
    assert proof.unresolved_intent_refs == (order_ref,)
    broker.release_submit.set()
    repo.close()


async def test_stop_racing_the_sweeps_cancel_proves_flat_and_writes_checkpoint(
    tmp_path: Path,
) -> None:
    """The sweep's cancel is parked while Stop proves: Stop waits, never loses a claim race."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    order_ref = await _working_enter(facade)
    await _stop(facade)
    broker.release_cancel.clear()
    sweep = asyncio.create_task(facade.reconcile_account(trigger="AUTOMATIC"))
    await broker.cancel_started.wait()
    checkpoint_path = tmp_path / "cp.json"
    stop_task = asyncio.create_task(
        prove_stop_outcome(binding, clerk=facade, checkpoint_path=checkpoint_path, now_ms=lambda: 1)
    )
    await asyncio.sleep(0.2)
    assert not stop_task.done()

    broker.release_cancel.set()
    outcome = await stop_task
    await sweep

    assert outcome == "STOPPED_FLAT"
    checkpoint = read_checkpoint(checkpoint_path)
    assert checkpoint is not None
    assert checkpoint.outcome == "STOPPED_FLAT"
    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_stop_while_the_sweeps_lookup_holds_the_claim_proves_flat(
    tmp_path: Path,
) -> None:
    """#2361 variant A: the sweep's exact lookup holds the ENTER's claim at Stop."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    order_ref = await _working_enter(facade)

    broker.park_lookup = True
    sweep = asyncio.create_task(facade.reconcile_account(trigger="AUTOMATIC"))
    await broker.lookup_started.wait()
    await _stop(facade)
    checkpoint_path = tmp_path / "cp.json"
    stop_task = asyncio.create_task(
        prove_stop_outcome(binding, clerk=facade, checkpoint_path=checkpoint_path, now_ms=lambda: 1)
    )
    await asyncio.sleep(0.2)
    broker.park_lookup = False
    broker.release_lookup.set()
    outcome = await stop_task
    await sweep

    assert outcome == "STOPPED_FLAT"
    assert read_checkpoint(checkpoint_path) is not None
    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_stop_colliding_with_the_enter_post_claim_is_cancelled_by_a_later_sweep(
    tmp_path: Path,
) -> None:
    """#2361 variant B: a ws ``new`` frame marks the order working mid-POST.

    The proof's own pass and a sweep both hit the POST's claim: each defers
    (neither raises), Stop reports UNPROVABLE, and the first sweep after the
    POST folds cancels the order.
    """
    broker = _OpenOrdersBroker()
    repo, facade = _facade(tmp_path, broker)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=facade.intake, reconciler=facade)
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = _start_enter(facade)
    await broker.submit_started.wait()
    order_ref = broker.submissions[0]
    observed = _order(order_ref, BrokerOrderLeg(symbol="SPY", side="buy", quantity=1))
    broker.open[order_ref] = observed
    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=BrokerOrderEvent(event_type="new", occurred_at_ms=10, price=None, quantity=None),
        event_key=f"broker-{order_ref}|new|10",
        order=observed,
        recovery_source=None,
        recovery_window_limit=None,
    )
    assert repo.order(order_ref).broker_state == "accepted"
    await _stop(facade)
    await _stop_task(bot_task)

    outcome = await prove_stop_outcome(
        binding, clerk=facade, checkpoint_path=tmp_path / "cp.json", now_ms=lambda: 1
    )
    assert outcome == "STOPPED_CUSTODY_UNPROVABLE"

    await facade.reconcile_account(trigger="AUTOMATIC")
    assert broker.cancellations == []

    broker.release_submit.set()
    for _ in range(200):
        if not _claim_held(repo, order_ref):
            break
        await asyncio.sleep(0.01)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()
