"""No ENTER may stay working at the broker once its run is no longer ACTIVE (#2362).

Regression tests for #2347 (a crashed run never cancels its working ENTERs),
#2358 (Stop during an in-flight ENTER POST reports ``STOPPED_FLAT`` and sends
no cancel) and #2361 (Stop colliding with a held operation claim cancels
nothing and never retries). The cure for all three is one durable step of the
account reconciliation sweep that cancels every working ENTRY whose run is no
longer ACTIVE, plus a custody proof that counts an ENTER whose POST has not
folded yet.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite import runtime as runtime_module
from app.broker.alpaca.clerk.sqlite.exit import accept_recovery_exit
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.services.bot_carryover import prove_stop_outcome
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding, _Broker, _order


class _OpenOrdersBroker(_Broker):
    """Alpaca lists submitted orders as open; the exact lookup can be parked.

    A parked lookup stands in for a slow ``GET /v2/orders:by_client_order_id``
    inside the 15 s sweep, which holds the ENTER's operation claim.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open: dict[str, BrokerOrder] = {}
        self.park_lookup = False
        self.lookup_started = asyncio.Event()
        self.release_lookup = asyncio.Event()

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        order = await super().submit(leg, client_order_id=client_order_id)
        self.open[client_order_id] = order
        return order

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
        return await super().get_order_by_client_order_id(client_order_id)


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


async def _working_enter(facade: SqliteAlpacaClerkFacade) -> str:
    binding = _binding()
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


async def _stop(facade: SqliteAlpacaClerkFacade) -> None:
    binding = _binding()
    await facade.stop_strategy_run(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        reason="operator_stop",
    )


async def test_sweep_cancels_working_enter_of_a_run_that_ended_without_stop_proof(
    tmp_path: Path,
) -> None:
    """#2347: a crash commits RUN_STOPPED only; the next sweep cancels the ENTER."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)
    assert repo.order(order_ref).broker_state == "accepted"
    # The crash path (finalize_crash) commits STOP and never calls prove_stop_outcome.
    await _stop(facade)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_sweep_never_cancels_a_working_enter_of_the_active_run(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.stopped_run_entries import entries_owed_a_cancel

    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    order_ref = await _working_enter(facade)

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == []
    assert repo.order(order_ref).broker_state == "accepted"
    assert await asyncio.to_thread(entries_owed_a_cancel, repo) == []
    repo.close()


async def test_enter_with_an_active_exit_is_left_to_that_exit(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.stopped_run_entries import entries_owed_a_cancel

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


async def test_stop_during_inflight_enter_post_is_not_flat_and_sweep_cancels_after_post(
    tmp_path: Path,
) -> None:
    """#2358: the POST is in flight, the order row has no broker state yet."""
    broker = _OpenOrdersBroker()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = asyncio.create_task(
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
    order_ref = broker.submissions[0]
    await _stop(facade)
    bot_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task

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


async def test_stop_colliding_with_the_sweeps_claim_is_cancelled_by_a_later_sweep(
    tmp_path: Path,
) -> None:
    """#2361 variant A: the sweep's exact lookup holds the ENTER's claim."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    binding = _binding()
    order_ref = await _working_enter(facade)

    broker.park_lookup = True
    sweep = asyncio.create_task(facade.reconcile_account(trigger="AUTOMATIC"))
    await broker.lookup_started.wait()
    await _stop(facade)
    stop_task = asyncio.create_task(
        prove_stop_outcome(
            binding, clerk=facade, checkpoint_path=tmp_path / "cp.json", now_ms=lambda: 1
        )
    )
    await asyncio.sleep(0.2)
    broker.park_lookup = False
    broker.release_lookup.set()
    outcome = await stop_task
    await sweep

    assert outcome == "STOPPED_CUSTODY_UNPROVABLE"

    await facade.reconcile_account(trigger="AUTOMATIC")

    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_stop_colliding_with_the_enter_post_claim_is_cancelled_by_a_later_sweep(
    tmp_path: Path,
) -> None:
    """#2361 variant B: a ws ``new`` frame marks the order working mid-POST.

    A sweep that hits the POST's claim defers (it must not raise); the first
    sweep after the POST folds cancels the order.
    """
    broker = _OpenOrdersBroker()
    repo, facade = _facade(tmp_path, broker)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=facade.intake, reconciler=facade)
    binding = _binding()
    await facade.register_strategy_run(binding)
    bot_task = asyncio.create_task(
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
    bot_task.cancel()
    with suppress(asyncio.CancelledError):
        await bot_task

    outcome = await prove_stop_outcome(
        binding, clerk=facade, checkpoint_path=tmp_path / "cp.json", now_ms=lambda: 1
    )
    assert outcome == "STOPPED_CUSTODY_UNPROVABLE"

    # The POST still holds the claim: the sweep defers the cancel, never raises.
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


async def test_stop_cancel_attempts_every_entry_when_one_is_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2361: one claim collision no longer abandons the rest of the cancel set.

    Stop's ``cancel_working_entries_for_instance`` runs this same loop; two
    bots give two working ENTRYs without a second ENTER being refused.
    """
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo, facade = _facade(tmp_path, broker)
    first = await _working_enter(facade)
    other = _binding().model_copy(update={"strategy_instance_id": "spy-bot-2"})
    await facade.register_strategy_run(other)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=other.strategy_instance_id,
        run_id=other.run_id,
        decision_id="decision-1",
        purpose=EffectPurpose.ENTER,
        action_plan=other.action_plan,
        quantity=other.quantity,
    )
    second = receipt.child_order_refs[0]
    real_cancel = runtime_module.cancel_and_prove_owned_entry

    async def _first_is_claimed(
        repo_arg: ClerkSqliteRepository, *, entry_order_ref: str, trade: Any, **kwargs: Any
    ) -> Any:
        if entry_order_ref == first:
            raise OperationClaimError("claimed by a live owner")
        return await real_cancel(repo_arg, entry_order_ref=entry_order_ref, trade=trade, **kwargs)

    monkeypatch.setattr(runtime_module, "cancel_and_prove_owned_entry", _first_is_claimed)

    with pytest.raises(OperationClaimError):
        await facade.cancel_verified_working_orders(
            strategy_instance_id=None, order_refs=(first, second)
        )

    assert broker.cancellations == [f"broker-{second}"]
    assert repo.order(second).broker_state == "canceled"
    repo.close()
