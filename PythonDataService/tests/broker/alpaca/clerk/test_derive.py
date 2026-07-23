"""Unit tests for the pure ledger-derivation helpers (Alpaca phase 2, S6).

These functions are pure over a ``list[OrderJournalEntry]``, so they are tested
directly with hand-built ledgers — no clerk, no journal, no I/O.
"""

from __future__ import annotations

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrder, BrokerPosition

_MS = 1_700_000_000_000


def _hold_set(ms: int = _MS) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.HOLD_SET,
        account_id="A",
        reason_code="UNEXPLAINED_ORDER_HOLD",
        reason="foreign order",
        recorded_at_ms=ms,
    )


def _hold_cleared(ms: int) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.HOLD_CLEARED,
        account_id="A",
        reason_code="UNEXPLAINED_ORDER_HOLD",
        reason="cleared",
        recorded_at_ms=ms,
    )


def _reconciliation(verdict: str, ms: int) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.RECONCILIATION,
        account_id="A",
        verdict=verdict,  # type: ignore[arg-type]
        recorded_at_ms=ms,
    )


def _order(client_order_id: str | None) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id="o1",
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
        status="accepted",
        submitted_at_ms=_MS,
        created_at_ms=_MS,
        updated_at_ms=_MS,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=_MS,
    )


def _position() -> BrokerPosition:
    return BrokerPosition(
        broker="alpaca",
        symbol="SPY",
        asset_id="a1",
        asset_class="us_equity",
        quantity=1.0,
        side="long",
        average_entry_price=100.0,
        market_value=101.0,
        cost_basis=100.0,
        current_price=101.0,
        unrealized_pl=1.0,
        unrealized_plpc=0.01,
        observed_at_ms=_MS,
    )


def _intent(order_ref: str) -> OrderJournalEntry:
    from app.broker.contract.models import BrokerOrderLeg

    return OrderJournalEntry(
        kind=ClerkEntryKind.INTENT_RECORDED,
        account_id="A",
        operator="op",
        intent_id="i",
        order_ref=order_ref,
        client_order_id=order_ref,
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        recorded_at_ms=_MS,
    )


def test_hold_state_empty_ledger_is_inactive() -> None:
    assert derive.hold_state([]).active is False


def test_hold_state_set_without_clear_is_active() -> None:
    state = derive.hold_state([_hold_set()])
    assert state.active is True
    assert state.reason_code == "UNEXPLAINED_ORDER_HOLD"
    assert state.since_ms == _MS


def test_hold_state_last_write_wins() -> None:
    # set → clear → set: the final set wins.
    entries = [_hold_set(1), _hold_cleared(2), _hold_set(3)]
    assert derive.hold_state(entries).active is True
    # set → clear: cleared wins.
    assert derive.hold_state([_hold_set(1), _hold_cleared(2)]).active is False


def test_latest_reconciliation_returns_most_recent() -> None:
    entries = [_reconciliation("clean", 1), _reconciliation("unexplained_order", 2)]
    summary = derive.latest_reconciliation(entries)
    assert summary is not None
    assert summary.verdict == "unexplained_order"
    assert summary.recorded_at_ms == 2


def test_latest_reconciliation_none_when_never_swept() -> None:
    assert derive.latest_reconciliation([_hold_set()]) is None


def test_has_missing_intent_true_for_owned_order_without_intent() -> None:
    # An order with a client_order_id we never recorded an intent for.
    assert derive.has_missing_intent([], [_order("manual/op/v1:x")], []) is True


def test_has_missing_intent_false_when_intent_recorded() -> None:
    ref = "manual/op/v1:x"
    assert derive.has_missing_intent([_intent(ref)], [_order(ref)], []) is False


def test_has_missing_intent_true_for_position_with_no_owned_orders() -> None:
    assert derive.has_missing_intent([], [], [_position()]) is True


def test_has_missing_intent_false_on_empty_broker_state() -> None:
    assert derive.has_missing_intent([], [], []) is False
