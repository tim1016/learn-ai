"""Direction-1 done-when: crash-with-exposure -> refuse-resume -> flatten
(via the presented recovery action) -> resume-to-flat, entirely under SQLite
Clerk custody with fake broker ports (PRD #1752).

Test-only proof: it pins the whole exposure-lifecycle guarantee with one
executable chain. If any link regresses, this fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock, SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

ACCOUNT_ID = "PA-WALK"
SID = "walk-bot"
RUN_ID = "run-1"


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 10}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order(
    client_order_id: str,
    *,
    order_id: str = "broker-order-1",
    symbol: str = "SPY",
    status: str = "accepted",
    side: str = "buy",
    quantity: float = 10.0,
    filled_quantity: float = 0.0,
    filled_avg_price: float | None = None,
) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=quantity,
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


def _position(symbol: str, *, quantity: float, side: str = "long") -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca",
        symbol=symbol,
        asset_id=None,
        asset_class="us_equity",
        quantity=abs(quantity),
        side=side,
        average_entry_price=100.0,
        market_value=100.0 * abs(quantity),
        cost_basis=100.0 * abs(quantity),
        current_price=100.0,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
        observed_at_ms=1_700_000_000_500,
    )


class _FakeTrade:
    def __init__(self) -> None:
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        return _broker_order(client_order_id, side=leg.side).model_copy(
            update={"order_id": f"bo-{client_order_id}"}
        )

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        return _broker_order(client_order_id).model_copy(update={"order_id": f"bo-{client_order_id}"})


class _FakeRead:
    def __init__(
        self,
        *,
        orders: list[BrokerOrder] | None = None,
        positions: list[BrokerPosition] | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        return self._positions


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str) -> Any:
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


async def _held_position(repo: ClerkSqliteRepository) -> str:
    """Filled 10-share SPY entry with an exact execution slice -> attributed +10."""
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=10),
        trade=_FakeTrade(),
    )
    assert submission.order_ref is not None
    filled = _broker_order(
        submission.order_ref, status="filled", quantity=10.0,
        filled_quantity=10, filled_avg_price=100.0,
    )
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=filled)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600,
            price=100, quantity=10, execution_id="exec-1",
        ),
        event_key="execution:exec-1",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return submission.order_ref


async def test_crashed_exposure_walks_to_flat_and_readmits_resume(tmp_path: Path) -> None:
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    await _held_position(repo)  # filled entry, attributed +10

    # 1. Crash analog: the same durable STOP runtime.recover() commits on restart.
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="service_restart_recovery",
    )

    # 2. Refuse-resume: fresh exposure is refused while custody holds the position.
    refused = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert refused.allowed is False
    assert refused.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"

    # 3. Operator reconciles; the panel presents execute_safe_flatten.
    broker_read = _FakeRead(positions=[_position("SPY", quantity=10.0)])
    flatten_trade = _FakeTrade()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker_read, trade=flatten_trade)
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    async def current_context():
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert context is not None
        return context

    catalog = {
        item.action_id: item for item in build_recovery_catalog(await current_context())
    }
    capability = catalog["execute_safe_flatten"]
    assert capability.available and capability.mutation

    # 4. Execute through the same dispatcher the panel uses.
    result = await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten",
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason="walkthrough",
        ),
        current_context=current_context,
    )
    assert result.applied is True
    assert len(result.orders) == 1
    reducing_ref = result.orders[0].order_ref

    # 5. The reducing fill arrives (websocket analog) and the sweep proves flat.
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    filled_reducing = _broker_order(
        reducing_ref, side="sell", status="filled", quantity=10.0,
        filled_quantity=10, filled_avg_price=101.0,
    )
    await sink.record_lifecycle_event(
        client_order_id=reducing_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=101.0, quantity=10, execution_id="walk-exec-2",
        ),
        event_key="execution:walk-exec-2",
        order=filled_reducing,
        recovery_source=None,
        recovery_window_limit=None,
    )
    await reconcile_account(
        repo, read=_FakeRead(positions=[]), trade=_FakeTrade(), trigger="AUTOMATIC"
    )
    attributed = repo.attributed_positions_for_strategy(SID)
    assert not any(position_quantity_is_nonzero(qty) for qty in attributed.values())

    # 6. Resume-to-flat: fresh exposure is admissible again.
    readmitted = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert readmitted.allowed is True

    repo.close()
