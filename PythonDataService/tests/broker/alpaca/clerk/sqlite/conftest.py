"""Shared deterministic helpers for SQLite Account Clerk tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from app.services.session_authority import et_minute_of_day_ms


class _TestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ms: int) -> None:
        self.value += delta_ms


def _clock_at(start_ms: int) -> _TestClock:
    return _TestClock(start_ms)


def _hold_transition(
    *,
    reason_code: str = "UNEXPLAINED_ORDER_HOLD",
    evidence_refs: list[str] | None = None,
    transition_kind: str = "ACCOUNT_HOLD_RAISED",
) -> TransitionInput:
    """One pre-v12 ``ACCOUNT_HOLD_RAISED``/``_REFRESHED`` transition.

    The kind is retired as a *writer* (ADR 0048 Decision 2) but is still
    replayed from any mirror recorded before v12, so this helper now builds
    the legacy shape deliberately: it is how the replay folds are exercised.
    ``ACCOUNT_HOLD_REFRESHED`` carries the same ``AccountHoldRaisedFacts``
    envelope, so one builder covers both.
    """
    facts = AccountHoldRaisedFacts(
        reason_code=reason_code,
        evidence_refs=evidence_refs or ["bo-1"],
    )
    return TransitionInput(
        transition_kind=transition_kind,
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code=transition_kind,
        facts_json=facts.to_facts_json(),
    )


# ── Shared broker doubles + factories (reused across the SQLite Clerk suite) ──
# Per AGENTS.md "don't duplicate utility functions": new test modules import
# these from here rather than copying per-file. The fake trade port is honest
# about the submitted leg (echoes side AND quantity), so an exact-close proof
# cannot pass on a wrong-sized reduction.


def _broker_leg(**overrides: Any) -> BrokerOrderLeg:
    base: dict[str, Any] = {"symbol": "SPY", "side": "buy", "quantity": 10}
    base.update(overrides)
    return BrokerOrderLeg(**base)


def _broker_order_fixture(
    client_order_id: str,
    *,
    order_id: str = "broker-order-1",
    symbol: str = "SPY",
    status: str = "accepted",
    side: str = "buy",
    quantity: float = 10.0,
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
        quantity=quantity,
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


def _broker_position_fixture(
    symbol: str, *, quantity: float, side: str = "long"
) -> BrokerPosition:
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


class _FakeTradePort:
    """A minimal, honest ``BrokerTradePort`` double.

    ``submit`` echoes the submitted leg's side *and* quantity, so a caller that
    regresses to an under- or over-sized reduction cannot pass an exact-close
    assertion. ``submitted_legs`` records each submitted leg for that check.
    ``lookup_absent`` models a broker that definitively has no order for the
    exact client order id, which is how a never-accepted submit reads.
    """

    def __init__(
        self,
        *,
        submit_error: Exception | None = None,
        lookup_absent: bool = False,
    ) -> None:
        self._submit_error = submit_error
        self._lookup_absent = lookup_absent
        self.submit_calls: list[str] = []
        self.submitted_legs: list[BrokerOrderLeg] = []
        self.cancel_calls: list[str] = []
        self.lookup_calls: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        self.submitted_legs.append(leg)
        if self._submit_error is not None:
            raise self._submit_error
        return _broker_order_fixture(
            client_order_id, side=leg.side, quantity=leg.quantity
        ).model_copy(update={"order_id": f"bo-{client_order_id}"})

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._lookup_absent:
            # Definitive absence: the broker has no order for this exact
            # client order id (Alpaca answers the read-only lookup with 404).
            return None
        return _broker_order_fixture(client_order_id).model_copy(
            update={"order_id": f"bo-{client_order_id}"}
        )


class _FakeReadPort:
    """A minimal ``BrokerReadPort`` double — only list_orders/list_positions."""

    def __init__(
        self,
        *,
        orders: list[BrokerOrder] | None = None,
        positions: list[BrokerPosition] | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        return self._positions


class _AssertingNoReconciler:
    async def reconcile_account(self, *, trigger: str) -> Any:
        raise AssertionError(f"unexpected reconciliation trigger: {trigger}")


async def _make_held_position(
    repo: ClerkSqliteRepository,
    *,
    account_id: str,
    strategy_instance_id: str,
    run_id: str,
    decision_id: str = "enter-1",
    execution_id: str = "exec-1",
    quantity: float = 10.0,
) -> str:
    """Filled entry with an exact execution slice -> attributed +quantity."""
    submission = await submit_enter(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=_broker_leg(quantity=quantity),
        trade=_FakeTradePort(),
    )
    assert submission.order_ref is not None
    filled = _broker_order_fixture(
        submission.order_ref, status="filled", quantity=quantity,
        filled_quantity=quantity, filled_avg_price=100.0,
    )
    fold_order_evidence(repo, effect_operation_id=submission.effect_operation_id, order=filled)
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=_AssertingNoReconciler()
    )
    await sink.record_lifecycle_event(
        client_order_id=submission.order_ref,
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=1_700_000_000_600,
            price=100, quantity=quantity, execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return submission.order_ref


# ── The live-envelope admission harness (ADR 0059 D4) ─────────────────────────
# One authority, one pinned clock, and the registered instances the envelope's
# two SQLite suites drive ``accept_enter`` through. Both files judge the same
# seam, so a second copy of this block is a second thing to keep true.

ENVELOPE_ACCOUNT_ID = "PA-ENVELOPE"
ENVELOPE_SID = "spy-bot"
ENVELOPE_SID_B = "qqq-bot"
ENVELOPE_RUN_ID = "run-1"
ENVELOPE_RUN_ID_B = "run-2"
ENVELOPE_T0 = 1_788_040_000_000  # a fixed int64 ms UTC; every stamp is repo.clock()


@pytest.fixture
def envelope_clock() -> _TestClock:
    return _clock_at(ENVELOPE_T0)


@pytest.fixture
def envelope_repo(
    tmp_path: Path, envelope_clock: _TestClock
) -> Iterator[ClerkSqliteRepository]:
    clerk = ClerkSqliteRepository.initialize(
        account_id=ENVELOPE_ACCOUNT_ID, artifacts_root=tmp_path, clock=envelope_clock
    )
    yield clerk
    clerk.close()


def _register_active(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    *,
    strategy_instance_id: str,
    symbol: str,
    run_id: str,
) -> None:
    """Register one instance and start its run — every stamp from ``clock``."""
    repo.register_strategy_instance(
        strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1"
    )
    submit_start_run(
        repo,
        account_id=ENVELOPE_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=run_id,
        clock=clock,
    )


@pytest.fixture
def active_instance(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> tuple[str, str]:
    _register_active(
        envelope_repo,
        envelope_clock,
        strategy_instance_id=ENVELOPE_SID,
        symbol="SPY",
        run_id=ENVELOPE_RUN_ID,
    )
    return ENVELOPE_SID, ENVELOPE_RUN_ID


@pytest.fixture
def two_active_instances(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> tuple[tuple[str, str], tuple[str, str]]:
    _register_active(
        envelope_repo,
        envelope_clock,
        strategy_instance_id=ENVELOPE_SID,
        symbol="SPY",
        run_id=ENVELOPE_RUN_ID,
    )
    _register_active(
        envelope_repo,
        envelope_clock,
        strategy_instance_id=ENVELOPE_SID_B,
        symbol="QQQ",
        run_id=ENVELOPE_RUN_ID_B,
    )
    return (ENVELOPE_SID, ENVELOPE_RUN_ID), (ENVELOPE_SID_B, ENVELOPE_RUN_ID_B)


# ── The day-P&L ledger harness (ADR 0059 D4) ──────────────────────────────────
# The seeded ledger the day-P&L rule and the envelope sync are both judged
# against. Every stamp is an explicit ``int64 ms UTC`` built from
# ``et_minute_of_day_ms``; the repository clock is fixed at ``NOON``.

DAY_PNL_ACCOUNT_ID = "PA-DAY-PNL"
DAY_PNL_SID = "day-pnl-bot"
DAY_PNL_RUN_ID = "run-day-pnl"
DAY_PNL_SYMBOL = "SPY"

# 2026-09-08 is a Tuesday; the 7th is Labor Day, so "yesterday" is Friday the 4th.
NOON = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
TODAY_OPEN = et_minute_of_day_ms(date(2026, 9, 8), 9 * 60 + 30)
YESTERDAY_NOON = et_minute_of_day_ms(date(2026, 9, 4), 12 * 60)
YESTERDAY_OPEN = et_minute_of_day_ms(date(2026, 9, 4), 9 * 60 + 30)


@pytest.fixture
def day_pnl_clock() -> _TestClock:
    return _clock_at(NOON)


@pytest.fixture
def day_pnl_repo(
    tmp_path: Path, day_pnl_clock: _TestClock
) -> Iterator[ClerkSqliteRepository]:
    """One authority with a registered, running instance — every stamp from the clock."""
    clerk = ClerkSqliteRepository.initialize(
        account_id=DAY_PNL_ACCOUNT_ID, artifacts_root=tmp_path, clock=day_pnl_clock
    )
    clerk.register_strategy_instance(
        strategy_instance_id=DAY_PNL_SID, symbol=DAY_PNL_SYMBOL, config_hash="day-pnl-config"
    )
    submit_start_run(
        clerk,
        account_id=DAY_PNL_ACCOUNT_ID,
        strategy_instance_id=DAY_PNL_SID,
        lifecycle_run_id=DAY_PNL_RUN_ID,
        clock=day_pnl_clock,
    )
    yield clerk
    clerk.close()


def _accept_day_pnl_enter(
    repo: ClerkSqliteRepository, *, decision_id: str
) -> EnterSubmission:
    accepted = accept_enter(
        repo,
        account_id=DAY_PNL_ACCOUNT_ID,
        strategy_instance_id=DAY_PNL_SID,
        decision_id=decision_id,
        lifecycle_run_id=DAY_PNL_RUN_ID,
        leg=BrokerOrderLeg(symbol=DAY_PNL_SYMBOL, side="buy", quantity=10),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    return accepted


def _append_day_pnl_slice(
    repo: ClerkSqliteRepository,
    accepted: EnterSubmission,
    *,
    execution_id: str,
    side: str,
    quantity: float,
    price: float,
    occurred_at_ms: int,
    fee: float | None = None,
    fee_fidelity: str = "not_reported",
) -> None:
    """Fold one websocket execution slice at an explicit economic time.

    ``source_event_at_ms`` is the fill's economic time — what FIFO orders by
    and what the day window filters on — so it, not the fixed repository
    clock, is what puts a fill on Friday or on Tuesday.
    """
    facts = ExecutionSliceFilledFacts(
        execution_id=execution_id,
        symbol=DAY_PNL_SYMBOL,
        side=side,
        slice_qty=quantity,
        slice_price=price,
        fee=fee,
        fee_fidelity=fee_fidelity,
        evidence_source="websocket",
        source_event_at_ms=occurred_at_ms,
    )
    result = repo.append_execution_slice_if_absent(
        execution_id=execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=lambda: TransitionInput(
            strategy_instance_id=DAY_PNL_SID,
            run_id=accepted.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref=accepted.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=facts.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=facts.to_facts_json(),
        ),
        build_coverage_conflict=lambda: (_ for _ in ()).throw(
            AssertionError("an exact first execution has nothing to conflict with")
        ),
    )
    assert result == "appended"


def _observe_foreign_order(repo: ClerkSqliteRepository, *, observed_at_ms: int) -> None:
    """Record one order the Clerk did not place, observed at an explicit instant."""
    observe_external_order(
        repo,
        order=BrokerOrder(
            broker="alpaca",
            order_id="external-order-1",
            client_order_id="alpaca-console:external-1",
            symbol="MSFT",
            asset_class="us_equity",
            side="buy",
            order_type="market",
            time_in_force="day",
            quantity=3.0,
            filled_quantity=3.0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=50.0,
            status="filled",
            submitted_at_ms=observed_at_ms,
            created_at_ms=observed_at_ms,
            updated_at_ms=observed_at_ms,
            filled_at_ms=observed_at_ms,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=observed_at_ms,
        ),
    )


@pytest.fixture
def seeded_round_trip(day_pnl_repo: ClerkSqliteRepository) -> None:
    """BUY 10 @ 100 on Friday, SELL 10 @ 110 today at noon with a $0.05 fee."""
    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="d-round-trip")
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-buy-friday",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_NOON,
        fee=0.0,
        fee_fidelity="reported",
    )
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-sell-today",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=NOON,
        fee=0.05,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_round_trip_without_fees(day_pnl_repo: ClerkSqliteRepository) -> None:
    """The same round trip, with no commission data on the closing fill."""
    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="d-round-trip-no-fees")
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-buy-friday",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_NOON,
    )
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-sell-today",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=NOON,
    )


@pytest.fixture
def seeded_round_trip_closed_yesterday(day_pnl_repo: ClerkSqliteRepository) -> None:
    """A whole round trip that opened and closed on Friday — none of it is today's."""
    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="d-closed-yesterday")
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-buy-friday-open",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=YESTERDAY_OPEN,
        fee=0.0,
        fee_fidelity="reported",
    )
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-sell-friday-noon",
        side="SELL",
        quantity=10.0,
        price=110.0,
        occurred_at_ms=YESTERDAY_NOON,
        fee=0.0,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_open_buy(day_pnl_repo: ClerkSqliteRepository) -> None:
    """BUY 10 @ 100 at today's open, still held — nothing realized yet."""
    accepted = _accept_day_pnl_enter(day_pnl_repo, decision_id="d-open-buy")
    _append_day_pnl_slice(
        day_pnl_repo,
        accepted,
        execution_id="exec-buy-today",
        side="BUY",
        quantity=10.0,
        price=100.0,
        occurred_at_ms=TODAY_OPEN,
        fee=0.0,
        fee_fidelity="reported",
    )


@pytest.fixture
def seeded_external_order_today(day_pnl_repo: ClerkSqliteRepository) -> None:
    _observe_foreign_order(day_pnl_repo, observed_at_ms=TODAY_OPEN)


@pytest.fixture
def seeded_external_order_yesterday(day_pnl_repo: ClerkSqliteRepository) -> None:
    _observe_foreign_order(day_pnl_repo, observed_at_ms=YESTERDAY_NOON)
