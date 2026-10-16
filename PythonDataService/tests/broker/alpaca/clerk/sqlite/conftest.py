"""Shared deterministic helpers for SQLite Account Clerk tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.broker.alpaca import adapter as alpaca_adapter
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    ExecutionSliceFilledFacts,
)
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import (
    BrokerActivity,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from app.broker.contract.options import BrokerOptionContract, BrokerOptionOrder, OptionOrderRequest
from app.options.contract import OptionContract
from app.schemas.exit_terms import ExitTerms
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.session_clock import pin_wall_clock_in_session
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import submit_budgeted_test_enter
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started
from tests.broker.alpaca.documented_examples import documented


@pytest.fixture
def wall_clock_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Clerk on the default clock reads a regular session, whatever the host's hour (#2596).

    The Clerk sends a market ENTER only while it could reach the broker inside
    the regular session, so a suite that sends one off the wall clock opts in
    (``pytestmark = pytest.mark.usefixtures("wall_clock_in_session")``). A test
    that pins its own clock is unaffected.
    """
    pin_wall_clock_in_session(monkeypatch)


class _TestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ms: int) -> None:
        self.value += delta_ms



def complete_fee_evidence(repo: ClerkSqliteRepository, activities: tuple[BrokerActivity, ...] = ()) -> None:
    """Explicit successful broker activity evidence, never a production fallback."""
    record_fee_evidence(repo, activities, checked_at_ms=repo.clock(), history_complete=True)


def remove_v25_order_state_for_legacy_fixture(conn: sqlite3.Connection) -> None:
    """Restore orders before the effect-state mirror was added."""
    conn.execute("DROP TRIGGER trg_orders_effect_state_insert")
    conn.execute("DROP TRIGGER trg_orders_effect_state_update")
    conn.execute("DROP INDEX ix_orders_entry_broker_state")
    conn.execute("ALTER TABLE orders DROP COLUMN effect_state")


def remove_budget_schema_for_legacy_fixture(conn: sqlite3.Connection) -> None:
    """Restore the pre-budget shape before exercising an earlier migration."""
    remove_v25_order_state_for_legacy_fixture(conn)
    assert not conn.execute("SELECT 1 FROM deployment_budgets LIMIT 1").fetchone()
    conn.execute("DROP TABLE deployment_budgets")
    conn.execute("DROP TABLE account_risk_policy")
    conn.execute("ALTER TABLE envelope_reservations DROP COLUMN exact_reference_price")
    conn.execute("ALTER TABLE envelope_reservations DROP COLUMN fee_provision_cents")
    conn.execute("DROP TRIGGER trg_budget_authority_monotonic")
    conn.execute("ALTER TABLE control_meta DROP COLUMN authorization_version")


def build_v13_authority(conn: sqlite3.Connection, *, account_id: str) -> None:
    """A real v13 file, the oldest schema a store can still migrate from.

    Built from the fresh v9..v13 DDL blocks, the same statements a fresh
    authority runs before its later blocks.
    """
    schema.apply_v9_schema(conn)
    conn.executescript(
        "\n\n".join(
            (schema.SCHEMA_V10_DDL, schema.SCHEMA_V11_DDL, schema.SCHEMA_V12_DDL, schema.SCHEMA_V13_DDL)
        )
    )
    conn.execute(
        "INSERT INTO control_meta "
        "(id, schema_version, broker, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms, reset_provenance_json, "
        "execution_lease_owner, execution_lease_expires_at_ms) "
        "VALUES (1, 13, 'alpaca', ?, 'identity', 1, 0, 1, 1, NULL, NULL, NULL)",
        (account_id,),
    )
    conn.commit()


def _clock_at(start_ms: int) -> _TestClock:
    return _TestClock(start_ms)


FIXTURE_RTH_MS = 1_700_060_400_000
"""2023-11-15 10:00 ET (a Wednesday): inside the regular session, the morning
after every ``1_700_000_000_xxx`` broker stamp these fixtures carry.

``1_700_000_000_000`` itself is 17:13 ET -- after-hours -- where an unshaped
recovery reduction's market leg cannot go out (#2440): it is re-priced as an
after-hours limit, or folded for the operator when nothing can price one. A
test of a regular-hours flatten starts here instead."""


def _walk_clock_to(repo: ClerkSqliteRepository, target_ms: int) -> None:
    """Advance to ``target_ms`` as a running Clerk would: its lease heartbeat renews throughout."""
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    step = repo.lease_ttl_ms // 3
    while clock.value < target_ms:
        clock.value = min(clock.value + step, target_ms)
        repo.renew_execution_lease()


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
# New test modules import
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

    **Option orders (#2913).** The port also takes one option order, as a
    venue that rests it would: the order is kept in ``option_orders`` in the
    shape Alpaca's documentation shows for one
    (``tests/fixtures/alpaca_documented/``), and every answer is read from
    that payload by the adapter's own strict reader, so the double cannot
    answer in a shape the real port could not. A test moves the venue with
    ``option_order_fills`` and ``option_order_ends``, and sets what goes
    wrong next through the ``option_*`` and ``cancel_*`` attributes. Nothing
    here was recorded from Alpaca.
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
        # The venue's option orders, by client order id, each as Alpaca's documented payload.
        self.option_orders: dict[str, dict[str, Any]] = {}
        self.submitted_option_orders: list[OptionOrderRequest] = []
        self.option_submit_calls: list[str] = []
        self.option_lookup_calls: list[str] = []
        # Raised by the next option submits before anything lands: a refusal, or an outage.
        self.option_submit_error: Exception | None = None
        # The order lands at the venue and the answer never comes back.
        self.option_answer_lost = False
        # Raised by every option lookup while set.
        self.option_lookup_error: Exception | None = None
        # Raised by every cancel while set; the order is left as it was.
        self.cancel_error: Exception | None = None
        # The venue takes a cancel and goes on showing the order working.
        self.cancel_is_slow = False
        # The venue's clock for the times it stamps; each stamp is later than the last.
        self.venue_clock: Callable[[], int] = lambda: 1_700_000_001_000
        self._last_venue_stamp_ms = 0

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
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_is_slow:
            return
        for payload in self.option_orders.values():
            if payload["id"] == order_id and payload["status"] in _WORKING_OPTION_ORDER_STATUSES:
                payload.update(status="canceled", canceled_at=self._venue_stamp())
                payload["updated_at"] = payload["canceled_at"]

    async def submit_option_order(self, order: OptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        self.option_submit_calls.append(client_order_id)
        self.submitted_option_orders.append(order)
        if self.option_submit_error is not None:
            raise self.option_submit_error
        self.option_orders[client_order_id] = documented_option_order_payload(
            order, client_order_id=client_order_id, at=self._venue_stamp()
        )
        if self.option_answer_lost:
            raise BrokerUnavailable("the venue took the order and its answer was lost", broker="alpaca")
        return alpaca_adapter.from_alpaca_option_order(self.option_orders[client_order_id])

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        self.option_lookup_calls.append(client_order_id)
        if self.option_lookup_error is not None:
            raise self.option_lookup_error
        payload = self.option_orders.get(client_order_id)
        return None if payload is None else alpaca_adapter.from_alpaca_option_order(payload)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        if self.option_lookup_error is not None:
            raise self.option_lookup_error
        payload = next((held for held in self.option_orders.values() if held["id"] == order_id), None)
        return None if payload is None else alpaca_adapter.from_alpaca_option_order(payload)

    def option_order_fills(self, client_order_id: str, *, contracts: int, average_price: str) -> None:
        """The venue has now filled ``contracts`` of the order in all, at ``average_price`` a share."""
        payload = self.option_orders[client_order_id]
        stamp = self._venue_stamp()
        whole = contracts == int(payload["qty"])
        payload.update(
            filled_qty=str(contracts), filled_avg_price=average_price, updated_at=stamp,
            status="filled" if whole else "partially_filled", filled_at=stamp if whole else None,
        )

    def option_order_ends(self, client_order_id: str, status: str = "canceled") -> None:
        """The venue ended the order by itself: cancelled, expired or rejected, with whatever had filled."""
        stamp = self._venue_stamp()
        self.option_orders[client_order_id].update(
            status=status, updated_at=stamp, **({f"{status}_at": stamp} if status in ("canceled", "expired") else {})
        )

    def _venue_stamp(self) -> str:
        self._last_venue_stamp_ms = max(self.venue_clock(), self._last_venue_stamp_ms + 1)
        return datetime.fromtimestamp(self._last_venue_stamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        # An option order is looked up by the option read, which reads strictly (ADR 0076).
        assert client_order_id not in self.option_orders, "the stock lookup was asked about an option order"
        self.lookup_calls.append(client_order_id)
        if self._lookup_absent:
            # Definitive absence: the broker has no order for this exact
            # client order id (Alpaca answers the read-only lookup with 404).
            return None
        return _broker_order_fixture(client_order_id).model_copy(
            update={"order_id": f"bo-{client_order_id}"}
        )


_WORKING_OPTION_ORDER_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel"})


def documented_option_order_payload(order: OptionOrderRequest, *, client_order_id: str, at: str) -> dict[str, Any]:
    """Alpaca's documented answer to one option order, with this order's own values in it.

    Every key is the documented example's (``OptionOrderResponse``); none is
    added. The order has just been taken: ``new``, nothing filled.
    """
    payload = dict(documented("option_orders", "documented_responses.json")["OptionOrderResponse"])
    payload.update(
        id=str(uuid5(NAMESPACE_URL, client_order_id)), client_order_id=client_order_id,
        symbol=order.contract.occ_symbol, qty=str(order.quantity), side=order.side.value,
        type=order.order_type.value, order_type=order.order_type.value,
        limit_price=None if order.limit_price is None else format(order.limit_price, "f"),
        status="new", filled_qty="0", filled_avg_price=None, filled_at=None,
        created_at=at, submitted_at=at, updated_at=at,
    )
    return payload


def documented_option_listing(contract: OptionContract, **changes: Any) -> BrokerOptionContract:
    """Alpaca's documented listing of one contract, as an active standard listing of ``contract``."""
    payload = dict(documented("option_contracts", "documented_contracts.json")["reference_example"])
    payload.update(
        symbol=contract.occ_symbol, root_symbol=contract.root, underlying_symbol=contract.root,
        expiration_date=contract.expiry_date.isoformat(), type=contract.right, strike_price=str(contract.strike),
    )
    payload.update(changes)
    return alpaca_adapter.from_alpaca_option_contract(payload)


class _ScriptedNonTradeActivities:
    """The Alpaca client seam for the account's non-trade activities: raw rows, the newest first, in one page."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = sorted(rows, key=lambda row: row.get("date", ""), reverse=True)

    async def list_non_trade_activities(self, *, limit: int, page_token: str | None = None) -> list[Any]:
        assert page_token is None and len(self._rows) < limit, "a test's rows fit one page"
        return self._rows


class ScriptedOptionEvents:
    """What Alpaca would report of the account's option events: raw rows a test scripts (#2913).

    The rows are Alpaca's documented ones, or those with a contract, a date
    or a count laid over them. They are read by the real Alpaca port and
    adapter, so what a test books is what those would hand the Clerk.
    Nothing here was recorded from Alpaca.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        # Raised by the next read, and by every one after it, until it is cleared.
        self.error: Exception | None = None
        # The window each read asked for.
        self.reads: list[int] = []
        # Alpaca's walk stops before the window's start: what it read is true, and it is not all of it.
        self.cut_short = False

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        from app.broker.alpaca.broker import AlpacaBroker

        if resume is None:
            self.reads.append(after_ms)
        if self.error is not None:
            raise self.error
        client = _ScriptedNonTradeActivities(self.rows)
        evidence = await AlpacaBroker(client=client).read_option_event_activities(  # type: ignore[arg-type]
            after_ms=after_ms, resume=resume
        )
        return evidence.model_copy(update={"history_complete": False}) if self.cut_short else evidence


def documented_option_activity(product: str, heading: str, index: int = 0, **changes: Any) -> dict[str, Any]:
    """One option activity row Alpaca's pages document, with ``changes`` laid over it."""
    row = dict(documented("option_activities", "documented_activities.json")[product][heading][index])
    row.update(changes)
    return row


class _FakeReadPort:
    """A minimal ``BrokerReadPort`` double — list_orders/list_positions, the option contracts it lists, and
    the option events a test scripts."""

    def __init__(
        self,
        *,
        orders: list[BrokerOrder] | None = None,
        positions: list[BrokerPosition] | None = None,
        option_contracts: dict[str, BrokerOptionContract] | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._option_contracts = option_contracts or {}
        self.option_events = ScriptedOptionEvents()

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        return await self.option_events.read_option_event_activities(after_ms=after_ms, resume=resume)

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        return self._option_contracts.get(contract.occ_symbol)

    async def list_orders(
        self, *, status: str | None = None, limit: int | None = None, after_ms: int | None = None
    ) -> list[BrokerOrder]:
        return self._orders

    async def list_positions(self) -> list[BrokerPosition]:
        return self._positions


def _covering_read(symbol: str = "SPY", *, quantity: float = 10.0) -> _FakeReadPort:
    """The broker's account holding what the Clerk attributes, with nothing open.

    What an operator's Flatten is checked against as it is sent (#2839): a
    test that drives one through a trade double that is not a simulation
    hands it this, or the send is refused.
    """
    return _FakeReadPort(positions=[_broker_position_fixture(symbol, quantity=quantity)])


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
    submission = await submit_budgeted_test_enter(
        repo,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        decision_id=decision_id,
        lifecycle_run_id=run_id,
        leg=_broker_leg(quantity=quantity),
        trade=_FakeTradePort(),
    )
    await _fill_entry(repo, submission, quantity=quantity, execution_id=execution_id)
    assert submission.order_ref is not None
    return submission.order_ref


async def _fill_entry(
    repo: ClerkSqliteRepository,
    submission: EnterSubmission,
    *,
    quantity: float,
    execution_id: str,
) -> BrokerOrder:
    """The broker fills a submitted entry at $100: order evidence plus its exact execution slice."""
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
            event_type="fill", occurred_at_ms=repo.clock(),
            price=100, quantity=quantity, execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}",
        order=filled,
        recovery_source=None,
        recovery_window_limit=None,
    )
    return filled


# ── The live-envelope admission harness (ADR 0059 D4) ─────────────────────────
# One authority, one pinned clock, and the registered instances the envelope's
# two SQLite suites drive ``accept_enter`` through. Both files judge the same
# seam, so a second copy of this block is a second thing to keep true. Only an
# account switched to budgets admits an ENTER (#2553), so an active instance
# is a budgeted Deploy; ``_start_legacy_run`` is the pre-cutover (version 1)
# Start, for the stores such an account left behind.

ENVELOPE_ACCOUNT_ID = "PA-ENVELOPE"
ENVELOPE_SID = "spy-bot"
ENVELOPE_SID_B = "qqq-bot"
ENVELOPE_RUN_ID = "run-1"
ENVELOPE_RUN_ID_B = "run-2"
# A fixed int64 ms UTC; every stamp is repo.clock(). Saturday 2026-09-05
# 17:46:40 ET: on or after 2026-09-01, so every regulatory fee a BUY owes is
# pinned and an ENTER's recorded fee provision can be priced (#2553).
ENVELOPE_T0 = 1_788_644_800_000


# The harness account's daily loss limit is the envelope's own values, applied
# as the owner's revision 1; every observation a gate publishes names it.
ENVELOPE_RISK_REVISION = 1
ENVELOPE_EXIT_TERMS = ExitTerms(band_multiple=2.0, spread_cap_bps=100.0, exit_allowance_bps=5.0, provenance="deployed")
# One bot's budget unless a test names its own: the default gate's whole cash.
ENVELOPE_BUDGET_CENTS = 10_000_000
# Each of two bots' budget: one $1,000 ENTER (10 x $100) and its one-cent fee.
# Another bot's unspent budget is a claim on cash, so both fit only in a
# reading of at least $2,000.02.
ENVELOPE_PAIR_BUDGET_CENTS = 100_001


def envelope_gate(
    *,
    cash: float = 100_000.0,
    observed_at_ms: int = ENVELOPE_T0,
    fill_sequence: int = 0,
) -> LiveEnvelopeGate:
    """A gate holding one observation of ``cash``, fresh at ``observed_at_ms``.

    ``fill_sequence`` is the executions the reading saw (``day_pnl.risk_fill_sequence``);
    a reading older than the newest fill admits nothing.
    """
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True)
    gate.publish(
        AccountObservation(
            observed_at_ms=observed_at_ms,
            broker_cash_usd=cash,
            cash_available_usd=cash,
            equity_usd=cash,
            last_equity_usd=cash,
            position_count=0,
            risk_revision=ENVELOPE_RISK_REVISION,
            risk_fill_sequence=fill_sequence,
            risk_cash_flow_evidence_complete=True,
            risk_cash_flow_window_start_ms=day_pnl_window_start_ms(observed_at_ms),
            risk_equity_window_start_ms=day_pnl_window_start_ms(observed_at_ms),
        )
    )
    return gate


def switch_to_budgets(repo: ClerkSqliteRepository) -> None:
    """The owner's switch in Settings, and a daily loss limit; each only once."""
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="reviewed", stop_receipt="stopped")
    if repo.account_risk_policy() is None:
        append_risk_policy(repo, policy=AccountRiskPolicy(
            ENVELOPE_RISK_REVISION, TEST_ENVELOPE_VALUES.loss_fraction, TEST_ENVELOPE_VALUES.loss_usd,
            "profile", 1, "owner", repo.clock(),
        ), expected_revision=0)


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
    complete_fee_evidence(clerk)
    yield clerk
    clerk.close()


def _register_active(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    *,
    strategy_instance_id: str,
    symbol: str,
    run_id: str,
    committed_cents: int = ENVELOPE_BUDGET_CENTS,
) -> None:
    """Register one instance and deploy it with a budget -- every stamp from ``clock``.

    The account is switched to budgets first. The Deploy is reviewed against
    a million-dollar reading, so any budgets a test names fit beside each
    other; each ENTER is then judged against the gate its test publishes.
    """
    switch_to_budgets(repo)
    repo.register_strategy_instance(
        strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1", exit_terms=ENVELOPE_EXIT_TERMS,
    )
    submit_budgeted_deploy(
        repo, strategy_instance_id=strategy_instance_id, lifecycle_run_id=run_id, world="real_paper",
        committed_cents=committed_cents, configuration_hash="h1",
        exit_terms_hash=canonical_sha256(ENVELOPE_EXIT_TERMS.model_dump(mode="json")),
        risk_revision=ENVELOPE_RISK_REVISION, actor="owner",
        envelope=envelope_gate(cash=1_000_000.0, observed_at_ms=clock(), fill_sequence=risk_fill_sequence(repo)),
        minimum_position_cost=Decimal("0.01"),
    )


def _start_legacy_run(
    repo: ClerkSqliteRepository,
    clock: _TestClock,
    *,
    strategy_instance_id: str,
    symbol: str,
    run_id: str,
) -> None:
    """Register one instance and Start it on an account still on version 1."""
    repo.register_strategy_instance(
        strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1"
    )
    seed_historical_run_started(
        repo,
        account_id=ENVELOPE_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=run_id,
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
        committed_cents=ENVELOPE_PAIR_BUDGET_CENTS,
    )
    _register_active(
        envelope_repo,
        envelope_clock,
        strategy_instance_id=ENVELOPE_SID_B,
        symbol="QQQ",
        run_id=ENVELOPE_RUN_ID_B,
        committed_cents=ENVELOPE_PAIR_BUDGET_CENTS,
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
BUDGETED_RTH_MS = et_minute_of_day_ms(date(2026, 9, 16), 10 * 60)
TODAY_OPEN = et_minute_of_day_ms(date(2026, 9, 8), 9 * 60 + 30)
YESTERDAY_NOON = et_minute_of_day_ms(date(2026, 9, 4), 12 * 60)
YESTERDAY_OPEN = et_minute_of_day_ms(date(2026, 9, 4), 9 * 60 + 30)


@pytest.fixture
def day_pnl_clock() -> _TestClock:
    return _clock_at(NOON)


def initialize_day_pnl_repo(artifacts_root: Path, clock: _TestClock) -> ClerkSqliteRepository:
    """Pre-cutover custody for fee-history tests that begin before any fee read."""
    clerk = ClerkSqliteRepository.initialize(
        account_id=DAY_PNL_ACCOUNT_ID, artifacts_root=artifacts_root, clock=clock
    )
    clerk.register_strategy_instance(
        strategy_instance_id=DAY_PNL_SID, symbol=DAY_PNL_SYMBOL, config_hash="day-pnl-config"
    )
    seed_historical_run_started(
        clerk,
        account_id=DAY_PNL_ACCOUNT_ID,
        strategy_instance_id=DAY_PNL_SID,
        lifecycle_run_id=DAY_PNL_RUN_ID,
    )
    return clerk


@pytest.fixture
def day_pnl_repo(
    tmp_path: Path, day_pnl_clock: _TestClock
) -> Iterator[ClerkSqliteRepository]:
    clerk = initialize_day_pnl_repo(tmp_path, day_pnl_clock)
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
def seeded_external_order_today(day_pnl_repo: ClerkSqliteRepository) -> None:
    _observe_foreign_order(day_pnl_repo, observed_at_ms=TODAY_OPEN)
