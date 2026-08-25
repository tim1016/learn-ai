"""Executor-side acceptance for the prepared SafeFlattenPlan (F18)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    execute_safe_flatten_plan,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

ACCOUNT_ID = "PA-FLATTEN"
SID = "crashed-bot"
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
    """A minimal ``BrokerTradePort`` double — submit + lookup, configurable."""

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


@pytest.fixture
def crashed_with_exposure(tmp_path: Path):
    """F18 shape: filled entry, attributed +10, run stopped (crash analog)."""
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield repo, clock
    repo.close()


async def _reconciled_flatten_plan(repo: ClerkSqliteRepository):
    """Operator flow: Reconcile now -> presented plan (production path)."""
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    assert context is not None
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}
    prepare = catalog["prepare_safe_flatten"]
    assert prepare.available, prepare.unavailable_reason
    assert prepare.reduction_plan is not None
    return prepare.reduction_plan


async def test_execute_safe_flatten_plan_reduces_attributed_exposure_exactly(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    trade = _FakeTrade()

    orders = await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID
    )

    assert len(orders) == 1
    assert len(trade.submit_calls) == 1
    reducing = repo.order(orders[0].order_ref)
    assert reducing is not None and reducing.role == "REDUCING"


async def test_execute_safe_flatten_plan_refuses_expired_plans(
    crashed_with_exposure,
) -> None:
    repo, clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    clock.advance(plan.expires_at_ms - clock.value + 1)

    with pytest.raises(SafeFlattenExecutionError, match="expired"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=_FakeTrade(), intake=ReentrantAsyncLock(),
            account_id=ACCOUNT_ID,
        )


async def test_execute_safe_flatten_plan_refuses_when_a_resume_landed_after_recheck(
    crashed_with_exposure,
) -> None:
    """P0 race regression: policy checks no-active-run at presentation/recheck,
    but execution happens later. A Resume landing before EXIT capture must fail
    closed inside the capture transaction, never submit a reduction."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    # Resume analog lands between recheck and capture (approved-carryover
    # resumes are legitimate while custody holds exposure).
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2"
    )
    trade = _FakeTrade()

    with pytest.raises(SafeFlattenExecutionError, match="re-activated"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(),
            account_id=ACCOUNT_ID,
        )

    assert trade.submit_calls == []
