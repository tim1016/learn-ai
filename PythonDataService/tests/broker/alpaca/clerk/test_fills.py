"""Tests for project_instance_fills — fill attribution (broker-v2 S0).

Verified against a synthetic clerk order-journal fixture covering:
- Namespace filtering (only the target sid's fills returned)
- Dedup on (account_id, event_key)
- Fills sorted ascending by filled_at_ms
- fee=None for Alpaca trade_updates fills
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.fills import project_instance_fills
from app.broker.alpaca.clerk.models import ClerkEntryKind, OrderJournalEntry
from app.broker.contract.models import BrokerOrderEvent, BrokerOrderLeg, OrderSide
from app.engine.live.order_identity import NAMESPACE_ROOT

_ACCT = "test-acct"
_SID = "bot-alpha"
_OTHER_SID = "bot-beta"


def _order_ref(sid: str, intent: str) -> str:
    return f"{NAMESPACE_ROOT}/{sid}/v1:{intent}"


def _fill_entry(
    *,
    sid: str,
    intent: str,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: float = 100.0,
    price: float = 500.0,
    ts_ms: int = 1_000,
    event_key: str | None = None,
    account_id: str = _ACCT,
) -> OrderJournalEntry:
    ref = _order_ref(sid, intent)
    ekey = event_key or f"exec:{ts_ms}"
    return OrderJournalEntry(
        kind=ClerkEntryKind.ORDER_EVENT,
        account_id=account_id,
        order_ref=ref,
        client_order_id=ref,
        operator="op",
        intent_id=intent,
        owned=True,
        recorded_at_ms=ts_ms,
        leg=BrokerOrderLeg(symbol=symbol, side=side, quantity=qty),
        event=BrokerOrderEvent(
            event_type="fill",
            occurred_at_ms=ts_ms,
            price=price,
            quantity=qty,
        ),
        event_key=ekey,
    )


def _non_fill_entry() -> OrderJournalEntry:
    """A RECONCILIATION entry (not a fill)."""
    return OrderJournalEntry(
        kind=ClerkEntryKind.RECONCILIATION,
        account_id=_ACCT,
        recorded_at_ms=1000,
        verdict="clean",
        operator="op",
    )


# ── Basic attribution ─────────────────────────────────────────────────────────


def test_only_target_sid_fills_returned() -> None:
    entries = [
        _fill_entry(sid=_SID, intent="i1", ts_ms=1000),
        _fill_entry(sid=_OTHER_SID, intent="i2", ts_ms=2000),
    ]
    fills = project_instance_fills(_SID, entries)
    assert len(fills) == 1
    assert fills[0].sid == _SID


def test_empty_journal_returns_empty() -> None:
    assert project_instance_fills(_SID, []) == ()


def test_non_fill_entries_ignored() -> None:
    entries = [_non_fill_entry(), _fill_entry(sid=_SID, intent="i1")]
    fills = project_instance_fills(_SID, entries)
    assert len(fills) == 1


def test_fill_fields_mapped_correctly() -> None:
    entry = _fill_entry(
        sid=_SID, intent="i1", symbol="QQQ",
        side=OrderSide.SELL, qty=50.0, price=450.0, ts_ms=5000,
        event_key="exec:xyz",
    )
    fills = project_instance_fills(_SID, [entry])
    assert len(fills) == 1
    f = fills[0]
    assert f.symbol == "QQQ"
    assert f.side is OrderSide.SELL
    assert f.quantity == 50.0
    assert f.fill_price == 450.0
    assert f.filled_at_ms == 5000
    assert f.event_key == "exec:xyz"
    assert f.fee is None  # Alpaca does not report per-fill commission


def test_sorted_ascending_by_filled_at_ms() -> None:
    entries = [
        _fill_entry(sid=_SID, intent="i3", ts_ms=3000, event_key="exec:3"),
        _fill_entry(sid=_SID, intent="i1", ts_ms=1000, event_key="exec:1"),
        _fill_entry(sid=_SID, intent="i2", ts_ms=2000, event_key="exec:2"),
    ]
    fills = project_instance_fills(_SID, entries)
    assert [f.filled_at_ms for f in fills] == [1000, 2000, 3000]


# ── Dedup ─────────────────────────────────────────────────────────────────────


def test_duplicate_event_key_deduped() -> None:
    """Same (account_id, event_key) → only one fill record."""
    entry = _fill_entry(sid=_SID, intent="i1", ts_ms=1000, event_key="exec:abc")
    # Simulate redelivery: second entry with same event_key
    entry2 = _fill_entry(sid=_SID, intent="i1", ts_ms=1001, event_key="exec:abc")
    fills = project_instance_fills(_SID, [entry, entry2])
    assert len(fills) == 1


def test_different_event_keys_not_deduped() -> None:
    e1 = _fill_entry(sid=_SID, intent="i1", ts_ms=1000, event_key="exec:111")
    e2 = _fill_entry(sid=_SID, intent="i2", ts_ms=2000, event_key="exec:222")
    fills = project_instance_fills(_SID, [e1, e2])
    assert len(fills) == 2


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_sid_raises() -> None:
    with pytest.raises(ValueError, match="sid"):
        project_instance_fills("", [])


def test_partial_fill_event_included() -> None:
    """partial_fill events are attributed (same as fill)."""
    entry = _fill_entry(sid=_SID, intent="i1", ts_ms=1000)
    entry_pf = entry.model_copy(
        update={
            "event": BrokerOrderEvent(
                event_type="partial_fill",
                occurred_at_ms=2000,
                price=500.0,
                quantity=50.0,
            ),
            "event_key": "exec:pf",
        }
    )
    fills = project_instance_fills(_SID, [entry_pf])
    assert len(fills) == 1


def test_unowned_order_event_excluded() -> None:
    """owned=False entries must not be attributed."""
    entry = _fill_entry(sid=_SID, intent="i1")
    unowned = entry.model_copy(update={"owned": False, "order_ref": "", "intent_id": ""})
    fills = project_instance_fills(_SID, [unowned])
    assert len(fills) == 0


def test_fill_without_event_key_excluded() -> None:
    """A fill event with no event_key cannot be safely deduped — must be excluded."""
    entry = _fill_entry(sid=_SID, intent="i1")
    no_key = entry.model_copy(update={"event_key": None})
    fills = project_instance_fills(_SID, [no_key])
    assert len(fills) == 0


def test_multiple_accounts_same_sid_isolated() -> None:
    """Fills from a different account with the same sid format are included
    (the namespace matches; account isolation is the journal layer's job)."""
    e1 = _fill_entry(sid=_SID, intent="i1", account_id="ACCT-A", event_key="exec:a")
    e2 = _fill_entry(sid=_SID, intent="i2", account_id="ACCT-B", event_key="exec:b")
    fills = project_instance_fills(_SID, [e1, e2])
    # Both have the same namespace; dedup is per (account_id, event_key)
    assert len(fills) == 2
