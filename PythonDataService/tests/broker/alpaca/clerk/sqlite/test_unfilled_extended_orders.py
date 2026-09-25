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

from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    cancel_and_prove_owned_entry,
    resolve_exit,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence, fold_uncertain
from app.broker.alpaca.clerk.sqlite.reads import NONTERMINAL_EFFECT_STATES
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    decide_capability,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import FAILED_ENTER_FILLED_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    OrderSide,
    OrderType,
    TimeInForce,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit import (
    ACCOUNT_ID,
    AFTER_HOURS_MS,
    POST_CLOSE_MS,
    RUN_ID,
    SID,
    _broker_order,
    _FakeTrade,
    _make_entry,
    _NoReconciler,
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

    This pin covers the two operational facts: zero attributed exposure, and
    a fresh ENTER decision is not blocked by what happened to this one. The
    effect row's own terminal state is pinned separately by
    ``test_enter_cancelled_by_the_vendor_unfilled_reaches_failed`` below —
    it read ``in_progress`` forever until #2006 mirrored R12 for ENTER.
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


async def test_enter_cancelled_by_the_vendor_unfilled_reaches_failed(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2006 — the ENTER mirror of R12.

    A vendor cancel/expire/reject with zero fills is proven unfilled, so the
    ENTER's effect operation must reach ``failed`` rather than sit
    ``in_progress`` forever. Before this, ``fold_order_acknowledgement`` wrote
    ``operation_state="in_progress"`` unconditionally, so the instance stayed
    in the nonterminal set that ``strategy_instances_with_live_custody`` reads
    even when provably flat — daily residue in extended hours.
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

    fold_order_evidence(
        repo,
        effect_operation_id=submission.effect_operation_id,
        order=_broker_order(submission.order_ref, status="expired", filled_quantity=0.0),
    )

    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None
    assert effect.state == "failed"
    assert effect.state not in NONTERMINAL_EFFECT_STATES


async def test_terminal_unfilled_enter_folds_its_failure_once(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """Re-observing the same dead ENTER is routine — a poll, the reconciliation
    sweep and a trade-update frame can all deliver it. Only one
    ``ENTER_UNFILLED`` transition may be appended for the order."""
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

    dead = _broker_order(submission.order_ref, status="canceled", filled_quantity=0.0)
    for _ in range(3):
        fold_order_evidence(
            repo,
            effect_operation_id=submission.effect_operation_id,
            order=dead,
        )

    transitions = [
        row
        for row in repo.transitions_for_order(submission.order_ref)
        if row["transition_kind"] == "ENTER_UNFILLED"
    ]
    assert len(transitions) == 1


async def test_partially_filled_then_cancelled_enter_does_not_reach_failed(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The negative case that keeps the mirror honest.

    ``canceled`` with a recorded execution is not proven unfilled — real
    exposure was opened. Folding that to ``failed`` would declare an ENTER
    dead while the strategy still holds shares, which is the opposite of the
    residue this fix removes.
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

    fold_order_evidence(
        repo,
        effect_operation_id=submission.effect_operation_id,
        order=_broker_order(
            submission.order_ref,
            status="canceled",
            filled_quantity=4.0,
            filled_avg_price=100.10,
        ),
    )

    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None
    assert effect.state != "failed"
    assert repo.position(SID, "SPY") == 4.0


def _enter_unfilled_rows(clerk: ClerkSqliteRepository, effect_operation_id: str) -> list[dict]:
    rows = clerk._conn.execute(
        "SELECT * FROM custody_transitions WHERE effect_operation_id = ? "
        "AND transition_kind = 'ENTER_UNFILLED'",
        (effect_operation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


async def test_an_exit_owned_entry_cancel_ends_the_enter_without_breaking_the_nesting(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2251: an ENTER whose entry an EXIT cancels unfilled reaches ``failed``.

    Every observation route hands the evidence gate
    ``active_exit_for_order(order) or order.effect_operation_id``, so while an
    EXIT is cancelling a working entry the effect in charge is the EXIT. Before
    #2251 the ENTER kept ``in_progress`` there forever, holding its instance in
    ``strategy_instances_with_live_custody`` while provably flat.

    Both halves of #1379 still hold: every transition against ``entry_ref``
    after ``EXIT_ACCEPTED`` carries the EXIT's id, and the ENTER's receipt sits
    on the ENTER's own timeline, unkeyed to the order, so the fold only ends
    the effect that carries it.
    """
    entry_ref = await _make_entry(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    enter_effect_id = entry.effect_operation_id

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    active = repo.active_exit_for_order(entry_ref)
    assert active is not None and active.kind == "EXIT", (
        "this test is only meaningful while an EXIT owns the entry's fold"
    )
    assert active.effect_operation_id != enter_effect_id

    cancel_trade = _FakeTrade(
        lookup_results=[_broker_order(entry_ref, status="canceled", filled_quantity=0.0)]
    )
    await cancel_and_prove_owned_entry(repo, entry_order_ref=entry_ref, trade=cancel_trade)

    enter_effect = repo.effect_operation(enter_effect_id)
    assert enter_effect is not None
    assert enter_effect.state == "failed"
    exit_effect = repo.effect_operation(accepted.effect_operation_id)
    assert exit_effect is not None
    assert exit_effect.state in NONTERMINAL_EFFECT_STATES, (
        "the ENTER's receipt must not end the EXIT, which did not fail"
    )

    (receipt,) = _enter_unfilled_rows(repo, enter_effect_id)
    assert receipt["order_ref"] is None
    assert accepted.effect_operation_id in receipt["facts_json"]

    transitions = repo.transitions_for_order(entry_ref)
    exit_accepted_seq = next(
        t["sequence"] for t in transitions if t["transition_kind"] == "EXIT_ACCEPTED"
    )
    for transition in transitions:
        if transition["sequence"] >= exit_accepted_seq:
            assert transition["effect_operation_id"] == accepted.effect_operation_id


async def test_an_exit_owned_entry_fold_is_idempotent_across_routes(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """A poll, the sweep and a trade-update frame all re-deliver the same dead
    entry under the EXIT; the ENTER's failure is folded exactly once."""
    entry_ref = await _make_entry(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    dead = _broker_order(entry_ref, status="canceled", filled_quantity=0.0)
    for _ in range(3):
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=dead)

    assert len(_enter_unfilled_rows(repo, entry.effect_operation_id)) == 1


async def test_an_unknown_enter_is_left_to_its_own_route_so_its_episode_resolves(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The EXIT-side receipt carries no ``order_ref``, so it cannot resolve an
    unknown-outcome episode naming ``(ENTER, entry_ref)``. Terminalizing the
    ENTER there would drop it out of every reconciliation read and strand the
    episode, blocking new exposure for good. It declines instead, and the
    ENTER's own route closes both once the EXIT has ended."""
    entry_ref = await _make_entry(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    enter_effect_id = entry.effect_operation_id
    fold_uncertain(
        repo, effect_operation_id=enter_effect_id, order_ref=entry_ref, why="lost response"
    )
    assert repo.effect_operation(enter_effect_id).state == "unknown"  # type: ignore[union-attr]

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    dead = _broker_order(entry_ref, status="canceled", filled_quantity=0.0)
    await resolve_exit(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        trade=_FakeTrade(lookup_results=[dead] * 5),
        pricing=UNPRICEABLE_RECOVERY,
    )
    assert repo.active_exit_for_order(entry_ref) is None

    assert repo.effect_operation(enter_effect_id).state == "unknown"  # type: ignore[union-attr]
    assert not _enter_unfilled_rows(repo, enter_effect_id)
    assert enter_effect_id in {
        effect.effect_operation_id for effect in repo.reconcilable_effect_operations()
    }, "the ENTER must stay reconcilable so its own route can resolve the episode"

    fold_order_evidence(repo, effect_operation_id=enter_effect_id, order=dead)

    assert repo.effect_operation(enter_effect_id).state == "failed"  # type: ignore[union-attr]
    assert decide_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=SID,
    ).allowed


async def test_a_partially_filled_entry_an_exit_cancels_keeps_its_enter_open(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """A partial fill opened real exposure; an EXIT cancelling the remainder
    does not prove the ENTER unfilled."""
    entry_ref = await _make_entry(repo, status="partially_filled", filled_quantity=3.0)
    entry = repo.order(entry_ref)
    assert entry is not None
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order=_broker_order(
            entry_ref, status="canceled", filled_quantity=3.0, filled_avg_price=100.0
        ),
    )

    enter_effect = repo.effect_operation(entry.effect_operation_id)
    assert enter_effect is not None
    assert enter_effect.state in NONTERMINAL_EFFECT_STATES
    assert not _enter_unfilled_rows(repo, entry.effect_operation_id)


async def test_a_submit_response_that_returns_cancelled_does_not_end_the_enter(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """Owner decision, 2026-09-21: only a later observation ends an ENTER.

    A terminal effect surfaces as an ``EffectOperationState.REJECTED``
    receipt, which ``bot_trade_strategy`` reads as "discard this evaluation".
    ``SyntheticBroker``'s ruling R9 cancels every non-marketable limit on the
    spot with zero fills, so folding on the submit response would flip Dry Run
    and Shadow from commit to discard for a whole class of decisions. #2006 is
    a custody-residue fix, not a decision-loop change.
    """
    trade = _FakeTrade(
        submit_result=_broker_order("placeholder", status="canceled", filled_quantity=0.0)
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
    assert submission.effect_operation_id is not None

    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None
    assert effect.state != "failed"


async def test_a_recorded_fill_blocks_the_fold_even_when_the_snapshot_reports_zero(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The durable fill row is read as well as the snapshot, and this is the
    case that makes it load-bearing.

    A snapshot reporting ``filled_quantity=0`` is not proof of nothing filled
    when an execution is already recorded against the order — that ENTER
    opened real exposure, and calling it ``failed`` would declare it dead
    while the strategy still holds shares.
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

    fold_order_evidence(
        repo,
        effect_operation_id=submission.effect_operation_id,
        order=_broker_order(
            submission.order_ref,
            status="partially_filled",
            filled_quantity=4.0,
            filled_avg_price=100.10,
        ),
    )
    assert repo.fills_for_order(submission.order_ref)

    fold_order_evidence(
        repo,
        effect_operation_id=submission.effect_operation_id,
        order=_broker_order(submission.order_ref, status="canceled", filled_quantity=0.0),
    )

    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None
    assert effect.state != "failed"


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
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=ack_trade, pricing=UNPRICEABLE_RECOVERY)
    assert first.reducing_order_ref is not None

    cancel_trade = _FakeTrade(
        lookup_results=[
            _broker_order(first.reducing_order_ref, side="sell", status="canceled", filled_quantity=0.0)
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=cancel_trade, pricing=UNPRICEABLE_RECOVERY)

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


async def test_an_after_hours_exit_unfilled_at_the_session_end_tells_the_operator_the_position_is_open(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2440 (owner decision #2431): the after-hours limit is not queued, and not silent.

    The day's last EXIT goes out at 16:00 as an extended-hours DAY limit. If
    the after-hours session ends without filling it, Alpaca ends the order;
    the first pass that observes that raises the operator-visible
    ``EXIT_NOT_FLAT`` episode — the bot page's evidence and the lane's
    attention bell both read it — saying the position is still open. Nothing
    is resubmitted for the next day.
    """
    _walk_clock_to(repo, AFTER_HOURS_MS)
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-last-bar",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        program_leg=ProgramLeg(
            LegShape(
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=99.80,
                extended_hours=True,
                side=OrderSide.SELL,
            ),
            valid_until_ms=POST_CLOSE_MS,
        ),
    )
    assert accepted.effect_operation_id is not None
    working = _FakeTrade(submit_result=_broker_order("placeholder", side="sell", status="accepted"))
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=working, pricing=UNPRICEABLE_RECOVERY)
    assert first.reducing_order_ref is not None
    assert repo.active_uncertainties() == []

    _walk_clock_to(repo, POST_CLOSE_MS + 5_000)
    expired = _FakeTrade(
        lookup_results=[
            _broker_order(first.reducing_order_ref, side="sell", status="expired", filled_quantity=0.0)
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=expired, pricing=UNPRICEABLE_RECOVERY)

    assert expired.submit_calls == []
    assert repo.position(SID, "SPY") == 10
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    # The attention bell's input is exactly the active uncertainty set.
    [episode] = repo.active_uncertainties()
    assert episode["reason_code"] == "EXIT_NOT_FLAT"
    assert episode["strategy_instance_id"] == SID
    assert episode["headline"] == (
        "An extended-hours exit ended unfilled; the position is still open"
    )
    assert episode["explanation"] == (
        "The extended-hours limit to sell 10 SPY at 99.8 ended expired without "
        "flattening the position; 10 SPY is still held."
    )
    assert "Nothing was queued for the next open." in episode["next_step"]


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
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=ack_trade, pricing=UNPRICEABLE_RECOVERY)
    assert first.reducing_order_ref is not None
    cancel_trade = _FakeTrade(
        lookup_results=[
            _broker_order(first.reducing_order_ref, side="sell", status="canceled", filled_quantity=0.0)
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=cancel_trade, pricing=UNPRICEABLE_RECOVERY)
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"

    new_anchor = LegShape(
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=99.50,
        extended_hours=True,
        side=OrderSide.SELL,
    )
    # The next decision lands after the close: an extended-hours anchor,
    # sendable until the declared close (#2440).
    _walk_clock_to(repo, AFTER_HOURS_MS)
    try:
        second_accepted = accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-2",
            lifecycle_run_id=RUN_ID,
            entry_order_ref=entry_ref,
            program_leg=ProgramLeg(new_anchor, valid_until_ms=POST_CLOSE_MS),
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
        pricing=UNPRICEABLE_RECOVERY,
    )

    assert second.reducing_order_ref is not None
    assert second.reducing_order_ref != first.reducing_order_ref
    ((leg, _client_order_id),) = second_trade.submit_calls
    assert leg.limit_price == 99.50
    assert leg.order_type is OrderType.LIMIT
    assert leg.extended_hours is True


async def _frame(
    clerk: ClerkSqliteRepository,
    order: BrokerOrder,
    *,
    event_type: str,
    source_ms: int,
    execution_id: str | None = None,
) -> None:
    """Deliver one ``trade_updates`` frame through the real SQLite sink."""
    order = order.model_copy(update={"updated_at_ms": source_ms})
    is_fill = event_type in {"fill", "partial_fill"}
    await SqliteTradeUpdateEvidenceSink(
        repo=clerk, intake=ReentrantAsyncLock(), reconciler=_NoReconciler()
    ).record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=BrokerOrderEvent(
            event_type=event_type,
            occurred_at_ms=source_ms,
            price=order.filled_avg_price if is_fill else None,
            quantity=order.filled_quantity if is_fill else None,
            execution_id=execution_id,
        ),
        event_key=f"{event_type}:{order.client_order_id}:{source_ms}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )


async def _working_xh_enter(clerk: ClerkSqliteRepository) -> tuple[str, str]:
    submission = await submit_enter(
        clerk,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_XH_LEG,
        trade=_FakeTrade(
            submit_result=_broker_order("placeholder", status="accepted", filled_quantity=0.0)
        ),
    )
    assert submission.effect_operation_id is not None and submission.order_ref is not None
    return submission.effect_operation_id, submission.order_ref


async def test_a_websocket_cancel_of_an_unfilled_enter_reaches_failed(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2306: the ``trade_updates`` route folds ``ENTER_UNFILLED`` too.

    The websocket normally beats the sweep to a vendor cancel. Its sink used
    to fold only the acknowledgement, which recorded ``broker_state=canceled``
    and so dropped the ENTER out of every reconciliation read while it stayed
    ``in_progress`` for ever -- the #2006 residue by another route.
    """
    effect_id, order_ref = await _working_xh_enter(repo)

    await _frame(repo, _broker_order(order_ref, status="new"), event_type="new", source_ms=1_700_000_001_000)
    await _frame(
        repo,
        _broker_order(order_ref, status="canceled", filled_quantity=0.0),
        event_type="canceled",
        source_ms=1_700_000_002_000,
    )

    effect = repo.effect_operation(effect_id)
    assert effect is not None
    assert effect.state == "failed"
    (receipt,) = _enter_unfilled_rows(repo, effect_id)
    assert receipt["order_ref"] == order_ref
    assert repo.position(SID, "SPY") == 0


async def test_a_websocket_cancel_of_an_exit_owned_entry_ends_the_enter(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2306 x #2251: under an EXIT the websocket sink hands the gate the EXIT,
    exactly as the REST route does, so the ENTER's receipt lands unkeyed on
    its own timeline and the EXIT is not ended."""
    entry_ref = await _make_entry(repo)
    entry = repo.order(entry_ref)
    assert entry is not None
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    await _frame(
        repo,
        _broker_order(entry_ref, status="canceled", filled_quantity=0.0),
        event_type="canceled",
        source_ms=1_700_000_002_000,
    )

    assert repo.effect_operation(entry.effect_operation_id).state == "failed"  # type: ignore[union-attr]
    (receipt,) = _enter_unfilled_rows(repo, entry.effect_operation_id)
    assert receipt["order_ref"] is None
    assert repo.effect_operation(accepted.effect_operation_id).state in NONTERMINAL_EFFECT_STATES  # type: ignore[union-attr]


async def test_a_websocket_cancelled_enter_that_later_fills_still_raises_the_fence(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2306 x #2348: the fold ends a proven-unfilled ENTER; it never absorbs
    a fill the broker reports afterwards. The late execution is kept (the
    Clerk's position must match the broker's) and ``FAILED_ENTER_FILLED``
    is raised against the ENTER the websocket had already ended."""
    effect_id, order_ref = await _working_xh_enter(repo)
    await _frame(
        repo,
        _broker_order(order_ref, status="canceled", filled_quantity=0.0),
        event_type="canceled",
        source_ms=1_700_000_002_000,
    )
    assert repo.effect_operation(effect_id).state == "failed"  # type: ignore[union-attr]

    await _frame(
        repo,
        _broker_order(order_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.10),
        event_type="fill",
        source_ms=1_700_000_003_000,
        execution_id="exec-late-1",
    )

    assert repo.position(SID, "SPY") == pytest.approx(10.0, abs=1e-9, rel=0)
    episode = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code=FAILED_ENTER_FILLED_REASON_CODE,
        strategy_instance_id=SID,
    )
    assert episode is not None
    assert order_ref in episode["explanation"]
    assert len(_enter_unfilled_rows(repo, effect_id)) == 1


def _is_reconcilable(clerk: ClerkSqliteRepository, effect_operation_id: str) -> bool:
    return effect_operation_id in {
        effect.effect_operation_id for effect in clerk.reconcilable_effect_operations()
    }


async def test_a_cancel_whose_ack_did_not_advance_keeps_the_enter_open(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """#2306 review: the fold follows only an ack that took effect.

    A ``fill`` frame with no execution identity records only its ack
    (``filled``, 10 reported), no fill row. A later ``canceled`` frame that
    reports nothing filled cannot move the order off ``filled``, so it proves
    nothing. Ending the ENTER there would hide 10 bought shares from every
    fence and lookup; instead it stays open and on the sweep's worklist,
    whose exact lookup re-derives the missing execution.
    """
    effect_id, order_ref = await _working_xh_enter(repo)
    await _frame(
        repo,
        _broker_order(order_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.10),
        event_type="fill",
        source_ms=1_700_000_002_000,
    )
    assert not repo.fills_for_order(order_ref)

    await _frame(
        repo,
        _broker_order(order_ref, status="canceled", filled_quantity=0.0),
        event_type="canceled",
        source_ms=1_700_000_003_000,
    )

    assert repo.effect_operation(effect_id).state in NONTERMINAL_EFFECT_STATES  # type: ignore[union-attr]
    assert not _enter_unfilled_rows(repo, effect_id)
    assert _is_reconcilable(repo, effect_id)


class _CancelledDuringSubmit(_FakeTrade):
    """The websocket reports the cancel while the submit POST is in flight."""

    def __init__(self, clerk: ClerkSqliteRepository) -> None:
        super().__init__()
        self._clerk = clerk

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        await _frame(
            self._clerk,
            _broker_order(client_order_id, status="canceled", filled_quantity=0.0),
            event_type="canceled",
            source_ms=1_700_000_002_000,
        )
        return _broker_order(client_order_id, status="accepted").model_copy(
            update={"updated_at_ms": 1_700_000_001_000}
        )


async def test_a_websocket_cancel_during_the_submit_post_ends_the_enter(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """Pins the fold while the ENTER's submit claim is held (#2306 review).

    Declining here strands the ENTER: the frame's ack has already made it
    ``in_progress`` over a ``canceled`` order, which drops it off the sweep's
    worklist, and the submit response never folds ``ENTER_UNFILLED``. So the
    frame ends it, and the late submit response does not revive it.
    """
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-1",
        lifecycle_run_id=RUN_ID,
        leg=_XH_LEG,
        trade=_CancelledDuringSubmit(repo),
    )
    assert submission.effect_operation_id is not None

    assert repo.effect_operation(submission.effect_operation_id).state == "failed"  # type: ignore[union-attr]
    assert len(_enter_unfilled_rows(repo, submission.effect_operation_id)) == 1


async def test_a_websocket_cancel_of_an_unknown_enter_ends_it_and_its_episode(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """The ENTER in charge of its own order folds from ``unknown`` too: the
    receipt is keyed to the order, so the terminal fold resolves the
    unknown-outcome episode and new exposure is admitted again."""
    effect_id, order_ref = await _working_xh_enter(repo)
    fold_uncertain(repo, effect_operation_id=effect_id, order_ref=order_ref, why="lost response")
    assert repo.effect_operation(effect_id).state == "unknown"  # type: ignore[union-attr]

    await _frame(
        repo,
        _broker_order(order_ref, status="canceled", filled_quantity=0.0),
        event_type="canceled",
        source_ms=1_700_000_002_000,
    )

    assert repo.effect_operation(effect_id).state == "failed"  # type: ignore[union-attr]
    assert decide_capability(
        repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=SID,
    ).allowed


async def test_a_replayed_websocket_cancel_folds_the_enter_once(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """A redelivered frame (reconnect, gap replay) finds the ENTER already
    ``failed``; only one ``ENTER_UNFILLED`` is ever appended."""
    effect_id, order_ref = await _working_xh_enter(repo)
    for _ in range(3):
        await _frame(
            repo,
            _broker_order(order_ref, status="canceled", filled_quantity=0.0),
            event_type="canceled",
            source_ms=1_700_000_002_000,
        )

    assert len(_enter_unfilled_rows(repo, effect_id)) == 1


async def test_a_websocket_partial_fill_then_cancel_keeps_the_enter_open(
    repo: ClerkSqliteRepository,  # noqa: F811 — the imported fixture
) -> None:
    """A partial fill opened real exposure; the vendor cancelling the rest
    does not prove the ENTER unfilled, on the websocket route either."""
    effect_id, order_ref = await _working_xh_enter(repo)
    await _frame(
        repo,
        _broker_order(
            order_ref, status="partially_filled", filled_quantity=4.0, filled_avg_price=100.10
        ),
        event_type="partial_fill",
        source_ms=1_700_000_002_000,
        execution_id="exec-partial-1",
    )
    await _frame(
        repo,
        _broker_order(order_ref, status="canceled", filled_quantity=4.0, filled_avg_price=100.10),
        event_type="canceled",
        source_ms=1_700_000_003_000,
    )

    assert repo.effect_operation(effect_id).state in NONTERMINAL_EFFECT_STATES  # type: ignore[union-attr]
    assert not _enter_unfilled_rows(repo, effect_id)
    assert repo.position(SID, "SPY") == pytest.approx(4.0, abs=1e-9, rel=0)
