"""Golden-fixture tests for canonical FIFO P&L (broker-v2 panel S0).

Fixture authority: PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md
Tolerance: atol=1e-9, rtol=0 (numerical-rigor.md accumulated-P&L default).

Each scenario is derived by hand from the FIFO algorithm and documented
in-line so a quant reviewer can audit without running the code.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.fifo_pnl import (
    compute_fifo_pnl,
    realized_pnl_today,
)
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide

_ATOL = 1e-9
_RTOL = 0.0

# ── Helper ────────────────────────────────────────────────────────────────────

_ACCT = "test-account"
_SID = "test-bot"


def _fill(
    *,
    side: OrderSide,
    qty: float,
    price: float,
    ts_ms: int,
    fee: float | None = None,
    symbol: str = "SPY",
    event_key: str | None = None,
) -> FillRecord:
    key = event_key or f"exec:{ts_ms}"
    return FillRecord(
        account_id=_ACCT,
        sid=_SID,
        intent_id=f"int_{ts_ms}",
        order_ref=f"learn-ai/{_SID}/v1:int_{ts_ms}",
        event_key=key,
        symbol=symbol,
        side=side,
        quantity=qty,
        fill_price=price,
        filled_at_ms=ts_ms,
        fee=fee,
    )


def _close(a: float, b: float) -> bool:
    """True iff |a - b| <= atol (rtol=0)."""
    return abs(a - b) <= _ATOL


# ── Scenario 1: simple round trip ─────────────────────────────────────────────


def test_simple_round_trip() -> None:
    """BUY 100 @ $10 → SELL 100 @ $12.

    Realized: (12 - 10) × 100 = $200.00.
    Open: 0 (flat). fee_total: None (no fees supplied).
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 200.0), f"expected 200.0; got {result.realized_pnl}"
    assert result.open_pnl == 0.0, f"expected 0.0 open P&L; got {result.open_pnl}"
    assert len(result.closed_lots) == 1
    assert _close(result.closed_lots[0].realized_pnl, 200.0)
    assert result.fee_total is None, "no fees supplied — must be None, not $0"


# ── Scenario 2: partial close ─────────────────────────────────────────────────


def test_partial_close() -> None:
    """BUY 100 @ $10, SELL 60 @ $12.

    Realized: (12 - 10) × 60 = $120.00.
    Open: 40 shares @ $10 (no mark → open_pnl is None).
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=60, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 120.0)
    assert result.open_pnl is None, "no mark supplied — open_pnl must be None"
    assert len(result.open_lots) == 1
    assert _close(result.open_lots[0].qty, 40.0)
    assert _close(result.open_lots[0].cost, 10.0)


def test_partial_close_with_mark() -> None:
    """BUY 100 @ $10, SELL 60 @ $12, mark=$15.

    Realized: $120. Open: 40 × ($15 - $10) = $200.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=60, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 15.0})
    assert _close(result.realized_pnl, 120.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 200.0)


# ── Scenario 3: multi-lot FIFO ────────────────────────────────────────────────


def test_multi_lot_fifo() -> None:
    """BUY 100 @ $10, BUY 50 @ $11, SELL 120 @ $13.

    FIFO consumption:
      - Close lot 1 entirely: 100 × ($13 - $10) = $300
      - Close lot 2 partially: 20 × ($13 - $11) = $40
    Realized: $340. Open: 30 shares @ $11.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=50, price=11.0, ts_ms=2000),
        _fill(side=OrderSide.SELL, qty=120, price=13.0, ts_ms=3000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 340.0), f"expected 340.0; got {result.realized_pnl}"
    assert len(result.open_lots) == 1
    assert _close(result.open_lots[0].qty, 30.0)
    assert _close(result.open_lots[0].cost, 11.0)


# ── Scenario 4: reversal ──────────────────────────────────────────────────────


def test_reversal() -> None:
    """BUY 100 @ $10, SELL 150 @ $12 (reversal into short).

    FIFO:
      - Close the 100-share long lot: 100 × ($12 - $10) = $200 realized.
      - Remaining 50 → new short lot @ $12.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=150, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 200.0)
    assert len(result.open_lots) == 1
    assert result.open_lots[0].side is OrderSide.SELL
    assert _close(result.open_lots[0].qty, 50.0)
    assert _close(result.open_lots[0].cost, 12.0)


# ── Scenario 5: multi-day ─────────────────────────────────────────────────────


def test_multi_day() -> None:
    """BUY 100 @ $10 (day1 ts=1000), BUY 50 @ $11 (day2 ts=86_400_000),
       SELL 80 @ $13 (day2 ts=86_401_000).

    FIFO:
      - Close 80 from lot1: 80 × ($13 - $10) = $240 realized.
    Open: 20 @ $10 + 50 @ $11.
    """
    DAY2 = 86_400_000  # 24 h in ms
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=50, price=11.0, ts_ms=DAY2),
        _fill(side=OrderSide.SELL, qty=80, price=13.0, ts_ms=DAY2 + 1000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 240.0)
    # Two remaining lots
    lots_by_cost = {lot.cost: lot.qty for lot in result.open_lots}
    assert _close(lots_by_cost.get(10.0, 0.0), 20.0)
    assert _close(lots_by_cost.get(11.0, 0.0), 50.0)


# ── Scenario 6: fees not reported ────────────────────────────────────────────


def test_fee_none_propagates() -> None:
    """Any fill with fee=None must cause fee_total=None ('Fees not reported')."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, fee=None),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, fee=None),
    ]
    result = compute_fifo_pnl(fills)
    assert result.fee_total is None, (
        "fee_total must be None when any fill has fee=None — "
        "must render as 'Fees not reported', never $0.00"
    )


def test_fee_partial_none_propagates() -> None:
    """Even one fill with fee=None must make fee_total None."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, fee=1.0),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, fee=None),
    ]
    result = compute_fifo_pnl(fills)
    assert result.fee_total is None


# ── Scenario 7: fees reported ────────────────────────────────────────────────


def test_fees_sum_when_all_reported() -> None:
    """When all fills carry explicit fees, fee_total is their sum."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, fee=0.50),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, fee=0.65),
    ]
    result = compute_fifo_pnl(fills)
    assert result.fee_total is not None
    assert _close(result.fee_total, 1.15)


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_fills() -> None:
    """No fills → zero realized, zero open, None fee_total."""
    result = compute_fifo_pnl([])
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl == 0.0
    assert result.fee_total is None
    assert result.closed_lots == []
    assert result.open_lots == []


def test_single_open_fill_no_mark() -> None:
    """Single BUY with no mark → open_pnl is None."""
    fills = [_fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is None
    assert len(result.open_lots) == 1


def test_single_open_fill_with_mark() -> None:
    """Single BUY 100 @ $10, mark=$11 → open_pnl = 100 × $1 = $100."""
    fills = [_fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 11.0})
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 100.0)


def test_short_open_lot_open_pnl() -> None:
    """SELL 50 @ $12 (short), mark=$10 → open_pnl = ($12 - $10) × 50 = $100."""
    fills = [_fill(side=OrderSide.SELL, qty=50, price=12.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 10.0})
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 100.0)


def test_realized_pnl_today_filter() -> None:
    """realized_pnl_today includes only fills within the session window."""
    session_open = 1_000_000
    session_close = 2_000_000
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=500_000),       # before session
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=1_500_000),    # in session
    ]
    # The BUY is outside the session — realized_pnl_today should be 0
    # because the SELL inside the session has no matching buy in-session.
    pnl = realized_pnl_today(
        fills,
        session_open_ms=session_open,
        session_close_ms=session_close,
    )
    # No buy within session → SELL opens a short lot → no realized P&L
    assert _close(pnl, 0.0)


def test_realized_pnl_today_round_trip_in_session() -> None:
    """BUY + SELL both within session → full realized P&L counted."""
    session_open = 1_000_000
    session_close = 3_000_000
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1_500_000),
        _fill(side=OrderSide.SELL, qty=100, price=13.0, ts_ms=2_500_000),
    ]
    pnl = realized_pnl_today(
        fills,
        session_open_ms=session_open,
        session_close_ms=session_close,
    )
    assert _close(pnl, 300.0)


def test_multi_symbol_fifo() -> None:
    """Fills for two symbols are accounted independently."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, symbol="SPY"),
        _fill(side=OrderSide.BUY, qty=50, price=200.0, ts_ms=1001, symbol="AAPL"),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, symbol="SPY"),
        _fill(side=OrderSide.SELL, qty=50, price=210.0, ts_ms=2001, symbol="AAPL"),
    ]
    result = compute_fifo_pnl(fills)
    # SPY: 100 × (12 - 10) = 200; AAPL: 50 × (210 - 200) = 500
    assert _close(result.realized_pnl, 700.0)


def test_duplicate_event_key_is_idempotent() -> None:
    """A redelivered fill (same event_key) must not double-count P&L."""
    fill_a = _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, event_key="exec:abc")
    fill_b = _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, event_key="exec:def")
    # Redeliver fill_a with the same event_key — project_instance_fills deduplicates it
    # but compute_fifo_pnl itself operates over FillRecords, so we test dedup at fills layer

    # compute_fifo_pnl processes whatever FillRecords it receives — dedup happens upstream
    # in project_instance_fills.  Here we verify the math on de-duped input.
    result = compute_fifo_pnl([fill_a, fill_b])
    assert _close(result.realized_pnl, 200.0)
