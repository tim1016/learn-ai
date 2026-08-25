"""EXIT over an ENTRY that never reached the broker (#1775, finding S15c).

The 2026-08-25 fleet-stress campaign froze an account this way: a lost submit
response, an ENTER correctly voided as definitively absent, and then an EXIT
whose cancel-prove step had no branch for "this order never existed" — so it
folded ``ORDER_CANCEL_UNCERTAIN`` on every reconciliation pass forever. The
committed fixture in ``fixtures/golden/clerk-exit-lost-submit-2026-08-25``
holds the incident's sanitized ledger shape; these tests replay it against a
broker port that answers the exact lookup with definitive absence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    UNCERTAIN_SUBMIT_GRACE_MS,
    fold_entry_never_accepted,
    fold_uncertain,
    resolve_order_submission,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_leg,
    _clock_at,
    _FakeTradePort,
)

ACCOUNT_ID = "PA-LOST-SUBMIT"
SID = "spy-bot"
RUN_ID = "run-1"

_INCIDENT_FIXTURE = (
    Path(__file__).parents[4] / "fixtures/golden/clerk-exit-lost-submit-2026-08-25"
)


def _incident_fixture() -> dict[str, object]:
    return json.loads((_INCIDENT_FIXTURE / "incident_ledger.json").read_text())


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    """``lease_ttl_ms`` is well past the 30 s R4 grace window so the clock jump
    below exercises only the grace-window math, not an incidental lease
    expiry — the same reason ``test_enter.py`` bumps it."""
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_700_000_000_000),
        lease_ttl_ms=300_000,
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


async def _replay_incident(
    repo: ClerkSqliteRepository, trade: _FakeTradePort
) -> tuple[str, str]:
    """Drive the clerk's real API to the incident's frozen precondition.

    ENTER accepted, its submit response lost, absence proven past the R4 grace
    window (the ENTER voids), then an EXIT accepted over that dead entry.
    Returns ``(entry_order_ref, exit_effect_operation_id)``.
    """
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-lost",
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(),
        trade=trade,
    )
    assert submission.order_ref is not None
    repo.clock.advance(UNCERTAIN_SUBMIT_GRACE_MS + 1)  # type: ignore[attr-defined]
    await resolve_order_submission(repo, order_ref=submission.order_ref, trade=trade)

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-after-void",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=submission.order_ref,
    )
    assert accepted.effect_operation_id is not None
    return submission.order_ref, accepted.effect_operation_id


def _ledger_shape(repo: ClerkSqliteRepository, order_ref: str) -> list[dict[str, object]]:
    return [
        {
            "transition_kind": transition["transition_kind"],
            "summary_code": transition["summary_code"],
            "operation_state": transition["operation_state"],
        }
        for transition in repo.transitions_for_order(order_ref)
    ]


async def test_the_committed_incident_ledger_shape_is_reproduced_by_the_clerk(
    repo: ClerkSqliteRepository,
) -> None:
    """Fixture fidelity: if the clerk stops producing this shape, the fixture
    — not the clerk — is what needs revisiting."""
    fixture = _incident_fixture()
    trade = _FakeTradePort(
        submit_error=BrokerUnavailable("websocket drop swallowed the submit response"),
        lookup_absent=True,
    )

    entry_ref, _ = await _replay_incident(repo, trade)

    assert _ledger_shape(repo, entry_ref) == fixture["entry_order_ledger"]
    entry = repo.order(entry_ref)
    assert entry is not None
    assert entry.broker_order_id == fixture["entry_broker_order_id"]


async def test_one_reconciliation_pass_resolves_an_exit_whose_entry_never_reached_the_broker(
    repo: ClerkSqliteRepository,
) -> None:
    """The S15c regression: definitive absence is a terminal answer, so one
    pass drives the EXIT to a terminal state and clears the account-wide
    outstanding-intent gate — instead of folding cancel-uncertain forever."""
    trade = _FakeTradePort(
        submit_error=BrokerUnavailable("websocket drop swallowed the submit response"),
        lookup_absent=True,
    )
    entry_ref, exit_effect_id = await _replay_incident(repo, trade)

    await resolve_exit(repo, effect_operation_id=exit_effect_id, trade=trade)

    effect = repo.effect_operation(exit_effect_id)
    assert effect is not None and effect.state == "succeeded"
    assert repo.uncertain_orders() == []
    pre_fix_loop_kind = _incident_fixture()["pre_fix_loop_transition_kind"]
    assert not any(
        transition["transition_kind"] == pre_fix_loop_kind
        for transition in repo.transitions_for_order(entry_ref)
    )


async def _make_working_entry(
    repo: ClerkSqliteRepository, *, decision_id: str
) -> str:
    """A healthy ENTRY: acknowledged by the broker, carrying a broker order id."""
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(),
        trade=_FakeTradePort(),
    )
    assert submission.order_ref is not None
    entry = repo.order(submission.order_ref)
    assert entry is not None and entry.broker_order_id is not None
    return submission.order_ref


async def _void_lost_entry(repo: ClerkSqliteRepository, *, decision_id: str) -> str:
    """An ENTRY whose submit response was lost and which the broker never had."""
    trade = _FakeTradePort(
        submit_error=BrokerUnavailable("websocket drop swallowed the submit response"),
        lookup_absent=True,
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(),
        trade=trade,
    )
    assert submission.order_ref is not None
    repo.clock.advance(UNCERTAIN_SUBMIT_GRACE_MS + 1)  # type: ignore[attr-defined]
    await resolve_order_submission(repo, order_ref=submission.order_ref, trade=trade)
    return submission.order_ref


async def test_exit_planning_excludes_a_sibling_entry_proven_never_accepted(
    repo: ClerkSqliteRepository,
) -> None:
    """Cancel-prove planning is the narrower set: a sibling the broker never
    accepted can hold no exposure and can never be cancelled, so the EXIT does
    not enumerate it as an order to prove."""
    dead_ref = await _void_lost_entry(repo, decision_id="enter-lost")
    live_ref = await _make_working_entry(repo, decision_id="enter-live")

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-live",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=live_ref,
    )
    assert accepted.effect_operation_id is not None

    linked = {
        order.order_ref
        for order in repo.orders_for_effect_operation(accepted.effect_operation_id)
        if order.role == "ENTRY"
    }
    assert linked == {live_ref}
    assert dead_ref not in linked


async def test_exit_planning_keeps_its_own_target_even_when_never_accepted(
    repo: ClerkSqliteRepository,
) -> None:
    """The EXIT's explicit target is always linked, whatever its state — the
    absence branch in ``exit_resolution`` is the correctness backstop for it,
    and an EXIT with no linked entry would have nothing to resolve."""
    dead_ref = await _void_lost_entry(repo, decision_id="enter-lost")

    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-dead-target",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=dead_ref,
    )
    assert accepted.effect_operation_id is not None

    linked = {
        order.order_ref
        for order in repo.orders_for_effect_operation(accepted.effect_operation_id)
        if order.role == "ENTRY"
    }
    assert linked == {dead_ref}


async def test_the_shared_entry_read_still_returns_every_entry(
    repo: ClerkSqliteRepository,
) -> None:
    """Safe-flatten, runtime recovery and the stuck-EXIT watchdog read the
    shared every-ENTRY set and need full custody evidence — the narrowing
    belongs to cancel-prove planning alone, not to that read."""
    dead_ref = await _void_lost_entry(repo, decision_id="enter-lost")
    live_ref = await _make_working_entry(repo, decision_id="enter-live")

    every_entry = {order.order_ref for order in repo.entry_orders_for_strategy(SID)}

    assert every_entry == {dead_ref, live_ref}


async def test_a_never_accepted_proof_leaves_an_effect_unknown_for_another_order(
    repo: ClerkSqliteRepository,
) -> None:
    """Proving one entry never landed must not declare the whole EXIT healthy
    while a different linked order's outcome is still unknown."""
    dead_ref = await _void_lost_entry(repo, decision_id="enter-lost")
    sibling_ref = await _make_working_entry(repo, decision_id="enter-live")
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-dead-target",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=dead_ref,
    )
    assert accepted.effect_operation_id is not None
    fold_uncertain(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=sibling_ref,
        why="The cancel response for the sibling entry was lost.",
        transition_kind="ORDER_CANCEL_UNCERTAIN",
    )

    fold_entry_never_accepted(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=dead_ref,
        why="The owning ENTER voided this exact order as definitively absent at the broker.",
    )

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"


def _append_fill_slice(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    effect_operation_id: str,
    execution_id: str = "exec-1",
) -> None:
    """One websocket execution slice, exactly as the trade-update sink records
    it — before any submit acknowledgement, which is the real ordering
    (`trade_evidence.py` appends the slice, then acknowledges the order)."""
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol="SPY",
        side="BUY",
        slice_qty=4.0,
        slice_price=100.0,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=repo.clock(),
    )

    def _transition() -> TransitionInput:
        return TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect_operation_id,
            order_ref=order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            proof_reference=execution_id,
            source_event_at_ms=repo.clock(),
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        )

    repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=order_ref,
        build_transition=_transition,
        build_coverage_conflict=_transition,
    )


async def test_a_durable_fill_outweighs_an_absent_lookup(
    repo: ClerkSqliteRepository,
) -> None:
    """Absence is terminal only when nothing else says the order existed.

    An execution slice can land before the submit acknowledgement, so an order
    can hold durable fills while its broker order id is still null. An absent
    lookup then contradicts the fills rather than proving the order never
    happened — and skipping cancel proof there could reduce a position while
    the entry is still working at the broker.
    """
    trade = _FakeTradePort(
        submit_error=BrokerUnavailable("websocket drop swallowed the submit response"),
        lookup_absent=True,
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-filled-then-lost",
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(),
        trade=trade,
    )
    assert submission.order_ref is not None and submission.effect_operation_id is not None
    entry = repo.order(submission.order_ref)
    assert entry is not None and entry.broker_order_id is None
    _append_fill_slice(
        repo,
        order_ref=submission.order_ref,
        effect_operation_id=submission.effect_operation_id,
    )
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-after-fill",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=submission.order_ref,
    )
    assert accepted.effect_operation_id is not None
    repo.clock.advance(UNCERTAIN_SUBMIT_GRACE_MS + 1)  # type: ignore[attr-defined]

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    transitions = repo.transitions_for_order(submission.order_ref)
    assert not any(t["transition_kind"] == "ENTRY_NEVER_ACCEPTED" for t in transitions)
    assert any(t["transition_kind"] == "ORDER_CANCEL_UNCERTAIN" for t in transitions)


async def test_live_absence_also_voids_the_entry_s_own_enter(
    repo: ClerkSqliteRepository,
) -> None:
    """An EXIT that proves absence first must close the entry's own ENTER too.

    The unknown-outcome episode is keyed by (effect, order), so proof recorded
    only against the EXIT leaves the ENTER's outstanding intent — and the
    admission gate it feeds — open indefinitely.
    """
    trade = _FakeTradePort(
        submit_error=BrokerUnavailable("websocket drop swallowed the submit response"),
        lookup_absent=True,
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="enter-lost",
        lifecycle_run_id=RUN_ID,
        leg=_broker_leg(),
        trade=trade,
    )
    assert submission.order_ref is not None and submission.effect_operation_id is not None
    enter_effect = repo.effect_operation(submission.effect_operation_id)
    assert enter_effect is not None and enter_effect.state == "unknown"  # never resolved
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-before-void",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=submission.order_ref,
    )
    assert accepted.effect_operation_id is not None
    repo.clock.advance(UNCERTAIN_SUBMIT_GRACE_MS + 1)  # type: ignore[attr-defined]

    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    enter_effect = repo.effect_operation(submission.effect_operation_id)
    assert enter_effect is not None and enter_effect.state == "failed"
    assert enter_effect.terminal_receipt_id is not None
    assert repo.uncertain_orders() == []
