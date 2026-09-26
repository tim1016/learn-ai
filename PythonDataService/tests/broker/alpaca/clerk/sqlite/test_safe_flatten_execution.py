"""Executor-side acceptance for the prepared SafeFlattenPlan (F18)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import (
    UNPRICEABLE_RECOVERY,
    ConfirmedRecoveryLimit,
    ConfirmedRecoveryShape,
    ExtendedLimitProposal,
    RecoveryPricing,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import priced_reduction_reference_price
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projection_models import SafeFlattenPlan, SafeFlattenPlanLeg
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock, SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import (
    SafeFlattenExecutionError,
    execute_safe_flatten_plan,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.errors import BrokerOrderRejected, BrokerUnavailable
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
    OrderSide,
    OrderType,
    TimeInForce,
)
from app.schemas.market_liveness import TopOfBookQuote
from app.services.broker_v2_panel.sqlite_panel_adapter import _recent_fill_view
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at, _walk_clock_to

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

    def __init__(self, *, submit_error: Exception | None = None) -> None:
        self._submit_error = submit_error
        self.submit_calls: list[str] = []
        self.submitted_legs: list[BrokerOrderLeg] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        self.submitted_legs.append(leg)
        if self._submit_error is not None:
            raise self._submit_error
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


async def _late_entry_slice(
    repo: ClerkSqliteRepository, entry_ref: str, *, quantity: int
) -> None:
    """One more execution slice on an entry the Clerk already proved filled.

    The race a confirmed price must survive: the operator reviewed the
    reduction the Clerk could see, and the broker reports another slice of
    the same entry before cancellation resolves.
    """
    filled = _broker_order(
        entry_ref, status="filled", quantity=10.0,
        filled_quantity=10 + quantity, filled_avg_price=100.0,
    )
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=entry_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=100, quantity=quantity, execution_id=f"exec-late-{quantity}",
        ),
        event_key=f"execution:exec-late-{quantity}",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )


@pytest.fixture
def crashed_with_exposure(tmp_path: Path):
    """F18 shape: filled entry, attributed +10, run stopped (crash analog).

    Inside the regular session, where an unshaped flatten reduces market DAY.
    """
    clock = _clock_at(FIXTURE_RTH_MS)
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
        pricing=UNPRICEABLE_RECOVERY,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock
    )
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

    result = await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID, pricing=UNPRICEABLE_RECOVERY
    )

    assert len(result.orders) == 1
    assert len(result.accepted_effect_operation_ids) == 1
    assert len(trade.submit_calls) == 1
    reducing = repo.order(result.orders[0].order_ref)
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
            pricing=UNPRICEABLE_RECOVERY,
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
            pricing=UNPRICEABLE_RECOVERY,
        )

    assert trade.submit_calls == []


async def test_execute_safe_flatten_presented_for_stopped_bot_with_exposure(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock
    )
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}

    execute = catalog["execute_safe_flatten"]
    assert execute.available, execute.unavailable_reason
    assert execute.mutation is True
    assert execute.confirmation is not None
    assert execute.reduction_plan is not None
    assert [leg.symbol for leg in execute.reduction_plan.legs] == ["SPY"]


def _unfoldable_open_order() -> BrokerOrder:
    """A resting multi-leg parent (``side`` null) on a symbol no bot holds (#2363)."""
    return _broker_order(
        "alpaca-console:mleg-1", order_id="mleg-open-1", symbol="MSFT", status="new"
    ).model_copy(update={"side": "None"})


def _flatten_catalog(repo: ClerkSqliteRepository) -> dict[str, Any]:
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock
    )
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    assert context is not None
    return {item.action_id: item for item in build_recovery_catalog(context)}


async def test_reconcile_now_with_only_a_contained_unfoldable_order_offers_safe_flatten(
    crashed_with_exposure,
) -> None:
    """#2363 review: "Reconcile now" must not re-block the flatten it recommends.

    Before the fix a contained unfoldable order still made the verdict
    ``unexplained_order``, so the operator receipt was ``STILL_UNKNOWN`` and
    the flatten refused with CLEAN_RECONCILIATION_REQUIRED while positions
    matched exactly and the contained order was the only residual fact.
    """
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )

    result = await reconcile_account(
        repo,
        read=_FakeRead(
            orders=[_unfoldable_open_order()], positions=[_position("SPY", quantity=10.0)]
        ),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )

    assert result.verdict == "clean" and result.foreign_order_count == 1
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER_HOLD") is None
    assert {row["reason_code"] for row in repo.active_uncertainties()} == {
        "UNFOLDABLE_BROKER_ORDER"
    }
    catalog = _flatten_catalog(repo)
    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert catalog[action_id].available, catalog[action_id].unavailable_reason


async def test_contained_unfoldable_order_keeps_position_drift_protection(
    crashed_with_exposure,
) -> None:
    """Containment removes the order from the *unexplained* verdict only: a
    broker position that disagrees with custody still judges as drift."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)

    result = await reconcile_account(
        repo,
        read=_FakeRead(
            orders=[_unfoldable_open_order()], positions=[_position("SPY", quantity=12.0)]
        ),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )

    assert result.verdict == "position_drift"
    assert result.drifted_symbols == ("SPY",)


async def test_safe_flatten_refuses_a_reconciliation_that_predates_an_unfoldable_order(
    crashed_with_exposure,
) -> None:
    """#2363 review: the flatten must not reuse broker truth older than the order.

    The trade-update stream raises the unfoldable episode after the last
    successful reconciliation; before the fix the admitted episode was simply
    ignored and the stale reconciliation authorized the flatten.
    """
    repo, clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    await _reconciled_flatten_plan(repo)
    _walk_clock_to(repo, clock.value + 5_000)
    await SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    ).record_lifecycle_event(
        client_order_id="alpaca-console:mleg-1",
        event=BrokerOrderEvent(
            event_type="new", occurred_at_ms=clock.value, price=None, quantity=None
        ),
        event_key="mleg-open-1|new",
        order=_unfoldable_open_order(),
        recovery_source=None,
        recovery_window_limit=None,
    )

    stale = _flatten_catalog(repo)
    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert stale[action_id].available is False, action_id
        assert stale[action_id].unavailable_reason_code == "UNCERTAINTY_EVIDENCE_NOT_RECONCILED"

    await reconcile_account(
        repo,
        read=_FakeRead(
            orders=[_unfoldable_open_order()], positions=[_position("SPY", quantity=10.0)]
        ),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )
    assert _flatten_catalog(repo)["prepare_safe_flatten"].available


async def test_execute_safe_flatten_blocked_while_a_run_is_active(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)  # run still ACTIVE - no stop
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock
    )
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}

    execute = catalog["execute_safe_flatten"]
    assert execute.available is False
    assert execute.unavailable_reason_code == "RUN_STILL_ACTIVE"


async def test_execute_recovery_action_dispatches_safe_flatten(
    crashed_with_exposure,
) -> None:
    """The generic recovery dispatcher drives execute_safe_flatten end-to-end
    through the facade, exactly as the panel does."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    async def current_context() -> RecoveryPolicyContext:
        reader = SqliteClerkProjectionReader.from_repository(
            repo, clock=repo.clock
        )
        try:
            context = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert context is not None
        return context

    catalog = {item.action_id: item for item in build_recovery_catalog(await current_context())}
    capability = catalog["execute_safe_flatten"]
    assert capability.available, capability.unavailable_reason

    result = await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten",
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason="test-dispatch",
        ),
        current_context=current_context,
    )

    assert result.applied is True
    assert len(result.orders) == 1
    reducing = repo.order(result.orders[0].order_ref)
    assert reducing is not None and reducing.role == "REDUCING"


# ── Codex review 2026-08-25: per-leg outcome + scope regressions ─────────────


async def test_execute_safe_flatten_plan_raises_when_broker_rejects_reduction(
    crashed_with_exposure,
) -> None:
    """P1 (Codex): a broker-rejected reduction folds the EXIT to `failed` but
    the order row (and its ref) still exists. The executor must read the
    terminal state, never report success while exposure remains."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    trade = _FakeTrade(submit_error=BrokerOrderRejected("insufficient buying power"))

    with pytest.raises(SafeFlattenExecutionError, match="rejected"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID, pricing=UNPRICEABLE_RECOVERY
        )


async def test_execute_safe_flatten_plan_defers_pending_on_transient_admission_refusal(
    crashed_with_exposure,
) -> None:
    """P1 (Codex): a transient admission refusal (stale broker snapshot) leaves
    the EXIT durably accepted with no reducing order for the sweep to re-drive.
    That is a committed, pending reduction — never an effect-free failure."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    # An account-wide stale-snapshot episode lands after the plan was prepared:
    # resolve_exit's REDUCE admission is refused transiently, so the sweep
    # re-drives later.
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="Broker account truth is unavailable",
        explanation="test: snapshot read failed",
        operator_impact="New exposure and unproven reduction are paused account-wide.",
        next_step="Reconcile now after broker connectivity is restored.",
        evidence_refs=(),
        cause_facts={"snapshot": "open_orders_and_positions"},
        severity="error",
    )
    trade = _FakeTrade()

    result = await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID, pricing=UNPRICEABLE_RECOVERY
    )

    assert result.orders == ()  # nothing at the broker yet
    assert len(result.accepted_effect_operation_ids) == 1  # durably captured, sweep re-drives
    assert trade.submit_calls == []


async def test_execute_safe_flatten_plan_refuses_manual_custody_leg(
    crashed_with_exposure,
) -> None:
    """P1 (Codex): the executor cannot reduce a manual (NULL-strategy) leg;
    it must refuse before any broker contact rather than fail mid-flatten."""
    repo, _clock = crashed_with_exposure
    manual_plan = SafeFlattenPlan(
        version_token="vt-manual",
        account_id=ACCOUNT_ID,
        authority_generation=1,
        db_identity_token="db-token",
        control_revision=1,
        scope="ACCOUNT_CLERK",
        strategy_instance_id=None,
        reconciliation_id="reconciliation:1",
        prepared_at_ms=1_700_000_000_000,
        expires_at_ms=1_700_000_999_999,
        legs=(
            SafeFlattenPlanLeg(
                strategy_instance_id="",  # manual custody has no strategy owner
                symbol="SPY",
                side="sell",
                quantity=10.0,
                position_updated_at_ms=1_700_000_000_000,
            ),
        ),
    )
    trade = _FakeTrade()

    with pytest.raises(SafeFlattenExecutionError, match="manual-custody"):
        await execute_safe_flatten_plan(
            repo, plan=manual_plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID, pricing=UNPRICEABLE_RECOVERY
        )
    assert trade.submit_calls == []


async def test_execute_safe_flatten_unavailable_for_account_scope(
    crashed_with_exposure,
) -> None:
    """P1 (Codex): account-scoped recovery must not advertise execute_safe_flatten
    (it can span strategies and manual custody the executor cannot reduce)."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock
    )
    try:
        account_context = reader.recovery_context(strategy_instance_id=None)
    finally:
        reader.close()
    assert account_context is not None
    catalog = {item.action_id: item for item in build_recovery_catalog(account_context)}

    execute = catalog["execute_safe_flatten"]
    assert execute.available is False
    assert execute.unavailable_reason_code == "RECOVERY_SCOPE_UNSUPPORTED"


# ── #2007: an extended-hours flatten is the operator's confirmed limit ────────

_XH_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_XH_POLICY = ProgramLegPolicy(
    window=_XH_WINDOW,
    allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
)
_PRE_MARKET_MS = 1_700_136_000_000  # 2023-11-16 07:00 ET
_OVERNIGHT_MS = 1_700_100_000_000  # 2023-11-15 21:00 ET
# 2023-11-15 19:59:50 ET: the latest a confirmed limit is still accepted — a
# send is judged at now plus the 5 s guard band (#2440 review), 19:59:55.
_JUST_BEFORE_POST_CLOSE_MS = 1_700_096_390_000
_JUST_AFTER_POST_CLOSE_MS = 1_700_096_410_000  # 2023-11-15 20:00:10 ET


def _live_quote(now_ms: int) -> TopOfBookQuote:
    return TopOfBookQuote(
        symbol="SPY", bid=100.00, ask=100.05, source="ibkr.market_data.status",
        observed_at_ms=now_ms,
    )


def _confirmed_limit_shape(
    side: OrderSide = OrderSide.SELL, *, quantity: float = 10.0
) -> ConfirmedRecoveryShape:
    return ConfirmedRecoveryShape(
        shape=LegShape(
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=99.95,
            extended_hours=True,
            side=side,
        ),
        # Still inside its session at the fixture's clock.
        valid_until_ms=FIXTURE_RTH_MS + 3_600_000,
        reference_quote=_live_quote(FIXTURE_RTH_MS),
        # The ``_held_position`` fixture attributes exactly ten shares.
        quantity=quantity,
    )


async def test_execute_safe_flatten_plan_reduces_with_the_confirmed_extended_limit(
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

    await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID,
        confirmed_shape=_confirmed_limit_shape(),
        pricing=UNPRICEABLE_RECOVERY,
    )

    ((leg,),) = (trade.submitted_legs,)
    assert (leg.side, leg.quantity, leg.order_type, leg.limit_price, leg.extended_hours) == (
        OrderSide.SELL, 10, OrderType.LIMIT, 99.95, True,
    )


async def test_the_flatten_drives_its_exit_with_the_pricing_seam_it_is_handed(
    crashed_with_exposure,
) -> None:
    """#2440 review: the operator's flatten names the authority's one pricing seam.

    Its inline pass used to drive the EXIT on the degraded default while the
    sweep's next pass of the very same EXIT re-priced it from the live quote —
    one EXIT, two outcomes, chosen by whichever driver reached it first.
    Presented as a market flatten at 15:59:50 and created after the close,
    the inline pass now re-prices it exactly as the sweep would.
    """
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    _walk_clock_to(repo, FIXTURE_RTH_MS + 5 * 3_600_000 + 59 * 60_000 + 50_000)  # 15:59:50 ET
    plan = await _reconciled_flatten_plan(repo)
    _walk_clock_to(repo, FIXTURE_RTH_MS + 6 * 3_600_000 + 5_000)  # 16:00:05 ET
    trade = _FakeTrade()

    result = await execute_safe_flatten_plan(
        repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID,
        pricing=RecoveryPricing(
            policy_source=lambda: _XH_POLICY,
            quote_source=lambda _symbol, now_ms: _live_quote(now_ms),
        ),
    )

    ((leg,),) = (trade.submitted_legs,)
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.LIMIT, TimeInForce.DAY, 99.8, True,
    )
    (reducing,) = result.orders
    assert priced_reduction_reference_price(repo, reducing.order_ref) == 100.00


async def test_execute_safe_flatten_plan_refuses_a_shape_priced_for_the_other_side(
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

    with pytest.raises(SafeFlattenExecutionError, match="priced for"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID,
            confirmed_shape=_confirmed_limit_shape(OrderSide.BUY),
            pricing=UNPRICEABLE_RECOVERY,
        )

    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


async def _stopped_facade_at(
    repo: ClerkSqliteRepository, now_ms: int, *, trade: _FakeTrade | None = None
) -> tuple[SqliteAlpacaClerkFacade, _FakeTrade, Any]:
    """A stopped bot holding +10, reconciled at ``now_ms`` under an extended window.

    The Clerk's live IBKR quote is read at whatever the repository clock says.
    """
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    _walk_clock_to(repo, now_ms)
    trade = trade or _FakeTrade()
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=trade,
        program_leg_policy=_XH_POLICY,
        quote_source=lambda _symbol, quote_now_ms: _live_quote(quote_now_ms),
    )
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")

    async def current_context() -> RecoveryPolicyContext:
        reader = SqliteClerkProjectionReader.from_repository(
            repo, clock=repo.clock
        )
        try:
            context = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert context is not None
        return context

    return facade, trade, current_context


async def _execute(
    facade: SqliteAlpacaClerkFacade,
    current_context: Callable[[], Awaitable[RecoveryPolicyContext]],
    *,
    confirmed_limit: ConfirmedRecoveryLimit | None,
) -> RecoveryExecutionResult:
    capability = {
        item.action_id: item for item in build_recovery_catalog(await current_context())
    }["execute_safe_flatten"]
    assert capability.available, capability.unavailable_reason
    return await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id="execute_safe_flatten",
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason="test-extended-flatten",
            confirmed_limit=confirmed_limit,
        ),
        current_context=current_context,
    )


async def test_a_pre_market_flatten_sends_the_operators_confirmed_limit(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _PRE_MARKET_MS)

    result = await _execute(
        facade,
        current_context,
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=_PRE_MARKET_MS - 3_000
        ),
    )

    assert result.applied is True
    ((leg,),) = (trade.submitted_legs,)
    assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        OrderType.LIMIT, TimeInForce.DAY, 99.95, True,
    )
    # The quote the limit was priced against is durable with the EXIT, so a
    # fill's realized slippage is measured from the bid the Clerk saw at send.
    (reducing,) = result.orders
    assert priced_reduction_reference_price(repo, reducing.order_ref) == 100.00
    fill = FillRecord(
        account_id=ACCOUNT_ID, sid=SID, intent_id="flatten", order_ref=reducing.order_ref,
        event_key="execution:flatten-1", symbol="SPY", side=OrderSide.SELL, quantity=10.0,
        fill_price=99.90, filled_at_ms=_PRE_MARKET_MS + 1_000, fee=None,
    )
    view = _recent_fill_view(fill, authority_account_id=ACCOUNT_ID, repository=repo)
    assert view.slippage_reference_price == 100.00
    assert view.slippage_bps == pytest.approx(10.0, abs=1e-9, rel=0)


async def test_a_limit_past_the_band_is_refused_before_any_exit_is_accepted(
    crashed_with_exposure,
) -> None:
    """Owner decision 2026-09-19: no more than 2 × 20 bps below the 100.00 bid."""
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _PRE_MARKET_MS)

    with pytest.raises(RecoveryExecutionError) as excinfo:
        await _execute(
            facade,
            current_context,
            confirmed_limit=ConfirmedRecoveryLimit(
                limit_price=Decimal("99.59"), quote_observed_at_ms=_PRE_MARKET_MS
            ),
        )

    assert excinfo.value.refusal is not None
    assert excinfo.value.refusal.reason_code == "RECOVERY_LIMIT_OUTSIDE_BAND"
    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


class _LookupOutageTrade(_FakeTrade):
    """Broker lookups fail while ``lookups_fail`` holds, so entry proof defers."""

    def __init__(self) -> None:
        super().__init__()
        self.lookups_fail = True

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        if self.lookups_fail:
            self.lookup_calls.append(client_order_id)
            raise BrokerUnavailable("broker lookups are down")
        return await super().get_order_by_client_order_id(client_order_id)


async def test_a_confirmed_limit_never_goes_out_after_its_session_ends(
    crashed_with_exposure,
) -> None:
    """Backend review of #2007: confirmed at 19:59:50, the reduction deferred by
    an unproven entry, and the next pass at 20:00:10 must send nothing — the
    EXIT fails through EXIT_NOT_FLAT so the entry is free to price again."""
    repo, _clock = crashed_with_exposure
    trade = _LookupOutageTrade()
    facade, trade, current_context = await _stopped_facade_at(
        repo, _JUST_BEFORE_POST_CLOSE_MS, trade=trade
    )

    result = await _execute(
        facade,
        current_context,
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=_JUST_BEFORE_POST_CLOSE_MS
        ),
    )
    assert result.orders == ()
    active = repo.active_exit_for_strategy(SID)
    assert active is not None
    effect_operation_id = active.effect_operation_id

    _walk_clock_to(repo, _JUST_AFTER_POST_CLOSE_MS)
    trade.lookups_fail = False
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY)

    assert trade.submit_calls == []
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    assert repo.active_exit_for_strategy(SID) is None
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=SID,
    ) is not None


async def test_a_confirmed_price_never_reduces_a_quantity_the_operator_never_saw(
    crashed_with_exposure: tuple[ClerkSqliteRepository, Any],
) -> None:
    """Codex review of #2007: a price is confirmed for the reduction the operator
    can see. Cancellation fixes the real quantity several steps later, and a fill
    in between would otherwise send their price for a position they never
    reviewed. The EXIT fails instead, leaving the entry free to price again."""
    repo, _clock = crashed_with_exposure
    trade = _LookupOutageTrade()
    facade, trade, current_context = await _stopped_facade_at(
        repo, _PRE_MARKET_MS, trade=trade
    )
    (entry,) = [
        order for order in repo.entry_orders_for_strategy(SID) if order.role == "ENTRY"
    ]

    result = await _execute(
        facade,
        current_context,
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=_PRE_MARKET_MS
        ),
    )
    assert result.orders == ()
    active = repo.active_exit_for_strategy(SID)
    assert active is not None
    effect_operation_id = active.effect_operation_id

    # Five more shares land before cancellation resolves, so the attributed
    # reduction is fifteen where ten was confirmed.
    await _late_entry_slice(repo, entry.order_ref, quantity=5)
    trade.lookups_fail = False
    await resolve_exit(repo, effect_operation_id=effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY)

    assert trade.submit_calls == []
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state == "failed"
    assert any(
        transition["summary_code"] == "RECOVERY_LIMIT_QUANTITY_CHANGED"
        for transition in repo.transitions_for_order(entry.order_ref)
    )
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=SID,
    )
    assert episode is not None
    # The operator confirmed this price, so the copy addresses them (PR #2230
    # review, blocker 1: the two provenances must not share copy).
    assert episode["headline"] == "The flatten's quantity changed after its price was confirmed"



async def test_a_plan_leg_the_price_was_not_confirmed_for_is_refused(
    crashed_with_exposure: tuple[ClerkSqliteRepository, Any],
) -> None:
    """The plan presents ten shares; a price confirmed for eight is not for it."""
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    plan = await _reconciled_flatten_plan(repo)
    trade = _FakeTrade()

    with pytest.raises(SafeFlattenExecutionError, match="priced for"):
        await execute_safe_flatten_plan(
            repo, plan=plan, trade=trade, intake=ReentrantAsyncLock(), account_id=ACCOUNT_ID,
            confirmed_shape=_confirmed_limit_shape(quantity=8.0),
            pricing=UNPRICEABLE_RECOVERY,
        )

    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


async def test_a_pre_market_flatten_without_a_confirmed_limit_sends_nothing(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _PRE_MARKET_MS)

    with pytest.raises(RecoveryExecutionError, match="limit"):
        await _execute(facade, current_context, confirmed_limit=None)

    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


async def test_an_overnight_flatten_is_refused_rather_than_queued(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _OVERNIGHT_MS)

    with pytest.raises(RecoveryExecutionError, match="No trading session would be open"):
        await _execute(
            facade,
            current_context,
            confirmed_limit=ConfirmedRecoveryLimit(
                limit_price=Decimal("99.95"), quote_observed_at_ms=_OVERNIGHT_MS - 1_000
            ),
        )

    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


async def test_a_flatten_confirmed_within_the_guard_band_of_the_after_hours_close_is_refused(
    crashed_with_exposure,
) -> None:
    """#2440 review: the ticket judges the session where the send does — now plus the guard band.

    Confirmed at 19:59:57, the limit could reach Alpaca after 20:00, when no
    session is open. It is refused before any EXIT is accepted, rather than
    accepted and then folded by the send-time rule a moment later.
    """
    repo, _clock = crashed_with_exposure
    at_ms = 1_700_096_397_000  # 2023-11-15 19:59:57 ET
    facade, trade, current_context = await _stopped_facade_at(repo, at_ms)

    with pytest.raises(RecoveryExecutionError, match="No trading session would be open"):
        await _execute(
            facade,
            current_context,
            confirmed_limit=ConfirmedRecoveryLimit(limit_price=Decimal("99.95"), quote_observed_at_ms=at_ms),
        )

    assert trade.submit_calls == []
    assert repo.active_exit_for_strategy(SID) is None


async def test_a_confirmed_quotes_age_is_measured_now_not_at_the_send_instant(
    crashed_with_exposure,
) -> None:
    """Only the session judgement moves to the send instant (#2440 review).

    A quote 9.5 s old is inside the ten-second freshness rule now, though it
    will be 14.5 s old when the order arrives; the operator's price is sent.
    """
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _PRE_MARKET_MS)

    result = await _execute(
        facade,
        current_context,
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=_PRE_MARKET_MS - 9_500
        ),
    )

    assert result.applied is True
    assert len(trade.submitted_legs) == 1


async def test_the_flatten_ticket_prices_for_the_session_the_send_arrives_in(
    crashed_with_exposure,
) -> None:
    """At 15:59:57 the ticket proposes the after-hours limit the send will require, not a market leg.

    A market leg confirmed then would be refused a moment later — it may
    reach Alpaca after 16:00 — so the proposal is judged at the send instant,
    while the live quote is still read at now.
    """
    repo, _clock = crashed_with_exposure
    at_ms = 1_700_081_997_000  # 2023-11-15 15:59:57 ET
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    _walk_clock_to(repo, at_ms)
    plan = await _reconciled_flatten_plan(repo)
    asked: list[int] = []

    def quote_source(_symbol: str, now_ms: int) -> TopOfBookQuote:
        asked.append(now_ms)
        return _live_quote(now_ms)

    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        program_leg_policy=_XH_POLICY,
        quote_source=quote_source,
    )

    pricing = facade.price_safe_flatten(plan)

    assert isinstance(pricing, ExtendedLimitProposal)
    assert pricing.phase == "POST"
    assert asked == [at_ms]


async def test_the_facade_prices_a_single_leg_flatten_from_the_live_quote(
    crashed_with_exposure,
) -> None:
    repo, _clock = crashed_with_exposure
    await _held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="crash_analog",
    )
    _walk_clock_to(repo, _PRE_MARKET_MS)
    plan = await _reconciled_flatten_plan(repo)
    quote = TopOfBookQuote(
        symbol="SPY", bid=100.00, ask=100.05, source="ibkr.market_data.status",
        observed_at_ms=_PRE_MARKET_MS,
    )
    asked: list[tuple[str, int]] = []

    def quote_source(symbol: str, now_ms: int) -> TopOfBookQuote | None:
        asked.append((symbol, now_ms))
        return quote

    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        program_leg_policy=_XH_POLICY,
        quote_source=quote_source,
    )

    pricing = facade.price_safe_flatten(plan)

    assert asked == [("SPY", _PRE_MARKET_MS)]
    assert isinstance(pricing, ExtendedLimitProposal)
    assert (pricing.phase, pricing.quote, pricing.suggested_limit_price) == (
        "PRE", quote, Decimal("99.80"),
    )


async def test_a_clerk_priced_quantity_change_folds_without_asking_the_operator(
    crashed_with_exposure: tuple[ClerkSqliteRepository, Any],
) -> None:
    """PR #2230 review, blocker 1: a watchdog-priced leg whose attributed
    quantity changes mid-flight folds with copy that asks no operator for
    anything — nobody confirmed the price, and the next automatic re-drive
    prices the new quantity afresh."""
    from dataclasses import replace as _replace

    from app.broker.alpaca.clerk.sqlite.exit import accept_recovery_exit

    repo, _clock = crashed_with_exposure
    trade = _LookupOutageTrade()
    _facade, trade, _current_context = await _stopped_facade_at(
        repo, _PRE_MARKET_MS, trade=trade
    )
    (entry,) = [
        order for order in repo.entry_orders_for_strategy(SID) if order.role == "ENTRY"
    ]

    accepted = accept_recovery_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-redrive-clerkpriced01",
        entry_order_ref=entry.order_ref,
        confirmed_shape=_replace(
            _confirmed_limit_shape(quantity=10),
            priced_by="clerk",
            # The helper's validity is yesterday's session; this leg is priced
            # in this morning's PRE, valid until the 09:30 open.
            valid_until_ms=_PRE_MARKET_MS + 9_000_000,
        ),
    )
    assert accepted.effect_operation_id is not None

    # Five more shares land before cancellation resolves, so the attributed
    # reduction is fifteen where the Clerk priced ten.
    await _late_entry_slice(repo, entry.order_ref, quantity=5)
    trade.lookups_fail = False
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY)

    assert trade.submit_calls == []
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=SID,
    )
    assert episode is not None
    assert episode["headline"] == "The flatten's quantity changed after the Clerk priced it"
    assert "No action needed" in episode["next_step"]


async def test_outside_band_requires_explicit_override_and_records_it(crashed_with_exposure):
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await _stopped_facade_at(repo, _PRE_MARKET_MS)
    result = await _execute(facade, current_context, confirmed_limit=ConfirmedRecoveryLimit(
        limit_price=Decimal('99.59'), quote_observed_at_ms=_PRE_MARKET_MS, band_override=True,
    ))
    assert result.applied is True
    assert trade.submitted_legs[0].limit_price == pytest.approx(99.59, abs=1e-9)
    row = repo.last_strategy_transition(strategy_instance_id=SID, transition_kind='EXIT_ACCEPTED')
    import json
    assert json.loads(row['facts_json'])['band_override'] is True
