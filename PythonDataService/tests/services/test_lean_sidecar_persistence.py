"""Tests for LEAN order-event pairing into round-trip BacktestTrade rows."""

from __future__ import annotations

import pytest

from app.services.lean_sidecar_persistence import pair_order_events


def _filled_event(
    event_id: int,
    direction: str,
    ms_utc: int,
    fill_price: float,
    fill_qty: float,
    fee: float = 0.0,
) -> dict:
    return {
        "id": f"MyAlgorithm-{event_id}-2",
        "order_id": event_id,
        "order_event_id": 2,
        "direction": direction,
        "status": "filled",
        "ms_utc": ms_utc,
        "fill_price": fill_price,
        "fill_quantity": fill_qty,
        "quantity": fill_qty,
        "order_fee_amount": fee,
        "order_fee_currency": "USD",
    }


def test_pair_empty_events_returns_empty_list() -> None:
    trades, open_lot = pair_order_events([])
    assert trades == []
    assert open_lot is None


def test_pair_skips_non_filled_events() -> None:
    events = [
        {**_filled_event(1, "buy", 1_700_000_000_000, 100.0, 10), "status": "submitted"},
        _filled_event(1, "buy", 1_700_000_060_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_120_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert len(trades) == 1
    assert open_lot is None


def test_pair_single_round_trip() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 1
    t = trades[0]
    assert t.trade_number == 1
    assert t.entry_ms_utc == 1_700_000_000_000
    assert t.exit_ms_utc == 1_700_000_060_000
    assert t.entry_price == pytest.approx(100.0)
    assert t.exit_price == pytest.approx(101.0)
    assert t.quantity == 10
    # pnl = (101 - 100) * 10 - 0.5 - 0.5 = 9.0
    assert t.pnl == pytest.approx(9.0)
    assert t.is_synthetic_exit is False


def test_pair_multiple_round_trips() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
        _filled_event(2, "sell", 1_700_000_060_000, 101.0, 10, fee=0.5),
        _filled_event(3, "buy", 1_700_000_120_000, 102.0, 10, fee=0.5),
        _filled_event(4, "sell", 1_700_000_180_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert open_lot is None
    assert len(trades) == 2
    assert [t.trade_number for t in trades] == [1, 2]
    assert trades[1].pnl == pytest.approx((100.0 - 102.0) * 10 - 1.0)


def test_pair_half_open_returns_open_lot() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10, fee=0.5),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is not None
    assert open_lot.entry_ms_utc == 1_700_000_000_000
    assert open_lot.entry_price == pytest.approx(100.0)
    assert open_lot.quantity == 10
    assert open_lot.fees == [0.5]


def test_pair_raises_on_pyramiding() -> None:
    events = [
        _filled_event(1, "buy", 1_700_000_000_000, 100.0, 10),
        _filled_event(2, "buy", 1_700_000_060_000, 101.0, 10),  # second buy without sell
    ]
    with pytest.raises(NotImplementedError, match="Pyramiding not supported"):
        pair_order_events(events)


def test_pair_ignores_sell_without_open_lot() -> None:
    """Defensive: short selling not expected for current templates."""
    events = [
        _filled_event(1, "sell", 1_700_000_000_000, 100.0, 10),
    ]
    trades, open_lot = pair_order_events(events)
    assert trades == []
    assert open_lot is None
