"""Automatic reconciliation and UNKNOWN resolution tests (#1378).

Covers every acceptance criterion on the issue: automatic UNKNOWN
resolution with no new command, an unexplained/foreign order raising an
``ACCOUNT_CLERK`` hold, "Reconcile now" creating no second intent,
idempotent/non-regressing folding of duplicate and out-of-order broker
events, in-flight-order drift suppression, and a truthful stale verdict on
broker unreachability.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

import app.broker.alpaca.clerk.sqlite.exit_watchdog as watchdog_module
from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    GuardedBrokerReadPort,
    GuardedBrokerTradePort,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.external_orders import (
    InvalidExternalOrderCursor,
    SqliteExternalOrderReader,
    acknowledge_external_order,
    observe_external_order,
)
from app.broker.alpaca.clerk.sqlite.folds import (
    POSITION_QTY_EPSILON,
    position_quantity_is_nonzero,
)
from app.broker.alpaca.clerk.sqlite.manual_orders import submit_manual_order
from app.broker.alpaca.clerk.sqlite.models import CommittedTransition, TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
    _invariant_failure_outcome,
    plan_account_reconciliation,
    reconcile_account,
)
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    AdmissionBlockedError,
    admit_new_exposure,
    raise_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    ExitNotFlatCause,
    ExitStuckCause,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _hold_transition

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"
APPROVED_POSITION_QTY_EPSILON = 0.000000001


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (0.0, False),
        (0.0000000005, False),
        (-0.0000000005, False),
        (0.000000001, True),
        (-0.000000001, True),
    ],
)
def test_position_quantity_boundary_is_unambiguous(
    quantity: float,
    expected: bool,
) -> None:
    assert POSITION_QTY_EPSILON == APPROVED_POSITION_QTY_EPSILON
    assert position_quantity_is_nonzero(quantity) is expected


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(1_700_000_000_000)
    # A long lease TTL decouples the execution lease from the R4 30s grace
    # window under test — several tests advance the clock past 30s to prove
    # grace-elapsed behavior, which must not also expire the lease itself
    # (matches test_enter.py's own fix for the identical coupling).
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 1}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order(
    client_order_id: str,
    *,
    order_id: str = "broker-order-1",
    symbol: str = "SPY",
    status: str = "accepted",
    side: str = "buy",
    quantity: float = 1.0,
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

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        lookup_result: BrokerOrder | None = None,
        lookup_error: Exception | None = None,
        lookup_absent: bool = False,
        cancel_error: Exception | None = None,
    ) -> None:
        self._submit_error = submit_error
        self._lookup_result = lookup_result
        self._lookup_error = lookup_error
        self._lookup_absent = lookup_absent
        self._cancel_error = cancel_error
        self.submit_calls: list[str] = []
        self.lookup_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        if self._submit_error is not None:
            raise self._submit_error
        return _broker_order(client_order_id).model_copy(update={"order_id": f"bo-{client_order_id}"})

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self._cancel_error is not None:
            raise self._cancel_error

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_absent:
            return None
        if self._lookup_result is not None:
            return self._lookup_result
        return _broker_order(client_order_id).model_copy(update={"order_id": f"bo-{client_order_id}"})


class _FakeRead:
    """A minimal ``BrokerReadPort`` double — only ``list_orders``/``list_positions``
    are called by ``reconcile_account``."""

    def __init__(
        self,
        *,
        orders: list[BrokerOrder] | None = None,
        positions: list[BrokerPosition] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._error = error

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        if self._error is not None:
            raise self._error
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        if self._error is not None:
            raise self._error
        return self._positions


class _SequentialRead(_FakeRead):
    def __init__(
        self,
        *,
        order_snapshots: list[list[BrokerOrder]],
        position_snapshots: list[list[BrokerPosition]],
    ) -> None:
        super().__init__()
        self._order_snapshots = list(order_snapshots)
        self._position_snapshots = list(position_snapshots)

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        if not self._order_snapshots:
            raise AssertionError("order snapshot queue exhausted")
        return self._order_snapshots.pop(0)

    async def list_positions(self) -> list[BrokerPosition]:
        if not self._position_snapshots:
            raise AssertionError("position snapshot queue exhausted")
        return self._position_snapshots.pop(0)


class _FailingFinalSnapshotRead(_FakeRead):
    def __init__(self) -> None:
        super().__init__()
        self._order_reads = 0

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        self._order_reads += 1
        if self._order_reads == 2:
            raise RuntimeError("unexpected final-snapshot failure")
        return []


def _remove_captured_order(
    repo: ClerkSqliteRepository,
    *,
    effect_operation_id: str,
) -> None:
    """Surgically corrupt one real accepted effect for invariant recovery tests."""
    with repo._write_lock:
        repo._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            repo._conn.execute(
                "DELETE FROM operation_order_links WHERE effect_operation_id = ?",
                (effect_operation_id,),
            )
            repo._conn.execute(
                "DELETE FROM orders WHERE effect_operation_id = ?",
                (effect_operation_id,),
            )
            repo._conn.commit()
        finally:
            repo._conn.execute("PRAGMA foreign_keys = ON")


async def _make_uncertain_order(
    repo: ClerkSqliteRepository,
    *,
    decision_id: str = "d1",
    strategy_instance_id: str = SID,
    lifecycle_run_id: str = RUN_ID,
) -> str:
    """Drive a real ENTER through a lost submit response so the effect
    operation lands (and stays) in ``unknown`` — grace has not elapsed."""
    submit_trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"), lookup_absent=True)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=lifecycle_run_id,
        leg=_leg(),
        trade=submit_trade,
    )
    assert submission.order_ref is not None
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    return submission.order_ref


# ── plan_account_reconciliation (pure) ──────────────────────────────────────


def _namespaces() -> frozenset[str]:
    from app.engine.live.order_identity import build_bot_order_namespace

    return frozenset({build_bot_order_namespace(SID)})


def _our_order_ref(intent_id: str = "abc") -> str:
    from app.engine.live.order_identity import build_bot_order_namespace, build_order_ref

    return build_order_ref(build_bot_order_namespace(SID), intent_id)


def test_plan_is_clean_when_no_foreign_orders_and_positions_match() -> None:
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[],
        broker_positions=[_position("SPY", quantity=5)],
        attributed_positions={"SPY": 5.0},
    )
    assert plan.verdict == "clean"


def test_plan_flags_unexplained_order_for_a_foreign_client_order_id() -> None:
    foreign = _broker_order("manual/someone/v1:xyz")
    plan = plan_account_reconciliation(
        namespaces=_namespaces(), broker_orders=[foreign], broker_positions=[], attributed_positions={}
    )
    assert plan.verdict == "unexplained_order"
    assert plan.foreign_orders == (foreign,)


def test_plan_treats_an_order_with_no_client_order_id_as_foreign() -> None:
    manual = _broker_order("placeholder").model_copy(update={"client_order_id": None})
    plan = plan_account_reconciliation(
        namespaces=_namespaces(), broker_orders=[manual], broker_positions=[], attributed_positions={}
    )
    assert plan.verdict == "unexplained_order"


@pytest.mark.parametrize(
    ("effect_kind", "expected"),
    [
        ("ENTER", "STILL_UNKNOWN"),
        ("MANUAL_ORDER", "STILL_UNKNOWN"),
        ("CANCEL", "STILL_UNKNOWN"),
        ("EXIT", "RESOLVED_FAILURE"),
    ],
)
def test_invariant_failures_have_exhaustive_recovery_policy(
    effect_kind: str,
    expected: str,
) -> None:
    assert _invariant_failure_outcome(effect_kind) == expected


def test_plan_treats_namespace_shaped_but_uncaptured_order_as_foreign() -> None:
    broker_only = _broker_order(_our_order_ref("never-captured"))
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[broker_only],
        broker_positions=[],
        attributed_positions={},
        known_order_refs=frozenset(),
    )
    assert plan.verdict == "unexplained_order"
    assert plan.foreign_orders == (broker_only,)


def test_plan_flags_position_drift_when_broker_and_attributed_disagree() -> None:
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[],
        broker_positions=[_position("SPY", quantity=5)],
        attributed_positions={"SPY": 3.0},
    )
    assert plan.verdict == "position_drift"
    assert plan.drifted_symbols == ("SPY",)


def test_plan_marks_indeterminate_for_a_symbol_with_a_non_terminal_in_flight_order() -> None:
    """#1378: a symbol with a non-terminal in-flight order is not flagged as
    a *confirmed* drift — the fill/ack for it just hasn't landed yet.

    #1655: it is still not proven equal, so it must not be reported "clean"
    either — that would admit new exposure while custody is indeterminate.
    """
    working_order = _broker_order(_our_order_ref(), status="partially_filled")
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[working_order],
        broker_positions=[_position("SPY", quantity=5)],
        attributed_positions={"SPY": 3.0},
    )
    assert plan.verdict == "position_drift"
    assert plan.drifted_symbols == ()
    assert plan.indeterminate_symbols == ("SPY",)


def test_plan_flags_position_drift_verdict_when_drift_and_indeterminate_both_present() -> None:
    """A confirmed drift on one symbol and an indeterminate mismatch on
    another must both surface — neither category may hide the other."""
    working_order = _broker_order(_our_order_ref(), symbol="QQQ", status="accepted")
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[working_order],
        broker_positions=[_position("SPY", quantity=5), _position("QQQ", quantity=2)],
        attributed_positions={"SPY": 3.0, "QQQ": 0.0},
    )
    assert plan.verdict == "position_drift"
    assert plan.drifted_symbols == ("SPY",)
    assert plan.indeterminate_symbols == ("QQQ",)


def test_plan_prioritizes_unexplained_order_over_position_drift() -> None:
    foreign = _broker_order("manual/someone/v1:xyz")
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[foreign],
        broker_positions=[_position("SPY", quantity=5)],
        attributed_positions={"SPY": 3.0},
    )
    assert plan.verdict == "unexplained_order"


def test_plan_drift_tolerance_ignores_float_residue_within_epsilon() -> None:
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[],
        broker_positions=[_position("SPY", quantity=3.0 + 4e-13)],
        attributed_positions={"SPY": 3.0},
    )
    assert plan.verdict == "clean"


def test_plan_drift_uses_canonical_exact_epsilon_boundary() -> None:
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[],
        broker_positions=[_position("SPY", quantity=APPROVED_POSITION_QTY_EPSILON)],
        attributed_positions={},
    )

    assert plan.verdict == "position_drift"
    assert plan.drifted_symbols == ("SPY",)


# ── account reconciliation recovers UNKNOWN effects ──────────────────────────


async def _reconcile_unknown_effect(
    repo: ClerkSqliteRepository,
    *,
    trade: _FakeTrade,
    trigger: Literal["AUTOMATIC", "OPERATOR_RECONCILE_NOW"] = "AUTOMATIC",
) -> AccountReconciliationResult:
    """Exercise recovery only through the public account-level reconciler."""
    return await reconcile_account(repo, read=_FakeRead(), trade=trade, trigger=trigger)


async def test_account_reconciliation_resolves_unknown_effect_to_success(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    result = await _reconcile_unknown_effect(repo, trade=_FakeTrade())
    assert result.resolved_count == 1
    order = repo.order(order_ref)
    assert order is not None and order.broker_order_id is not None


async def test_account_reconciliation_resolves_unknown_effect_to_failure_past_grace(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    repo.clock.advance(30_001)  # type: ignore[attr-defined]
    result = await _reconcile_unknown_effect(repo, trade=_FakeTrade(lookup_absent=True))
    assert result.resolved_count == 1
    effect = repo.effect_operation(repo.order(order_ref).effect_operation_id)  # type: ignore[union-attr]
    assert effect is not None and effect.state == "failed"


async def test_account_reconciliation_leaves_unknown_effect_within_grace(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    result = await _reconcile_unknown_effect(repo, trade=_FakeTrade(lookup_absent=True))
    assert result.resolved_count == 0
    effect = repo.effect_operation(repo.order(order_ref).effect_operation_id)  # type: ignore[union-attr]
    assert effect is not None and effect.state == "unknown"


async def test_account_reconciliation_leaves_unknown_effect_on_broker_lookup_error(
    repo: ClerkSqliteRepository,
) -> None:
    """Never fabricate a terminal outcome on a broker error (#1378 acceptance,
    order-level slice of the account-wide 'truthful stale verdict' rule)."""
    await _make_uncertain_order(repo)
    result = await _reconcile_unknown_effect(
        repo, trade=_FakeTrade(lookup_error=BrokerUnavailable("down"))
    )
    assert result.resolved_count == 0


async def test_accepted_enter_lookup_failure_blocks_admission_until_terminal_resolution(
    repo: ClerkSqliteRepository,
) -> None:
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-crash-before-contact",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    assert repo.effect_operation(accepted.effect_operation_id).state == "accepted"  # type: ignore[union-attr]

    result = await _reconcile_unknown_effect(
        repo,
        trade=_FakeTrade(lookup_error=BrokerUnavailable("exact lookup unavailable")),
    )

    assert result.resolved_count == 0
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    attempts = [
        transition
        for transition in repo.transitions_for_order(accepted.order_ref)
        if transition["transition_kind"] == "RECONCILIATION_ATTEMPTED"
    ]
    assert len(attempts) == 1
    assert '"outcome":"STILL_UNKNOWN"' in attempts[0]["facts_json"]
    with pytest.raises(AdmissionBlockedError) as exc_info:
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-must-wait-for-proof",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )
    assert exc_info.value.decision.reason_code == "ORDER_OUTCOME_UNKNOWN"

    repo.clock.advance(30_001)  # type: ignore[attr-defined]
    resolved = await _reconcile_unknown_effect(
        repo,
        trade=_FakeTrade(lookup_absent=True),
    )

    assert resolved.resolved_count == 1
    terminal = repo.effect_operation(accepted.effect_operation_id)
    assert terminal is not None and terminal.state == "failed"
    assert accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-after-terminal-proof",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    ).created


async def test_reconcile_now_refreshes_resolved_order_without_second_intent(
    repo: ClerkSqliteRepository,
) -> None:
    """'Reconcile now' on an operation that already finished creates no
    second intent (#1378 acceptance)."""
    await _make_uncertain_order(repo)
    await _reconcile_unknown_effect(repo, trade=_FakeTrade())
    before = len(repo.custody_transitions())

    trade = _FakeTrade()
    result = await _reconcile_unknown_effect(repo, trigger="OPERATOR_RECONCILE_NOW", trade=trade)
    assert result.verdict == "clean"
    assert trade.submit_calls == []
    # Account reconciliation records the final pass and its operator receipt;
    # neither record creates another command or broker effect.
    assert len(repo.custody_transitions()) == before + 2


async def test_account_reconciliation_records_unknown_effect_attempt(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    await _reconcile_unknown_effect(repo, trigger="OPERATOR_RECONCILE_NOW", trade=_FakeTrade())
    transitions = repo.transitions_for_order(order_ref)
    reconciliation_rows = [t for t in transitions if t["transition_kind"] == "RECONCILIATION_ATTEMPTED"]
    assert len(reconciliation_rows) == 1


async def test_account_reconciliation_delegates_an_exit_owned_entry_to_resolve_exit(
    repo: ClerkSqliteRepository,
) -> None:
    """An entry order linked to an EXIT and stuck in cancel-uncertainty
    must NOT be resolved via the ENTER-style 'already has a broker_order_id'
    short-circuit — that field was set by the original ENTER submission long
    before EXIT began and proves nothing about whether EXIT's cancel
    resolved. Before this dispatch existed, reconciliation
    short-circuited on it immediately, reporting a false RESOLVED_SUCCESS
    with zero broker calls and zero audit trail, while the EXIT effect
    silently stayed unknown forever."""
    submit_trade = _FakeTrade()
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=submit_trade,
    )
    entry_ref = submission.order_ref
    assert entry_ref is not None

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    # Put the EXIT into cancel-uncertainty, exactly like a lost cancel
    # response in production.
    await resolve_exit(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        trade=_FakeTrade(cancel_error=BrokerUnavailable("timeout")),
    )
    effect_stuck = repo.effect_operation(accepted.effect_operation_id)
    assert effect_stuck is not None and effect_stuck.state == "unknown"

    resolving_trade = _FakeTrade(lookup_result=_broker_order(entry_ref, status="canceled", filled_quantity=0.0))
    result = await _reconcile_unknown_effect(repo, trade=resolving_trade)

    # A genuine broker call happened (impossible under the old short-circuit,
    # which returned before ever calling the trade port).
    assert resolving_trade.cancel_calls or resolving_trade.lookup_calls
    assert result.verdict == "clean"
    effect_after = repo.effect_operation(accepted.effect_operation_id)
    assert effect_after is not None and effect_after.state == "succeeded"


# ── reconcile_account ────────────────────────────────────────────────────────


async def test_reconcile_account_raises_an_account_clerk_hold_for_a_foreign_order(
    repo: ClerkSqliteRepository,
) -> None:
    foreign = _broker_order("manual/someone/v1:xyz", order_id="bo-foreign-1")
    read = _FakeRead(orders=[foreign])
    result = await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert result.verdict == "unexplained_order"
    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    assert hold is not None and hold["state"] == "ACTIVE"


async def test_reconcile_foreign_order_records_external_observation_without_bot_economics(
    repo: ClerkSqliteRepository,
) -> None:
    """A foreign broker order is durable account evidence, never a bot fill."""
    foreign = _broker_order("alpaca-console:operator-order-1", order_id="external-order-1")

    result = await reconcile_account(repo, read=_FakeRead(orders=[foreign]), trade=_FakeTrade())

    assert result.verdict == "unexplained_order"
    assert repo.external_orders() == [
        {
            "external_order_id": "external-order-1",
            "broker_order_id": "external-order-1",
            "client_order_id": "alpaca-console:operator-order-1",
                "symbol": "SPY",
                "side": "BUY",
                "qty": 1.0,
                "order_type": "market",
                "limit_price": None,
                "stop_price": None,
                "filled_avg_price": None,
            "observed_at_ms": 1_700_000_000_500,
            "acknowledged_at_ms": None,
            "ack_operator": None,
            "evidence_refs": ("external-order-1",),
        }
    ]
    assert repo.attributed_positions_by_symbol() == {}
    assert repo.fills_for_order("external-order-1") == []


async def test_acknowledging_one_external_order_keeps_another_external_cause_held(
    repo: ClerkSqliteRepository,
) -> None:
    """An acknowledgement can clear only the selected external-order cause."""
    first = _broker_order("alpaca-console:first", order_id="external-1")
    second = _broker_order("alpaca-console:second", order_id="external-2")
    await reconcile_account(repo, read=_FakeRead(orders=[first, second]), trade=_FakeTrade())

    acknowledged = acknowledge_external_order(
        repo,
        external_order_id="external-1",
        operator="operator-1",
    )

    assert acknowledged.acknowledged_at_ms is not None
    assert acknowledged.ack_operator == "operator-1"
    assert acknowledged.observation_sequence >= 1
    assert acknowledged.acknowledgement_sequence is not None
    assert acknowledged.observation_recorded_at_ms is not None
    assert acknowledged.acknowledgement_recorded_at_ms is not None
    active = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    assert active is not None
    assert active["evidence_refs_json"] == '["external-2"]'
    assert repo.external_order("external-2").acknowledged_at_ms is None  # type: ignore[union-attr]
    assert repo.attributed_positions_by_symbol() == {}


async def test_acknowledging_external_order_resolves_only_its_hold_and_keeps_audit_row(
    repo: ClerkSqliteRepository,
) -> None:
    foreign = _broker_order("alpaca-console:operator-order-1", order_id="external-order-1")
    await reconcile_account(repo, read=_FakeRead(orders=[foreign]), trade=_FakeTrade())
    repo.append_transition(_hold_transition(reason_code="STREAM_HEALTH", evidence_refs=["stream"]))

    acknowledged = acknowledge_external_order(
        repo,
        external_order_id="external-order-1",
        operator="operator-1",
    )

    assert acknowledged.acknowledged_at_ms is not None
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="STREAM_HEALTH") is not None
    assert [row["transition_kind"] for row in repo.custody_transitions()].count(
        "EXTERNAL_ORDER_ACKNOWLEDGED"
    ) == 1


async def test_external_order_reader_paginates_durable_observations_with_account_scoped_cursor(
    repo: ClerkSqliteRepository,
) -> None:
    first = _broker_order("alpaca-console:first", order_id="external-1")
    second = _broker_order("alpaca-console:second", order_id="external-2")
    await reconcile_account(repo, read=_FakeRead(orders=[first, second]), trade=_FakeTrade())
    reader = SqliteExternalOrderReader.from_repository(repo)
    try:
        first_page = reader.external_orders(page_size=1)
        second_page = reader.external_orders(cursor=first_page.next_cursor, page_size=1)

        assert [order.external_order_id for order in first_page.orders] == ["external-2"]
        assert first_page.orders[0].observation_sequence >= 1
        assert first_page.orders[0].acknowledgement_sequence is None
        assert first_page.orders[0].observation_recorded_at_ms is not None
        assert first_page.orders[0].acknowledgement_recorded_at_ms is None
        assert first_page.next_cursor is not None
        assert [order.external_order_id for order in second_page.orders] == ["external-1"]
        assert second_page.next_cursor is None
    finally:
        reader.close()


async def test_external_order_cursor_survives_a_later_broker_snapshot_update(
    repo: ClerkSqliteRepository,
) -> None:
    """The cursor follows immutable first-observation custody, not mutable poll time."""
    first = _broker_order("alpaca-console:first", order_id="external-1")
    second = _broker_order("alpaca-console:second", order_id="external-2")
    await reconcile_account(repo, read=_FakeRead(orders=[first, second]), trade=_FakeTrade())
    reader = SqliteExternalOrderReader.from_repository(repo)
    try:
        first_page = reader.external_orders(page_size=1)
        assert [order.external_order_id for order in first_page.orders] == ["external-2"]
        assert first_page.next_cursor is not None

        observe_external_order(
            repo,
            order=second.model_copy(
                update={"filled_avg_price": 101.25, "observed_at_ms": second.observed_at_ms + 1}
            ),
        )

        second_page = reader.external_orders(cursor=first_page.next_cursor, page_size=1)
        refreshed = repo.external_order("external-2")
        assert [order.external_order_id for order in second_page.orders] == ["external-1"]
        assert refreshed is not None
        assert refreshed.order_type == "market"
        assert refreshed.limit_price is None
        assert refreshed.filled_avg_price == 101.25
    finally:
        reader.close()


async def test_reconciliation_does_not_append_duplicate_external_fact_for_a_new_poll_time(
    repo: ClerkSqliteRepository,
) -> None:
    foreign = _broker_order("alpaca-console:operator-order-1", order_id="external-order-1")
    await reconcile_account(repo, read=_FakeRead(orders=[foreign]), trade=_FakeTrade())
    before = len(repo.custody_transitions())

    await reconcile_account(
        repo,
        read=_FakeRead(orders=[foreign.model_copy(update={"observed_at_ms": foreign.observed_at_ms + 1})]),
        trade=_FakeTrade(),
    )

    assert len(repo.custody_transitions()) == before


async def test_external_order_reader_filters_review_state_without_cross_filter_cursor_reuse(
    repo: ClerkSqliteRepository,
) -> None:
    first = _broker_order("alpaca-console:first", order_id="external-1")
    second = _broker_order("alpaca-console:second", order_id="external-2")
    third = _broker_order("alpaca-console:third", order_id="external-3")
    await reconcile_account(repo, read=_FakeRead(orders=[first, second, third]), trade=_FakeTrade())
    acknowledge_external_order(repo, external_order_id="external-1", operator="operator-1")
    reader = SqliteExternalOrderReader.from_repository(repo)
    try:
        review_required = reader.external_orders(lifecycle_state="review_required", page_size=1)
        reviewed = reader.external_orders(lifecycle_state="reviewed", page_size=10)

        assert [order.external_order_id for order in review_required.orders] == ["external-3"]
        assert review_required.next_cursor is not None
        assert [order.external_order_id for order in reviewed.orders] == ["external-1"]
        with pytest.raises(InvalidExternalOrderCursor, match="filter scope"):
            reader.external_orders(
                cursor=review_required.next_cursor,
                lifecycle_state="reviewed",
                page_size=1,
            )
    finally:
        reader.close()


async def test_acknowledgement_does_not_reactivate_stale_external_observations(
    repo: ClerkSqliteRepository,
) -> None:
    stale_first = _broker_order("alpaca-console:first", order_id="external-stale-1")
    stale_second = _broker_order("alpaca-console:second", order_id="external-stale-2")
    current = _broker_order("alpaca-console:current", order_id="external-current")
    await reconcile_account(
        repo,
        read=_FakeRead(orders=[stale_first, stale_second]),
        trade=_FakeTrade(),
    )
    await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade())
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None
    await reconcile_account(repo, read=_FakeRead(orders=[current]), trade=_FakeTrade())

    acknowledge_external_order(repo, external_order_id="external-current", operator="operator-1")

    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None


async def test_reconcile_account_raises_an_account_clerk_uncertainty_for_position_drift(
    repo: ClerkSqliteRepository,
) -> None:
    """#1380: a position_drift verdict raises a durable, ACCOUNT_CLERK-scoped
    uncertainty that blocks new exposure but still allows reduction."""
    read = _FakeRead(orders=[], positions=[_position("SPY", quantity=5)])
    result = await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert result.verdict == "position_drift"

    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None
    assert uncertainty["blocks_new_exposure"] == 1
    assert uncertainty["allows_reduction"] == 1


async def test_reconcile_blocks_new_exposure_on_first_indeterminate_mismatch(
    repo: ClerkSqliteRepository,
) -> None:
    """#1655 acceptance: a first-time in-flight position mismatch (a broker
    position that disagrees with attributed exposure on a symbol whose
    captured order is still working) must not be admission-clean. It must
    author a durable account-wide blocker that fences manual submission, an
    already-active bot's next ENTER, and a different bot's ENTER — while
    cancel/reduce/reconcile stay reachable."""
    submitted = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="entry-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=5),
        trade=_FakeTrade(),
    )
    assert submitted.order_ref is not None

    # The order and position snapshots land on different points of the same
    # fill's propagation: the order reports 2 filled (still working), but
    # the broker's position already shows 3 — a real broker race, not a bug
    # in either read.
    working = _broker_order(
        submitted.order_ref,
        status="partially_filled",
        filled_quantity=2.0,
        filled_avg_price=100.0,
    )
    other_sid, other_run_id = "qqq-bot", "run-qqq"
    repo.register_strategy_instance(strategy_instance_id=other_sid, symbol="QQQ", config_hash="h2")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=other_sid, lifecycle_run_id=other_run_id)

    result = await reconcile_account(
        repo,
        read=_FakeRead(orders=[working], positions=[_position("SPY", quantity=3.0)]),
        trade=_FakeTrade(),
    )

    assert result.verdict == "position_drift"
    assert result.drifted_symbols == ()
    assert result.indeterminate_symbols == ("SPY",)

    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None
    assert uncertainty["blocks_new_exposure"] == 1
    assert uncertainty["allows_reduction"] == 1

    # New exposure is blocked account-wide: the affected bot's own next
    # ENTER, a different bot's ENTER, and manual submission all read the
    # same ACCOUNT_CLERK-scoped uncertainty.
    same_strategy = admit_new_exposure(repo, strategy_instance_id=SID)
    assert same_strategy.allowed is False
    assert same_strategy.reason_code == "POSITION_DRIFT"

    different_strategy = admit_new_exposure(repo, strategy_instance_id=other_sid)
    assert different_strategy.allowed is False
    assert different_strategy.reason_code == "POSITION_DRIFT"

    from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
    from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability

    manual = decide_capability(
        repo, capability=Capability.NEW_EXPOSURE, subject_id=manual_operator_subject_id("desk")
    )
    assert manual.allowed is False
    assert manual.reason_code == "POSITION_DRIFT"

    # Safety capabilities stay reachable while blocked.
    assert decide_capability(repo, capability=Capability.CANCEL, strategy_instance_id=SID).allowed
    assert decide_capability(repo, capability=Capability.RECONCILE, strategy_instance_id=SID).allowed


async def test_reconcile_resolves_indeterminate_mismatch_only_once_proven_equal(
    repo: ClerkSqliteRepository,
) -> None:
    """#1655: the blocker must survive while the order is still working (even
    once the broker position momentarily agrees with a *stale* attributed
    read), and resolve only after a complete pass proves exact equality with
    no in-flight indeterminacy — exactly once."""
    submitted = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="entry-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=5),
        trade=_FakeTrade(),
    )
    assert submitted.order_ref is not None
    working = _broker_order(
        submitted.order_ref,
        status="partially_filled",
        filled_quantity=2.0,
        filled_avg_price=100.0,
    )
    first = await reconcile_account(
        repo,
        read=_FakeRead(orders=[working], positions=[_position("SPY", quantity=3.0)]),
        trade=_FakeTrade(),
    )
    assert first.verdict == "position_drift"
    assert not admit_new_exposure(repo, strategy_instance_id=SID).allowed

    # The order finally reports fully filled and the broker position agrees
    # exactly — a coherent snapshot with no in-flight indeterminacy.
    filled = _broker_order(
        submitted.order_ref,
        status="filled",
        filled_quantity=5.0,
        filled_avg_price=100.0,
    )
    second = await reconcile_account(
        repo,
        read=_FakeRead(orders=[filled], positions=[_position("SPY", quantity=5.0)]),
        trade=_FakeTrade(),
    )
    assert second.verdict == "clean"
    assert second.indeterminate_symbols == ()
    assert (
        repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None)
        is None
    )
    resolutions = [
        transition
        for transition in repo.custody_transitions()
        if transition["transition_kind"] == "UNCERTAINTY_RESOLVED"
        and '"resolution_kind":"CLEAN_BROKER_RECONCILIATION"' in transition["facts_json"]
    ]
    assert len(resolutions) == 1
    # The drift-specific blocker is gone, but the strategy's ENTER is still
    # filled and open (never exited) — #1722's ENTER fence (ADR 0042, PRD
    # FR-020) refuses a fresh ENTER whenever attributed exposure exists,
    # independent of whether an unrelated uncertainty resolved cleanly.
    admission = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not admission.allowed
    assert admission.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"

    # A further clean pass must not re-resolve (idempotent — no phantom
    # second UNCERTAINTY_RESOLVED transition racing a fresh observation).
    await reconcile_account(
        repo,
        read=_FakeRead(orders=[filled], positions=[_position("SPY", quantity=5.0)]),
        trade=_FakeTrade(),
    )
    resolutions_after = [
        transition
        for transition in repo.custody_transitions()
        if transition["transition_kind"] == "UNCERTAINTY_RESOLVED"
        and '"resolution_kind":"CLEAN_BROKER_RECONCILIATION"' in transition["facts_json"]
    ]
    assert len(resolutions_after) == 1


async def test_reconcile_uses_post_recovery_broker_snapshot_for_final_verdict(
    repo: ClerkSqliteRepository,
) -> None:
    read = _SequentialRead(
        order_snapshots=[[], []],
        position_snapshots=[[], [_position("SPY", quantity=5)]],
    )

    result = await reconcile_account(repo, read=read, trade=_FakeTrade())

    assert result.verdict == "position_drift"
    assert result.drifted_symbols == ("SPY",)


async def test_reconciliation_fences_enter_before_reading_broker_truth(
    repo: ClerkSqliteRepository,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRead(_FakeRead):
        async def list_orders(
            self,
            *,
            status: str | None = None,
            limit: int | None = None,
            after_ms: int | None = None,
        ) -> list[BrokerOrder]:
            del status, limit, after_ms
            entered.set()
            await release.wait()
            return []

    task = asyncio.create_task(
        reconcile_account(repo, read=BlockingRead(), trade=_FakeTrade())
    )
    await entered.wait()

    with pytest.raises(AdmissionBlockedError) as exc_info:
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="during-reconcile",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )

    assert exc_info.value.decision.reason_code == "RECONCILIATION_IN_PROGRESS"
    release.set()
    await task


async def test_in_flight_mismatch_retains_existing_drift_episode(
    repo: ClerkSqliteRepository,
) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        headline="drift",
        explanation="drift",
        operator_impact="blocked",
        next_step="reconcile",
        cause_facts={
            "positions": [
                {"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 0.0}
            ]
        },
    )
    working = _broker_order(_our_order_ref("working"), status="accepted")

    result = await reconcile_account(
        repo,
        read=_FakeRead(orders=[working], positions=[_position("SPY", quantity=5)]),
        trade=_FakeTrade(),
    )

    assert result.verdict == "unexplained_order"
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="POSITION_DRIFT",
            strategy_instance_id=None,
        )
        is not None
    )


async def test_reconcile_account_refreshes_unchanged_position_drift_evidence(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(orders=[], positions=[_position("SPY", quantity=5)])
    await reconcile_account(repo, read=read, trade=_FakeTrade())
    initial = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert initial is not None
    before = len(repo.custody_transitions())
    repo._clock.advance(1_000)  # type: ignore[attr-defined]

    await reconcile_account(repo, read=read, trade=_FakeTrade())
    refreshed = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert refreshed is not None
    assert refreshed["uncertainty_id"] == initial["uncertainty_id"]
    assert refreshed["observed_at_ms"] > initial["observed_at_ms"]
    assert len(repo.custody_transitions()) == before + 1


async def test_reconcile_folds_recovered_fill_before_computing_position_drift(
    repo: ClerkSqliteRepository,
) -> None:
    """A broker position created by a just-recovered fill is not false drift."""
    order_ref = await _make_uncertain_order(repo)
    filled = _broker_order(
        order_ref,
        status="filled",
        filled_quantity=1.0,
        filled_avg_price=100.0,
    )

    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=1.0)]),
        trade=_FakeTrade(lookup_result=filled),
    )

    assert result.verdict == "clean"
    assert repo.attributed_positions_by_symbol() == {"SPY": pytest.approx(1.0)}
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="POSITION_DRIFT",
            strategy_instance_id=None,
        )
        is None
    )


async def test_reconcile_account_hold_raise_is_idempotent_across_repeated_passes(
    repo: ClerkSqliteRepository,
) -> None:
    foreign = _broker_order("manual/someone/v1:xyz", order_id="bo-foreign-1")
    read = _FakeRead(orders=[foreign])
    await reconcile_account(repo, read=read, trade=_FakeTrade())
    before = len(repo.custody_transitions())

    await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert len(repo.custody_transitions()) == before  # still one ACTIVE hold, no second raise


async def test_reconcile_refreshes_changed_hold_evidence_then_resolves_it(
    repo: ClerkSqliteRepository,
) -> None:
    await reconcile_account(
        repo,
        read=_FakeRead(orders=[_broker_order("manual/one", order_id="bo-1")]),
        trade=_FakeTrade(),
    )
    await reconcile_account(
        repo,
        read=_FakeRead(orders=[_broker_order("manual/two", order_id="bo-2")]),
        trade=_FakeTrade(),
    )
    active = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    assert active is not None and "bo-2" in active["evidence_refs_json"]
    assert any(transition["transition_kind"] == "ACCOUNT_HOLD_REFRESHED" for transition in repo.custody_transitions())

    clean = await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade())
    assert clean.verdict == "clean"
    assert repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER") is None
    assert any(transition["transition_kind"] == "ACCOUNT_HOLD_RESOLVED" for transition in repo.custody_transitions())


async def test_overlapping_reconciliation_passes_apply_verdicts_in_snapshot_order(
    repo: ClerkSqliteRepository,
) -> None:
    """An older clean snapshot cannot clear a newer foreign-order verdict."""
    snapshot_started = asyncio.Event()
    release_clean_snapshot = asyncio.Event()

    class BlockingCleanRead(_FakeRead):
        async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
            snapshot_started.set()
            return []

        async def list_positions(self) -> list[BrokerPosition]:
            await release_clean_snapshot.wait()
            return []

    clean_task = asyncio.create_task(reconcile_account(repo, read=BlockingCleanRead(), trade=_FakeTrade()))
    await snapshot_started.wait()
    foreign_task = asyncio.create_task(
        reconcile_account(
            repo,
            read=_FakeRead(orders=[_broker_order("manual/newer", order_id="bo-newer")]),
            trade=_FakeTrade(),
        )
    )
    await asyncio.sleep(0)
    assert not foreign_task.done()

    release_clean_snapshot.set()
    clean_result, foreign_result = await asyncio.gather(clean_task, foreign_task)
    assert clean_result.verdict == "clean"
    assert foreign_result.verdict == "unexplained_order"
    hold = repo.active_hold(scope="ACCOUNT_CLERK", reason_code="UNEXPLAINED_ORDER")
    assert hold is not None and "bo-newer" in hold["evidence_refs_json"]


async def test_clean_reconciliation_resolves_position_drift_uncertainty(
    repo: ClerkSqliteRepository,
) -> None:
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=5)]),
        trade=_FakeTrade(),
    )
    assert repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None)

    result = await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade())
    assert result.verdict == "clean"
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="POSITION_DRIFT",
            strategy_instance_id=None,
        )
        is None
    )


def test_raise_hold_if_none_active_serializes_two_concurrent_callers(
    repo: ClerkSqliteRepository,
) -> None:
    """``raise_hold_if_none_active``'s check-then-append must be one
    continuous critical section, not two separately-lockable steps — else
    two genuinely concurrent callers (an automatic sweep pass and an
    operator's "Reconcile now" landing at the same instant) could both
    observe "no active hold" before either appends one.

    Forcing that exact interleaving via a barrier *inside* the critical
    section (the technique test_enter.py's same-owner-race test uses) isn't
    possible here: a true mutex makes the interleaving structurally
    unreachable, so a barrier planted inside it would simply deadlock
    (thread B can never reach the barrier while genuinely excluded by the
    lock). Proving mutual exclusion instead: pause thread A *after* its
    append (still inside the lock, since ``append_transition`` hasn't
    returned to ``raise_hold_if_none_active`` yet) and confirm thread B —
    attempting the identical call concurrently — is genuinely blocked
    (hasn't returned) for as long as thread A holds the lock, then only
    proceeds once released, at which point it must see A's hold and no-op.
    """
    thread_a_appended = threading.Event()
    release_thread_a = threading.Event()
    original_append_transition = ClerkSqliteRepository.append_transition

    def paused_append_transition(
        self: ClerkSqliteRepository, transition: TransitionInput
    ) -> CommittedTransition:
        result = original_append_transition(self, transition)
        thread_a_appended.set()
        release_thread_a.wait(timeout=5)
        return result

    def build_transition() -> TransitionInput:
        return _hold_transition(reason_code="RACE_TEST")

    result_a: list[bool] = []
    result_b: list[bool] = []

    def worker_a() -> None:
        result_a.append(
            repo.raise_hold_if_none_active(
                scope="ACCOUNT_CLERK", reason_code="RACE_TEST", build_transition=build_transition
            )
        )

    def worker_b() -> None:
        result_b.append(
            repo.raise_hold_if_none_active(
                scope="ACCOUNT_CLERK", reason_code="RACE_TEST", build_transition=build_transition
            )
        )

    ClerkSqliteRepository.append_transition = paused_append_transition  # type: ignore[method-assign]
    try:
        thread_a = threading.Thread(target=worker_a)
        thread_a.start()
        assert thread_a_appended.wait(timeout=2)  # A appended; still holding the lock, paused

        thread_b = threading.Thread(target=worker_b)
        thread_b.start()
        thread_b.join(timeout=0.2)
        assert thread_b.is_alive()  # B is genuinely blocked on the lock, not racing ahead

        release_thread_a.set()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)
    finally:
        ClerkSqliteRepository.append_transition = original_append_transition  # type: ignore[method-assign]

    assert result_a == [True]
    assert result_b == [False]
    raised = [t for t in repo.custody_transitions() if t["transition_kind"] == "ACCOUNT_HOLD_RAISED"]
    assert len(raised) == 1


async def test_reconcile_account_reports_stale_and_fails_closed_on_broker_read_failure(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(error=BrokerUnavailable("down"))
    before = len(repo.custody_transitions())

    result = await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert result.verdict == "stale"
    assert len(repo.custody_transitions()) == before + 1
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code="BROKER_SNAPSHOT_STALE",
        strategy_instance_id=None,
    )
    assert uncertainty is not None and uncertainty["allows_reduction"] == 0
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="RECONCILIATION_INCOMPLETE",
            strategy_instance_id=None,
        )
        is None
    )


async def test_reconcile_fails_closed_when_open_order_snapshot_hits_limit(
    repo: ClerkSqliteRepository,
) -> None:
    orders = [_broker_order(f"manual/order-{index}", order_id=f"bo-{index}") for index in range(500)]
    result = await reconcile_account(
        repo,
        read=_FakeRead(orders=orders),
        trade=_FakeTrade(),
    )
    assert result.verdict == "stale"
    assert repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code="BROKER_SNAPSHOT_STALE",
        strategy_instance_id=None,
    )


async def test_reconcile_account_resolves_every_uncertain_order_in_one_pass(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref_1 = await _make_uncertain_order(repo, decision_id="d1")
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id="qqq-bot",
        lifecycle_run_id="run-2",
    )
    order_ref_2 = await _make_uncertain_order(
        repo,
        decision_id="d2",
        strategy_instance_id="qqq-bot",
        lifecycle_run_id="run-2",
    )
    read = _FakeRead(orders=[], positions=[])

    result = await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert result.resolved_count == 2
    assert repo.order(order_ref_1).broker_order_id is not None  # type: ignore[union-attr]
    assert repo.order(order_ref_2).broker_order_id is not None  # type: ignore[union-attr]


async def test_reconcile_account_resolved_count_excludes_orders_still_unknown(
    repo: ClerkSqliteRepository,
) -> None:
    """``resolved_count`` must count only orders that actually reached a
    terminal outcome this pass — an order still within its R4 grace window
    stays ``unknown`` and must not be miscounted as resolved."""
    await _make_uncertain_order(repo)  # still within grace; lookup absent
    read = _FakeRead(orders=[], positions=[])

    result = await reconcile_account(repo, read=read, trade=_FakeTrade(lookup_absent=True))
    assert result.resolved_count == 0


async def test_reconcile_account_recovers_an_unknown_manual_open_order(
    repo: ClerkSqliteRepository,
) -> None:
    """Manual UNKNOWN effects use the same exact client-order recovery path."""
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="desk",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
        leg=_leg(),
        trade=_FakeTrade(submit_error=BrokerUnavailable("timeout"), lookup_absent=True),
    )
    assert submitted.leg.order_ref is not None
    assert submitted.leg.effect_operation_id is not None
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "unknown"  # type: ignore[union-attr]

    trade = _FakeTrade()
    result = await reconcile_account(repo, read=_FakeRead(), trade=trade)

    assert result.resolved_count == 1
    assert trade.lookup_calls == [submitted.leg.order_ref]
    assert repo.order(submitted.leg.order_ref).broker_order_id is not None  # type: ignore[union-attr]
    assert repo.effect_operation(submitted.leg.effect_operation_id).state == "in_progress"  # type: ignore[union-attr]


async def test_reconcile_account_recovers_an_unknown_manual_order_after_repository_restart(
    tmp_path: Path,
) -> None:
    """Recovery after a process restart uses the persisted manual order identity only."""
    clock = _clock_at(1_700_000_000_000)
    before_restart = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    try:
        submitted = await submit_manual_order(
            before_restart,
            account_id=ACCOUNT_ID,
            operator_id="desk",
            ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
            leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
            leg=_leg(),
            trade=_FakeTrade(submit_error=BrokerUnavailable("timeout"), lookup_absent=True),
        )
        assert submitted.leg.order_ref is not None
        assert submitted.leg.effect_operation_id is not None
        assert before_restart.effect_operation(submitted.leg.effect_operation_id).state == "unknown"  # type: ignore[union-attr]
    finally:
        before_restart.close()

    after_restart = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    try:
        trade = _FakeTrade()
        result = await reconcile_account(after_restart, read=_FakeRead(), trade=trade)

        assert result.resolved_count == 1
        assert trade.submit_calls == []
        assert trade.lookup_calls == [submitted.leg.order_ref]
        assert after_restart.order(submitted.leg.order_ref).broker_order_id is not None  # type: ignore[union-attr]
        assert after_restart.effect_operation(submitted.leg.effect_operation_id).state == "in_progress"  # type: ignore[union-attr]
    finally:
        after_restart.close()


async def test_indeterminate_blocker_survives_restart_and_boot_recovery_stays_blocked(
    tmp_path: Path,
) -> None:
    """#1655: the durable indeterminate-mismatch blocker is not in-memory
    state. A process crash right after it is published (simulated here by
    closing the handle without any further writes) must leave a freshly
    reopened repository — including the boot-recovery reconciliation path
    (:meth:`SqliteAlpacaClerkFacade.recover`) — still fenced against new
    exposure, and a later coherent snapshot must resolve it exactly once."""
    clock = _clock_at(1_700_000_000_000)
    before_restart = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        before_restart.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
        submit_start_run(before_restart, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
        submitted = await submit_enter(
            before_restart,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="entry-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(quantity=5),
            trade=_FakeTrade(),
        )
        assert submitted.order_ref is not None
        order_ref = submitted.order_ref
        working = _broker_order(order_ref, status="partially_filled", filled_quantity=2.0, filled_avg_price=100.0)

        # Publish the blocker, then crash: no further durable writes happen
        # on this handle before it is closed.
        result = await reconcile_account(
            before_restart,
            read=_FakeRead(orders=[working], positions=[_position("SPY", quantity=3.0)]),
            trade=_FakeTrade(),
        )
        assert result.verdict == "position_drift"
        assert result.indeterminate_symbols == ("SPY",)
    finally:
        before_restart.close()

    after_restart = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    try:
        assert (
            after_restart.active_uncertainty(
                scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
            )
            is not None
        )
        assert not admit_new_exposure(after_restart, strategy_instance_id=SID).allowed

        from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade

        # Boot recovery: still fenced while the mismatch remains indeterminate.
        facade = SqliteAlpacaClerkFacade(
            repo=after_restart,
            read=_FakeRead(orders=[working], positions=[_position("SPY", quantity=3.0)]),
            trade=_FakeTrade(),
        )
        await facade.recover()
        assert not admit_new_exposure(after_restart, strategy_instance_id=SID).allowed

        # A later coherent, fully-filled snapshot resolves the block exactly once.
        filled = _broker_order(order_ref, status="filled", filled_quantity=5.0, filled_avg_price=100.0)
        cleared = await reconcile_account(
            after_restart,
            read=_FakeRead(orders=[filled], positions=[_position("SPY", quantity=5.0)]),
            trade=_FakeTrade(),
        )
        assert cleared.verdict == "clean"
        # The indeterminate-mismatch blocker is gone, but the strategy's
        # ENTER is still filled and open (never exited) — #1722's ENTER
        # fence (ADR 0042, PRD FR-020) refuses a fresh ENTER whenever
        # attributed exposure exists, independent of this resolution.
        admission = admit_new_exposure(after_restart, strategy_instance_id=SID)
        assert not admission.allowed
        assert admission.reason_code == "ATTRIBUTED_EXPOSURE_EXISTS"
        resolutions = [
            transition
            for transition in after_restart.custody_transitions()
            if transition["transition_kind"] == "UNCERTAINTY_RESOLVED"
            and '"resolution_kind":"CLEAN_BROKER_RECONCILIATION"' in transition["facts_json"]
        ]
        assert len(resolutions) == 1
    finally:
        after_restart.close()


async def test_reconcile_account_recovers_an_unknown_manual_closed_order(
    repo: ClerkSqliteRepository,
) -> None:
    """A closed manual order folds cumulative evidence without inventing an exact fill."""
    submitted = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="desk",
        ticket_id="7de3a77c-b698-4e0d-a5d1-2f624574ed35",
        leg_id="09d6d63e-6375-4e6d-8d20-3b1bf70c2465",
        leg=_leg(),
        trade=_FakeTrade(submit_error=BrokerUnavailable("timeout"), lookup_absent=True),
    )
    assert submitted.leg.order_ref is not None
    closed = _broker_order(
        submitted.leg.order_ref,
        status="filled",
        filled_quantity=1.0,
        filled_avg_price=100.0,
    )

    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=1.0)]),
        trade=_FakeTrade(lookup_result=closed),
    )

    assert result.resolved_count == 1
    assert repo.effective_fill_totals_for_order(submitted.leg.order_ref) == (1.0, 100.0)
    ticket = repo.manual_order_ticket(submitted.ticket.ticket_id)
    assert ticket is not None
    assert ticket.state == "ACTIVE"
    assert ticket.legs[0].state == "IN_PROGRESS"


async def test_reconcile_account_skips_a_claim_contended_order_but_still_resolves_the_rest(
    repo: ClerkSqliteRepository,
) -> None:
    """One order's claim being live-held by a concurrent owner (e.g. an
    in-flight submit_enter for that same order) must not abort the rest of
    the pass — the other, unrelated uncertain order still resolves."""
    contended_ref = await _make_uncertain_order(repo, decision_id="d1")
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id="qqq-bot",
        lifecycle_run_id="run-2",
    )
    free_ref = await _make_uncertain_order(
        repo,
        decision_id="d2",
        strategy_instance_id="qqq-bot",
        lifecycle_run_id="run-2",
    )
    contended_order = repo.order(contended_ref)
    assert contended_order is not None
    # Plants a still-live claim under a genuinely different owner directly
    # (bypassing the normal CAS acquisition) — the real-world equivalent is
    # a lease handoff: a prior process claimed this operation and crashed
    # before its claim expired, and a fresh process (a new lease_owner) is
    # the one now running reconciliation.
    with repo._write_lock:
        repo._conn.execute(
            "UPDATE effect_operations SET claim_owner = 'other-live-process', "
            "claim_token = 'tok-other', claimed_at_ms = ?, claim_expires_at_ms = ? "
            "WHERE effect_operation_id = ?",
            (repo.clock(), repo.clock() + 60_000, contended_order.effect_operation_id),
        )
        repo._conn.commit()
    read = _FakeRead(orders=[], positions=[])

    result = await reconcile_account(repo, read=read, trade=_FakeTrade())

    assert result.resolved_count == 1
    assert repo.order(contended_ref).broker_order_id is None  # type: ignore[union-attr]
    assert repo.order(free_ref).broker_order_id is not None  # type: ignore[union-attr]


async def test_reconcile_missing_exit_entry_records_failure_and_finishes_pass(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_uncertain_order(repo)
    filled = _broker_order(
        entry_ref,
        status="filled",
        filled_quantity=1.0,
        filled_avg_price=100.0,
    )
    entry = repo.order(entry_ref)
    assert entry is not None
    fold_order_evidence(
        repo,
        effect_operation_id=entry.effect_operation_id,
        order=filled,
    )
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-with-missing-link",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    with repo._write_lock:
        repo._conn.execute(
            "DELETE FROM operation_order_links WHERE effect_operation_id = ?",
            (accepted.effect_operation_id,),
        )
        repo._conn.commit()

    result = await reconcile_account(
        repo,
        read=_FakeRead(orders=[], positions=[_position("SPY", quantity=1)]),
        trade=_FakeTrade(),
    )

    assert result.resolved_count == 1
    attempts = [
        transition
        for transition in repo.custody_transitions()
        if transition["effect_operation_id"] == accepted.effect_operation_id
        and transition["transition_kind"] == "RECONCILIATION_ATTEMPTED"
    ]
    assert len(attempts) == 1
    assert '"outcome":"RESOLVED_FAILURE"' in attempts[0]["facts_json"]
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not decision.allowed
    assert decision.reason_code == "EXIT_IN_PROGRESS"


async def test_reconcile_missing_enter_order_remains_unresolved(
    repo: ClerkSqliteRepository,
) -> None:
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-with-missing-order",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.effect_operation_id is not None
    effect_operation_id = accepted.effect_operation_id
    _remove_captured_order(repo, effect_operation_id=effect_operation_id)

    await ReconciliationSweep(
        repo=repo,
        read=_FakeRead(),
        trade=_FakeTrade(),
        max_passes=1,
    ).run()

    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None and effect.state not in {"succeeded", "failed", "rejected"}
    attempts = [
        transition
        for transition in repo.custody_transitions()
        if transition["effect_operation_id"] == effect_operation_id
        and transition["transition_kind"] == "RECONCILIATION_ATTEMPTED"
    ]
    assert len(attempts) == 1
    assert '"outcome":"STILL_UNKNOWN"' in attempts[0]["facts_json"]
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not decision.allowed
    assert decision.reason_code == "RECONCILIATION_INCOMPLETE"


async def test_reconcile_account_operator_reconcile_now_trigger_is_recorded(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    read = _FakeRead(orders=[], positions=[])

    await reconcile_account(repo, read=read, trade=_FakeTrade(), trigger="OPERATOR_RECONCILE_NOW")
    transitions = repo.transitions_for_order(order_ref)
    reconciliation_facts = next(t for t in transitions if t["transition_kind"] == "RECONCILIATION_ATTEMPTED")
    import json

    facts = json.loads(reconciliation_facts["facts_json"])
    assert facts["trigger"] == "OPERATOR_RECONCILE_NOW"


# ── ReconciliationSweep ──────────────────────────────────────────────────────


def test_sqlite_sweep_direct_construction_guards_raw_broker_ports(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead()
    trade = _FakeTrade()
    sweep = ReconciliationSweep(repo=repo, read=read, trade=trade)

    assert isinstance(sweep._read, GuardedBrokerReadPort)
    assert isinstance(sweep._trade, GuardedBrokerTradePort)
    assert sweep._read.intake is sweep._intake
    assert sweep._trade.intake is sweep._intake


async def test_sweep_runs_bounded_passes_via_injected_sleep(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    read = _FakeRead(orders=[], positions=[])
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    sweep = ReconciliationSweep(repo=repo, read=read, trade=_FakeTrade(), sleep=fake_sleep, max_passes=3)
    await sweep.run()

    assert len(sleep_calls) == 2  # sleeps between passes, not after the last
    assert repo.order(order_ref).broker_order_id is not None  # type: ignore[union-attr]


async def test_sweep_survives_a_broker_error_and_continues_to_the_next_pass(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(error=BrokerUnavailable("down"))
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    sweep = ReconciliationSweep(repo=repo, read=read, trade=_FakeTrade(), sleep=fake_sleep, max_passes=3)
    await sweep.run()  # must not raise despite every pass hitting BrokerUnavailable
    assert sleep_calls == [30.0, 60.0]


async def test_sweep_failure_leaves_durable_admission_blocker(
    repo: ClerkSqliteRepository,
) -> None:
    sweep = ReconciliationSweep(
        repo=repo,
        read=_FailingFinalSnapshotRead(),
        trade=_FakeTrade(),
        max_passes=1,
    )

    await sweep.run()

    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code="RECONCILIATION_INCOMPLETE",
        strategy_instance_id=None,
    )
    assert uncertainty is not None
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not decision.allowed
    assert decision.reason_code == "RECONCILIATION_INCOMPLETE"


async def test_cancelled_reconciliation_leaves_durable_admission_blocker(
    repo: ClerkSqliteRepository,
) -> None:
    snapshot_started = asyncio.Event()

    class BlockingRead(_FakeRead):
        async def list_orders(self, **_kwargs: Any) -> list[BrokerOrder]:
            snapshot_started.set()
            await asyncio.Event().wait()
            return []

    task = asyncio.create_task(
        reconcile_account(repo, read=BlockingRead(), trade=_FakeTrade())
    )
    await snapshot_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not decision.allowed
    assert decision.reason_code == "RECONCILIATION_INCOMPLETE"


async def test_failed_blocker_publication_keeps_process_fence_closed(
    repo: ClerkSqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_uncertainty_publication(**_kwargs: Any) -> str:
        raise RuntimeError("durable uncertainty unavailable")

    monkeypatch.setattr(repo, "observe_uncertainty", fail_uncertainty_publication)

    with pytest.raises(RuntimeError, match="durable uncertainty unavailable"):
        await reconcile_account(
            repo,
            read=_FailingFinalSnapshotRead(),
            trade=_FakeTrade(),
        )

    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert not decision.allowed
    assert decision.reason_code == "RECONCILIATION_IN_PROGRESS"


async def test_complete_reconciliation_resolves_incomplete_pass_uncertainty(
    repo: ClerkSqliteRepository,
) -> None:
    await ReconciliationSweep(
        repo=repo,
        read=_FailingFinalSnapshotRead(),
        trade=_FakeTrade(),
        max_passes=1,
    ).run()

    result = await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade())

    assert result.verdict == "clean"
    assert (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="RECONCILIATION_INCOMPLETE",
            strategy_instance_id=None,
        )
        is None
    )
    assert admit_new_exposure(repo, strategy_instance_id=SID).allowed


async def test_nonclean_completed_pass_records_truthful_incomplete_resolution(
    repo: ClerkSqliteRepository,
) -> None:
    await ReconciliationSweep(
        repo=repo,
        read=_FailingFinalSnapshotRead(),
        trade=_FakeTrade(),
        max_passes=1,
    ).run()

    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=5)]),
        trade=_FakeTrade(),
    )

    assert result.verdict == "position_drift"
    resolution = next(
        transition
        for transition in reversed(repo.custody_transitions())
        if transition["transition_kind"] == "UNCERTAINTY_RESOLVED"
    )
    assert '"resolution_kind":"COMPLETE_ACCOUNT_RECONCILIATION"' in resolution["facts_json"]


async def test_repeated_sweep_failures_refresh_one_incomplete_pass_episode(
    repo: ClerkSqliteRepository,
) -> None:
    await ReconciliationSweep(
        repo=repo,
        read=_FailingFinalSnapshotRead(),
        trade=_FakeTrade(),
        max_passes=1,
    ).run()
    initial = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code="RECONCILIATION_INCOMPLETE",
        strategy_instance_id=None,
    )
    assert initial is not None
    repo.clock.advance(1_000)  # type: ignore[attr-defined]

    await ReconciliationSweep(
        repo=repo,
        read=_FailingFinalSnapshotRead(),
        trade=_FakeTrade(),
        max_passes=1,
    ).run()

    refreshed = repo.active_uncertainty(
        scope="ACCOUNT_CLERK",
        reason_code="RECONCILIATION_INCOMPLETE",
        strategy_instance_id=None,
    )
    assert refreshed is not None
    assert refreshed["uncertainty_id"] == initial["uncertainty_id"]
    assert refreshed["observed_at_ms"] > initial["observed_at_ms"]


async def test_sweep_backoff_is_capped_and_resets_after_success(
    repo: ClerkSqliteRepository,
) -> None:
    sleep_calls: list[float] = []
    outcomes = iter([False, False, True, False])

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    class DeterministicSweep(ReconciliationSweep):
        async def _run_one_pass(self) -> bool:
            return next(outcomes)

    sweep = DeterministicSweep(
        repo=repo,
        read=_FakeRead(),
        trade=_FakeTrade(),
        interval_s=10.0,
        max_backoff_s=25.0,
        sleep=fake_sleep,
        max_passes=4,
    )

    await sweep.run()

    assert sleep_calls == [20.0, 25.0, 10.0]


async def test_started_sweep_renews_the_execution_lease_while_idle(tmp_path: Path) -> None:
    now = {"ms": 1_700_000_000_000}
    clerk_repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=lambda: now["ms"],
        lease_ttl_ms=90,
    )
    heartbeat_renewed = asyncio.Event()
    hold_heartbeat = asyncio.Event()
    heartbeat_delays: list[float] = []

    async def controlled_heartbeat_sleep(delay: float) -> None:
        heartbeat_delays.append(delay)
        if len(heartbeat_delays) == 1:
            now["ms"] += 30
            return
        heartbeat_renewed.set()
        await hold_heartbeat.wait()

    class IdleSweep(ReconciliationSweep):
        async def run(self) -> None:
            await hold_heartbeat.wait()

    sweep = IdleSweep(
        repo=clerk_repo,
        read=_FakeRead(),
        trade=_FakeTrade(),
        lease_sleep=controlled_heartbeat_sleep,
    )
    try:
        sweep.start()
        await asyncio.wait_for(heartbeat_renewed.wait(), timeout=1.0)
        # The heartbeat must renew three times per TTL (90ms / 3 = 0.03s) so a
        # single missed renewal still leaves margin before the lease expires.
        # Pin the cadence: a regression to an unsafe (larger) interval must fail.
        assert heartbeat_delays[0] == pytest.approx(0.03, abs=1e-9)
        lease = clerk_repo._conn.execute(
            "SELECT execution_lease_expires_at_ms FROM control_meta WHERE id = 1"
        ).fetchone()
        assert lease["execution_lease_expires_at_ms"] == now["ms"] + 90
    finally:
        await sweep.stop()
        clerk_repo.close()


# ── stuck-EXIT watchdog: bounded redrive then durable EXIT_STUCK ──────────────

WATCHDOG_SID = "wd-bot"
WATCHDOG_RUN = "wd-run-1"


@pytest.fixture
def clocked_repo(tmp_path: Path):
    clock = _clock_at(1_700_000_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        strategy_instance_id=WATCHDOG_SID, symbol="SPY", config_hash="wd-h1"
    )
    submit_start_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
        lifecycle_run_id=WATCHDOG_RUN,
    )
    yield repo, clock
    repo.close()


class _NoReconciler:
    async def reconcile_account(self, *, trigger: str):
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


async def _held_position(repo: ClerkSqliteRepository, *, suffix: str = "1") -> str:
    """Filled 10-share SPY entry with an exact execution slice -> attributed +10."""
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=WATCHDOG_SID,
        decision_id=f"wd-enter-{suffix}",
        lifecycle_run_id=WATCHDOG_RUN,
        leg=_leg(quantity=10),
        trade=_FakeTrade(),
    )
    assert submission.order_ref is not None
    filled = _broker_order(
        submission.order_ref, status="filled", quantity=10.0,
        filled_quantity=10, filled_avg_price=100.0,
    )
    fold_order_evidence(
        repo, effect_operation_id=submission.effect_operation_id, order=filled
    )
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600,
            price=100, quantity=10, execution_id=f"wd-exec-{suffix}",
        ),
        event_key=f"execution:wd-exec-{suffix}",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return submission.order_ref


def _raise_exit_not_flat(
    repo: ClerkSqliteRepository, *, attributed_qty: float, evidence_ref: str = "wd-evidence"
) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=WATCHDOG_SID,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="A completed EXIT left attributed exposure",
        explanation="test: reducing order resolved without flattening",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Run another EXIT or reconcile until attributed exposure is flat.",
        evidence_refs=(evidence_ref,),
        cause_facts=ExitNotFlatCause(symbol="SPY", attributed_qty=attributed_qty).to_mapping(),
        severity="error",
    )


async def test_reconcile_account_redrives_stale_exit_not_flat(clocked_repo) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode is not None
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    trade = _FakeTrade()

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=trade,
    )

    token = hashlib.sha256(episode["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    assert repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token}-1") is not None
    assert len(trade.submit_calls) == 1  # the recovery reducing order reached the broker


async def test_reconcile_account_does_not_redrive_a_fresh_exit_not_flat(clocked_repo) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode is not None
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS - 1)

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    token = hashlib.sha256(episode["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    assert repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token}-1") is None


async def test_reconcile_account_escalates_exit_stuck_after_redrive_cap(
    clocked_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    monkeypatch.setattr(watchdog_module, "EXIT_NOT_FLAT_MAX_REDRIVES", 0)

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    stuck = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert stuck is not None  # durable, operator-visible escalation


async def test_watchdog_redrive_identity_is_scoped_per_episode(clocked_repo) -> None:
    """P1 regression: a later, independent stuck episode for the same strategy
    must mint fresh redrive identities. A bare `exit-redrive-<n>` collides:
    `_exit_identity` keys idempotency on (strategy_instance_id, decision_id)
    only, so a reused id either replays the earlier episode's terminal effect
    (same entry ref) or raises CommandExistingConflict forever (new entry ref,
    different payload hash)."""
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode_a = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode_a is not None
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )
    token_a = hashlib.sha256(episode_a["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    command_a = repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token_a}-1")
    assert command_a is not None

    # Episode A's redrive fills completely -> flat -> the fence resolves A.
    reducing_a = next(
        order
        for order in repo.orders_for_effect_operation(command_a.effect_operation_id)
        if order.role == "REDUCING"
    )
    filled_reducing = _broker_order(
        reducing_a.order_ref, status="filled", side="sell", quantity=10.0,
        filled_quantity=10, filled_avg_price=101.0,
    )
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=reducing_a.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=101.0, quantity=10, execution_id="wd-exec-flat-a",
        ),
        event_key="execution:wd-exec-flat-a",
        order=filled_reducing,
        recovery_source=None,
        recovery_window_limit=None,
    )
    await reconcile_account(repo, read=_FakeRead(positions=[]), trade=_FakeTrade())
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is None

    # A fresh entry gets stuck later: independent episode B on a new entry ref.
    submission = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=WATCHDOG_SID,
        decision_id="wd-enter-2", lifecycle_run_id=WATCHDOG_RUN,
        leg=_leg(quantity=10), trade=_FakeTrade(),
    )
    filled_entry = _broker_order(
        submission.order_ref, status="filled", quantity=10.0,
        filled_quantity=10, filled_avg_price=100.0,
    )
    fold_order_evidence(
        repo, effect_operation_id=submission.effect_operation_id, order=filled_entry
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=repo.clock(),
            price=100.0, quantity=10, execution_id="wd-exec-enter-2",
        ),
        event_key="execution:wd-exec-enter-2",
        order=filled_entry,
        recovery_source=None,
        recovery_window_limit=None,
    )
    clock.advance(1_000)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode_b = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode_b is not None
    assert episode_b["uncertainty_id"] != episode_a["uncertainty_id"]
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    token_b = hashlib.sha256(episode_b["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    assert token_b != token_a
    assert repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token_b}-1") is not None


async def test_watchdog_redrive_count_survives_episode_refresh(
    clocked_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 regression (Codex): a redrive that completes non-flat REFRESHES the
    EXIT_NOT_FLAT episode (exit_resolution re-raises with the new reducing
    order_ref), overwriting observed_at_ms. If the redrive count were anchored
    to observed_at_ms it would reset to zero, so the watchdog would re-mint
    `exit-redrive-<token>-1` forever and never escalate. With the count anchored
    to the episode identity, one redrive followed by a refresh must still be
    counted, so the next pass escalates. (MAX=1 isolates the count: the
    escalation branch runs before the active-exit entry filter.)"""
    repo, clock = clocked_repo
    await _held_position(repo)
    _raise_exit_not_flat(repo, attributed_qty=10.0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert episode is not None
    token = hashlib.sha256(episode["uncertainty_id"].encode("utf-8")).hexdigest()[:12]
    monkeypatch.setattr(watchdog_module, "EXIT_NOT_FLAT_MAX_REDRIVES", 1)

    # Pass 1: one redrive, no escalation yet.
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo, read=_FakeRead(positions=[_position("SPY", quantity=10.0)]), trade=_FakeTrade()
    )
    assert repo.get_command(f"cmd:{WATCHDOG_SID}:exit-redrive-{token}-1") is not None
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is None

    # The redrive completes non-flat: exit_resolution refreshes the same episode
    # with the new reducing order_ref, moving observed_at_ms forward.
    clock.advance(5_000)
    _raise_exit_not_flat(repo, attributed_qty=10.0, evidence_ref="reducing-redrive-1")
    refreshed = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    )
    assert refreshed is not None
    assert refreshed["uncertainty_id"] == episode["uncertainty_id"]  # same episode
    assert refreshed["observed_at_ms"] > episode["observed_at_ms"]  # observed moved

    # Pass 2: the one prior redrive is still counted (redrives=1 >= MAX=1), so
    # the watchdog escalates. A time-anchored count would read zero here and
    # never escalate.
    clock.advance(watchdog_module.EXIT_NOT_FLAT_REDRIVE_AFTER_MS + 1)
    await reconcile_account(
        repo, read=_FakeRead(positions=[_position("SPY", quantity=10.0)]), trade=_FakeTrade()
    )
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is not None


async def test_exit_stuck_is_resolved_once_attributed_reaches_flat(clocked_repo) -> None:
    """P1 regression (Codex): a durable EXIT_STUCK escalation must clear when
    the position later reaches flat — otherwise the now-flat strategy stays
    permanently barred from new exposure."""
    repo, _clock = clocked_repo
    raise_uncertainty(
        repo,
        strategy_instance_id=WATCHDOG_SID,
        reason_code=EXIT_STUCK_REASON_CODE,
        headline="A stuck EXIT exhausted automatic re-drives",
        explanation="test",
        operator_impact="new exposure paused; exact reduction available",
        next_step="execute the presented safe flatten",
        evidence_refs=("wd-evidence",),
        cause_facts=ExitStuckCause(
            symbol="SPY", attributed_qty=10.0, redrive_count=3,
            first_observed_at_ms=1_700_000_000_000,
        ).to_mapping(),
        severity="error",
    )
    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is not None

    # Attributed exposure is flat (operator completed the safe flatten).
    await reconcile_account(repo, read=_FakeRead(positions=[]), trade=_FakeTrade())

    assert repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=EXIT_STUCK_REASON_CODE,
        strategy_instance_id=WATCHDOG_SID,
    ) is None
    admit_new_exposure(repo, strategy_instance_id=WATCHDOG_SID)  # must not raise
