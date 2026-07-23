"""Clerk-service seam tests for S6 reconciliation + flag-and-hold (#1197).

A fake broker (read + trade ports) is wired to a REAL ``OrderJournal`` on a tmp
dir. These assert the S6 contract at the clerk-service seam — on-disk journal
entries + returned status, never internals:

- Each reconciliation verdict (clean / unexplained_order / missing_intent /
  stale) is recorded as a RECONCILIATION entry (fixed clock, crafted broker).
- An unexplained order → HOLD_SET + submit refused (typed error) while cancel
  still succeeds; clear_hold → HOLD_CLEARED restores submission.
- Hold state survives a new clerk instance on the same journal (journal-derived).
- Idempotency: double hold-set / double clear.

A fixed clock (mirroring the S4/S5 tests) makes journaled timestamps
deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import journal as journal_module
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import (
    UNEXPLAINED_ORDER_HOLD_CODE,
    ClerkEntryKind,
    OrderJournalEntry,
)
from app.broker.contract.errors import BrokerSubmissionHeld, BrokerUnavailable
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerOrderRequest,
    BrokerPosition,
)

_FIXED_MS = 1_700_000_000_000


def _account(account_id: str = "PA-TEST") -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=account_id,
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1000.0,
        buying_power=2000.0,
        portfolio_value=1000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1_600_000_000_000,
        observed_at_ms=_FIXED_MS,
    )


def _order(
    *,
    client_order_id: str | None,
    order_id: str = "broker-order-1",
    status: str = "accepted",
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
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status=status,
        submitted_at_ms=_FIXED_MS,
        created_at_ms=_FIXED_MS,
        updated_at_ms=_FIXED_MS,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=_FIXED_MS,
    )


def _position() -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca",
        symbol="SPY",
        asset_id="asset-1",
        asset_class="us_equity",
        quantity=1.0,
        side="long",
        average_entry_price=100.0,
        market_value=101.0,
        cost_basis=100.0,
        current_price=101.0,
        unrealized_pl=1.0,
        unrealized_plpc=0.01,
        observed_at_ms=_FIXED_MS,
    )


class _FakeBroker:
    """A read+trade port double whose reads return crafted orders/positions.

    ``list_error`` makes a read raise (the ``stale`` trigger). ``accept_submit``
    lets a submit succeed so a test can prime an owned order before a sweep.
    """

    broker_id = "alpaca"

    def __init__(
        self,
        *,
        account: BrokerAccountSnapshot | None = None,
        orders: list[BrokerOrder] | None = None,
        positions: list[BrokerPosition] | None = None,
        list_error: Exception | None = None,
        submit_error: Exception | None = None,
        lookup_error: Exception | None = None,
        lookup_result: BrokerOrder | None = None,
    ) -> None:
        self._account = account or _account()
        self._orders = orders or []
        self._positions = positions or []
        self._list_error = list_error
        self._submit_error = submit_error
        self._lookup_error = lookup_error
        self._lookup_result = lookup_result
        self.submit_calls: list[tuple[BrokerOrderLeg, str]] = []
        self.cancel_calls: list[str] = []
        self.orders_started: asyncio.Event | None = None
        self.release_orders: asyncio.Event | None = None

    async def get_account(self) -> BrokerAccountSnapshot:
        return self._account

    async def list_orders(self, **_: Any) -> list[BrokerOrder]:
        if self._list_error is not None:
            raise self._list_error
        if self.orders_started is not None:
            self.orders_started.set()
        if self.release_orders is not None:
            await self.release_orders.wait()
        return list(self._orders)

    async def list_positions(self) -> list[BrokerPosition]:
        if self._list_error is not None:
            raise self._list_error
        return list(self._positions)

    async def submit(
        self, leg: BrokerOrderLeg, *, client_order_id: str
    ) -> BrokerOrder:
        self.submit_calls.append((leg, client_order_id))
        if self._submit_error is not None:
            raise self._submit_error
        return _order(client_order_id=client_order_id)

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None:
        if self._lookup_error is not None:
            raise self._lookup_error
        return self._lookup_result

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        return None


@pytest.fixture(autouse=True)
def _clerk_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the clerk journal at a tmp dir for every test."""
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    journal_module.reset_clerk_settings_for_testing()
    yield tmp_path
    journal_module.reset_clerk_settings_for_testing()


def _fixed_clock() -> int:
    return _FIXED_MS


def _request(operator: str = "inkant") -> BrokerOrderRequest:
    return BrokerOrderRequest(
        operator=operator,
        legs=[BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)],
    )


def _kinds(clerk: AlpacaClerk) -> list[ClerkEntryKind]:
    return [entry.kind for entry in clerk._journal.read_entries()]  # type: ignore[union-attr]


# ── Verdicts ─────────────────────────────────────────────────────────────────


async def test_reconcile_clean_when_owned_exposure_matches() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    # An owned order whose intent the clerk recorded.
    submit = await clerk.submit(_request())
    order_ref = submit.results[0].order_ref
    broker._orders = [_order(client_order_id=order_ref)]

    verdict = await clerk.reconcile_once()

    assert verdict == "clean"
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    recon = [e for e in entries if e.kind is ClerkEntryKind.RECONCILIATION]
    assert [e.verdict for e in recon] == ["clean"]
    assert clerk.is_on_hold() is False


async def test_reconcile_unexplained_order_journals_and_sets_hold() -> None:
    # An order at Alpaca whose client_order_id is foreign → unexplained + hold.
    broker = _FakeBroker(orders=[_order(client_order_id="someone-elses-order")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    verdict = await clerk.reconcile_once()

    assert verdict == "unexplained_order"
    kinds = _kinds(clerk)
    assert ClerkEntryKind.UNEXPLAINED_ORDER in kinds
    assert ClerkEntryKind.HOLD_SET in kinds
    recon = [
        e
        for e in clerk._journal.read_entries()  # type: ignore[union-attr]
        if e.kind is ClerkEntryKind.RECONCILIATION
    ]
    assert [e.verdict for e in recon] == ["unexplained_order"]
    assert clerk.is_on_hold() is True
    assert clerk.unexplained_order_count == 1


async def test_reconcile_ignores_terminal_foreign_order_history() -> None:
    broker = _FakeBroker(
        orders=[_order(client_order_id="foreign", status="canceled")]
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    assert await clerk.reconcile_once() == "clean"
    assert clerk.is_on_hold() is False
    assert ClerkEntryKind.UNEXPLAINED_ORDER not in _kinds(clerk)


async def test_persistent_unexplained_order_is_journaled_once_across_sweeps() -> None:
    # M1: a foreign order still present on later sweeps must NOT re-journal an
    # UNEXPLAINED_ORDER / RECONCILIATION line every pass (that would grow the
    # ledger without bound while the account is held). Dedup on the broker order
    # id; the verdict line is written only on a change.
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    for _ in range(3):
        assert await clerk.reconcile_once() == "unexplained_order"

    kinds = _kinds(clerk)
    assert kinds.count(ClerkEntryKind.UNEXPLAINED_ORDER) == 1
    assert kinds.count(ClerkEntryKind.RECONCILIATION) == 1
    assert kinds.count(ClerkEntryKind.HOLD_SET) == 1
    assert clerk.unexplained_order_count == 1
    assert clerk.is_on_hold() is True


async def test_hold_is_re_raised_after_clear_when_foreign_order_persists() -> None:
    # If an operator clears the hold but the foreign order is still at the broker,
    # the next sweep must re-raise the hold (safety) — WITHOUT re-journaling a
    # duplicate UNEXPLAINED_ORDER line (the order was already recorded).
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    await clerk.clear_hold(operator="inkant", reason="reviewed")
    assert clerk.is_on_hold() is False

    await clerk.reconcile_once()  # the foreign order persists

    assert clerk.is_on_hold() is True
    kinds = _kinds(clerk)
    assert kinds.count(ClerkEntryKind.HOLD_SET) == 2  # re-held after the clear
    assert kinds.count(ClerkEntryKind.UNEXPLAINED_ORDER) == 1  # journaled once
    assert clerk.unexplained_order_count == 1


async def test_reconcile_missing_intent_is_observational() -> None:
    # A position exists but the ledger has never recorded an owned order → drift.
    broker = _FakeBroker(positions=[_position()])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    verdict = await clerk.reconcile_once()

    assert verdict == "missing_intent"
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    recon = [e for e in entries if e.kind is ClerkEntryKind.RECONCILIATION]
    assert [e.verdict for e in recon] == ["missing_intent"]
    # Observational: no hold set.
    assert clerk.is_on_hold() is False


async def test_reconcile_stale_when_broker_read_fails() -> None:
    broker = _FakeBroker(
        list_error=BrokerUnavailable("down", broker="alpaca", detail="5xx")
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    verdict = await clerk.reconcile_once()

    assert verdict == "stale"
    entries = clerk._journal.read_entries()  # type: ignore[union-attr]
    recon = [e for e in entries if e.kind is ClerkEntryKind.RECONCILIATION]
    assert [e.verdict for e in recon] == ["stale"]
    assert clerk.is_on_hold() is False


# ── Hold gating: submit refused, cancel allowed ──────────────────────────────


async def test_submit_is_refused_while_held_and_records_no_intent() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()  # raises the hold
    assert clerk.is_on_hold() is True

    kinds_before = _kinds(clerk)
    with pytest.raises(BrokerSubmissionHeld) as excinfo:
        await clerk.submit(_request())

    assert excinfo.value.reason_code == UNEXPLAINED_ORDER_HOLD_CODE
    assert excinfo.value.http_status == 409
    # Capture-before-submit: a refused submit records NO intent and never
    # reaches the broker.
    assert broker.submit_calls == []
    assert _kinds(clerk) == kinds_before


async def test_submit_is_fail_closed_after_unexplained_entry_before_hold_receipt() -> None:
    # Simulate a crash between durable UNEXPLAINED_ORDER and HOLD_SET. Restart
    # must derive the hold from the observation and refuse a fresh submit.
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    account_id, journal = await clerk._ensure_journal()  # type: ignore[attr-defined]
    journal.append(
        OrderJournalEntry(
            kind=ClerkEntryKind.UNEXPLAINED_ORDER,
            account_id=account_id,
            broker_order_id="foreign-order",
            client_order_id="foreign",
            owned=False,
            recorded_at_ms=_FIXED_MS,
        )
    )

    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit(_request())
    assert broker.submit_calls == []


async def test_cancel_is_allowed_while_held() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    result = await clerk.cancel("broker-order-1")

    # Cancel reduces exposure — never blocked by the hold.
    assert result.status == "acked"
    assert broker.cancel_calls == ["broker-order-1"]


async def test_slow_reconciliation_read_does_not_block_cancel() -> None:
    broker = _FakeBroker()
    broker.orders_started = asyncio.Event()
    broker.release_orders = asyncio.Event()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    sweep = asyncio.create_task(clerk.reconcile_once())
    await broker.orders_started.wait()

    cancel = await clerk.cancel("order-to-reduce")
    assert cancel.status == "acked"
    assert broker.cancel_calls == ["order-to-reduce"]

    broker.release_orders.set()
    await sweep


async def test_clear_hold_restores_submission() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    status = await clerk.clear_hold(operator="ops", reason="Verified account safe.")

    assert status.hold.active is False
    assert ClerkEntryKind.HOLD_CLEARED in _kinds(clerk)
    # Submission works again once the foreign order is no longer present.
    broker._orders = []
    result = await clerk.submit(_request())
    assert result.results[0].status == "acked"


async def test_periodic_sweep_resolves_a_stranded_uncertain_submit() -> None:
    broker = _FakeBroker(
        submit_error=BrokerUnavailable("timeout", broker="alpaca", detail="timeout"),
        lookup_error=BrokerUnavailable("down", broker="alpaca", detail="5xx"),
    )
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    submitted = await clerk.submit(_request())
    order_ref = submitted.results[0].order_ref
    assert submitted.results[0].status == "uncertain"

    broker._lookup_error = None
    broker._lookup_result = _order(client_order_id=order_ref)
    await clerk.reconcile_once()

    assert ClerkEntryKind.SUBMIT_ACKED in _kinds(clerk)


# ── Journal-derived durability + idempotency ─────────────────────────────────


async def test_hold_survives_a_new_clerk_on_the_same_journal() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()
    assert clerk.is_on_hold() is True

    # A fresh clerk instance (restart) reading the same on-disk journal must see
    # the hold — it is journal-derived, not an in-memory flag.
    broker2 = _FakeBroker()
    clerk2 = AlpacaClerk(read=broker2, trade=broker2, clock=_fixed_clock)
    status = await clerk2.status()

    assert status.hold.active is True
    assert status.hold.reason_code == UNEXPLAINED_ORDER_HOLD_CODE
    with pytest.raises(BrokerSubmissionHeld):
        await clerk2.submit(_request())


async def test_hold_set_is_idempotent_across_two_unexplained_sweeps() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)

    await clerk.reconcile_once()
    await clerk.reconcile_once()

    kinds = _kinds(clerk)
    # Exactly one HOLD_SET despite two unexplained sweeps.
    assert kinds.count(ClerkEntryKind.HOLD_SET) == 1


async def test_clear_hold_is_benign_noop_when_not_held() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    # Warm the journal so an account id exists.
    await clerk.submit(_request())

    status = await clerk.clear_hold(operator="ops", reason="nothing to clear")

    assert status.hold.active is False
    # No HOLD_CLEARED written for a no-op clear.
    assert ClerkEntryKind.HOLD_CLEARED not in _kinds(clerk)


async def test_double_clear_writes_exactly_one_hold_cleared() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()

    await clerk.clear_hold(operator="ops", reason="cleared")
    await clerk.clear_hold(operator="ops", reason="cleared again")

    assert _kinds(clerk).count(ClerkEntryKind.HOLD_CLEARED) == 1


# ── S4 seam: an unexplained lifecycle event raises the hold ──────────────────


async def test_unexplained_lifecycle_event_sets_hold() -> None:
    broker = _FakeBroker()
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    # Warm the journal.
    await clerk.submit(_request())

    kind = await clerk.record_lifecycle_event(
        client_order_id="foreign-coid",
        event=BrokerOrderEvent(
            event_type="fill", occurred_at_ms=_FIXED_MS, price=1.0, quantity=1.0
        ),
        event_key="k1",
        order=None,
    )

    assert kind is ClerkEntryKind.UNEXPLAINED_ORDER
    assert clerk.is_on_hold() is True
    with pytest.raises(BrokerSubmissionHeld):
        await clerk.submit(_request())


# ── Status shape ─────────────────────────────────────────────────────────────


async def test_status_reports_hold_verdict_and_outstanding_intents() -> None:
    broker = _FakeBroker(orders=[_order(client_order_id="foreign")])
    clerk = AlpacaClerk(read=broker, trade=broker, clock=_fixed_clock)
    await clerk.reconcile_once()

    status = await clerk.status()

    assert status.broker == "alpaca"
    assert status.account_id == "PA-TEST"
    assert status.hold.active is True
    assert status.hold.reason_code == UNEXPLAINED_ORDER_HOLD_CODE
    assert status.hold.since_ms == _FIXED_MS
    assert status.latest_reconciliation is not None
    assert status.latest_reconciliation.verdict == "unexplained_order"
    assert status.outstanding_intents == 0
    assert status.observed_at_ms == _FIXED_MS
