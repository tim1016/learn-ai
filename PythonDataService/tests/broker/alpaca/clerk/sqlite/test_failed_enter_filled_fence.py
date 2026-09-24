"""The ``FAILED_ENTER_FILLED`` fence: a fill on a terminal ENTER alarms (#2348).

Clerk-level coverage of the fence the runner-level regression
(``tests/services/bot_runner/test_failed_enter_late_fill_2348.py``) proves end
to end: where it is raised, what it refuses and admits, how reconciliation
reports it, and how the operator's safe flatten closes it.

Real code: ``submit_enter``, the SQLite folds, ``SqliteTradeUpdateEvidenceSink``,
``reconcile_account``, the recovery catalog and ``execute_safe_flatten_plan``
on a temp clerk repo. Faked: the broker ports and the clock.
"""

from __future__ import annotations

import json

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import (
    AccountReconciliationResult,
    reconcile_account,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import (
    ReentrantAsyncLock,
    _instance_legacy_verdict,
    _legacy_verdict,
)
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import execute_safe_flatten_plan
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    decide_capability,
    raise_failed_enter_filled_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerRequestInvalid
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    _broker_order,
    _FakeRead,
    _FakeTrade,
    _leg,
    _NoReconciler,
    _position,
    crashed_with_exposure,  # noqa: F401 -- pytest fixture
)

QTY_ATOL = 1e-9


def _sink(repo: ClerkSqliteRepository) -> SqliteTradeUpdateEvidenceSink:
    return SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    )


async def _deliver_fill(
    repo: ClerkSqliteRepository,
    order: BrokerOrder,
    *,
    quantity: float,
    execution_id: str,
) -> None:
    await _sink(repo).record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type="fill",
            occurred_at_ms=repo.clock(),
            price=100,
            quantity=quantity,
            execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )


def _late_filled_entry(order_ref: str) -> BrokerOrder:
    return _broker_order(
        order_ref,
        status="filled",
        quantity=10.0,
        filled_quantity=10,
        filled_avg_price=100.0,
    ).model_copy(update={"order_id": "broker-late-1"})


class _DuplicateIdTrade(_FakeTrade):
    """#2304: the submit is answered 'duplicate client_order_id' though the order is live.

    ``fill_first`` delivers the order's fill on the stream before that answer
    returns -- the ordering where the fill precedes the Clerk's failed fold.
    """

    def __init__(self, repo: ClerkSqliteRepository, *, fill_first: bool) -> None:
        super().__init__()
        self._repo = repo
        self._fill_first = fill_first

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        if self._fill_first:
            await _deliver_fill(
                self._repo,
                _late_filled_entry(client_order_id),
                quantity=10,
                execution_id="exec-late-1",
            )
        raise BrokerRequestInvalid("client_order_id must be unique")


async def _failed_enter_that_filled(
    repo: ClerkSqliteRepository, *, fill_first: bool = False
) -> str:
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=10),
        trade=_DuplicateIdTrade(repo, fill_first=fill_first),
    )
    assert submission.order_ref is not None
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    if not fill_first:
        await _deliver_fill(
            repo,
            _late_filled_entry(submission.order_ref),
            quantity=10,
            execution_id="exec-late-1",
        )
    return submission.order_ref


def _fence(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=SID,
    )


def _cause_orders(episode: dict) -> list[dict]:
    return json.loads(episode["facts_json"])["cause_facts"]["orders"]


async def test_fill_on_a_failed_enter_keeps_the_position_and_raises_the_fence(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure

    order_ref = await _failed_enter_that_filled(repo)

    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    episode = _fence(repo)
    assert episode is not None
    assert episode["severity"] == "error"
    assert _cause_orders(episode) == [{"order_ref": order_ref, "symbol": "SPY"}]
    assert order_ref in episode["explanation"]


async def test_a_fill_that_precedes_the_failed_fold_still_raises_the_fence(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure

    order_ref = await _failed_enter_that_filled(repo, fill_first=True)

    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [{"order_ref": order_ref, "symbol": "SPY"}]


async def test_the_fence_refuses_entry_and_admits_only_reduction_toward_zero(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure
    await _failed_enter_that_filled(repo)

    def decide(capability: Capability, intent: ReductionIntent | None = None):
        return decide_capability(
            repo,
            capability=capability,
            strategy_instance_id=SID,
            reduction_intent=intent,
        )

    entry = decide(Capability.NEW_EXPOSURE)
    assert not entry.allowed
    assert entry.reason_code == FAILED_ENTER_FILLED_REASON_CODE
    assert decide(Capability.REDUCE, ReductionIntent("SPY", "SELL", 10)).allowed
    assert decide(Capability.REDUCE, ReductionIntent("SPY", "SELL", 4)).allowed
    for refused in (
        ReductionIntent("SPY", "BUY", 1),  # adds exposure
        ReductionIntent("SPY", "SELL", 11),  # crosses through zero
        ReductionIntent("QQQ", "SELL", 1),  # not a contradicted symbol
    ):
        decision = decide(Capability.REDUCE, refused)
        assert not decision.allowed, refused
        assert decision.reason_code == FAILED_ENTER_FILLED_REASON_CODE


async def test_a_second_contradicted_order_widens_the_open_fence(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure
    order_ref = await _failed_enter_that_filled(repo)

    raise_failed_enter_filled_uncertainty(
        repo, strategy_instance_id=SID, order_ref="aaa-other-order", symbol="qqq"
    )
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": "aaa-other-order", "symbol": "QQQ"},
        {"order_ref": order_ref, "symbol": "SPY"},
    ]

    # Re-raising an order the episode already names changes nothing.
    assert (
        raise_failed_enter_filled_uncertainty(
            repo, strategy_instance_id=SID, order_ref=order_ref, symbol="SPY"
        )
        == "unchanged"
    )


async def test_reconcile_reports_the_fence_and_the_safe_flatten_clears_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure
    order_ref = await _failed_enter_that_filled(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_flatten",
    )

    # Broker and Clerk agree (+10), yet the account is not clean.
    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
    )
    assert result.verdict == "failed_enter_filled"
    assert result.failed_enter_filled_instance_ids == (SID,)
    assert _fence(repo) is not None

    # The pass still proved broker truth, so the operator's flatten is offered
    # and the fence it exists to clear does not gate it.
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        context = reader.recovery_context(strategy_instance_id=SID)
    finally:
        reader.close()
    assert context is not None
    catalog = {item.action_id: item for item in build_recovery_catalog(context)}
    execute = catalog["execute_safe_flatten"]
    assert execute.available, execute.unavailable_reason
    assert execute.reduction_plan is not None
    trade = _FakeTrade()
    flatten = await execute_safe_flatten_plan(
        repo,
        plan=execute.reduction_plan,
        trade=trade,
        intake=ReentrantAsyncLock(),
        account_id=ACCOUNT_ID,
    )
    assert [leg.side for leg in trade.submitted_legs] == ["sell"]
    assert [leg.quantity for leg in trade.submitted_legs] == [10]

    reducing_ref = flatten.orders[0].order_ref
    await _deliver_fill(
        repo,
        _broker_order(
            reducing_ref,
            side="sell",
            status="filled",
            quantity=10.0,
            filled_quantity=10,
            filled_avg_price=100.0,
        ).model_copy(update={"order_id": f"bo-{reducing_ref}"}),
        quantity=10,
        execution_id="exec-flatten-1",
    )
    assert repo.position(SID, "SPY") == pytest.approx(0.0, abs=QTY_ATOL, rel=0)

    # Attributed-flat alone is the Clerk's own belief: only a pass whose broker
    # snapshot agrees resolves the fence.
    drifted = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )
    assert drifted.verdict == "position_drift"
    assert _fence(repo) is not None

    clean = await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade())
    assert clean.verdict == "clean"
    assert _fence(repo) is None

    # A redelivered frame for the old order is not a new fill: no re-raise.
    await _deliver_fill(
        repo, _late_filled_entry(order_ref), quantity=10, execution_id="exec-late-1"
    )
    assert _fence(repo) is None


def test_only_the_fenced_instance_reads_the_unexplained_position() -> None:
    result = AccountReconciliationResult(
        verdict="failed_enter_filled",
        failed_enter_filled_instance_ids=("fenced-bot",),
    )

    assert _legacy_verdict("failed_enter_filled") == "missing_intent"
    assert _instance_legacy_verdict(result, "fenced-bot") == "missing_intent"
    assert _instance_legacy_verdict(result, "other-bot") == "clean"
