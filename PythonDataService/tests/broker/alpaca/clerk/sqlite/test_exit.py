"""EXIT domain tests (#1379) — cancel-first, prove-terminal, attributed
flatten, over the SQLite spine. Covers every #1379 acceptance criterion:
cancel-before-computing-quantity, partial-fill-during-cancel uses the
Clerk-proven remaining quantity, a lost cancel response blocks the closing
order, terminal success requires a precise receipt, and the nested
operation-first timeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import (
    ExitSubmission,
    accept_exit,
    resolve_exit,
    submit_exit,
)
from app.broker.alpaca.clerk.sqlite.idempotency import (
    DurableConflictError,
    NoActiveRunError,
    UnknownEntryOrderError,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    AdmissionBlockedError,
    raise_uncertainty,
)
from app.broker.contract.errors import BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"


class _TestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ms: int) -> None:
        self.value += delta_ms


def _clock_at(start_ms: int) -> _TestClock:
    return _TestClock(start_ms)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


def _leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 10}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order(
    client_order_id: str,
    *,
    order_id: str = "bo-1",
    symbol: str = "SPY",
    side: str = "buy",
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
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=10.0,
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


class _FakeTrade:
    """A configurable ``BrokerTradePort`` double for EXIT's cancel/submit/poll."""

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        submit_result: BrokerOrder | None = None,
        submit_client_order_id_override: str | None = None,
        cancel_error: Exception | None = None,
        lookup_results: list[BrokerOrder | None] | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self._submit_error = submit_error
        self._submit_result = submit_result
        self._submit_client_order_id_override = submit_client_order_id_override
        self._cancel_error = cancel_error
        # A queue of successive lookup results — each call to
        # get_order_by_client_order_id pops the next one, so a test can model
        # "first poll shows still-working, second poll shows canceled."
        self._lookup_results = list(lookup_results) if lookup_results is not None else None
        self._lookup_error = lookup_error
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._submit_error is not None:
            raise self._submit_error
        returned_client_order_id = self._submit_client_order_id_override or client_order_id
        template = self._submit_result or _broker_order(returned_client_order_id, side=leg.side)
        return template.model_copy(
            update={
                "client_order_id": returned_client_order_id,
                "order_id": f"bo-{returned_client_order_id}",
            }
        )

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self._cancel_error is not None:
            raise self._cancel_error
        return None

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_results is not None:
            if not self._lookup_results:
                return None
            result = self._lookup_results.pop(0)
            if result is not None:
                return result.model_copy(update={"client_order_id": client_order_id})
            return None
        return _broker_order(client_order_id, status="canceled").model_copy(
            update={"order_id": f"bo-{client_order_id}"}
        )


async def _make_entry(
    repo: ClerkSqliteRepository,
    *,
    decision_id: str = "enter-1",
    quantity: float = 10,
    filled_quantity: float = 0.0,
    status: str = "accepted",
) -> str:
    """Drive a real ENTER through to a fully-acked (and optionally
    partially/fully filled) working order, returning its order_ref."""
    trade = _FakeTrade(
        submit_result=_broker_order(
            "placeholder",
            status=status,
            filled_quantity=filled_quantity,
            filled_avg_price=100.0 if filled_quantity > 0 else None,
        )
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id=decision_id,
        lifecycle_run_id=RUN_ID,
        leg=_leg(quantity=quantity),
        trade=trade,
    )
    assert submission.order_ref is not None
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id is not None
    return submission.order_ref


# ── accept_exit ──────────────────────────────────────────────────────────────


async def test_accept_exit_links_resolution_custody_without_reparenting_origin(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.created
    assert accepted.effect_operation_id is not None
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.kind == "EXIT" and effect.state == "accepted"
    entry_order = repo.order(entry_ref)
    assert entry_order is not None
    assert entry_order.effect_operation_id != accepted.effect_operation_id
    linked = repo.orders_for_effect_operation(accepted.effect_operation_id)
    assert [order.order_ref for order in linked] == [entry_ref]


async def test_accept_exit_captures_every_same_symbol_sibling_entry(
    repo: ClerkSqliteRepository,
) -> None:
    first = await _make_entry(repo, decision_id="enter-1", quantity=5, status="filled", filled_quantity=5)
    second = await _make_entry(repo, decision_id="enter-2", quantity=5, status="filled", filled_quantity=5)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=first,
    )
    assert accepted.effect_operation_id is not None
    linked = repo.orders_for_effect_operation(accepted.effect_operation_id)
    assert {order.order_ref for order in linked if order.role == "ENTRY"} == {first, second}

    first_filled = _broker_order(first, status="filled", filled_quantity=5, filled_avg_price=100)
    second_filled = _broker_order(
        second,
        order_id="bo-2",
        status="filled",
        filled_quantity=5,
        filled_avg_price=100,
    )
    trade = _FakeTrade(
        lookup_results=[first_filled, second_filled, first_filled, second_filled],
        submit_result=_broker_order(
            "placeholder",
            side="sell",
            status="filled",
            filled_quantity=10,
            filled_avg_price=101,
        ),
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert set(trade.lookup_calls[:2]) == {first, second}
    assert trade.lookup_calls[2:4] == trade.lookup_calls[:2]
    assert len(trade.submit_calls) == 1
    assert trade.submit_calls[0][0].quantity == pytest.approx(10)
    assert repo.position(SID, "SPY") == pytest.approx(0)


async def test_active_exit_fences_a_concurrent_new_enter(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, status="accepted")
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None

    with pytest.raises(AdmissionBlockedError) as raised:
        await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="enter-after-exit",
            lifecycle_run_id=RUN_ID,
            leg=_leg(),
            trade=_FakeTrade(),
        )

    assert raised.value.decision.reason_code == "EXIT_IN_PROGRESS"


async def test_accept_exit_rejects_an_unknown_entry_order_ref(repo: ClerkSqliteRepository) -> None:
    with pytest.raises(UnknownEntryOrderError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-1",
            lifecycle_run_id=RUN_ID,
            entry_order_ref="learn-ai/spy-bot/v1:doesnotexist",
        )


async def test_accept_exit_rejects_an_entry_order_belonging_to_a_different_bot(
    repo: ClerkSqliteRepository,
) -> None:
    repo.register_strategy_instance(strategy_instance_id="qqq-bot", symbol="QQQ", config_hash="h2")
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id="qqq-bot", lifecycle_run_id="run-q")
    entry_ref = await _make_entry(repo)

    with pytest.raises(UnknownEntryOrderError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id="qqq-bot",
            decision_id="exit-1",
            lifecycle_run_id="run-q",
            entry_order_ref=entry_ref,
        )


async def test_accept_exit_rejects_an_entry_order_already_under_a_live_exit(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo)
    accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    with pytest.raises(UnknownEntryOrderError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-2",
            lifecycle_run_id=RUN_ID,
            entry_order_ref=entry_ref,
        )


async def test_accept_exit_rejects_when_no_active_run_matches(repo: ClerkSqliteRepository) -> None:
    entry_ref = await _make_entry(repo)
    with pytest.raises(NoActiveRunError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-1",
            lifecycle_run_id="stale-run",
            entry_order_ref=entry_ref,
        )


async def test_accept_exit_same_decision_is_a_transport_retry(repo: ClerkSqliteRepository) -> None:
    entry_ref = await _make_entry(repo)
    first = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    second = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert not second.created
    assert second.effect_operation_id == first.effect_operation_id


async def test_accept_exit_same_decision_id_different_entry_is_a_durable_conflict(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref_1 = await _make_entry(repo, decision_id="enter-1")
    entry_ref_2 = await _make_entry(repo, decision_id="enter-2")
    accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref_1,
    )
    with pytest.raises(DurableConflictError):
        accept_exit(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="exit-1",
            lifecycle_run_id=RUN_ID,
            entry_order_ref=entry_ref_2,
        )


# ── resolve_exit: cancel-first sequencing ────────────────────────────────────


async def test_resolve_exit_cancels_the_working_entry_before_computing_quantity(
    repo: ClerkSqliteRepository,
) -> None:
    """#1379 acceptance: EXIT cancels working entries before computing the
    reducing quantity. A cancel that resolves to a clean (zero-fill)
    cancellation means no reducing order is needed at all — the acceptance
    criterion is the ordering (cancel-and-poll happens, and no submit call
    is made until after it resolves), not that a reducing order always
    follows."""
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    trade = _FakeTrade(lookup_results=[_broker_order(entry_ref, status="canceled", filled_quantity=0.0)])
    assert accepted.effect_operation_id is not None
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.cancel_calls == [f"bo-{entry_ref}"]
    assert trade.lookup_calls == [entry_ref]
    assert trade.submit_calls == []  # nothing filled before the clean cancel; nothing to reduce


async def test_resolve_exit_skips_cancel_when_entry_already_terminal(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    trade = _FakeTrade()
    assert accepted.effect_operation_id is not None
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.cancel_calls == []  # already terminal; never attempted


async def test_partial_fill_during_cancel_uses_only_the_clerk_proven_remaining_quantity(
    repo: ClerkSqliteRepository,
) -> None:
    """#1379 acceptance: a working entry that partially fills during
    cancellation must have the close use only the Clerk-proven remaining
    ATTRIBUTED quantity — the 4 shares actually bought before cancellation
    took effect (attributed_qty=+4), not the original REQUESTED order
    quantity of 10 (of which only 4 ever became real exposure to close)."""
    entry_ref = await _make_entry(repo, quantity=10, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        lookup_results=[
            _broker_order(entry_ref, status="canceled", filled_quantity=4.0, filled_avg_price=100.0),
            _broker_order(entry_ref, status="canceled", filled_quantity=4.0, filled_avg_price=100.0),
        ]
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert len(trade.submit_calls) == 1
    reducing_leg, _ = trade.submit_calls[0]
    assert reducing_leg.quantity == pytest.approx(4.0)
    assert reducing_leg.side == "sell"


async def test_lost_cancel_response_blocks_the_closing_order(repo: ClerkSqliteRepository) -> None:
    """#1379 acceptance: a lost cancel response means no closing order until
    cancellation/fill truth is proven."""
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(cancel_error=BrokerUnavailable("timeout"))
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.submit_calls == []  # no reducing order submitted
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    transitions = repo.transitions_for_order(entry_ref)
    assert any(t["transition_kind"] == "ORDER_CANCEL_UNCERTAIN" for t in transitions)
    assert result.reducing_order_ref is None


async def test_a_definitive_cancel_error_falls_through_to_the_poll(
    repo: ClerkSqliteRepository,
) -> None:
    """A vendor 422 'not cancelable' (order already resolved at the broker)
    is not itself a failure — the very next poll discovers the truth (the
    entry fully filled, so the position is now +10 and needs reducing) and
    EXIT proceeds to submit the reducing order in the same pass."""
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        cancel_error=BrokerRequestInvalid("not cancelable", broker="alpaca"),
        lookup_results=[
            _broker_order(entry_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.0),
            _broker_order(entry_ref, status="filled", filled_quantity=10.0, filled_avg_price=100.0),
        ],
    )
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    entry_order = repo.order(entry_ref)
    assert entry_order is not None and entry_order.broker_state == "filled"
    assert len(trade.submit_calls) == 1
    reducing_leg, _ = trade.submit_calls[0]
    assert reducing_leg.side == "sell" and reducing_leg.quantity == pytest.approx(10.0)
    assert result.reducing_order_ref is not None


async def test_lost_poll_response_stays_uncertain(repo: ClerkSqliteRepository) -> None:
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(lookup_error=BrokerUnavailable("timeout"))
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    assert trade.submit_calls == []


async def test_a_mismatched_client_order_id_on_the_cancel_poll_stays_uncertain(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    mismatched = _broker_order("some-other-order-ref", status="canceled")

    class _VerbatimTrade(_FakeTrade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            self.lookup_calls.append(client_order_id)
            return mismatched  # returned verbatim, not forced to match

    verbatim_trade = _VerbatimTrade()
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=verbatim_trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    assert verbatim_trade.submit_calls == []


async def test_still_working_after_cancel_and_poll_leaves_no_reducing_order(
    repo: ClerkSqliteRepository,
) -> None:
    """The cancel request succeeded but the broker still reports the entry
    as working (e.g. an async cancel not yet applied) — no reducing order
    is submitted this pass."""
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(lookup_results=[_broker_order(entry_ref, status="accepted", filled_quantity=0.0)])
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.submit_calls == []
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"


# ── resolve_exit: zero-remaining, reducing order, verification ──────────────


async def test_zero_remaining_quantity_skips_the_reducing_order_entirely(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(lookup_results=[_broker_order(entry_ref, status="canceled", filled_quantity=0.0)])
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.submit_calls == []
    assert result.reducing_order_ref is None
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "succeeded"
    transitions = repo.transitions_for_order(entry_ref)
    assert any(t["transition_kind"] == "EXIT_ATTRIBUTED_FLAT" for t in transitions)


async def test_reducing_order_fully_filled_yields_a_succeeded_terminal_receipt(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        submit_result=_broker_order(
            "placeholder", status="filled", filled_quantity=10.0, filled_avg_price=101.0, side="sell"
        )
    )
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert result.reducing_order_ref is not None
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "succeeded"
    reducing_order = repo.order(result.reducing_order_ref)
    assert reducing_order is not None and reducing_order.role == "REDUCING"
    assert repo.position(SID, "SPY") == pytest.approx(0.0)


async def test_reducing_order_lost_submit_response_stays_uncertain(
    repo: ClerkSqliteRepository,
) -> None:
    """#1379 acceptance's 'precise terminal receipt' rule, reducing-order
    side: a lost submit response never wraps as a generic success."""
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    assert result.reducing_order_ref is not None  # the orders row exists even though unresolved

    # Late evidence for a different order linked to this same EXIT does not
    # prove the reducing submit's exact (effect, order_ref) identity.
    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        order=_broker_order(
            entry_ref,
            status="filled",
            filled_quantity=10.0,
            filled_avg_price=100.0,
        ),
    )
    effect_after_unrelated_ack = repo.effect_operation(accepted.effect_operation_id)
    assert effect_after_unrelated_ack is not None
    assert effect_after_unrelated_ack.state == "unknown"
    uncertainty = repo.active_uncertainty(
        scope="BOT",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        strategy_instance_id=SID,
    )
    assert uncertainty is not None


async def test_a_later_resolve_call_resolves_the_reducing_orders_lost_submit(
    repo: ClerkSqliteRepository,
) -> None:
    """Reuses resolve_enter_submission for the reducing order's own
    uncertain-submit resolution — proving a second resolve_exit call
    (recovery/reconciliation) makes forward progress."""
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    first_trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=first_trade)
    assert first.reducing_order_ref is not None

    second_trade = _FakeTrade()  # lookup finds the reducing order landed
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=second_trade)

    assert second_trade.submit_calls == []  # never re-submitted a duplicate
    assert second_trade.lookup_calls == [first.reducing_order_ref]
    reducing_order = repo.order(first.reducing_order_ref)
    assert reducing_order is not None and reducing_order.broker_order_id is not None


async def test_acknowledged_reducing_order_is_polled_until_terminal(
    repo: ClerkSqliteRepository,
) -> None:
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
    first = await resolve_exit(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        trade=_FakeTrade(submit_result=_broker_order("placeholder", status="accepted")),
    )
    assert first.reducing_order_ref is not None

    terminal = _broker_order(
        first.reducing_order_ref,
        side="sell",
        status="filled",
        filled_quantity=10,
        filled_avg_price=101,
    )
    trade = _FakeTrade(lookup_results=[terminal])
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    assert trade.submit_calls == []
    assert trade.lookup_calls == [first.reducing_order_ref]
    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "succeeded"


async def test_stale_snapshot_policy_blocks_exit_before_reducing_order_creation(
    repo: ClerkSqliteRepository,
) -> None:
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
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="stale",
        explanation="broker snapshot is stale",
        operator_impact="reductions are paused",
        next_step="reconcile",
    )
    terminal_entry = _broker_order(entry_ref, status="filled", filled_quantity=10, filled_avg_price=100)
    trade = _FakeTrade(lookup_results=[terminal_entry, terminal_entry])

    with pytest.raises(AdmissionBlockedError):
        await resolve_exit(
            repo,
            effect_operation_id=accepted.effect_operation_id,
            trade=trade,
        )

    assert trade.submit_calls == []
    assert all(order.role != "REDUCING" for order in repo.orders_for_effect_operation(accepted.effect_operation_id))


async def test_reducing_order_resolves_without_flattening_folds_a_precise_failure(
    repo: ClerkSqliteRepository,
) -> None:
    """#1379 acceptance: terminal EXIT renders success only with a precise
    terminal receipt — a reducing order that resolves (e.g. rejected, or
    partially filled short of target) without flattening the position must
    never be reported as a generic success."""
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        submit_result=_broker_order(
            "placeholder", status="canceled", filled_quantity=4.0, filled_avg_price=101.0, side="sell"
        )
    )
    result = await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    transitions = repo.transitions_for_order(result.reducing_order_ref)  # type: ignore[arg-type]
    assert any(t["summary_code"] == "EXIT_NOT_FLAT" for t in transitions)


async def test_mismatched_client_order_id_on_reducing_submit_stays_uncertain(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(submit_client_order_id_override="learn-ai/other-bot/v1:zzz")
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    effect = repo.effect_operation(accepted.effect_operation_id)
    assert effect is not None and effect.state == "unknown"


# ── idempotency, terminal no-ops, nested timeline ───────────────────────────


async def test_resolve_exit_on_an_already_succeeded_operation_is_a_true_noop(
    repo: ClerkSqliteRepository,
) -> None:
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    trade = _FakeTrade(
        submit_result=_broker_order(
            "placeholder", status="filled", filled_quantity=10.0, filled_avg_price=101.0, side="sell"
        )
    )
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)
    before = len(repo.custody_transitions())

    trade2 = _FakeTrade()
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade2)

    assert len(repo.custody_transitions()) == before
    assert trade2.submit_calls == [] and trade2.cancel_calls == [] and trade2.lookup_calls == []


async def test_late_and_duplicate_entry_evidence_cannot_regress_succeeded_exit(
    repo: ClerkSqliteRepository,
) -> None:
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
    await resolve_exit(
        repo,
        effect_operation_id=accepted.effect_operation_id,
        trade=_FakeTrade(
            submit_result=_broker_order(
                "placeholder",
                side="sell",
                status="filled",
                filled_quantity=10,
                filled_avg_price=101,
            )
        ),
    )
    late = _broker_order(entry_ref, status="accepted").model_copy(
        update={"updated_at_ms": 1_699_999_999_000, "observed_at_ms": 1_700_000_001_000}
    )

    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=late)
    fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=late)

    effect = repo.effect_operation(accepted.effect_operation_id)
    entry = repo.order(entry_ref)
    assert effect is not None and effect.state == "succeeded"
    assert entry is not None and entry.broker_state == "filled"
    assert repo.position(SID, "SPY") == pytest.approx(0)


async def test_same_process_overlapping_exit_resolvers_submit_one_reduction(
    repo: ClerkSqliteRepository,
) -> None:
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

    class BlockingTrade(_FakeTrade):
        def __init__(self) -> None:
            super().__init__(
                submit_result=_broker_order(
                    "placeholder",
                    side="sell",
                    status="filled",
                    filled_quantity=10,
                    filled_avg_price=101,
                )
            )
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()

        async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
            self.submit_started.set()
            await self.release_submit.wait()
            return await super().submit(leg, client_order_id=client_order_id)

    trade = BlockingTrade()
    first = asyncio.create_task(resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade))
    await asyncio.wait_for(trade.submit_started.wait(), timeout=2)

    with pytest.raises(OperationClaimError):
        await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    trade.release_submit.set()
    await first
    assert len(trade.submit_calls) == 1
    assert (
        len(
            [
                order
                for order in repo.orders_for_effect_operation(accepted.effect_operation_id)
                if order.role == "REDUCING"
            ]
        )
        == 1
    )


async def test_nested_timeline_carries_the_exit_effect_operation_id_not_the_enters(
    repo: ClerkSqliteRepository,
) -> None:
    """#1379 acceptance: the operation-first timeline nests the child-order
    transitions under the EXIT operation — every transition appended against
    entry_ref during EXIT must carry EXIT's own effect_operation_id, not
    ENTER's."""
    entry_ref = await _make_entry(repo, status="accepted", filled_quantity=0.0)
    enter_effect_id_before = repo.order(entry_ref).effect_operation_id  # type: ignore[union-attr]
    accepted = accept_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    assert accepted.effect_operation_id != enter_effect_id_before

    trade = _FakeTrade(lookup_results=[_broker_order(entry_ref, status="canceled", filled_quantity=0.0)])
    await resolve_exit(repo, effect_operation_id=accepted.effect_operation_id, trade=trade)

    transitions = repo.transitions_for_order(entry_ref)
    exit_accepted_seq = next(t["sequence"] for t in transitions if t["transition_kind"] == "EXIT_ACCEPTED")
    # Everything from EXIT_ACCEPTED onward is unambiguously EXIT's — the
    # entry's own pre-EXIT history (ENTER_ACCEPTED, and ENTER's own initial
    # ORDER_SUBMIT_ACKED from when submit_enter first acked it) correctly
    # predates the EXIT custody link and still names the ENTER effect.
    post_accept = [t for t in transitions if t["sequence"] >= exit_accepted_seq]
    assert len(post_accept) >= 2  # EXIT_ACCEPTED itself + at least the cancel-poll evidence
    for transition in post_accept:
        assert transition["effect_operation_id"] == accepted.effect_operation_id

    pre_accept = [t for t in transitions if t["sequence"] < exit_accepted_seq]
    assert pre_accept  # ENTER_ACCEPTED + the entry's own initial ack
    for transition in pre_accept:
        assert transition["effect_operation_id"] == enter_effect_id_before


async def test_submit_exit_drives_the_full_happy_path_in_one_call(repo: ClerkSqliteRepository) -> None:
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    trade = _FakeTrade(
        submit_result=_broker_order(
            "placeholder", status="filled", filled_quantity=10.0, filled_avg_price=101.0, side="sell"
        )
    )

    result = await submit_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        trade=trade,
    )

    assert isinstance(result, ExitSubmission)
    effect = repo.effect_operation(result.effect_operation_id)  # type: ignore[arg-type]
    assert effect is not None and effect.state == "succeeded"
    assert repo.position(SID, "SPY") == pytest.approx(0.0)


async def test_submit_exit_on_a_duplicate_decision_still_advances_the_state_machine(
    repo: ClerkSqliteRepository,
) -> None:
    """Unlike submit_enter, submit_exit has no 'skip resolve' branch for a
    duplicate accept — resolve_exit is safe to call every time."""
    entry_ref = await _make_entry(repo, quantity=10, status="filled", filled_quantity=10.0)
    stalled_trade = _FakeTrade(submit_error=BrokerUnavailable("timeout"))
    first = await submit_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        trade=stalled_trade,
    )
    effect = repo.effect_operation(first.effect_operation_id)  # type: ignore[arg-type]
    assert effect is not None and effect.state == "unknown"

    resuming_trade = _FakeTrade(
        lookup_results=[
            _broker_order("placeholder", status="filled", filled_quantity=10.0, filled_avg_price=101.0, side="sell")
        ]
    )
    second = await submit_exit(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="exit-1",
        lifecycle_run_id=RUN_ID,
        entry_order_ref=entry_ref,
        trade=resuming_trade,
    )
    assert not second.created  # accept_exit itself sees the transport retry
    assert resuming_trade.submit_calls == []  # never re-submitted a duplicate reducing order
    effect_after = repo.effect_operation(second.effect_operation_id)  # type: ignore[arg-type]
    assert effect_after is not None and effect_after.state == "succeeded"
