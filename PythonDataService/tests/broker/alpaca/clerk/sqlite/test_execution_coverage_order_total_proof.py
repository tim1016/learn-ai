"""The order-level execution-coverage proof: a lost exact slice never blocks an EXIT for good (#2346).

A cumulative REST fill and websocket exact slices describe the same broker
executions. When an exact slice Alpaca never re-sends leaves the set proof
incomplete, the broker's final order total still proves every quarantined
exact is inside the recorded position, so the ``EXECUTION_COVERAGE_CONFLICT``
episode closes without moving a fill. A broker total that disagrees with the
recorded fills, a still-working order, and a changed redelivery of an
execution ID stay fail-closed.

Real code: the SQLite repository and folds, ``SqliteTradeUpdateEvidenceSink``,
``fold_order_evidence``, the EXIT machine. Faked: the broker trade port.
Probes adapted from the #2346 issue body (G3) and its #2360 comment (S2).
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.exit import ExitSubmission, accept_exit, resolve_accepted_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import resolve_exit
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    ReductionIntent,
    decide_capability,
)
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrderEvent
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
    _NoReconciler,
    repo,  # noqa: F401 -- pytest fixture
)

QTY_ATOL = 1e-9


async def _ws_fill(
    clerk: ClerkSqliteRepository,
    order_ref: str,
    *,
    execution_id: str,
    qty: float,
    price: float,
    cumulative: float,
    avg: float,
    status: str,
) -> None:
    await SqliteTradeUpdateEvidenceSink(
        repo=clerk, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    ).record_lifecycle_event(
        client_order_id=order_ref,
        event=BrokerOrderEvent(
            event_type="fill" if status == "filled" else "partial_fill",
            occurred_at_ms=1_700_000_001_000,
            price=price,
            quantity=qty,
            execution_id=execution_id,
        ),
        event_key=f"exec:{execution_id}",
        order=_broker_order(
            order_ref, status=status, filled_quantity=cumulative, filled_avg_price=avg
        ),
        recovery_source=None,
        recovery_window_limit=None,
    )


def _rest(clerk: ClerkSqliteRepository, order_ref: str, *, cumulative: float, avg: float, status: str) -> None:
    order = clerk.order(order_ref)
    assert order is not None
    fold_order_evidence(
        clerk,
        effect_operation_id=order.effect_operation_id,
        order=_broker_order(order_ref, status=status, filled_quantity=cumulative, filled_avg_price=avg),
    )


def _conflicts(clerk: ClerkSqliteRepository) -> list[dict]:
    return [
        u
        for u in clerk.active_uncertainties_for_admission(strategy_instance_id=SID)
        if u["reason_code"] == "EXECUTION_COVERAGE_CONFLICT"
    ]


def _reduce_allowed(clerk: ClerkSqliteRepository, quantity: float) -> bool:
    return decide_capability(
        clerk,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=quantity),
    ).allowed


def _accept_exit(clerk: ClerkSqliteRepository, order_ref: str) -> ExitSubmission:
    return accept_exit(
        clerk,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=order_ref,
    )


async def test_rest_recorded_fill_then_live_exact_lets_the_exit_reduce(
    repo: ClerkSqliteRepository,  # noqa: F811
) -> None:
    """G3: exec-A (3) missed in an outage, exec-B (7) live, then REST folds cumulative 10."""
    order_ref = await _make_entry(repo, filled_quantity=3.0, status="partially_filled")
    await _ws_fill(
        repo, order_ref, execution_id="exec-B", qty=7.0, price=101.0,
        cumulative=10.0, avg=100.7, status="filled",
    )
    # Before REST confirms the order's total, the exact slice is unexplained.
    assert len(_conflicts(repo)) == 1
    assert not _reduce_allowed(repo, 3.0)

    _rest(repo, order_ref, cumulative=10.0, avg=100.7, status="filled")

    assert _conflicts(repo) == []
    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    assert [(f["qty"], f["evidence_source"]) for f in repo.fills_for_order(order_ref)] == [
        (3.0, "cumulative_recovery"),
        (7.0, "cumulative_recovery"),
    ]
    # The only exact Alpaca will ever send again is a redelivery: no new episode.
    await _ws_fill(
        repo, order_ref, execution_id="exec-B", qty=7.0, price=101.0,
        cumulative=10.0, avg=100.7, status="filled",
    )
    assert _conflicts(repo) == []

    accepted = _accept_exit(repo, order_ref)
    filled = _broker_order(order_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.7)
    trade = _FakeTrade(lookup_results=[filled] * 5)
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert [(str(leg.side), leg.quantity) for leg, _ in trade.submit_calls] == [("sell", 10.0)]


async def test_late_exact_between_exit_passes_does_not_block_the_reduction(
    repo: ClerkSqliteRepository,  # noqa: F811
) -> None:
    """S2: the EXIT's cancel proof folds cumulative 5 first; exec-A (2) lands late, exec-B (3) never."""
    order_ref = await _make_entry(repo, quantity=10, status="accepted")
    accepted = _accept_exit(repo, order_ref)
    pending = _broker_order(order_ref, status="pending_cancel", filled_quantity=5.0, filled_avg_price=100.6)
    await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=_FakeTrade(lookup_results=[pending])
    )
    await _ws_fill(
        repo, order_ref, execution_id="exec-A", qty=2.0, price=100.0,
        cumulative=2.0, avg=100.0, status="partially_filled",
    )
    assert len(_conflicts(repo)) == 1

    canceled = _broker_order(order_ref, status="canceled", filled_quantity=5.0, filled_avg_price=100.6)
    trade = _FakeTrade(lookup_results=[canceled] * 3)
    await resolve_accepted_exit(repo, accepted=accepted, trade=trade)

    assert [(str(leg.side), leg.quantity) for leg, _ in trade.submit_calls] == [("sell", 5.0)]
    assert _conflicts(repo) == []
    assert repo.position(SID, "SPY") == pytest.approx(5.0, abs=QTY_ATOL, rel=0)


async def test_exacts_beyond_the_broker_final_total_stay_blocked(
    repo: ClerkSqliteRepository,  # noqa: F811
) -> None:
    """A genuine conflict: the broker's final total cannot hold the quarantined exact."""
    order_ref = await _make_entry(repo, filled_quantity=3.0, status="filled")
    await _ws_fill(
        repo, order_ref, execution_id="exec-B", qty=7.0, price=101.0,
        cumulative=10.0, avg=100.7, status="filled",
    )
    _rest(repo, order_ref, cumulative=3.0, avg=100.0, status="filled")

    assert len(_conflicts(repo)) == 1
    assert repo.resolve_order_total_covered_coverage_conflicts() == 0
    assert not _reduce_allowed(repo, 3.0)


async def test_a_working_order_total_is_not_final_and_stays_blocked(
    repo: ClerkSqliteRepository,  # noqa: F811
) -> None:
    """A partially filled order's cumulative may still grow past the recorded fills."""
    order_ref = await _make_entry(repo, filled_quantity=5.0, status="partially_filled")
    await _ws_fill(
        repo, order_ref, execution_id="exec-A", qty=2.0, price=100.0,
        cumulative=2.0, avg=100.0, status="partially_filled",
    )
    _rest(repo, order_ref, cumulative=5.0, avg=100.0, status="partially_filled")

    assert len(_conflicts(repo)) == 1
    assert not _reduce_allowed(repo, 5.0)


async def test_changed_redelivery_of_an_effective_execution_stays_blocked(
    repo: ClerkSqliteRepository,  # noqa: F811
) -> None:
    """One execution ID with two economics is never explained by a matching order total."""
    order_ref = await _make_entry(repo, quantity=10, status="accepted")
    await _ws_fill(
        repo, order_ref, execution_id="exec-A", qty=10.0, price=100.0,
        cumulative=10.0, avg=100.0, status="filled",
    )
    await _ws_fill(
        repo, order_ref, execution_id="exec-A", qty=10.0, price=100.5,
        cumulative=10.0, avg=100.5, status="filled",
    )
    assert len(_conflicts(repo)) == 1

    _rest(repo, order_ref, cumulative=10.0, avg=100.0, status="filled")

    assert len(_conflicts(repo)) == 1
    accepted = _accept_exit(repo, order_ref)
    filled = _broker_order(order_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.0)
    trade = _FakeTrade(lookup_results=[filled] * 5)
    with pytest.raises(AdmissionBlockedError) as blocked:
        await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    assert blocked.value.decision.reason_code == "EXECUTION_COVERAGE_CONFLICT"
    assert trade.submit_calls == []
