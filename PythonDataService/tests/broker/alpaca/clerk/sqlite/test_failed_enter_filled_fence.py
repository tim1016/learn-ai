"""The ``FAILED_ENTER_FILLED`` fence: a fill on a terminal ENTER alarms (#2348).

Clerk-level coverage of the fence the runner-level regression
(``tests/services/bot_runner/test_failed_enter_late_fill_2348.py``) proves end
to end: where it is raised, what it refuses and admits, how reconciliation
reports it, and how the operator's safe flatten closes it.

Real code: ``submit_enter``, the SQLite folds, ``SqliteTradeUpdateEvidenceSink``,
``reconcile_account``, the facade's custody proof, the recovery catalog and
``execute_safe_flatten_plan`` on a temp clerk repo. Faked: the broker ports and
the clock.
"""

from __future__ import annotations

import json

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.execution_coverage import ORDER_TOTAL_PROVEN_SUMMARY_CODE
from app.broker.alpaca.clerk.sqlite.order_evidence import fence_fills_on_terminal_enters
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import (
    MAX_TERMINAL_ENTER_LOOKUPS,
    TERMINAL_ENTER_LOOKUP_RECHECK_MS,
    reconcile_account,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import (
    ReentrantAsyncLock,
    SqliteAlpacaClerkFacade,
)
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import execute_safe_flatten_plan
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    Capability,
    ReductionIntent,
    decide_capability,
    raise_failed_enter_filled_uncertainty,
    resolve_failed_enter_filled_uncertainty_if_flat,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerRequestInvalid
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.services.run_admission import evaluate_run_admission
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
from tests.services.test_run_admission import _NOW, _bot

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


def _late_filled_entry(order_ref: str, *, filled_quantity: int = 10) -> BrokerOrder:
    return _broker_order(
        order_ref,
        status="filled",
        quantity=float(filled_quantity),
        filled_quantity=filled_quantity,
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
    assert _cause_orders(episode) == [
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0}
    ]
    assert order_ref in episode["explanation"]


async def _sweep(repo: ClerkSqliteRepository, *, broker_spy: float = 10.0):
    positions = [_position("SPY", quantity=broker_spy)] if broker_spy else []
    return await reconcile_account(
        repo, read=_FakeRead(positions=positions), trade=_FakeTrade(),
        pricing=UNPRICEABLE_RECOVERY,
    )


async def test_a_fill_that_precedes_the_failed_fold_is_fenced_by_the_next_pass(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure

    order_ref = await _failed_enter_that_filled(repo, fill_first=True)
    # The fill folded while the ENTER was still live; the failed fold that
    # followed contradicts it, and the canonical detector is the sweep.
    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)

    await _sweep(repo)

    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0}
    ]


async def test_a_fence_lost_to_a_crash_after_the_fill_is_raised_by_the_next_sweep(
    crashed_with_exposure,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash window between the fill's commit and the fence's own (#2348).

    The fill commits, then the process dies before the episode is written:
    here the raise throws once, exactly where a crash would stop it. The slice
    is durable and deduplicated, so a redelivered frame is not "new"; only a
    detector that re-derives from durable facts can see the contradiction.
    """
    repo, _clock = crashed_with_exposure
    import app.broker.alpaca.clerk.sqlite.order_evidence as order_evidence

    real_raise = order_evidence.raise_failed_enter_filled_uncertainty
    crashes: list[str] = []

    def crash_once(*args, **kwargs):
        if not crashes:
            crashes.append(kwargs["order_ref"])
            raise RuntimeError("process died between the fill and the fence")
        return real_raise(*args, **kwargs)

    monkeypatch.setattr(order_evidence, "raise_failed_enter_filled_uncertainty", crash_once)
    with pytest.raises(RuntimeError, match="process died"):
        await _failed_enter_that_filled(repo)
    order_ref = crashes[0]
    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    assert _fence(repo) is None

    # The redelivered frame deduplicates, and the sweep is still clean
    # (broker and journal agree), yet the fence is raised.
    await _deliver_fill(
        repo, _late_filled_entry(order_ref), quantity=10, execution_id="exec-late-1"
    )
    result = await _sweep(repo)

    assert result.verdict == "clean"
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0}
    ]


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
        repo,
        strategy_instance_id=SID,
        order_ref="aaa-other-order",
        symbol="qqq",
        filled_qty=3.0,
    )
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": "aaa-other-order", "symbol": "QQQ", "filled_qty": 3.0},
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0},
    ]

    # Re-raising an order the episode already names changes nothing.
    assert (
        raise_failed_enter_filled_uncertainty(
            repo,
            strategy_instance_id=SID,
            order_ref=order_ref,
            symbol="SPY",
            filled_qty=10.0,
        )
        == "unchanged"
    )


def _clear_fence_as_if_flattened(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the open fence through its real resolver, as a proven flatten would."""
    with monkeypatch.context() as flat:
        flat.setattr(repo, "position", lambda *_args: 0.0)
        assert resolve_failed_enter_filled_uncertainty_if_flat(
            repo, strategy_instance_id=SID, evidence_refs=("test_flatten",)
        )
    assert _fence(repo) is None


async def test_a_correction_back_to_an_older_answered_quantity_raises_a_new_fence(
    crashed_with_exposure,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrections 10 -> 5 -> 10: only the NEWEST episode's quantity is an answer.

    The first episode answered 10 and a flatten cleared it; a correction to 5
    was fenced and flattened again. Back at 10, the order carries 5 shares of
    exposure the latest answer never covered -- the older 10-share episode
    must not suppress the fence. The frozen clock also ties every episode's
    ``observed_at_ms``, so "newest" must come from the raise order.
    """
    repo, _clock = crashed_with_exposure
    order_ref = await _failed_enter_that_filled(repo)
    _clear_fence_as_if_flattened(repo, monkeypatch)
    raise_failed_enter_filled_uncertainty(
        repo, strategy_instance_id=SID, order_ref=order_ref, symbol="SPY", filled_qty=5.0
    )
    _clear_fence_as_if_flattened(repo, monkeypatch)

    # The order's effective fills stand at 10 again.
    fence_fills_on_terminal_enters(repo, order_ref=order_ref)

    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0}
    ]


async def test_reconcile_reports_the_fence_and_the_safe_flatten_clears_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure
    order_ref = await _failed_enter_that_filled(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_flatten",
    )

    # Broker and Clerk agree (+10): the account verdict is clean, and the
    # fence -- not the verdict -- carries the contradiction.
    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
        trigger="OPERATOR_RECONCILE_NOW",
        pricing=UNPRICEABLE_RECOVERY,
    )
    assert result.verdict == "clean"
    assert _fence(repo) is not None

    # The pass proved broker truth, so the operator's flatten is offered and
    # the fence it exists to clear does not gate it.
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=repo.clock, pricing=UNPRICEABLE_RECOVERY
    )
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
        pricing=UNPRICEABLE_RECOVERY,
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
        pricing=UNPRICEABLE_RECOVERY,
    )
    assert drifted.verdict == "position_drift"
    assert _fence(repo) is not None

    clean = await reconcile_account(repo, read=_FakeRead(), trade=_FakeTrade(), pricing=UNPRICEABLE_RECOVERY)
    assert clean.verdict == "clean"
    assert _fence(repo) is None

    # A redelivered frame for the old order is not a new fill, and the sweep
    # sees the fill total the resolved episode already answered: no re-raise.
    await _deliver_fill(
        repo, _late_filled_entry(order_ref), quantity=10, execution_id="exec-late-1"
    )
    assert (await _sweep(repo, broker_spy=0.0)).verdict == "clean"
    assert _fence(repo) is None

    # A genuinely new fill on the same failed order is a new contradiction.
    await _deliver_fill(
        repo,
        _late_filled_entry(order_ref, filled_quantity=11),
        quantity=1,
        execution_id="exec-late-2",
    )
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": order_ref, "symbol": "SPY", "filled_qty": 11.0}
    ]


async def test_the_fenced_bot_alone_is_frozen_with_its_exposure_still_proven(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, _clock = crashed_with_exposure
    repo.register_strategy_instance(strategy_instance_id="other-bot", symbol="QQQ", config_hash="h2")
    order_ref = await _failed_enter_that_filled(repo)
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_FakeTrade(),
    )

    # The account-level answer is clean: broker and journal agree.
    assert await facade.reconcile_once() == "clean"

    proof = await facade.prove_instance_custody(SID)
    assert proof.reconciliation_verdict == "clean"
    assert proof.exposure == {"SPY": pytest.approx(10.0, abs=QTY_ATOL, rel=0)}
    assert proof.freeze.active
    assert order_ref in (proof.freeze.explanation or "")
    assert "flatten" in (proof.freeze.next_step or "")

    other = await facade.prove_instance_custody("other-bot")
    assert other.reconciliation_verdict == "clean"
    assert not other.freeze.active

    # The custody snapshot keeps the +10 SPY known, and Start is refused on
    # the fence's own freeze -- before the generic flat-custody rule.
    snapshot = await facade.custody_snapshot(SID)
    assert snapshot.reconciliation_state == "clean"
    assert snapshot.exposure.state == "non_zero"
    assert snapshot.exposure.positions == {"SPY": pytest.approx(10.0, abs=QTY_ATOL, rel=0)}
    bot = _bot()
    decision = evaluate_run_admission(
        bot,
        snapshot.model_copy(
            update={
                "strategy_instance_id": bot.strategy_instance_id,
                "account_id": bot.sealed_account_id,
                "observed_at_ms": _NOW - 500,
            }
        ),
        evaluated_at_ms=_NOW,
    )
    assert not decision.allowed
    assert decision.explanation == proof.freeze.explanation


class _LookupTrade(_FakeTrade):
    """Exact lookups answer from ``landed``; every other ref never reached the broker."""

    def __init__(self, landed: dict[str, BrokerOrder]) -> None:
        super().__init__()
        self._landed = landed

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        return self._landed.get(client_order_id)


async def _failed_enter_never_filled(
    repo: ClerkSqliteRepository, clock, *, decision_id: str
) -> str:
    """A duplicate-id-refused ENTER (#2304) whose fill is never delivered."""
    clock.advance(1_000)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=10),
        trade=_DuplicateIdTrade(repo, fill_first=False),
    )
    assert submission.order_ref is not None
    return submission.order_ref


async def test_never_landed_voids_cannot_starve_the_lookup_of_an_older_filled_one(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """CodeRabbit #2378 review: a "not found" answer leaves ``broker_state``
    NULL, so newest-first lookups spent every slot on the same never-landed
    voids and an older void that did land and fill was never looked up."""
    repo, clock = crashed_with_exposure
    filled_ref = await _failed_enter_never_filled(repo, clock, decision_id="enter-old")
    for index in range(MAX_TERMINAL_ENTER_LOOKUPS + 1):
        await _failed_enter_never_filled(repo, clock, decision_id=f"enter-new-{index}")
    trade = _LookupTrade({filled_ref: _late_filled_entry(filled_ref)})
    read = _FakeRead(positions=[_position("SPY", quantity=10.0)])

    for _ in range(2):
        clock.advance(15_000)
        await reconcile_account(repo, read=read, trade=trade, pricing=UNPRICEABLE_RECOVERY)
        if _fence(repo) is not None:
            break

    assert filled_ref in trade.lookup_calls
    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    episode = _fence(repo)
    assert episode is not None
    assert _cause_orders(episode) == [
        {"order_ref": filled_ref, "symbol": "SPY", "filled_qty": 10.0}
    ]


async def test_an_answered_lookup_rests_until_the_recheck_window_passes(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A void the broker said it never saw is not asked about every pass, and
    not dropped for good either: the abandoned POST (#2342) can still land."""
    repo, clock = crashed_with_exposure
    order_ref = await _failed_enter_never_filled(repo, clock, decision_id="enter-1")
    trade = _LookupTrade({})
    read = _FakeRead(positions=[_position("SPY", quantity=10.0)])

    await reconcile_account(repo, read=read, trade=trade, pricing=UNPRICEABLE_RECOVERY)
    clock.advance(TERMINAL_ENTER_LOOKUP_RECHECK_MS - 1)
    await reconcile_account(repo, read=read, trade=trade, pricing=UNPRICEABLE_RECOVERY)
    assert trade.lookup_calls == [order_ref]

    clock.advance(1)
    await reconcile_account(repo, read=read, trade=trade, pricing=UNPRICEABLE_RECOVERY)
    assert trade.lookup_calls == [order_ref, order_ref]


async def test_an_order_in_the_open_snapshot_spends_no_lookup_slot(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Snapshot refs are dropped before the budget, not after it."""
    repo, clock = crashed_with_exposure
    filled_ref = await _failed_enter_never_filled(repo, clock, decision_id="enter-old")
    open_refs = [
        await _failed_enter_never_filled(repo, clock, decision_id=f"enter-open-{index}")
        for index in range(MAX_TERMINAL_ENTER_LOOKUPS)
    ]
    trade = _LookupTrade({filled_ref: _late_filled_entry(filled_ref)})
    read = _FakeRead(
        orders=[
            _broker_order(ref).model_copy(update={"order_id": f"bo-{ref}"}) for ref in open_refs
        ],
        positions=[_position("SPY", quantity=10.0)],
    )

    await reconcile_account(repo, read=read, trade=trade, pricing=UNPRICEABLE_RECOVERY)

    assert trade.lookup_calls == [filled_ref]


def _coverage_conflicts(repo: ClerkSqliteRepository) -> list[dict]:
    return [
        episode
        for episode in repo.active_uncertainties_for_admission(strategy_instance_id=SID)
        if episode["reason_code"] == "EXECUTION_COVERAGE_CONFLICT"
    ]


async def test_proving_a_coverage_conflict_leaves_the_failed_enter_fence_standing(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """#2346 x #2348: a failed ENTER for 12 fills 10 across a websocket outage.

    exec-A (3) is lost in the outage; the sweep's terminal-ENTER lookup
    recovers it as a cumulative fill and raises the fence. exec-B (7) then
    arrives live as an exact slice the recorded fills cannot explain, raising
    an ``EXECUTION_COVERAGE_CONFLICT``. The remainder is canceled, and the next
    lookup folds the broker's final total (10), which proves the conflict and
    closes it. That proof resolves only its own episode: the fence stays up,
    widened to the full 10 by the same pass's detector.
    """
    repo, clock = crashed_with_exposure
    order_ref = await _failed_enter_never_filled(repo, clock, decision_id="enter-outage")

    def broker_view(status: str, filled: int, avg: float) -> BrokerOrder:
        return _broker_order(
            order_ref, status=status, quantity=12.0, filled_quantity=filled, filled_avg_price=avg
        ).model_copy(update={"order_id": "broker-late-1"})

    await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=3.0)]),
        trade=_LookupTrade({order_ref: broker_view("partially_filled", 3, 100.0)}),
        pricing=UNPRICEABLE_RECOVERY,
    )
    fence = _fence(repo)
    assert fence is not None
    assert _cause_orders(fence) == [{"order_ref": order_ref, "symbol": "SPY", "filled_qty": 3.0}]

    await _deliver_fill(
        repo, broker_view("partially_filled", 10, 100.0), quantity=7, execution_id="exec-B"
    )
    assert len(_coverage_conflicts(repo)) == 1
    assert _fence(repo) is not None

    clock.advance(TERMINAL_ENTER_LOOKUP_RECHECK_MS)
    result = await reconcile_account(
        repo,
        read=_FakeRead(positions=[_position("SPY", quantity=10.0)]),
        trade=_LookupTrade({order_ref: broker_view("canceled", 10, 100.0)}),
        pricing=UNPRICEABLE_RECOVERY,
    )

    assert result.verdict == "clean"
    assert _coverage_conflicts(repo) == []
    assert [
        transition["summary_code"]
        for transition in repo.transitions_for_order(order_ref)
        if transition["transition_kind"] == "UNCERTAINTY_RESOLVED"
    ] == [ORDER_TOTAL_PROVEN_SUMMARY_CODE]
    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=QTY_ATOL, rel=0)
    fence = _fence(repo)
    assert fence is not None
    assert _cause_orders(fence) == [{"order_ref": order_ref, "symbol": "SPY", "filled_qty": 10.0}]
    assert not decide_capability(
        repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID
    ).allowed
