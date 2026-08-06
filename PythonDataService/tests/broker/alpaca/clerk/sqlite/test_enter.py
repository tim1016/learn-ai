"""ENTER domain tests (#1377) — capture-before-contact, lost-response
UNKNOWN resolution with the 30s grace, concurrent-duplicate dedup, and
namespace-attributed idempotent fill folding, over the SQLite spine.

Rebuilt against the corrective foundation slice's ``commit_first_transition``
(no ``reserve_command()``/``serialized()``); see the pinned contract's §4
transaction-matrix row "Command/effect admission, broker-eligible" for why a
fresh ENTER lands in ``accepted`` state, not ``in_progress``, before any
broker attempt.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import (
    EnterSubmission,
    accept_enter,
    fold_order_evidence,
    resolve_enter_submission,
    submit_enter,
)
from app.broker.alpaca.clerk.sqlite.facts import OrderFillObservedFacts
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    InvalidIdentityError,
    NoActiveRunError,
    UnknownStrategyInstanceError,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, OperationClaimError
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError, raise_uncertainty
from app.broker.contract.errors import BrokerError, BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from conftest import _clock_at

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
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
    status: str = "accepted",
    filled_quantity: float = 0.0,
    filled_avg_price: float | None = None,
    updated_at_ms: int | None = 1_700_000_000_500,
) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=order_id,
        client_order_id=client_order_id,
        symbol="SPY",
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
        updated_at_ms=updated_at_ms,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=updated_at_ms if updated_at_ms is not None else 1_700_000_000_500,
    )


class _FakeTrade:
    """A minimal ``BrokerTradePort`` double for the ENTER submission path."""

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        submit_result: BrokerOrder | None = None,
        submit_client_order_id_override: str | None = None,
        on_submit: Any = None,
        lookup_result: BrokerOrder | None = None,
        lookup_error: Exception | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._submit_error = submit_error
        self._submit_result = submit_result
        self._submit_client_order_id_override = submit_client_order_id_override
        self._on_submit = on_submit
        self._lookup_result = lookup_result
        self._lookup_error = lookup_error
        self._lookup_absent = lookup_absent
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._on_submit is not None:
            self._on_submit(leg, client_order_id)
        if self._submit_error is not None:
            raise self._submit_error
        # A caller-forced override models a broker response that comes back
        # under a different client_order_id than the one we asked for.
        returned_client_order_id = self._submit_client_order_id_override or client_order_id
        template = self._submit_result or _broker_order(returned_client_order_id)
        return template.model_copy(
            update={
                "client_order_id": returned_client_order_id,
                "order_id": f"bo-{returned_client_order_id}",
            }
        )

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - unused by ENTER
        raise NotImplementedError

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_absent:
            return None
        if self._lookup_result is not None:
            # Returned verbatim — a caller-supplied lookup_result may
            # deliberately carry a client_order_id that does not match the
            # requested one, to exercise the mismatch guard.
            return self._lookup_result
        return _broker_order(client_order_id).model_copy(update={"order_id": f"bo-{client_order_id}"})


# ── Capture-before-contact, concurrency, and the definitive outcomes ────────


def test_accept_alone_commits_accepted_state_before_any_broker_attempt(
    repo: ClerkSqliteRepository,
) -> None:
    """Pinned contract §4/§5: a fresh ENTER lands its command and effect
    operation in ``accepted`` — not ``in_progress`` — since no broker or
    local work has begun yet. ``in_progress`` is reserved for the moment a
    broker attempt (or its resolution) actually starts."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.created
    assert accepted.command.state == "accepted"
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None
    assert effect.state == "accepted"
    assert effect.kind == "ENTER"
    order = repo.order(accepted.order_ref)
    assert order is not None
    assert order.role == "ENTRY"
    assert order.broker_order_id is None
    assert order.client_order_id == accepted.order_ref


async def test_intent_commits_before_the_broker_is_ever_called(repo: ClerkSqliteRepository) -> None:
    """R1: the effect operation, order row, and mirror finalize must all be
    durable before ``trade.submit`` is invoked."""
    seen_orders_row_at_submit_time = {}

    def on_submit(leg: BrokerOrderLeg, client_order_id: str) -> None:
        seen_orders_row_at_submit_time["order"] = repo.order(client_order_id)

    trade = _FakeTrade(on_submit=on_submit)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )

    assert seen_orders_row_at_submit_time["order"] is not None
    assert seen_orders_row_at_submit_time["order"].order_ref == submission.order_ref
    assert len(trade.submit_calls) == 1


async def test_failed_reservation_produces_no_broker_call(repo: ClerkSqliteRepository) -> None:
    """A durable conflict (same identity, different payload) must never reach
    the broker — no broker call for a reservation that fails."""
    trade = _FakeTrade()
    await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=1),
        trade=trade,
    )

    with pytest.raises(DurableConflictError):
        await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(quantity=2),  # different payload, same (sid, decision_id)
            trade=trade,
        )
    assert len(trade.submit_calls) == 1  # only the first, successful submit


def test_concurrent_duplicate_enter_produces_exactly_one_broker_intent(
    repo: ClerkSqliteRepository,
) -> None:
    """Acceptance criterion: concurrent duplicate ENTER for the same
    (sid, decision_id) produces one broker intent, not two.

    Driven from two real OS threads, each running its own event loop via
    ``asyncio.run`` — the repository is built with ``check_same_thread=False``
    for exactly this dispatch shape. A single-event-loop ``asyncio.gather``
    race never actually contends for ``commit_first_transition``'s write
    lock at the same instant (everything but the awaited broker call runs
    synchronously on one thread); real OS threads do.
    """
    trade = _FakeTrade()
    barrier = threading.Barrier(2)
    results: list[EnterSubmission | None] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def worker(index: int) -> None:
        barrier.wait()
        try:
            results[index] = asyncio.run(
                submit_enter(
                    repo,
                    account_id=ACCOUNT_ID,
                    strategy_instance_id=SID,
                    decision_id="dec-1",
                    lifecycle_run_id=RUN_ID,
                    leg=_leg(),
                    trade=trade,
                )
            )
        except BaseException as exc:  # re-raised on the main thread below
            errors[index] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for exc in errors:
        if exc is not None:
            raise exc
    assert len(trade.submit_calls) == 1
    assert {r.order_ref for r in results if r is not None} == {results[0].order_ref}


async def test_definitive_broker_rejection_folds_failed_not_unknown(
    repo: ClerkSqliteRepository,
) -> None:
    """A non-``BrokerUnavailable`` ``BrokerError`` is a DEFINITIVE failure —
    never left ``unknown``, per the preserved JSONL-clerk discipline."""
    trade = _FakeTrade(submit_error=BrokerRequestInvalid("bad request", broker="alpaca"))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    command = repo.get_command(submission.command.command_id)
    assert command is not None and command.state == "failed"


async def test_cancelled_submit_retains_unknown_custody_before_claim_release(
    repo: ClerkSqliteRepository,
) -> None:
    class BlockingTrade(_FakeTrade):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()

        async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            self.submit_calls.append((leg, client_order_id))
            self.submit_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    trade = BlockingTrade()
    task = asyncio.create_task(
        submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="cancelled-submit",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
            trade=trade,
        )
    )
    await trade.submit_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    effect = repo.effect_operation("effect:spy-bot:cancelled-submit")
    assert effect is not None and effect.state == "unknown"
    uncertainty = repo.active_uncertainty(
        scope="BOT",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        strategy_instance_id=SID,
    )
    assert uncertainty is not None


async def test_submit_ack_with_a_mismatched_client_order_id_folds_uncertain_not_a_raw_exception(
    repo: ClerkSqliteRepository,
) -> None:
    """A broker response whose ``client_order_id`` doesn't match the order
    we asked for must never be folded as this order's evidence — mirrors
    :func:`resolve_enter_submission`'s own mismatch guard. Treated exactly
    like a lost response (R4): fold ``unknown`` and let the standard
    by-identity resolution find the real outcome, instead of a raw
    exception from folding evidence under the wrong ``order_ref``."""
    trade = _FakeTrade(submit_client_order_id_override="some-other-order-entirely")
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"  # resolved via lookup-by-identity
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id == f"bo-{submission.order_ref}"


# ── Lost response → UNKNOWN → grace-gated resolution by client_order_id ─────


async def test_lost_response_resolves_immediately_when_order_is_found(
    repo: ClerkSqliteRepository,
) -> None:
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id == f"bo-{submission.order_ref}"


async def test_lost_response_absent_within_grace_stays_unknown_not_failed(
    repo: ClerkSqliteRepository,
) -> None:
    """A first absent lookup must not be treated as terminal — the order may
    still be in flight at the broker."""
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"


async def test_absence_past_the_grace_window_resolves_failed(tmp_path: Path) -> None:
    """Advances the clock past the 30s R4 grace window. ``lease_ttl_ms`` is
    bumped well past that same 30s so the artificial clock jump exercises
    only the grace-window math, not an incidental execution-lease expiry —
    the two are unrelated 30s windows that happen to share a default."""
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(
        account_id="PA-GRACE", artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id="PA-GRACE", strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True)
    submission = await submit_enter(
        r,
        account_id="PA-GRACE",
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    effect = r.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"  # immediate call: still in grace

    clock.advance(30_001)
    resolved = await resolve_enter_submission(r, order_ref=submission.order_ref, trade=trade)
    effect = r.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    command = r.get_command(resolved.command.command_id)
    assert command is not None and command.state == "failed"
    r.close()


async def test_same_owner_concurrent_resolves_fold_the_terminal_outcome_once(
    tmp_path: Path,
) -> None:
    """An overlapping same-process resolver is fenced before broker contact."""
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(
        account_id="PA-RACE", artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id="PA-RACE", strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    accepted = accept_enter(
        r,
        account_id="PA-RACE",
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.order_ref is not None
    clock.advance(30_001)  # past the R4 grace window

    class _RacingTrade:
        def __init__(self) -> None:
            self.lookup_calls = 0

        async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            raise NotImplementedError

        async def cancel(self, order_id: str) -> None:
            raise NotImplementedError

        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            self.lookup_calls += 1
            if self.lookup_calls == 1:
                # Simulate a second, independent recovery sweep for the same
                # order completing entirely while this (the first, "outer")
                # sweep is mid-flight — the same-process analogue of two
                # overlapping periodic recovery invocations.
                inner_trade = _FakeTrade(lookup_absent=True)
                with pytest.raises(OperationClaimError):
                    await resolve_enter_submission(r, order_ref=accepted.order_ref, trade=inner_trade)
            return None

    outer_trade = _RacingTrade()
    resolved = await resolve_enter_submission(r, order_ref=accepted.order_ref, trade=outer_trade)

    effect = r.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    failed_transitions = [
        t for t in r.transitions_for_order(accepted.order_ref) if t["transition_kind"] == "ORDER_SUBMIT_FAILED"
    ]
    assert len(failed_transitions) == 1  # not duplicated by the losing side of the race
    r.close()


async def test_kill_before_broker_contact_recovery_finds_one_accepted_operation_no_duplicate(
    repo: ClerkSqliteRepository,
) -> None:
    """Simulates a crash after the intent commits but before ``trade.submit``
    is ever called: recovery resolves by client_order_id and never resubmits."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.created  # a fresh reservation — the "crash" happens right here

    trade = _FakeTrade()  # never had submit() called before the "restart"
    resolved = await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    assert len(trade.submit_calls) == 0  # recovery never resubmits
    assert len(trade.lookup_calls) == 1
    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"  # found -> acked


async def test_resolving_an_acknowledged_nonterminal_order_refreshes_evidence(
    repo: ClerkSqliteRepository,
) -> None:
    trade = _FakeTrade()
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=trade)
    assert len(trade.lookup_calls) == 1
    assert resolved.effect_operation_id == submission.effect_operation_id


async def test_resolve_stays_unknown_on_a_lookup_broker_error(repo: ClerkSqliteRepository) -> None:
    """A lookup ``BrokerError`` (not absence) must never be fabricated into a
    terminal outcome — stays ``unknown``, nothing written."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    trade = _FakeTrade(lookup_error=BrokerError("rate limited", broker="alpaca"))
    resolved = await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "accepted"
    order = repo.order(accepted.order_ref)
    assert order is not None and order.broker_order_id is None


async def test_resolve_stays_unknown_on_a_mismatched_client_order_id(
    repo: ClerkSqliteRepository,
) -> None:
    """A response whose ``client_order_id`` doesn't match the order being
    resolved must never be folded as this order's evidence — stays
    ``unknown`` rather than fabricating a terminal outcome from someone
    else's order."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    mismatched = _broker_order("a-completely-different-client-order-id")
    trade = _FakeTrade(lookup_result=mismatched)
    resolved = await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "accepted"
    order = repo.order(accepted.order_ref)
    assert order is not None and order.broker_order_id is None


# ── Namespace-attributed exposure and fold idempotency ──────────────────────


async def test_fills_fold_into_namespace_attributed_exposure_not_account_netting(
    repo: ClerkSqliteRepository,
) -> None:
    """Two different strategy instances trading the same symbol must get
    independently attributed positions, never netted against each other."""
    other_sid = "qqq-bot"
    repo.register_strategy_instance(strategy_instance_id=other_sid, symbol="SPY", config_hash="h2")

    trade_a = _FakeTrade(submit_result=_broker_order("will-be-replaced", filled_quantity=3, filled_avg_price=500.0))
    submission_a = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-a",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=3),
        trade=trade_a,
    )

    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=other_sid, lifecycle_run_id=RUN_ID)
    trade_b = _FakeTrade(submit_result=_broker_order("will-be-replaced", filled_quantity=5, filled_avg_price=501.0))
    await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=other_sid,
        decision_id="dec-b",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=5),
        trade=trade_b,
    )

    assert repo.position(SID, "SPY") == 3.0
    assert repo.position(other_sid, "SPY") == 5.0
    fills = repo.fills_for_order(submission_a.order_ref)
    assert len(fills) == 1


async def test_position_symbols_are_normalized_at_write_and_read_boundaries(
    repo: ClerkSqliteRepository,
) -> None:
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="lowercase-symbol",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=2),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None
    facts = OrderFillObservedFacts(
        symbol="spy",
        side="BUY",
        cumulative_filled_quantity=2.0,
        avg_price=100.0,
        is_correction=False,
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="ORDER_FILL_OBSERVED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=1_700_000_000_100,
            clerk_observed_at_ms=repo.clock(),
            summary_code="ORDER_FILL_OBSERVED",
            facts_json=facts.to_facts_json(),
        )
    )

    assert repo.position(SID, "spy") == pytest.approx(2.0)
    assert repo.position(SID, "SPY") == pytest.approx(2.0)
    assert repo.attributed_positions_by_symbol() == {"SPY": pytest.approx(2.0)}


async def test_duplicate_fill_observation_does_not_double_count(
    repo: ClerkSqliteRepository,
) -> None:
    """Re-observing the same broker order state twice (e.g. a redundant
    reconciliation poll) must not double the attributed exposure."""
    trade = _FakeTrade(submit_result=_broker_order("x", filled_quantity=4, filled_avg_price=500.0))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=4),
        trade=trade,
    )
    assert repo.position(SID, "SPY") == 4.0

    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=trade)
    assert repo.position(SID, "SPY") == 4.0  # unchanged
    assert len(repo.fills_for_order(submission.order_ref)) == 1
    assert resolved.effect_operation_id == submission.effect_operation_id


async def test_out_of_order_broker_state_event_does_not_regress_orders_broker_state(
    repo: ClerkSqliteRepository,
) -> None:
    """§3c: an event whose source timestamp is older than the value already
    recorded must not regress ``orders.broker_state``."""
    trade = _FakeTrade(submit_result=_broker_order("x", status="accepted", updated_at_ms=1_700_000_001_000))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"

    stale = _broker_order(submission.order_ref, status="pending_new", updated_at_ms=1_700_000_000_600)
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=stale)

    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"  # not regressed


def test_fill_quantity_with_no_avg_price_withholds_ack_and_folds_uncertain(
    repo: ClerkSqliteRepository,
) -> None:
    """A broker snapshot reporting ``filled_quantity > 0`` with no
    ``filled_avg_price`` can't be recorded as a fill (``fills.price`` is
    ``NOT NULL``). Acking anyway would close the door on ever re-observing
    that fill, so both the fill and the ack are withheld and the effect
    stays ``unknown`` for a later resolution to retry."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=3),
    )
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None
    anomalous = _broker_order(accepted.order_ref, status="partially_filled", filled_quantity=3, filled_avg_price=None)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=anomalous)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    order = repo.order(accepted.order_ref)
    assert order is not None and order.broker_order_id is None  # ack withheld
    assert repo.fills_for_order(accepted.order_ref) == []
    assert repo.position(SID, "SPY") == 0.0


def test_partial_fill_delta_price_is_the_weighted_average_not_the_cumulative_one(
    repo: ClerkSqliteRepository,
) -> None:
    """Alpaca's ``filled_avg_price`` is the cumulative volume-weighted
    average over the *whole* order, not a per-delta price. 2 shares @ $10
    then a cumulative 5 @ $20 average means the second delta (3 shares) must
    be priced at $26.666... (``(5*20 - 2*10) / 3``), not copied as $20 —
    ``docs/references/clerk-fill-quantity-tolerance.md`` pins this."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=5),
    )
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None

    first = _broker_order(accepted.order_ref, status="partially_filled", filled_quantity=2, filled_avg_price=10.0)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=first)
    second = _broker_order(accepted.order_ref, status="filled", filled_quantity=5, filled_avg_price=20.0)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=second)

    fills = repo.fills_for_order(accepted.order_ref)
    assert len(fills) == 2
    assert fills[0]["qty"] == 2.0 and fills[0]["price"] == 10.0
    assert abs(fills[1]["qty"] - 3.0) < 1e-9
    assert abs(fills[1]["price"] - (80.0 / 3.0)) < 1e-9
    assert repo.position(SID, "SPY") == 5.0


def test_fractional_residual_reobservation_does_not_create_a_spurious_fill(
    repo: ClerkSqliteRepository,
) -> None:
    """Re-observing a mathematically-identical cumulative quantity that
    differs only by float64 residue must not create a second fill row or
    drift the attributed position — the FILL_QTY_EPSILON-pinned gate, not a
    bare ``delta_qty <= 0``."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=2.5),
    )
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None

    first = _broker_order(accepted.order_ref, status="filled", filled_quantity=2.5, filled_avg_price=500.0)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=first)
    residual = _broker_order(
        accepted.order_ref,
        status="filled",
        filled_quantity=2.5 + 4e-13,
        filled_avg_price=500.0,
    )
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=residual)

    assert len(repo.fills_for_order(accepted.order_ref)) == 1
    assert repo.position(SID, "SPY") == 2.5


def test_fill_is_durable_before_ack_so_a_crash_between_them_recovers_cleanly(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fold_order_evidence`` appends two independent transitions when a
    fill is reported. If the process dies between them, the *fill* must
    already be durable and the order must still look un-acked
    (``broker_order_id is None``). Recovery re-polls every nonterminal effect
    and safely reconstructs the missing acknowledgement."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=4),
    )
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None
    filled_order = _broker_order(accepted.order_ref, status="filled", filled_quantity=4, filled_avg_price=500.0)

    real_append = repo.append_transition
    calls = {"n": 0}

    def crash_on_second_append(transition: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash between the two evidence transitions")
        return real_append(transition)

    monkeypatch.setattr(repo, "append_transition", crash_on_second_append)
    with pytest.raises(RuntimeError):
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=filled_order)
    monkeypatch.undo()

    order = repo.order(accepted.order_ref)
    assert order is not None and order.broker_order_id is None  # not yet acked
    assert repo.position(SID, "SPY") == 4.0  # but the fill already landed durably

    # "recovery": re-observe the same broker snapshot for real.
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=filled_order)
    order = repo.order(accepted.order_ref)
    assert order is not None and order.broker_order_id is not None  # now acked
    assert repo.position(SID, "SPY") == 4.0  # not double-counted
    assert len(repo.fills_for_order(accepted.order_ref)) == 1


async def test_ack_with_no_source_timestamp_does_not_crash_or_regress_broker_state(
    repo: ClerkSqliteRepository,
) -> None:
    """``BrokerOrder.updated_at_ms`` is optional (Alpaca can omit it); a
    ``None`` source timestamp must never crash the §3c idempotency guard,
    and must never be treated as newer than an already-recorded real one."""
    trade = _FakeTrade(submit_result=_broker_order("x", status="accepted", updated_at_ms=1_700_000_000_900))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"

    undated = _broker_order(submission.order_ref, status="pending_new", updated_at_ms=None)
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=undated)

    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"  # not regressed, no crash


# ── Identity/validation guardrails, matching commands.py's precedent ───────


def test_accept_rejects_a_colon_in_strategy_instance_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id="a:b",
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )


def test_accept_rejects_a_colon_in_decision_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="a:b",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )


def test_accept_rejects_a_colon_in_lifecycle_run_id(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(InvalidIdentityError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id="a:b",
            leg=_leg(),
        )


def test_accept_on_unknown_strategy_instance_is_a_typed_error_not_a_raw_500(
    repo: ClerkSqliteRepository,
) -> None:
    with pytest.raises(UnknownStrategyInstanceError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id="never-registered",
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )


def test_accept_binds_the_active_run_onto_command_and_effect_operation(
    repo: ClerkSqliteRepository,
) -> None:
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    active = repo.active_run(SID)
    assert active is not None
    assert accepted.command.run_id == active.run_id
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.run_id == active.run_id


def test_evidence_transitions_carry_the_same_run_id_as_the_accept(
    repo: ClerkSqliteRepository,
) -> None:
    """``ORDER_SUBMIT_ACKED``/``ORDER_FILL_OBSERVED`` must carry the same
    ``run_id`` as the ``ENTER_ACCEPTED`` transition that created the effect
    operation — otherwise the hash-chained log loses run attribution for
    exactly the rows that carry fills and acks (CodeRabbit)."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=3),
    )
    assert accepted.order_ref is not None and accepted.effect_operation_id is not None
    active = repo.active_run(SID)
    assert active is not None

    filled = _broker_order(accepted.order_ref, status="filled", filled_quantity=3, filled_avg_price=500.0)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=filled)

    transitions = repo.transitions_for_order(accepted.order_ref)
    evidence_kinds = {"ORDER_SUBMIT_ACKED", "ORDER_FILL_OBSERVED"}
    evidence = [t for t in transitions if t["transition_kind"] in evidence_kinds]
    assert len(evidence) == 2
    assert all(t["run_id"] == active.run_id for t in evidence)


async def test_uncertain_and_failed_transitions_also_carry_the_run_id(
    repo: ClerkSqliteRepository,
) -> None:
    """Same fix as the ack/fill case, applied to ``_fold_uncertain`` and
    ``_fold_failed`` — every evidence-bearing transition for an ENTER
    attributes back to the run that made the decision, not just the accept."""
    active = repo.active_run(SID)
    assert active is not None

    rejected = _FakeTrade(submit_error=BrokerRequestInvalid("bad request", broker="alpaca"))
    failed_submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-failed",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=rejected,
    )
    lost = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True)
    uncertain_submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-uncertain",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=lost,
    )

    uncertain_transitions = repo.transitions_for_order(uncertain_submission.order_ref)
    assert any(
        t["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN" and t["run_id"] == active.run_id for t in uncertain_transitions
    )
    failed_transitions = repo.transitions_for_order(failed_submission.order_ref)
    assert any(
        t["transition_kind"] == "ORDER_SUBMIT_FAILED" and t["run_id"] == active.run_id for t in failed_transitions
    )


def test_accept_rejects_when_no_active_run_matches_lifecycle_run_id(
    repo: ClerkSqliteRepository,
) -> None:
    """The active-run fence (Start/Stop's, reused here): a stopped or stale
    caller must never make a bot order-capable again just by presenting a
    strategy_instance_id that exists. Nothing new is written on rejection."""
    before = len(repo.custody_transitions())
    with pytest.raises(NoActiveRunError):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id="a-run-that-was-never-started",
            leg=_leg(),
        )
    assert len(repo.custody_transitions()) == before


async def test_submit_enter_never_calls_broker_when_no_active_run_matches(
    repo: ClerkSqliteRepository,
) -> None:
    trade = _FakeTrade()
    with pytest.raises(NoActiveRunError):
        await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id="stale-run",
            leg=_leg(),
            trade=trade,
        )
    assert len(trade.submit_calls) == 0


# ── R6 admission gating (#1380) ─────────────────────────────────────────────


async def test_accept_enter_rejects_when_an_account_clerk_uncertainty_blocks_new_exposure(
    repo: ClerkSqliteRepository,
) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code="ACCOUNT_WIDE_ISSUE",
        headline="h",
        explanation="e",
        operator_impact="oi",
        next_step="ns",
    )
    before = len(repo.custody_transitions())
    with pytest.raises(AdmissionBlockedError) as exc_info:
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )
    assert exc_info.value.decision.reason_code == "ACCOUNT_WIDE_ISSUE"
    assert len(repo.custody_transitions()) == before  # no ENTER_ACCEPTED written


async def test_accept_enter_unaffected_by_a_different_bots_uncertainty(
    repo: ClerkSqliteRepository,
) -> None:
    other_sid = "qqq-bot"
    repo.register_strategy_instance(strategy_instance_id=other_sid, symbol="QQQ", config_hash="h2")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=other_sid, lifecycle_run_id="run-q")
    raise_uncertainty(
        repo,
        strategy_instance_id=other_sid,
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="h",
        explanation="e",
        operator_impact="oi",
        next_step="ns",
    )

    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert accepted.created


async def test_lost_submit_atomically_blocks_more_exposure_until_exact_recovery(
    repo: ClerkSqliteRepository,
) -> None:
    lost = _FakeTrade(
        submit_error=BrokerUnavailable("timeout", broker="alpaca"),
        lookup_absent=True,
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-unknown",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=lost,
    )
    uncertainty = repo.active_uncertainty(
        scope="BOT",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        strategy_instance_id=SID,
    )
    assert uncertainty is not None

    with pytest.raises(AdmissionBlockedError) as exc_info:
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-must-not-pass",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
        )
    assert exc_info.value.decision.reason_code == "ORDER_OUTCOME_UNKNOWN"

    await resolve_enter_submission(
        repo,
        order_ref=submission.order_ref,
        trade=_FakeTrade(lookup_result=_broker_order(submission.order_ref)),
    )
    assert (
        repo.active_uncertainty(
            scope="BOT",
            reason_code="ORDER_OUTCOME_UNKNOWN",
            strategy_instance_id=SID,
        )
        is None
    )
    assert accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-after-recovery",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    ).created


def test_exact_recovery_advances_each_effect_while_a_sibling_unknown_remains(
    repo: ClerkSqliteRepository,
) -> None:
    """Two pre-accepted operations share one episode without stranding the first."""
    first = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-preaccepted-a",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    second = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-preaccepted-b",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    assert first.effect_operation_id and first.order_ref
    assert second.effect_operation_id and second.order_ref
    fold_uncertain(
        repo,
        effect_operation_id=first.effect_operation_id,
        order_ref=first.order_ref,
        why="lost A",
    )
    fold_uncertain(
        repo,
        effect_operation_id=second.effect_operation_id,
        order_ref=second.order_ref,
        why="lost B",
    )

    fold_order_evidence(
        repo,
        effect_operation_id=first.effect_operation_id,
        order=_broker_order(first.order_ref, order_id="bo-a"),
    )
    first_after = repo.effect_operation(first.effect_operation_id)
    second_after = repo.effect_operation(second.effect_operation_id)
    assert first_after is not None and first_after.state == "in_progress"
    assert second_after is not None and second_after.state == "unknown"
    assert repo.active_uncertainty(
        scope="BOT",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        strategy_instance_id=SID,
    )

    fold_order_evidence(
        repo,
        effect_operation_id=second.effect_operation_id,
        order=_broker_order(second.order_ref, order_id="bo-b"),
    )
    assert repo.effect_operation(first.effect_operation_id).state == "in_progress"  # type: ignore[union-attr]
    assert repo.effect_operation(second.effect_operation_id).state == "in_progress"  # type: ignore[union-attr]
    assert (
        repo.active_uncertainty(
            scope="BOT",
            reason_code="ORDER_OUTCOME_UNKNOWN",
            strategy_instance_id=SID,
        )
        is None
    )


# ── Operation claim fences broker contact (pinned contract §2) ──────────────


async def test_submit_enter_claims_the_operation_before_broker_contact(
    repo: ClerkSqliteRepository,
) -> None:
    """The attempt claim is live during contact and released afterward."""

    def assert_claimed(_leg: BrokerOrderLeg, _client_order_id: str) -> None:
        with pytest.raises(OperationClaimError):
            repo.claim_effect_operation(effect_operation_id=f"effect:{SID}:dec-1", owner="other-process")

    trade = _FakeTrade(on_submit=assert_claimed)
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=trade,
    )
    after = repo.claim_effect_operation(effect_operation_id=submission.effect_operation_id, owner="other-process")
    assert repo.release_operation_claim(effect_operation_id=submission.effect_operation_id, token=after.token)


async def test_expired_submit_attempt_cannot_fold_its_late_broker_response(
    repo: ClerkSqliteRepository,
) -> None:
    def expire_claim(_leg: BrokerOrderLeg, _client_order_id: str) -> None:
        repo._conn.execute(
            "UPDATE effect_operations SET claim_expires_at_ms = claimed_at_ms + 1 WHERE effect_operation_id = ?",
            (f"effect:{SID}:dec-1",),
        )
        repo._conn.commit()
        repo.clock.advance(2)  # type: ignore[attr-defined]

    trade = _FakeTrade(on_submit=expire_claim)
    with pytest.raises(OperationClaimError):
        await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
            trade=trade,
        )

    effect_id = f"effect:{SID}:dec-1"
    order = repo.order_for_effect_operation(effect_id)
    assert order is not None and order.broker_order_id is None

    recovered = await resolve_enter_submission(repo, order_ref=order.order_ref, trade=_FakeTrade())
    assert recovered.effect_operation_id == effect_id
    assert repo.order(order.order_ref).broker_order_id is not None  # type: ignore[union-attr]


async def test_resolve_enter_submission_fails_closed_when_operation_claimed_elsewhere(
    repo: ClerkSqliteRepository,
) -> None:
    """Two concurrent recovery sweeps for the same order must not both reach
    the broker: the second one's claim attempt loses to the live owner."""
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
    )
    repo.claim_effect_operation(effect_operation_id=accepted.effect_operation_id, owner="other-process")

    trade = _FakeTrade()
    with pytest.raises(OperationClaimError):
        await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    assert len(trade.lookup_calls) == 0
