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


def _unexplained(ms: int) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.UNEXPLAINED_ORDER,
        account_id="A",
        broker_order_id="foreign-order",
        client_order_id="foreign",
        owned=False,
        recorded_at_ms=ms,
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


def test_unexplained_order_after_clear_derives_a_fail_closed_hold() -> None:
    # A crash after UNEXPLAINED_ORDER but before the companion HOLD_SET append
    # must not leave the next process able to submit new exposure.
    state = derive.hold_state([_hold_cleared(1), _unexplained(2)])

    assert state.active is True
    assert state.reason_code == "UNEXPLAINED_ORDER_HOLD"
    assert state.since_ms == 2


def test_clear_after_unexplained_order_releases_the_derived_hold() -> None:
    assert derive.hold_state([_unexplained(1), _hold_cleared(2)]).active is False


def test_latest_reconciliation_returns_most_recent() -> None:
    entries = [_reconciliation("clean", 1), _reconciliation("unexplained_order", 2)]
    summary = derive.latest_reconciliation(entries)
    assert summary is not None
    assert summary.verdict == "unexplained_order"
    assert summary.recorded_at_ms == 2


def test_latest_reconciliation_none_when_never_swept() -> None:
    assert derive.latest_reconciliation([_hold_set()]) is None


def test_account_freeze_has_exactly_two_durable_categories() -> None:
    unattributable = derive.account_freeze_state(
        [_reconciliation("missing_intent", 1)]
    )
    unprovable = derive.account_freeze_state([_reconciliation("stale", 2)])
    recovered = derive.account_freeze_state(
        [_reconciliation("stale", 2), _reconciliation("clean", 3)]
    )
    instance_hold_only = derive.account_freeze_state([_hold_set(4)])

    assert unattributable.active is True
    assert unattributable.category == "ACCOUNT_STATE_UNATTRIBUTABLE"
    assert unprovable.active is True
    assert unprovable.category == "ACCOUNT_STATE_UNPROVABLE"
    assert recovered.active is False
    assert instance_hold_only.active is False


def test_has_missing_intent_true_for_owned_order_without_intent() -> None:
    # An order with a client_order_id we never recorded an intent for.
    assert derive.has_missing_intent([], [_order("manual/op/v1:x")], []) is True


def test_has_missing_intent_false_when_intent_recorded() -> None:
    ref = "manual/op/v1:x"
    assert derive.has_missing_intent([_intent(ref)], [_order(ref)], []) is False


def test_has_missing_intent_true_for_position_with_no_owned_orders() -> None:
    assert derive.has_missing_intent([], [], [_position()]) is True


def test_has_missing_intent_detects_manual_position_after_unrelated_intent() -> None:
    # An old SPY intent cannot mask a live manual position. The journal has no
    # filled state for it, so expected SPY exposure remains zero.
    assert derive.has_missing_intent([_intent("manual/op/v1:old")], [], [_position()])


def test_has_missing_intent_false_on_empty_broker_state() -> None:
    assert derive.has_missing_intent([], [], []) is False


# ── Position-drift race: fill ORDER_EVENT not yet journaled (2026-07-30) ─────


def _order_snapshot(
    *, symbol: str, side: str, status: str, filled_quantity: float
) -> BrokerOrder:
    order = _order("learn-ai/alp8-spy-0730/v1:i1")
    return order.model_copy(
        update={
            "symbol": symbol,
            "side": side,
            "status": status,
            "filled_quantity": filled_quantity,
        }
    )


def _entry_with_order(order_ref: str, order: BrokerOrder) -> OrderJournalEntry:
    return OrderJournalEntry(
        kind=ClerkEntryKind.SUBMIT_ACKED,
        account_id="A",
        operator="op",
        intent_id="i1",
        order_ref=order_ref,
        client_order_id=order_ref,
        order=order,
        recorded_at_ms=_MS,
    )


def _position_for(symbol: str, quantity: float = 1.0) -> BrokerPosition:
    return _position().model_copy(update={"symbol": symbol, "quantity": quantity})


def test_has_missing_intent_suppressed_while_fill_event_in_flight() -> None:
    """Race regression: broker shows the position before the fill ORDER_EVENT
    reaches the journal (observed live: verdict fired ~7s early). An in-flight
    journaled order for the symbol suppresses the flag for this sweep."""
    ref = "learn-ai/alp8-spy-0730/v1:i1"
    entries = [
        _intent(ref),
        _entry_with_order(
            ref,
            _order_snapshot(
                symbol="SPY", side="buy", status="pending_new", filled_quantity=0.0
            ),
        ),
    ]

    flagged = derive.has_missing_intent(entries, [], [_position_for("SPY")])

    assert flagged is False


def test_has_missing_intent_clean_after_fill_event_journaled() -> None:
    ref = "learn-ai/alp8-spy-0730/v1:i1"
    entries = [
        _intent(ref),
        _entry_with_order(
            ref,
            _order_snapshot(
                symbol="SPY", side="buy", status="filled", filled_quantity=1.0
            ),
        ),
    ]

    flagged = derive.has_missing_intent(entries, [], [_position_for("SPY")])

    assert flagged is False


def test_has_missing_intent_foreign_position_not_masked_by_suppression() -> None:
    """A position with no journal presence must still flag — the in-flight
    suppression is per-symbol and must not hide foreign exposure."""
    ref = "learn-ai/alp8-spy-0730/v1:i1"
    entries = [
        _intent(ref),
        _entry_with_order(
            ref,
            _order_snapshot(
                symbol="SPY", side="buy", status="pending_new", filled_quantity=0.0
            ),
        ),
    ]
    positions = [_position_for("SPY"), _position_for("TSLA")]

    flagged = derive.has_missing_intent(entries, [], positions)

    assert flagged is True
