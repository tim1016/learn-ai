"""Tests for LEAN order-event pairing into round-trip BacktestTrade rows."""

from __future__ import annotations

import pytest

from app.services.lean_sidecar_persistence import (
    OpenLot,
    finalize_open_lot_as_synthetic,
    pair_order_events,
)


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


def test_finalize_open_lot_as_synthetic_uses_last_equity_point() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    equity_curve = [
        {"ms_utc": 1_700_000_000_000, "value": 100_000.0},
        {"ms_utc": 1_700_000_300_000, "value": 100_050.0},
        {"ms_utc": 1_700_000_600_000, "value": 100_090.0},
    ]
    trade = finalize_open_lot_as_synthetic(
        open_lot,
        equity_curve=equity_curve,
        starting_cash=100_000.0,
        trade_number=5,
    )
    assert trade.trade_number == 5
    assert trade.exit_ms_utc == 1_700_000_600_000
    assert trade.is_synthetic_exit is True
    assert trade.signal_reason == "EndOfAlgorithm:MTM (synthetic exit)"
    # exit_price reconstructed via portfolio-value identity:
    #   equity = cash_remaining + qty * exit_price
    #   cash_remaining = starting_cash - qty * entry_price - sum(fees)
    # => exit_price = (equity - starting_cash + qty * entry_price + sum(fees)) / qty
    #              = (100090 - 100000 + 10*100 + 0.5) / 10
    #              = 1090.5 / 10 = 109.05
    assert trade.exit_price == pytest.approx(109.05)
    # pnl = (109.05 - 100) * 10 - 0.5 = 90.5 - 0.5 = 90.0
    assert trade.pnl == pytest.approx(90.0)


def test_finalize_open_lot_raises_on_empty_equity_curve() -> None:
    open_lot = OpenLot(
        entry_ms_utc=1_700_000_000_000,
        entry_price=100.0,
        quantity=10,
        fees=[0.5],
    )
    with pytest.raises(ValueError, match="equity_curve is empty"):
        finalize_open_lot_as_synthetic(open_lot, [], 100_000.0, 1)
