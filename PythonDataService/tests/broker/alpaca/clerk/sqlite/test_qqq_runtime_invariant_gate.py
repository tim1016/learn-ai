"""Deterministic runtime-composition regression for the QQQ intake incident.

The scenario intentionally composes the live SQLite facade, shared intake,
trade-update evidence sink, and reconciliation path.  Its barriers name the
incident ordering without using elapsed time as a correctness oracle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite import reconcile as reconcile_module
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg, BrokerPosition
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan


class _ParkedQqqBroker:
    """A broker double whose barriers expose the required composition seam."""

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.first_exit_lookup_started = asyncio.Event()
        self.two_exit_lookups_started = asyncio.Event()
        self.release_exit_lookups = asyncio.Event()
        self.snapshot_read_started = asyncio.Event()
        self.release_first_snapshot = asyncio.Event()
        self.allow_reconciliation_lookups = False
        self._exit_lookup_count = 0
        self._snapshot_read_count = 0
        self.exit_lookup_client_order_ids: list[str] = []
        self.exact_orders: dict[str, BrokerOrder] = {}
        self.snapshot_orders: list[BrokerOrder] = []
        self.snapshot_position_quantity = 1.0

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        return _order(
            client_order_id,
            symbol=leg.symbol,
            side=leg.side,
            status="accepted",
            filled_quantity=0.0,
            filled_avg_price=None,
        )

    async def cancel(self, order_id: str) -> None:
        del order_id

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if self.allow_reconciliation_lookups:
            return self.exact_orders.get(client_order_id)
        self.exit_lookup_client_order_ids.append(client_order_id)
        self._exit_lookup_count += 1
        if self._exit_lookup_count == 1:
            self.first_exit_lookup_started.set()
        if self._exit_lookup_count == 2:
            self.two_exit_lookups_started.set()
        await self.release_exit_lookups.wait()
        return self.exact_orders.get(client_order_id)

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        self._snapshot_read_count += 1
        if self._snapshot_read_count == 1:
            self.snapshot_read_started.set()
            await self.release_first_snapshot.wait()
        return self.snapshot_orders

    async def list_positions(self) -> list[BrokerPosition]:
        return [_position("QQQ", quantity=self.snapshot_position_quantity)] if self.snapshot_orders else []


@dataclass(frozen=True)
class _PreparedQqqEntry:
    binding: BrokerBotBinding
    client_order_id: str
    filled_order: BrokerOrder


@dataclass
class _QqqRuntime:
    repo: ClerkSqliteRepository
    broker: _ParkedQqqBroker
    facade: SqliteAlpacaClerkFacade
    sink: SqliteTradeUpdateEvidenceSink


def _binding(strategy_instance_id: str, *, run_id: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=strategy_instance_id,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="QQQ",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("QQQ"),
        run_id=run_id,
        created_at_ms=1,
    )


def _order(
    client_order_id: str,
    *,
    symbol: str,
    side: str,
    status: str,
    filled_quantity: float,
    filled_avg_price: float | None,
) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"broker-{client_order_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=filled_quantity,
        limit_price=None,
        stop_price=None,
        filled_avg_price=filled_avg_price,
        status=status,
        submitted_at_ms=1_700_000_000_100,
        created_at_ms=1_700_000_000_100,
        updated_at_ms=1_700_000_000_500,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_500,
    )


def _position(symbol: str, *, quantity: float) -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca",
        symbol=symbol,
        asset_id=None,
        asset_class="us_equity",
        quantity=abs(quantity),
        side="long" if quantity >= 0 else "short",
        average_entry_price=100.0,
        market_value=100.0 * abs(quantity),
        cost_basis=100.0 * abs(quantity),
        current_price=100.0,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
        observed_at_ms=1_700_000_000_500,
    )


async def _enter(
    facade: SqliteAlpacaClerkFacade,
    binding: BrokerBotBinding,
    *,
    decision_id: str,
) -> str:
    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id=decision_id,
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    assert len(receipt.child_order_refs) == 1
    return receipt.child_order_refs[0]


async def _open_qqq_runtime(tmp_path: Path) -> _QqqRuntime:
    """Build the real runtime composition shared by the incident scenarios."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-QQQ", artifacts_root=tmp_path)
    broker = _ParkedQqqBroker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=facade.intake, reconciler=facade)
    return _QqqRuntime(repo=repo, broker=broker, facade=facade, sink=sink)


async def _prepare_qqq_entry(
    runtime: _QqqRuntime,
    *,
    strategy_instance_id: str = "qqq-runtime",
    run_id: str = "run-runtime",
    decision_id: str = "enter-runtime",
) -> _PreparedQqqEntry:
    """Register and enter one order that both evidence paths can observe."""
    binding = _binding(strategy_instance_id, run_id=run_id)
    await runtime.facade.register_strategy_run(binding)
    entry = await _enter(runtime.facade, binding, decision_id=decision_id)
    filled = _order(
        entry,
        symbol="QQQ",
        side="buy",
        status="filled",
        filled_quantity=1.0,
        filled_avg_price=100.0,
    )
    runtime.broker.exact_orders[entry] = filled
    return _PreparedQqqEntry(binding=binding, client_order_id=entry, filled_order=filled)


async def test_qqq_parked_exits_do_not_block_sequential_evidence_or_coverage_supersession(
    tmp_path: Path,
) -> None:
    """The incident's exact-after-cumulative evidence converges without a hold.

    Two EXIT broker conversations are deliberately parked. A reconciliation
    REST snapshot is parked too, then folds one cumulative recovery row. The
    sequential websocket ``pending_new`` and exact-fill frames both progress
    before either EXIT is released; the exact row subsequently supersedes the
    cumulative row in the shared SQLite fold domain.
    """
    runtime = await _open_qqq_runtime(tmp_path)
    repo, broker, facade, sink = runtime.repo, runtime.broker, runtime.facade, runtime.sink
    first = await _prepare_qqq_entry(
        runtime,
        strategy_instance_id="qqq-first",
        run_id="run-first",
        decision_id="enter-first",
    )
    second = await _prepare_qqq_entry(
        runtime,
        strategy_instance_id="qqq-second",
        run_id="run-second",
        decision_id="enter-second",
    )
    first_local_order = repo.order(first.client_order_id)
    second_local_order = repo.order(second.client_order_id)
    assert first_local_order is not None and first_local_order.client_order_id == first.client_order_id
    assert second_local_order is not None and second_local_order.client_order_id == second.client_order_id
    broker.snapshot_orders = [first.filled_order, second.filled_order]
    broker.snapshot_position_quantity = 2.0

    first_exit = asyncio.create_task(
        facade.execute_for_instance(
            strategy_instance_id=first.binding.strategy_instance_id,
            run_id=first.binding.run_id,
            decision_id="exit-first",
            purpose=EffectPurpose.EXIT,
            action_plan=first.binding.action_plan,
            quantity=first.binding.quantity,
        )
    )
    second_exit = asyncio.create_task(
        facade.execute_for_instance(
            strategy_instance_id=second.binding.strategy_instance_id,
            run_id=second.binding.run_id,
            decision_id="exit-second",
            purpose=EffectPurpose.EXIT,
            action_plan=second.binding.action_plan,
            quantity=second.binding.quantity,
        )
    )
    # The event-loop-turn sentinel lets both facade callers reach their
    # broker boundary without turning elapsed time into the liveness oracle.
    await broker.first_exit_lookup_started.wait()
    await asyncio.sleep(0)
    if first_exit.done():
        await first_exit
    if second_exit.done():
        await second_exit
    pending_order = first.filled_order.model_copy(
        update={"status": "pending_new", "filled_quantity": 0.0, "filled_avg_price": None}
    )
    pending_frame = asyncio.create_task(
        sink.record_lifecycle_event(
            client_order_id=first.client_order_id,
            event=BrokerOrderEvent(
                event_type="pending_new",
                occurred_at_ms=1_700_000_000_600,
                price=None,
                quantity=None,
            ),
            event_key="qqq-pending-new",
            order=pending_order,
            recovery_source=None,
            recovery_window_limit=None,
        )
    )
    await asyncio.sleep(0)
    assert pending_frame.done(), "parked broker I/O must not own intake"
    await pending_frame
    await broker.two_exit_lookups_started.wait()
    assert set(broker.exit_lookup_client_order_ids) == {first.client_order_id, second.client_order_id}

    reconciliation = asyncio.create_task(facade.reconcile_account(trigger="AUTOMATIC"))
    await broker.snapshot_read_started.wait()
    assert not first_exit.done()
    assert not second_exit.done()
    assert not reconciliation.done()

    broker.allow_reconciliation_lookups = True
    broker.release_first_snapshot.set()
    reconciliation_result = await reconciliation
    assert reconciliation_result.verdict == "clean"
    assert repo.fills_for_order(first.client_order_id)[0]["evidence_source"] == "cumulative_recovery"

    await sink.record_lifecycle_event(
        client_order_id=first.client_order_id,
        event=BrokerOrderEvent(
            event_type="fill",
            occurred_at_ms=1_700_000_000_700,
            price=100.0,
            quantity=1.0,
            execution_id="qqq-exact-execution",
        ),
        event_key="qqq-exact-execution",
        order=first.filled_order,
        recovery_source=None,
        recovery_window_limit=None,
    )

    effective_fills = repo.fills_for_order(first.client_order_id)
    assert [(fill["execution_id"], fill["evidence_source"]) for fill in effective_fills] == [
        ("qqq-exact-execution", "websocket")
    ]
    assert repo.position(first.binding.strategy_instance_id, "QQQ") == pytest.approx(1.0, abs=1e-9, rel=0)
    assert repo.active_uncertainties_for_admission(strategy_instance_id=first.binding.strategy_instance_id) == []
    assert "EXECUTION_COVERAGE_SUPERSEDED" in {
        transition["transition_kind"] for transition in repo.transitions_for_order(first.client_order_id)
    }

    broker.release_exit_lookups.set()
    await first_exit
    await second_exit
    repo.close()


async def test_qqq_exact_first_makes_later_reconciliation_cumulative_evidence_a_noop(
    tmp_path: Path,
) -> None:
    """An exact websocket slice wins before a later aggregate REST snapshot."""
    runtime = await _open_qqq_runtime(tmp_path)
    entry = await _prepare_qqq_entry(runtime)
    await runtime.sink.record_lifecycle_event(
        client_order_id=entry.client_order_id,
        event=BrokerOrderEvent(
            event_type="fill",
            occurred_at_ms=1_700_000_000_700,
            price=100.0,
            quantity=1.0,
            execution_id="qqq-exact-first",
        ),
        event_key="qqq-exact-first",
        order=entry.filled_order,
        recovery_source=None,
        recovery_window_limit=None,
    )

    runtime.broker.allow_reconciliation_lookups = True
    runtime.broker.snapshot_orders = [entry.filled_order]
    runtime.broker.release_first_snapshot.set()
    result = await runtime.facade.reconcile_account(trigger="AUTOMATIC")

    assert result.verdict == "clean"
    assert [
        (fill["execution_id"], fill["evidence_source"])
        for fill in runtime.repo.fills_for_order(entry.client_order_id)
    ] == [
        ("qqq-exact-first", "websocket")
    ]
    assert runtime.repo.position(entry.binding.strategy_instance_id, "QQQ") == pytest.approx(
        1.0, abs=1e-9, rel=0
    )
    assert runtime.repo.active_uncertainties_for_admission(
        strategy_instance_id=entry.binding.strategy_instance_id
    ) == []
    runtime.repo.close()


async def test_qqq_operator_reconciliation_broker_read_does_not_block_websocket_fold(
    tmp_path: Path,
) -> None:
    """The operator surface shares the short fold domain, not broker I/O."""
    runtime = await _open_qqq_runtime(tmp_path)
    entry = await _prepare_qqq_entry(runtime)
    runtime.broker.snapshot_orders = [entry.filled_order]
    operator_reconciliation = asyncio.create_task(
        runtime.facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    )
    await runtime.broker.snapshot_read_started.wait()

    pending_frame = asyncio.create_task(
        runtime.sink.record_lifecycle_event(
            client_order_id=entry.client_order_id,
            event=BrokerOrderEvent(
                event_type="pending_new",
                occurred_at_ms=1_700_000_000_600,
                price=None,
                quantity=None,
            ),
            event_key="qqq-operator-pending",
            order=entry.filled_order.model_copy(
                update={"status": "pending_new", "filled_quantity": 0.0, "filled_avg_price": None}
            ),
            recovery_source=None,
            recovery_window_limit=None,
        )
    )
    await asyncio.sleep(0)
    assert pending_frame.done(), "operator broker I/O must not own intake"
    await pending_frame

    runtime.broker.allow_reconciliation_lookups = True
    runtime.broker.release_first_snapshot.set()
    result = await operator_reconciliation

    assert result.verdict == "clean"
    assert result.receipt_id is not None
    runtime.repo.close()


async def test_qqq_large_snapshot_releases_the_fold_domain_between_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second snapshot unit cannot monopolize the evidence lane."""
    runtime = await _open_qqq_runtime(tmp_path)
    first = await _prepare_qqq_entry(
        runtime,
        strategy_instance_id="qqq-fair-first",
        run_id="run-fair-first",
        decision_id="enter-fair-first",
    )
    second = await _prepare_qqq_entry(
        runtime,
        strategy_instance_id="qqq-fair-second",
        run_id="run-fair-second",
        decision_id="enter-fair-second",
    )
    runtime.broker.snapshot_orders = [first.filled_order, second.filled_order]
    runtime.broker.snapshot_position_quantity = 2.0
    runtime.broker.allow_reconciliation_lookups = True
    runtime.broker.release_first_snapshot.set()
    checkpoint_entered = asyncio.Event()
    release_checkpoint = asyncio.Event()
    original_sleep = asyncio.sleep
    checkpoint_count = 0

    async def hold_first_cooperative_checkpoint(delay: float, result: object | None = None) -> None:
        nonlocal checkpoint_count
        if delay == 0 and checkpoint_count == 0:
            checkpoint_count += 1
            checkpoint_entered.set()
            await release_checkpoint.wait()
        await original_sleep(delay, result=result)

    monkeypatch.setattr(reconcile_module.asyncio, "sleep", hold_first_cooperative_checkpoint)
    reconciliation = asyncio.create_task(runtime.facade.reconcile_account(trigger="AUTOMATIC"))
    await checkpoint_entered.wait()

    await runtime.sink.record_lifecycle_event(
        client_order_id=second.client_order_id,
        event=BrokerOrderEvent(
            event_type="pending_new",
            occurred_at_ms=1_700_000_000_600,
            price=None,
            quantity=None,
        ),
        event_key="qqq-fair-pending",
        order=second.filled_order.model_copy(
            update={"status": "pending_new", "filled_quantity": 0.0, "filled_avg_price": None}
        ),
        recovery_source=None,
        recovery_window_limit=None,
    )
    release_checkpoint.set()
    result = await reconciliation

    assert result.verdict == "clean"
    assert checkpoint_count == 1
    runtime.repo.close()
