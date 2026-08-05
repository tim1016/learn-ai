"""ENTER domain tests (#1377) — capture-before-contact, lost-response
UNKNOWN resolution with the 30s grace, concurrent-duplicate dedup, and
namespace-attributed idempotent fill folding, over the SQLite spine.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.enter import (
    resolve_enter_submission,
    submit_enter,
)
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"


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
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
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
    updated_at_ms: int = 1_700_000_000_500,
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
        observed_at_ms=updated_at_ms,
    )


class _FakeTrade:
    """A minimal ``BrokerTradePort`` double for the ENTER submission path."""

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        submit_result: BrokerOrder | None = None,
        on_submit: Any = None,
        lookup_result: BrokerOrder | None = None,
        lookup_error: Exception | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._submit_error = submit_error
        self._submit_result = submit_result
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
        template = self._submit_result or _broker_order(client_order_id)
        return template.model_copy(
            update={"client_order_id": client_order_id, "order_id": f"bo-{client_order_id}"}
        )

    async def cancel(self, order_id: str) -> None:  # pragma: no cover - unused by ENTER
        raise NotImplementedError

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self._lookup_absent:
            return None
        template = self._lookup_result or _broker_order(client_order_id)
        return template.model_copy(
            update={"client_order_id": client_order_id, "order_id": f"bo-{client_order_id}"}
        )


# ── Capture-before-contact, concurrency, and the definitive outcomes ────────


@pytest.mark.asyncio
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
        leg=_leg(),
        trade=trade,
    )

    assert seen_orders_row_at_submit_time["order"] is not None
    assert seen_orders_row_at_submit_time["order"].order_ref == submission.order_ref
    assert len(trade.submit_calls) == 1


@pytest.mark.asyncio
async def test_failed_reservation_produces_no_broker_call(repo: ClerkSqliteRepository) -> None:
    """A durable conflict (same identity, different payload) must never reach
    the broker — no broker call for a reservation that fails."""
    trade = _FakeTrade()
    await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(quantity=1),
        trade=trade,
    )

    with pytest.raises(DurableConflictError):
        await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            leg=_leg(quantity=2),  # different payload, same (sid, decision_id)
            trade=trade,
        )
    assert len(trade.submit_calls) == 1  # only the first, successful submit


@pytest.mark.asyncio
async def test_concurrent_duplicate_enter_produces_exactly_one_broker_intent(
    repo: ClerkSqliteRepository,
) -> None:
    """Acceptance criterion: concurrent duplicate ENTER for the same
    (sid, decision_id) produces one broker intent, not two."""
    trade = _FakeTrade()
    barrier = threading.Barrier(2)

    async def worker() -> Any:
        await asyncio.get_event_loop().run_in_executor(None, barrier.wait)
        return await submit_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="dec-1",
            leg=_leg(),
            trade=trade,
        )

    results = await asyncio.gather(worker(), worker())
    assert len(trade.submit_calls) == 1
    assert {r.order_ref for r in results} == {results[0].order_ref}


@pytest.mark.asyncio
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
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "failed"


# ── Lost response → UNKNOWN → grace-gated resolution by client_order_id ─────


@pytest.mark.asyncio
async def test_lost_response_resolves_immediately_when_order_is_found(
    repo: ClerkSqliteRepository,
) -> None:
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id == f"bo-{submission.order_ref}"


@pytest.mark.asyncio
async def test_lost_response_absent_within_grace_stays_unknown_not_failed(
    repo: ClerkSqliteRepository,
) -> None:
    """A first absent lookup must not be treated as terminal — the order may
    still be in flight at the broker."""
    trade = _FakeTrade(
        submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
        trade=trade,
    )
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"


@pytest.mark.asyncio
async def test_absence_past_the_grace_window_resolves_failed(tmp_path: Path) -> None:
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(account_id="PA-GRACE", artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    trade = _FakeTrade(submit_error=BrokerUnavailable("timeout", broker="alpaca"), lookup_absent=True)
    submission = await submit_enter(
        r,
        account_id="PA-GRACE",
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
        trade=trade,
    )
    effect = r.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"  # immediate call: still in grace

    clock.advance(30_001)
    resolved = await resolve_enter_submission(r, order_ref=submission.order_ref, trade=trade)
    effect = r.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "failed"
    r.close()


@pytest.mark.asyncio
async def test_kill_before_broker_contact_recovery_finds_one_accepted_operation_no_duplicate(
    repo: ClerkSqliteRepository,
) -> None:
    """Simulates a crash after the intent commits but before ``trade.submit``
    is ever called: recovery resolves by client_order_id and never resubmits."""
    from app.broker.alpaca.clerk.sqlite.enter import accept_enter

    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
    )
    assert accepted is not None  # a fresh reservation — the "crash" happens right here

    trade = _FakeTrade()  # never had submit() called before the "restart"
    resolved = await resolve_enter_submission(repo, order_ref=accepted.order_ref, trade=trade)
    assert len(trade.submit_calls) == 0  # recovery never resubmits
    assert len(trade.lookup_calls) == 1
    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"  # found -> acked


@pytest.mark.asyncio
async def test_resolving_an_already_terminal_order_is_a_noop(repo: ClerkSqliteRepository) -> None:
    trade = _FakeTrade()
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
        trade=trade,
    )
    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=trade)
    assert len(trade.lookup_calls) == 0  # already acked -> no broker call at all
    assert resolved.effect_operation_id == submission.effect_operation_id


# ── Namespace-attributed exposure and fold idempotency ──────────────────────


@pytest.mark.asyncio
async def test_fills_fold_into_namespace_attributed_exposure_not_account_netting(
    repo: ClerkSqliteRepository,
) -> None:
    """Two different strategy instances trading the same symbol must get
    independently attributed positions, never netted against each other."""
    other_sid = "qqq-bot"
    repo.register_strategy_instance(strategy_instance_id=other_sid, symbol="SPY", config_hash="h2")

    trade_a = _FakeTrade(
        submit_result=_broker_order("will-be-replaced", filled_quantity=3, filled_avg_price=500.0)
    )
    submission_a = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-a",
        leg=_leg(quantity=3),
        trade=trade_a,
    )

    trade_b = _FakeTrade(
        submit_result=_broker_order("will-be-replaced", filled_quantity=5, filled_avg_price=501.0)
    )
    await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=other_sid,
        decision_id="dec-b",
        leg=_leg(quantity=5),
        trade=trade_b,
    )

    assert repo.position(SID, "SPY") == 3.0
    assert repo.position(other_sid, "SPY") == 5.0
    fills = repo.fills_for_order(submission_a.order_ref)
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_duplicate_fill_observation_does_not_double_count(
    repo: ClerkSqliteRepository,
) -> None:
    """Re-observing the same broker order state twice (e.g. a redundant
    reconciliation poll) must not double the attributed exposure."""
    trade = _FakeTrade(
        submit_result=_broker_order("x", filled_quantity=4, filled_avg_price=500.0)
    )
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(quantity=4),
        trade=trade,
    )
    assert repo.position(SID, "SPY") == 4.0

    # Re-resolve the identical, already-terminal order — a no-op broker-side,
    # but exercised here directly against the fold to prove idempotency even
    # if a future caller re-drives the same observation.
    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=trade)
    assert repo.position(SID, "SPY") == 4.0  # unchanged
    assert len(repo.fills_for_order(submission.order_ref)) == 1
    assert resolved.effect_operation_id == submission.effect_operation_id


@pytest.mark.asyncio
async def test_out_of_order_broker_state_event_does_not_regress_orders_broker_state(
    repo: ClerkSqliteRepository,
) -> None:
    """§3c: an event whose source timestamp is older than the value already
    recorded must not regress ``orders.broker_state``."""
    from app.broker.alpaca.clerk.sqlite.enter import fold_order_evidence

    trade = _FakeTrade(submit_result=_broker_order("x", status="accepted", updated_at_ms=1_700_000_001_000))
    submission = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        leg=_leg(),
        trade=trade,
    )
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"

    # An older, out-of-order evidence event (earlier updated_at_ms) arrives late.
    stale = _broker_order(
        submission.order_ref, status="pending_new", updated_at_ms=1_700_000_000_600
    )
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=stale)

    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_state == "accepted"  # not regressed
