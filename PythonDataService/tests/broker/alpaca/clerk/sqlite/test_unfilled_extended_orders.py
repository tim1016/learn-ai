"""ADR 0059 D5.4 — a terminal reducing order with no recorded execution is
proven unfilled, not an endless ``unknown`` (#slice-3 task 7).

Three pins:

1. An ENTER the vendor cancels/expires unfilled leaves no exposure and does
   not block admission — machinery that already existed before this task.
2. An EXIT's reducing order the vendor cancels/expires unfilled must become
   an ``EXIT_NOT_FLAT`` uncertainty immediately, not park the effect
   ``unknown`` forever awaiting an execution slice that will never arrive
   (the regression this task fixes in ``exit_resolution._resolve_claimed``).
3. The next EXIT decision, once accepted, re-issues at a new (extended-hours)
   anchor rather than resubmitting the dead reducing order.
"""

from __future__ import annotations

import json

import pytest

from app.broker.alpaca.clerk.program_leg import LegShape
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    decide_capability,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
    repo,  # noqa: F401 — the shared EXIT-machine repository fixture
)

_XH_LEG = BrokerOrderLeg(
    symbol="SPY",
    side="buy",
    quantity=10,
    order_type="limit",
    limit_price=100.10,
    extended_hours=True,
)


async def test_enter_cancelled_by_the_vendor_unfilled_leaves_no_exposure(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """An extended-hours ENTER the vendor expires with zero fill must leave
    the strategy flat and admissible again — no lingering exposure, no
    lingering admission block.

    ENTER's own module deliberately never drives its effect operation to a
    terminal ``state`` (``enter.py``'s docstring: that's EXIT/reconciliation
    territory) — so the invariant this test actually pins is not "the effect
    row reads a terminal state" but the two things that matter operationally:
    zero attributed exposure, and a fresh ENTER decision is not blocked by
    what happened to this one. See the test's assertions and the task
    report for the empirical effect-state finding.
    """
    trade = _FakeTrade(
        submit_result=_broker_order("placeholder", status="accepted", filled_quantity=0.0)
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_XH_LEG,
        trade=trade,
    )
    assert submission.effect_operation_id is not None and submission.order_ref is not None

    # A later poll/reconcile observation: the vendor expired the order with
    # no fill at all.
    fold_order_evidence(
        repo,
        effect_operation_id=submission.effect_operation_id,
        order=_broker_order(submission.order_ref, status="expired", filled_quantity=0.0),
    )

    assert repo.position(SID, "SPY") == 0
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "expired"

    # No strategy-scoped admission uncertainty was raised for this.
    assert repo.active_uncertainties_for_admission(strategy_instance_id=SID) == []

    # The strongest form of "leaves no exposure": a fresh ENTER decision is
    # not blocked by the dead one.
    assert decide_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=SID,
    ).allowed


async def test_exit_reducing_order_cancelled_unfilled_is_an_uncertainty_immediately(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The regression this task fixes: before it, a terminal reducing order
    with no recorded execution was parked ``unknown`` forever regardless of
    *why* no execution existed — indistinguishable from "a filled/replaced
    snapshot whose execution slice just hasn't reached the websocket yet".
    A vendor cancel/expire/reject with zero fill is proven unfilled and must
    become an EXIT_NOT_FLAT uncertainty right away.
    """
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    ack_trade = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=ack_trade)
    assert first.reducing_order_ref is not None

    cancel_trade = _FakeTrade(
        lookup_results=[
            _broker_order(first.reducing_order_ref, side="sell", status="canceled", filled_quantity=0.0)
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=cancel_trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None
    assert effect.state == "failed"
    assert effect.state != "unknown"  # the regression: this used to park forever

    uncertainty = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code="EXIT_NOT_FLAT",
        strategy_instance_id=SID,
    )
    assert uncertainty is not None
    evidence_refs = json.loads(uncertainty["evidence_refs_json"])
    assert first.reducing_order_ref in evidence_refs


async def test_next_exit_decision_reissues_at_the_new_anchor(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """After (2)'s uncertainty, the next EXIT decision — once accepted —
    submits a fresh reducing order at the new, decision-supplied anchor. It
    never resubmits the dead one."""
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    ack_trade = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=ack_trade)
    assert first.reducing_order_ref is not None
    cancel_trade = _FakeTrade(
        lookup_results=[
            _broker_order(first.reducing_order_ref, side="sell", status="canceled", filled_quantity=0.0)
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=cancel_trade)
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"

    new_anchor = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=99.50,
        extended_hours=True,
        side=OrderSide.SELL,
    )
    try:
        second_accepted = accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-2",
            lifecycle_run_id=RUN_ID,
            entry_order_ref=entry_ref,
            reducing_shape=new_anchor,
        )
    except AdmissionBlockedError as exc:
        pytest.fail(
            "accept_exit for the next EXIT decision was refused by "
            f"{exc.decision.reason_code!r} while the EXIT_NOT_FLAT uncertainty stood "
            "— record this policy in the task report instead of forcing the accept."
        )
    assert second_accepted.effect_operation_id is not None
    assert second_accepted.effect_operation_id != accepted.effect_operation_id

    second_trade = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))
    second = await resolve_exit(
        repo,
        effect_operation_id=second_accepted.effect_operation_id,
        trade=second_trade,
    )

    assert second.reducing_order_ref is not None
    assert second.reducing_order_ref != first.reducing_order_ref
    ((leg, _client_order_id),) = second_trade.submit_calls
    assert leg.limit_price == 99.50
    assert leg.order_type is OrderType.LIMIT
    assert leg.extended_hours is True
