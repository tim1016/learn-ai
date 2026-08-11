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
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

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
from app.broker.alpaca.clerk.sqlite.models import CommittedTransition, TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import (
    plan_account_reconciliation,
    reconcile_account,
    reconcile_uncertain_order,
)
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError, raise_uncertainty
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition
from conftest import _clock_at, _hold_transition

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
    filled_quantity: float = 0.0,
    filled_avg_price: float | None = None,
) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        asset_class="us_equity",
        side="buy",
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


def test_plan_suppresses_drift_for_a_symbol_with_a_non_terminal_in_flight_order() -> None:
    """#1378 acceptance: a symbol with a non-terminal in-flight order is not
    flagged as drift — the fill/ack for it just hasn't landed yet."""
    working_order = _broker_order(_our_order_ref(), status="partially_filled")
    plan = plan_account_reconciliation(
        namespaces=_namespaces(),
        broker_orders=[working_order],
        broker_positions=[_position("SPY", quantity=5)],
        attributed_positions={"SPY": 3.0},
    )
    assert plan.verdict == "clean"
    assert plan.indeterminate_symbols == ("SPY",)


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


# ── reconcile_uncertain_order ────────────────────────────────────────────────


async def test_reconcile_uncertain_order_resolves_to_resolved_success(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    outcome = await reconcile_uncertain_order(repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade())
    assert outcome == "RESOLVED_SUCCESS"
    order = repo.order(order_ref)
    assert order is not None and order.broker_order_id is not None


async def test_reconcile_uncertain_order_resolves_to_resolved_failure_past_grace(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    repo.clock.advance(30_001)  # type: ignore[attr-defined]
    outcome = await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade(lookup_absent=True)
    )
    assert outcome == "RESOLVED_FAILURE"
    effect = repo.effect_operation(repo.order(order_ref).effect_operation_id)  # type: ignore[union-attr]
    assert effect is not None and effect.state == "failed"


async def test_reconcile_uncertain_order_stays_still_unknown_within_grace(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    outcome = await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade(lookup_absent=True)
    )
    assert outcome == "STILL_UNKNOWN"
    effect = repo.effect_operation(repo.order(order_ref).effect_operation_id)  # type: ignore[union-attr]
    assert effect is not None and effect.state == "unknown"


async def test_reconcile_uncertain_order_stays_still_unknown_on_a_broker_lookup_error(
    repo: ClerkSqliteRepository,
) -> None:
    """Never fabricate a terminal outcome on a broker error (#1378 acceptance,
    order-level slice of the account-wide 'truthful stale verdict' rule)."""
    order_ref = await _make_uncertain_order(repo)
    outcome = await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade(lookup_error=BrokerUnavailable("down"))
    )
    assert outcome == "STILL_UNKNOWN"


async def test_reconcile_now_refreshes_resolved_order_without_second_intent(
    repo: ClerkSqliteRepository,
) -> None:
    """'Reconcile now' on an operation that already finished creates no
    second intent (#1378 acceptance)."""
    order_ref = await _make_uncertain_order(repo)
    await reconcile_uncertain_order(repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade())
    before = len(repo.custody_transitions())

    trade = _FakeTrade()
    outcome = await reconcile_uncertain_order(repo, order_ref=order_ref, trigger="OPERATOR_RECONCILE_NOW", trade=trade)
    assert outcome == "RESOLVED_SUCCESS"
    assert trade.submit_calls == []
    assert len(repo.custody_transitions()) == before + 1  # audit attempt only


async def test_reconcile_uncertain_order_records_reconciliations_row(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    await reconcile_uncertain_order(repo, order_ref=order_ref, trigger="OPERATOR_RECONCILE_NOW", trade=_FakeTrade())
    transitions = repo.transitions_for_order(order_ref)
    reconciliation_rows = [t for t in transitions if t["transition_kind"] == "RECONCILIATION_ATTEMPTED"]
    assert len(reconciliation_rows) == 1


async def test_reconcile_uncertain_order_delegates_an_exit_owned_entry_to_resolve_exit(
    repo: ClerkSqliteRepository,
) -> None:
    """An entry order linked to an EXIT and stuck in cancel-uncertainty
    must NOT be resolved via the ENTER-style 'already has a broker_order_id'
    short-circuit — that field was set by the original ENTER submission long
    before EXIT began and proves nothing about whether EXIT's cancel
    resolved. Before this dispatch existed, reconcile_uncertain_order
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
    outcome = await reconcile_uncertain_order(repo, order_ref=entry_ref, trigger="AUTOMATIC", trade=resolving_trade)

    # A genuine broker call happened (impossible under the old short-circuit,
    # which returned before ever calling the trade port).
    assert resolving_trade.cancel_calls or resolving_trade.lookup_calls
    assert outcome == "RESOLVED_SUCCESS"
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
