"""Golden-fixture tests for canonical FIFO P&L (broker-v2 panel S0).

Fixture authority: PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md
Tolerance: atol=1e-9, rtol=0 (numerical-rigor.md accumulated-P&L default).

Each scenario is derived by hand from the FIFO algorithm and documented
in-line so a quant reviewer can audit without running the code.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from app.broker.alpaca.clerk.fifo_pnl import (
    compute_fifo_pnl,
    realized_pnl_today,
)
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import display_cents, normalize_money
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
    marks_complete: True (flat).
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 200.0), f"expected 200.0; got {result.realized_pnl}"
    assert result.open_pnl == 0.0, f"expected 0.0 open P&L; got {result.open_pnl}"
    assert result.marks_complete is True, "flat bot must have marks_complete=True"
    assert len(result.closed_lots) == 1
    assert _close(result.closed_lots[0].realized_pnl, 200.0)
    assert result.fee_total is None, "no fees supplied — must be None, not $0"


# ── Scenario 2: partial close ─────────────────────────────────────────────────


def test_partial_close() -> None:
    """BUY 100 @ $10, SELL 60 @ $12.

    Realized: (12 - 10) × 60 = $120.00.
    Open: 40 shares @ $10 (no mark → open_pnl is None, marks_complete=False).
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=60, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 120.0)
    assert result.open_pnl is None, "no mark supplied — open_pnl must be None"
    assert result.marks_complete is False
    assert len(result.open_lots) == 1
    assert _close(result.open_lots[0].qty, 40.0)
    assert _close(result.open_lots[0].cost, 10.0)


def test_partial_close_with_mark() -> None:
    """BUY 100 @ $10, SELL 60 @ $12, mark=$15.

    Realized: $120. Open: 40 × ($15 - $10) = $200. marks_complete: True.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=60, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 15.0})
    assert _close(result.realized_pnl, 120.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 200.0)
    assert result.marks_complete is True


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
    """No fills → zero realized, zero open, None fee_total, marks_complete=True."""
    result = compute_fifo_pnl([])
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl == 0.0
    assert result.marks_complete is True
    assert result.fee_total is None
    assert result.closed_lots == []
    assert result.open_lots == []


def test_single_open_fill_no_mark() -> None:
    """Single BUY with no mark → open_pnl is None, marks_complete=False."""
    fills = [_fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is None
    assert result.marks_complete is False
    assert len(result.open_lots) == 1


def test_single_open_fill_with_mark() -> None:
    """Single BUY 100 @ $10, mark=$11 → open_pnl = 100 × $1 = $100, marks_complete=True."""
    fills = [_fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 11.0})
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 100.0)
    assert result.marks_complete is True


def test_short_open_lot_open_pnl() -> None:
    """SELL 50 @ $12 (short), mark=$10 → open_pnl = ($12 - $10) × 50 = $100."""
    fills = [_fill(side=OrderSide.SELL, qty=50, price=12.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 10.0})
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 100.0)


# ── realized_pnl_today — correct behavior ─────────────────────────────────────


def test_realized_pnl_today_overnight_buy_correct() -> None:
    """BUY before session, SELL in session — correctly yields realized P&L.

    Regression test for the pre-fix bug: the old implementation pre-filtered
    fills to the session window BEFORE running FIFO.  A BUY at ts=500_000
    (before session_open=1_000_000) was excluded, so the in-session SELL
    opened a new short lot and returned $0 realized.

    Correct behavior: run FIFO over ALL history, then filter closed lots by
    closed_at_ms.  The SELL at ts=1_500_000 closes the BUY at ts=500_000:
    realized = (12 - 10) × 100 = $200.  The closed lot's closed_at_ms is
    1_500_000, which is within [1_000_000, 2_000_000) → counted.
    """
    session_open = 1_000_000
    session_close = 2_000_000
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=500_000),    # before session
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=1_500_000), # in session
    ]
    pnl = realized_pnl_today(
        fills,
        session_open_ms=session_open,
        session_close_ms=session_close,
    )
    assert _close(pnl, 200.0), (
        f"expected $200.0 realized (overnight buy + in-session sell); got {pnl}. "
        "If you see 0.0, the old pre-filter bug was reintroduced."
    )


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


def test_realized_pnl_today_sell_outside_session_not_counted() -> None:
    """BUY in session, SELL after session_close → not counted in today's P&L."""
    session_open = 1_000_000
    session_close = 2_000_000
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1_500_000),  # in session
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=3_000_000), # after session
    ]
    pnl = realized_pnl_today(
        fills,
        session_open_ms=session_open,
        session_close_ms=session_close,
    )
    # Closed lot's closed_at_ms=3_000_000 is NOT in [1_000_000, 2_000_000) → $0
    assert _close(pnl, 0.0)


# ── marks_complete ─────────────────────────────────────────────────────────────


def test_marks_complete_partial_coverage_returns_none() -> None:
    """Two-symbol portfolio with mark for only one symbol → open_pnl=None.

    SPY: 100 open @ $10, mark=11.  AAPL: 50 open @ $200, NO mark.
    Expected: open_pnl=None, marks_complete=False.
    The SPY unrealized of $100 must NOT be returned — a partial sum is
    misleading and violates the marks_complete contract.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, symbol="SPY",
              event_key="exec:spy"),
        _fill(side=OrderSide.BUY, qty=50, price=200.0, ts_ms=1001, symbol="AAPL",
              event_key="exec:aapl"),
    ]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 11.0})
    assert result.open_pnl is None, (
        f"expected None (partial mark coverage); got {result.open_pnl}. "
        "open_pnl must never be a partial sum across symbols."
    )
    assert result.marks_complete is False


def test_marks_complete_all_symbols_covered() -> None:
    """Two-symbol portfolio with marks for BOTH symbols → open_pnl is correct sum."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, symbol="SPY",
              event_key="exec:spy"),
        _fill(side=OrderSide.BUY, qty=50, price=200.0, ts_ms=1001, symbol="AAPL",
              event_key="exec:aapl"),
    ]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 11.0, "AAPL": 205.0})
    # SPY: (11-10)×100=$100; AAPL: (205-200)×50=$250; total=$350
    assert result.marks_complete is True
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 350.0)


def test_marks_complete_flat_bot() -> None:
    """A fully flat bot always has marks_complete=True and open_pnl=0.0."""
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert result.marks_complete is True
    assert result.open_pnl == 0.0


# ── Multi-symbol ──────────────────────────────────────────────────────────────


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
    # compute_fifo_pnl processes whatever FillRecords it receives — dedup happens upstream
    # in project_instance_fills.  Here we verify the math on de-duped input.
    result = compute_fifo_pnl([fill_a, fill_b])
    assert _close(result.realized_pnl, 200.0)


# ── Exact arithmetic (#2550) ──────────────────────────────────────────────────


def test_multi_lot_partial_closes_match_exact_fraction_oracle() -> None:
    """Fractional lots close exactly; float views are the exact values rounded once.

    Independent oracle: each FIFO closure below is written by hand as an
    exact ``Fraction`` over the recorded decimal fill values.
      SELL 0.4 @ 10.07: 0.3 of lot1 (+0.3×0.06), 0.1 of lot2 (+0.1×0.04)
      SELL 1.2 @ 10.11: 0.6 of lot2 (+0.6×0.08), 0.6 of lot3 (+0.6×0.14)
      SELL 0.9 @ 9.93:  0.5 of lot3 (−0.5×0.04), then opens 0.4 short @ 9.93
      BUY  0.1 @ 9.90:  0.1 of the short (+0.1×0.03), leaving 0.3 short
    Binary-float FIFO drifts on every one of these closures and leaves a
    0.30000000000000004-share short lot.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=0.3, price=10.01, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=0.7, price=10.03, ts_ms=2000),
        _fill(side=OrderSide.BUY, qty=1.1, price=9.97, ts_ms=3000),
        _fill(side=OrderSide.SELL, qty=0.4, price=10.07, ts_ms=4000),
        _fill(side=OrderSide.SELL, qty=1.2, price=10.11, ts_ms=5000),
        _fill(side=OrderSide.SELL, qty=0.9, price=9.93, ts_ms=6000),
        _fill(side=OrderSide.BUY, qty=0.1, price=9.90, ts_ms=7000),
    ]
    f = Fraction
    closures = [
        f("0.3") * (f("10.07") - f("10.01")),
        f("0.1") * (f("10.07") - f("10.03")),
        f("0.6") * (f("10.11") - f("10.03")),
        f("0.6") * (f("10.11") - f("9.97")),
        f("0.5") * (f("9.93") - f("9.97")),
        f("0.1") * (f("9.93") - f("9.90")),
    ]
    expected = sum(closures, f(0))
    assert expected == f("0.137")

    result = compute_fifo_pnl(fills)

    # Float views are bit-exact (tolerance 0): the exact value rounded once.
    assert result.realized_pnl == float(expected)
    assert [lot.realized_pnl for lot in result.closed_lots] == [float(value) for value in closures]
    assert [(lot.side, lot.qty, lot.cost) for lot in result.open_lots] == [(OrderSide.SELL, 0.3, 9.93)]
    # The exact fields are the money authority.
    assert f(result.exact_realized_pnl) == expected
    assert [f(lot.exact_realized_pnl) for lot in result.closed_lots] == closures
    assert [(f(lot.exact_qty), f(lot.exact_cost)) for lot in result.open_lots] == [(f("0.3"), f("9.93"))]


def test_open_pnl_is_exact_and_its_float_view_is_rounded_once() -> None:
    """#2556: open valuation is an exact field; ``open_pnl`` only displays it.

    Independent oracle over the recorded decimal values:
      SPY: SELL 0.4 @ 10.07 closes lot1 (0.3) and 0.1 of lot2, leaving
           0.6 long @ 10.03, marked 10.11 -> +0.6×0.08
      QQQ: SELL 0.3 @ 9.93 opens a short, marked 9.90 -> +0.3×0.03
    Flat is exactly zero; a missing mark leaves the exact field ``None``.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=0.3, price=10.01, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=0.7, price=10.03, ts_ms=2000),
        _fill(side=OrderSide.SELL, qty=0.4, price=10.07, ts_ms=3000),
        _fill(side=OrderSide.SELL, qty=0.3, price=9.93, ts_ms=4000, symbol="QQQ"),
    ]
    f = Fraction
    expected = f("0.6") * (f("10.11") - f("10.03")) + f("0.3") * (f("9.93") - f("9.90"))
    assert expected == f("0.057")

    result = compute_fifo_pnl(fills, mark_prices={"SPY": 10.11, "QQQ": Decimal("9.90")})

    assert f(result.exact_open_pnl) == expected
    assert result.open_pnl == float(expected)
    assert compute_fifo_pnl(fills, mark_prices={"SPY": 10.11}).exact_open_pnl is None
    flat = [fills[0], _fill(side=OrderSide.SELL, qty=0.3, price=10.07, ts_ms=5000)]
    assert compute_fifo_pnl(flat).exact_open_pnl == 0


def test_open_pnl_money_is_exact_at_a_whole_cent_boundary() -> None:
    """#2556: 0.048360857 shares bought at $100 and marked at $100.3101682007
    gain exactly $0.0149999999999999999, which shows as 1 cent. A binary float
    cannot hold those 18 significant digits: the float view is 0.015, and
    normalizing it into money rounds half-even to 2 cents -- why no money
    reads a float view."""
    fills = [_fill(side=OrderSide.BUY, qty=0.048360857, price=100.0, ts_ms=1000)]

    result = compute_fifo_pnl(fills, mark_prices={"SPY": Decimal("100.3101682007")})

    assert Fraction(result.exact_open_pnl) == Fraction("0.048360857") * (Fraction("100.3101682007") - 100)
    assert display_cents(result.exact_open_pnl) == 1
    assert display_cents(normalize_money(result.open_pnl)) == 2


# Float display views of exact FIFO values, on FIFO's own results and on the
# SQLite projections that carry them (``EconomicSnapshot``,
# ``AccountPnlAttribution``); see ``fifo_pnl``'s module docstring.
_FIFO_FLOAT_VIEWS = frozenset({
    "realized_pnl", "open_pnl", "entry_price", "exit_price",
    "realized_pnl_today", "realized_pnl_total", "start_open_pnl_total", "open_pnl_total",
})


def test_no_normalize_money_call_names_a_fifo_float_view_attribute() -> None:
    """#2556: a direct-attribute grep guard -- a tripwire, not the guarantee.

    It flags ``normalize_money(<x>.<float view>)`` written anywhere in the
    service, which re-admits a rounded float as money. It cannot follow a
    float view through a variable or a parameter (the bot page's open P&L and
    Today's statement once did exactly that), so the guarantee is structural:
    those renderers take FIFO's exact ``Decimal`` fields and
    ``money.display_cents`` refuses a float -- pinned by the rendered-string
    regressions in ``test_panel_projection.py``,
    ``test_fee_attribution_view.py`` and ``test_simulated_account.py``.
    """
    app_root = Path(__file__).parents[4] / "app"
    paths = sorted(app_root.rglob("*.py"))
    assert len(paths) > 100, "the guard must scan the service, not an empty tree"
    offenders = [
        f"{path.relative_to(app_root.parent)}:{node.lineno}: {ast.unparse(node)}"
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "normalize_money"
        and any(isinstance(arg, ast.Attribute) and arg.attr in _FIFO_FLOAT_VIEWS for arg in node.args)
    ]
    assert offenders == []


# ── Golden fixture scenarios ──────────────────────────────────────────────────

_FIXTURE_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "golden"
    / "broker-v2-fifo-pnl"
)


def _build_fills_from_scenario(scenario: dict) -> list[FillRecord]:
    """Construct FillRecords from a fixture scenario's fills list."""
    fills = []
    for i, f in enumerate(scenario["fills"]):
        side = OrderSide.BUY if f["side"] == "BUY" else OrderSide.SELL
        fills.append(
            FillRecord(
                account_id=_ACCT,
                sid=_SID,
                intent_id=f"int_{i}",
                order_ref=f"learn-ai/{_SID}/v1:int_{i}",
                event_key=f.get("event_key") or f"exec:{i}",
                symbol=f["symbol"],
                side=side,
                quantity=f["qty"],
                fill_price=f["price"],
                filled_at_ms=f["ts_ms"],
                fee=f.get("fee"),
            )
        )
    return fills


def test_golden_fixture_overnight_position() -> None:
    """Golden fixture: overnight_position scenario.

    BUY 100@$10, BUY 50@$11, SELL 80@$13 the next day.
    Expected: realized=$240, open_pnl=null (no marks), marks_complete=false,
    1 closed lot, 2 open lots.
    """
    with open(_FIXTURE_DIR / "input.json") as fh:
        inp = json.load(fh)
    with open(_FIXTURE_DIR / "output.json") as fh:
        out = json.load(fh)

    scenario_in = inp["scenarios"]["overnight_position"]
    scenario_out = out["scenarios"]["overnight_position"]
    atol = out["tolerance"]["atol"]

    fills = _build_fills_from_scenario(scenario_in)
    result = compute_fifo_pnl(fills)

    assert abs(result.realized_pnl - scenario_out["realized_pnl"]) <= atol, (
        f"realized_pnl: expected {scenario_out['realized_pnl']}, got {result.realized_pnl}"
    )
    assert result.open_pnl is None, (
        f"open_pnl: expected null (no marks), got {result.open_pnl}"
    )
    assert result.marks_complete is False
    assert len(result.closed_lots) == scenario_out["closed_lots_count"]
    assert len(result.open_lots) == scenario_out["open_lots_count"]


def test_golden_fixture_duplicate_delivery() -> None:
    """Golden fixture: duplicate_delivery scenario.

    Same event_key delivered twice — compute_fifo_pnl receives both but
    for this test we simulate the upstream dedup by manually de-duping.
    The fixture tests the FIFO math; upstream dedup is tested in test_fills.py.

    After dedup: BUY 100@$10, SELL 100@$12. realized=$200, flat.
    """
    with open(_FIXTURE_DIR / "input.json") as fh:
        inp = json.load(fh)
    with open(_FIXTURE_DIR / "output.json") as fh:
        out = json.load(fh)

    scenario_in = inp["scenarios"]["duplicate_delivery"]
    scenario_out = out["scenarios"]["duplicate_delivery"]
    atol = out["tolerance"]["atol"]

    # Simulate dedup: only take unique event_keys
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for f in scenario_in["fills"]:
        key = f.get("event_key", "")
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(f)

    fills = _build_fills_from_scenario({"fills": deduped})
    result = compute_fifo_pnl(fills)

    assert abs(result.realized_pnl - scenario_out["realized_pnl"]) <= atol
    assert result.open_pnl == scenario_out["open_pnl"]
    assert result.marks_complete is scenario_out["marks_complete"]
    assert len(result.closed_lots) == scenario_out["closed_lots_count"]
    assert len(result.open_lots) == scenario_out["open_lots_count"]


def test_golden_fixture_partial_fill_sequence() -> None:
    """Golden fixture: partial_fill_sequence scenario.

    Two distinct event_keys for buys (60+40 = 100 shares @ $10 each).
    SELL 100@$12. realized=$200, flat, marks_complete=True.
    """
    with open(_FIXTURE_DIR / "input.json") as fh:
        inp = json.load(fh)
    with open(_FIXTURE_DIR / "output.json") as fh:
        out = json.load(fh)

    scenario_in = inp["scenarios"]["partial_fill_sequence"]
    scenario_out = out["scenarios"]["partial_fill_sequence"]
    atol = out["tolerance"]["atol"]

    fills = _build_fills_from_scenario(scenario_in)
    result = compute_fifo_pnl(fills)

    assert abs(result.realized_pnl - scenario_out["realized_pnl"]) <= atol
    assert result.open_pnl == scenario_out["open_pnl"]
    assert result.marks_complete is scenario_out["marks_complete"]
    assert len(result.closed_lots) == scenario_out["closed_lots_count"]
    assert len(result.open_lots) == scenario_out["open_lots_count"]


def test_golden_fixture_reversal() -> None:
    """Golden fixture: reversal scenario.

    BUY 100@$10, SELL 150@$12. realized=$200, 50sh short open, open_pnl=null.
    """
    with open(_FIXTURE_DIR / "input.json") as fh:
        inp = json.load(fh)
    with open(_FIXTURE_DIR / "output.json") as fh:
        out = json.load(fh)

    scenario_in = inp["scenarios"]["reversal"]
    scenario_out = out["scenarios"]["reversal"]
    atol = out["tolerance"]["atol"]

    fills = _build_fills_from_scenario(scenario_in)
    result = compute_fifo_pnl(fills)

    assert abs(result.realized_pnl - scenario_out["realized_pnl"]) <= atol
    assert result.open_pnl is None
    assert result.marks_complete is False
    assert len(result.closed_lots) == scenario_out["closed_lots_count"]
    assert len(result.open_lots) == scenario_out["open_lots_count"]


def test_golden_fixture_incomplete_marks() -> None:
    """Golden fixture: incomplete_marks scenario.

    SPY 100 open @ $10 (mark=11), AAPL 50 open @ $200 (no mark).
    Expected: realized=$0, open_pnl=null, marks_complete=false.
    """
    with open(_FIXTURE_DIR / "input.json") as fh:
        inp = json.load(fh)
    with open(_FIXTURE_DIR / "output.json") as fh:
        out = json.load(fh)

    scenario_in = inp["scenarios"]["incomplete_marks"]
    scenario_out = out["scenarios"]["incomplete_marks"]
    atol = out["tolerance"]["atol"]

    fills = _build_fills_from_scenario(scenario_in)
    mark_prices = scenario_in.get("mark_prices", {})
    result = compute_fifo_pnl(fills, mark_prices=mark_prices)

    assert abs(result.realized_pnl - scenario_out["realized_pnl"]) <= atol
    assert result.open_pnl is None, (
        f"expected null (incomplete marks); got {result.open_pnl}"
    )
    assert result.marks_complete is False
    assert len(result.closed_lots) == scenario_out["closed_lots_count"]
    assert len(result.open_lots) == scenario_out["open_lots_count"]
