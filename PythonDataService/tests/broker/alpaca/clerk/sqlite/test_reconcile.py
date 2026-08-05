"""Automatic reconciliation and UNKNOWN resolution tests (#1378).

Covers every acceptance criterion on the issue: automatic UNKNOWN
resolution with no new command, an unexplained/foreign order raising an
``ACCOUNT_CLERK`` hold, "Reconcile now" creating no second intent,
idempotent/non-regressing folding of duplicate and out-of-order broker
events, in-flight-order drift suppression, and a truthful stale verdict on
broker unreachability.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.reconcile import (
    ReconciliationSweep,
    plan_account_reconciliation,
    reconcile_account,
    reconcile_uncertain_order,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"


def _clock_at(start_ms: int):
    box = {"t": start_ms}

    def clock() -> int:
        return box["t"]

    def advance(delta_ms: int) -> None:
        box["t"] += delta_ms

    clock.advance = advance  # type: ignore[attr-defined]
    return clock


@pytest.fixture
def repo(tmp_path: Path):
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
        self.lookup_calls: list[str] = []
        self.cancel_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
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

    async def list_orders(self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None):
        if self._error is not None:
            raise self._error
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        if self._error is not None:
            raise self._error
        return self._positions


def _hold_transition(*, reason_code: str) -> TransitionInput:
    facts = AccountHoldRaisedFacts(reason_code=reason_code, evidence_refs=["bo-1"])
    return TransitionInput(
        transition_kind="ACCOUNT_HOLD_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code="ACCOUNT_HOLD_RAISED",
        facts_json=facts.to_facts_json(),
    )


async def _make_uncertain_order(repo: ClerkSqliteRepository, *, decision_id: str = "d1") -> str:
    """Drive a real ENTER through a lost submit response so the effect
    operation lands (and stays) in ``unknown`` — grace has not elapsed."""
    submit_trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"), lookup_absent=True)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
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


# ── reconcile_uncertain_order ────────────────────────────────────────────────


async def test_reconcile_uncertain_order_resolves_to_resolved_success(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    outcome = await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade()
    )
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


async def test_reconcile_uncertain_order_is_a_noop_on_an_already_resolved_order(
    repo: ClerkSqliteRepository,
) -> None:
    """'Reconcile now' on an operation that already finished creates no
    second intent (#1378 acceptance)."""
    order_ref = await _make_uncertain_order(repo)
    await reconcile_uncertain_order(repo, order_ref=order_ref, trigger="AUTOMATIC", trade=_FakeTrade())
    before = len(repo.custody_transitions())

    outcome = await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="OPERATOR_RECONCILE_NOW", trade=_FakeTrade()
    )
    assert outcome == "RESOLVED_SUCCESS"
    assert len(repo.custody_transitions()) == before  # no new transition appended


async def test_reconcile_uncertain_order_records_reconciliations_row(repo: ClerkSqliteRepository) -> None:
    order_ref = await _make_uncertain_order(repo)
    await reconcile_uncertain_order(
        repo, order_ref=order_ref, trigger="OPERATOR_RECONCILE_NOW", trade=_FakeTrade()
    )
    transitions = repo.transitions_for_order(order_ref)
    reconciliation_rows = [t for t in transitions if t["transition_kind"] == "RECONCILIATION_ATTEMPTED"]
    assert len(reconciliation_rows) == 1


async def test_reconcile_uncertain_order_delegates_an_exit_owned_entry_to_resolve_exit(
    repo: ClerkSqliteRepository,
) -> None:
    """An entry order reassigned to an EXIT and stuck in cancel-uncertainty
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
        repo, effect_operation_id=accepted.effect_operation_id,
        trade=_FakeTrade(cancel_error=BrokerUnavailable("timeout")),
    )
    effect_stuck = repo.effect_operation(accepted.effect_operation_id)
    assert effect_stuck is not None and effect_stuck.state == "unknown"

    resolving_trade = _FakeTrade(lookup_result=_broker_order(entry_ref, status="canceled", filled_quantity=0.0))
    outcome = await reconcile_uncertain_order(
        repo, order_ref=entry_ref, trigger="AUTOMATIC", trade=resolving_trade
    )

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


async def test_reconcile_account_position_drift_uncertainty_is_idempotent(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(orders=[], positions=[_position("SPY", quantity=5)])
    await reconcile_account(repo, read=read, trade=_FakeTrade())
    before = len(repo.custody_transitions())

    await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert len(repo.custody_transitions()) == before  # still one ACTIVE uncertainty, no second raise


async def test_reconcile_account_hold_raise_is_idempotent_across_repeated_passes(
    repo: ClerkSqliteRepository,
) -> None:
    foreign = _broker_order("manual/someone/v1:xyz", order_id="bo-foreign-1")
    read = _FakeRead(orders=[foreign])
    await reconcile_account(repo, read=read, trade=_FakeTrade())
    before = len(repo.custody_transitions())

    await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert len(repo.custody_transitions()) == before  # still one ACTIVE hold, no second raise


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

    def paused_append_transition(self: ClerkSqliteRepository, transition):
        result = original_append_transition(self, transition)
        thread_a_appended.set()
        release_thread_a.wait(timeout=5)
        return result

    def build_transition():
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


async def test_reconcile_account_reports_stale_on_broker_read_failure_and_writes_nothing(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(error=BrokerUnavailable("down"))
    before = len(repo.custody_transitions())

    result = await reconcile_account(repo, read=read, trade=_FakeTrade())
    assert result.verdict == "stale"
    assert len(repo.custody_transitions()) == before


async def test_reconcile_account_resolves_every_uncertain_order_in_one_pass(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref_1 = await _make_uncertain_order(repo, decision_id="d1")
    order_ref_2 = await _make_uncertain_order(repo, decision_id="d2")
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
    free_ref = await _make_uncertain_order(repo, decision_id="d2")
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


async def test_reconcile_account_operator_reconcile_now_trigger_is_recorded(
    repo: ClerkSqliteRepository,
) -> None:
    order_ref = await _make_uncertain_order(repo)
    read = _FakeRead(orders=[], positions=[])

    await reconcile_account(repo, read=read, trade=_FakeTrade(), trigger="OPERATOR_RECONCILE_NOW")
    transitions = repo.transitions_for_order(order_ref)
    reconciliation_facts = next(
        t for t in transitions if t["transition_kind"] == "RECONCILIATION_ATTEMPTED"
    )
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

    sweep = ReconciliationSweep(
        repo=repo, read=read, trade=_FakeTrade(), sleep=fake_sleep, max_passes=3
    )
    await sweep.run()

    assert len(sleep_calls) == 2  # sleeps between passes, not after the last
    assert repo.order(order_ref).broker_order_id is not None  # type: ignore[union-attr]


async def test_sweep_survives_a_broker_error_and_continues_to_the_next_pass(
    repo: ClerkSqliteRepository,
) -> None:
    read = _FakeRead(error=BrokerUnavailable("down"))
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1

    sweep = ReconciliationSweep(
        repo=repo, read=read, trade=_FakeTrade(), sleep=fake_sleep, max_passes=3
    )
    await sweep.run()  # must not raise despite every pass hitting BrokerUnavailable
    assert sleeps == 2  # 3 passes, sleeping only between them
