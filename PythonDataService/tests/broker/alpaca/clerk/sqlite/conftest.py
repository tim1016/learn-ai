"""Shared deterministic helpers for SQLite Account Clerk tests."""

from __future__ import annotations

from typing import Any

from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)


class _TestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ms: int) -> None:
        self.value += delta_ms


def _clock_at(start_ms: int) -> _TestClock:
    return _TestClock(start_ms)


def _hold_transition(
    *,
    reason_code: str = "UNEXPLAINED_ORDER_HOLD",
    evidence_refs: list[str] | None = None,
) -> TransitionInput:
    """One pre-v12 ``ACCOUNT_HOLD_RAISED`` transition.

    The kind is retired as a *writer* (ADR 0048 Decision 2) but is still
    replayed from any mirror recorded before v12, so this helper now builds
    the legacy shape deliberately: it is how the replay folds are exercised.
    """
    facts = AccountHoldRaisedFacts(
        reason_code=reason_code,
        evidence_refs=evidence_refs or ["bo-1"],
    )
    return TransitionInput(
        transition_kind="ACCOUNT_HOLD_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code="ACCOUNT_HOLD_RAISED",
        facts_json=facts.to_facts_json(),
    )


# ── Shared broker doubles + factories (reused across the SQLite Clerk suite) ──
# Per AGENTS.md "don't duplicate utility functions": new test modules import
# these from here rather than copying per-file. The fake trade port is honest
# about the submitted leg (echoes side AND quantity), so an exact-close proof
# cannot pass on a wrong-sized reduction.


def _broker_leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 10}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order_fixture(
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


def _broker_position_fixture(
    symbol: str, *, quantity: float, side: str = "long"
) -> BrokerPosition:
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


class _FakeTradePort:
    """A minimal, honest ``BrokerTradePort`` double.

    ``submit`` echoes the submitted leg's side *and* quantity, so a caller that
    regresses to an under- or over-sized reduction cannot pass an exact-close
    assertion. ``submitted_legs`` records each submitted leg for that check.
    ``lookup_absent`` models a broker that definitively has no order for the
    exact client order id, which is how a never-accepted submit reads.
    """

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._submit_error = submit_error
        self._lookup_absent = lookup_absent
        self.submit_calls: list[str] = []
        self.submitted_legs: list[BrokerOrderLeg] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        self.submitted_legs.append(leg)
        if self._submit_error is not None:
            raise self._submit_error
        return _broker_order_fixture(
            client_order_id, side=leg.side, quantity=leg.quantity
        ).model_copy(update={"order_id": f"bo-{client_order_id}"})

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_absent:
            # Definitive absence: the broker has no order for this exact
            # client order id (Alpaca answers the read-only lookup with 404).
            return None
        return _broker_order_fixture(client_order_id).model_copy(
            update={"order_id": f"bo-{client_order_id}"}
        )


class _FakeReadPort:
    """A minimal ``BrokerReadPort`` double — only list_orders/list_positions."""

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


class _AssertingNoReconciler:
    async def reconcile_account(self, *, trigger: str) -> Any:
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


async def _make_held_position(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    run_id: str,
    decision_id: str = "enter-1",
    execution_id: str = "exec-1",
    quantity: float = 10.0,
) -> str:
    """Filled entry with an exact execution slice -> attributed +quantity."""
    submission = await submit_enter(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=_broker_leg(quantity=quantity),
        trade=_FakeTradePort(),
    )
    assert submission.order_ref is not None
    filled = _broker_order_fixture(
        submission.order_ref, status="filled", quantity=quantity,
        filled_quantity=quantity, filled_avg_price=100.0,
    )
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=filled)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600,
            price=100, quantity=quantity, execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return submission.order_ref
