"""Golden-fixture tests for canonical FIFO P&L (broker-v2 panel S0).

Fixture authority: PythonDataService/tests/fixtures/golden/broker-v2-fifo-pnl/attribution.md
Tolerance: atol=1e-9, rtol=0 (stricter than the 1e-6 accumulated-P&L default, ADR 0069 §3).

Each scenario is derived by hand from the FIFO algorithm and documented
in-line so a quant reviewer can audit without running the code.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.fifo_pnl import (
    LotWriteOff,
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
    multiplier: int | None = None,
    credit_withheld: bool = False,
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
        multiplier=multiplier,
        credit_withheld=credit_withheld,
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


def test_partial_close_without_and_with_a_mark() -> None:
    """BUY 100 @ $10, SELL 60 @ $12.

    Realized: (12 - 10) × 60 = $120.00.
    Open: 40 shares @ $10 (no mark → open_pnl is None, marks_complete=False).

    With mark=$15: Realized: $120. Open: 40 × ($15 - $10) = $200. marks_complete: True.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=60, price=12.0, ts_ms=2000),
    ]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 120.0), "partial close without a mark realizes $120"
    assert result.open_pnl is None, "no mark supplied — open_pnl must be None"
    assert result.marks_complete is False, "no mark supplied — marks are not complete"
    assert len(result.open_lots) == 1, "one open lot is left"
    assert _close(result.open_lots[0].qty, 40.0), "the open lot holds 40 shares"
    assert _close(result.open_lots[0].cost, 10.0), "the open lot costs $10"

    marked = compute_fifo_pnl(fills, mark_prices={"SPY": 15.0})
    assert _close(marked.realized_pnl, 120.0), "partial close with a mark realizes $120"
    assert marked.open_pnl is not None, "a mark supplied — open_pnl is stated"
    assert _close(marked.open_pnl, 200.0), "open P&L is 40 × ($15 - $10) = $200"
    assert marked.marks_complete is True, "a mark supplied — marks are complete"


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


# ── Scenario 6: fees not reported ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("buy_fee", "sell_fee"),
    [
        # Any fill with fee=None must cause fee_total=None ('Fees not reported').
        pytest.param(None, None, id="fee-none-propagates"),
        # Even one fill with fee=None must make fee_total None.
        pytest.param(1.0, None, id="fee-partial-none-propagates"),
    ],
)
def test_a_fill_with_no_fee_makes_the_fee_total_none(buy_fee: float | None, sell_fee: float | None) -> None:
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000, fee=buy_fee),
        _fill(side=OrderSide.SELL, qty=100, price=12.0, ts_ms=2000, fee=sell_fee),
    ]
    result = compute_fifo_pnl(fills)
    assert result.fee_total is None, (
        "fee_total must be None when any fill has fee=None — "
        "must render as 'Fees not reported', never $0.00"
    )


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


def test_single_open_fill_without_and_with_a_mark() -> None:
    """Single BUY with no mark → open_pnl is None, marks_complete=False.

    Single BUY 100 @ $10, mark=$11 → open_pnl = 100 × $1 = $100, marks_complete=True.
    """
    fills = [_fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills)
    assert _close(result.realized_pnl, 0.0), "a single open fill without a mark realizes nothing"
    assert result.open_pnl is None, "no mark supplied — open_pnl must be None"
    assert result.marks_complete is False, "no mark supplied — marks are not complete"
    assert len(result.open_lots) == 1, "one open lot is held"

    marked = compute_fifo_pnl(fills, mark_prices={"SPY": 11.0})
    assert _close(marked.realized_pnl, 0.0), "a single open fill with a mark realizes nothing"
    assert marked.open_pnl is not None, "a mark supplied — open_pnl is stated"
    assert _close(marked.open_pnl, 100.0), "open P&L is 100 × ($11 - $10) = $100"
    assert marked.marks_complete is True, "a mark supplied — marks are complete"


def test_short_open_lot_open_pnl() -> None:
    """SELL 50 @ $12 (short), mark=$10 → open_pnl = ($12 - $10) × 50 = $100."""
    fills = [_fill(side=OrderSide.SELL, qty=50, price=12.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, mark_prices={"SPY": 10.0})
    assert _close(result.realized_pnl, 0.0)
    assert result.open_pnl is not None
    assert _close(result.open_pnl, 100.0)


# ── realized_pnl_today — correct behavior ─────────────────────────────────────


@pytest.mark.parametrize(
    ("buy_ts_ms", "sell_ts_ms", "sell_price", "session_close", "expected"),
    [
        # BUY before session, SELL in session — correctly yields realized P&L.
        #
        # Regression test for the pre-fix bug: the old implementation pre-filtered
        # fills to the session window BEFORE running FIFO.  A BUY at ts=500_000
        # (before session_open=1_000_000) was excluded, so the in-session SELL
        # opened a new short lot and returned $0 realized.
        #
        # Correct behavior: run FIFO over ALL history, then filter closed lots by
        # closed_at_ms.  The SELL at ts=1_500_000 closes the BUY at ts=500_000:
        # realized = (12 - 10) × 100 = $200.  The closed lot's closed_at_ms is
        # 1_500_000, which is within [1_000_000, 2_000_000) → counted.
        pytest.param(500_000, 1_500_000, 12.0, 2_000_000, 200.0, id="overnight-buy-correct-prefilter-regression"),
        # BUY + SELL both within session → full realized P&L counted.
        pytest.param(1_500_000, 2_500_000, 13.0, 3_000_000, 300.0, id="round-trip-in-session"),
        # BUY in session, SELL after session_close → not counted in today's P&L.
        # Closed lot's closed_at_ms=3_000_000 is NOT in [1_000_000, 2_000_000) → $0
        pytest.param(1_500_000, 3_000_000, 12.0, 2_000_000, 0.0, id="sell-outside-session-not-counted"),
    ],
)
def test_realized_pnl_today_counts_the_lots_closed_inside_the_session(
    buy_ts_ms: int, sell_ts_ms: int, sell_price: float, session_close: int, expected: float,
) -> None:
    session_open = 1_000_000
    fills = [
        _fill(side=OrderSide.BUY, qty=100, price=10.0, ts_ms=buy_ts_ms),
        _fill(side=OrderSide.SELL, qty=100, price=sell_price, ts_ms=sell_ts_ms),
    ]
    pnl = realized_pnl_today(
        fills,
        session_open_ms=session_open,
        session_close_ms=session_close,
    )
    assert _close(pnl, expected), (
        f"expected ${expected} realized today; got {pnl}. "
        "If the overnight-buy row reads 0.0, the old pre-filter bug was reintroduced."
    )


# ── marks_complete ─────────────────────────────────────────────────────────────


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


# ── Written-off lots (#2975) ──────────────────────────────────────────────────
#
# A residue discharge takes a bot's attributed position off the books and
# closes no lot. Supplied to the walk as one LotWriteOff each, the oldest
# lots of that owner and symbol are set apart: no later fill of the bot's
# closes them, they realise nothing, and they are not open exposure. The one
# fill that may close them is one whose credit a discharge withheld (#2854).
# Every scenario below is the re-reviewer's measured case from #2975,
# derived by hand.


def _write_off(
    *,
    qty: str,
    ts_ms: int,
    sequence: int = 1,
    symbol: str = "SPY",
) -> LotWriteOff:
    return LotWriteOff(
        symbol=symbol,
        exact_qty=Decimal(qty),
        written_off_at_ms=ts_ms,
        ledger_sequence=sequence,
        sid=_SID,
    )


def test_a_written_off_lots_expiry_closes_the_new_lot_not_the_written_off_one() -> None:
    """#2975 case 1, in contracts: BUY 1 @ $1.94, write-off, BUY 1 @ $3.00, expire at zero.

    The written-off lot realises nothing, ever. The expiry closes the lot
    that expired -- the $3.00 one -- for -$300.00, not the written-off one's
    -$194.00, and no lot stays wrongly open behind it.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=1, price=1.94, ts_ms=1000, multiplier=100),
        _fill(side=OrderSide.BUY, qty=1, price=3.00, ts_ms=3000, multiplier=100),
        _fill(side=OrderSide.SELL, qty=1, price=0.0, ts_ms=4000, multiplier=100),
    ]
    write_offs = [_write_off(qty="1", ts_ms=2000)]
    result = compute_fifo_pnl(fills, write_offs=write_offs)

    assert _close(result.realized_pnl, -300.0), f"expected -300.0; got {result.realized_pnl}"
    assert len(result.closed_lots) == 1
    assert _close(result.closed_lots[0].entry_price, 3.00), "the lot that expired cost $3.00"
    # The written-off lot stays an open lot for valuation (ADR 0076 decision 22),
    # flagged as written off; the expiry closed the held lot.
    assert len(result.open_lots) == 1
    assert result.open_lots[0].written_off is True
    assert _close(result.open_lots[0].cost, 1.94)
    assert len(result.written_off_lots) == 1
    assert _close(result.written_off_lots[0].cost, 1.94)


def test_a_re_entrys_part_sale_after_a_write_off_closes_only_the_new_lots() -> None:
    """#2975 case 2, in contracts: BUY 1 @ $1.94, write-off, BUY 2 @ $3.00, SELL 1 @ $2.00, exercise 1 at zero.

    The sale realises -$100.00 (one $3.00 lot), not +$6.00 (the written-off
    lot); the exercise brings the running total to -$400.00, not -$294.00.
    Before the write-off rule these were the booked figures the issue names.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=1, price=1.94, ts_ms=1000, multiplier=100),
        _fill(side=OrderSide.BUY, qty=2, price=3.00, ts_ms=3000, multiplier=100),
        _fill(side=OrderSide.SELL, qty=1, price=2.00, ts_ms=4000, multiplier=100),
        _fill(side=OrderSide.SELL, qty=1, price=0.0, ts_ms=5000, multiplier=100),
    ]
    write_offs = [_write_off(qty="1", ts_ms=2000)]
    result = compute_fifo_pnl(fills, write_offs=write_offs)

    assert len(result.closed_lots) == 2
    assert _close(result.closed_lots[0].realized_pnl, -100.0), "the sale closes a $3.00 lot"
    assert _close(result.closed_lots[1].realized_pnl, -300.0), "the exercise closes the other"
    assert _close(result.realized_pnl, -400.0), f"expected -400.0; got {result.realized_pnl}"


def test_a_written_off_lot_stays_an_open_lot_for_valuation_and_realises_nothing() -> None:
    """BUY 10 @ $10, write-off 10: the lot stays on the books for valuation, realising nothing.

    ADR 0076 decision 22: the budget carries the written-off premium as a
    position for good, and equity marks the lot as any open lot. Without a
    mark the valuation is honestly incomplete; with one it is exact.
    """
    fills = [_fill(side=OrderSide.BUY, qty=10, price=10.0, ts_ms=1000)]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="10", ts_ms=2000)])

    assert _close(result.realized_pnl, 0.0)
    assert len(result.open_lots) == 1
    assert result.open_lots[0].written_off is True
    assert result.marks_complete is False, "an open lot without a mark is honestly incomplete"
    assert result.open_pnl is None

    marked = compute_fifo_pnl(fills, mark_prices={"SPY": 4.0}, write_offs=[_write_off(qty="10", ts_ms=2000)])
    assert marked.marks_complete is True
    assert _close(marked.open_pnl, -60.0), "10 x (4 - 10), the written-off premium marked"
    assert len(marked.written_off_lots) == 1
    assert _close(marked.written_off_lots[0].qty, 10.0)


def test_a_withheld_fill_closes_the_written_off_lot_at_its_price() -> None:
    """BUY 10 @ $100, write-off, the unseen SELL 10 @ $102 arrives with its credit withheld (#2854).

    The discharge stood in for exactly that execution, so it is the one fill
    that closes the written-off lot: realised (102 - 100) x 10 = +$20.00.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=10, price=100.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=10, price=102.0, ts_ms=3000, credit_withheld=True),
    ]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="10", ts_ms=2000)])

    assert _close(result.realized_pnl, 20.0), f"expected +20.0; got {result.realized_pnl}"
    assert result.open_lots == []
    assert result.written_off_lots == [], "the withheld fill closed the written-off lot"


def test_a_write_off_of_a_short_position_sets_its_lots_apart() -> None:
    """SELL 5 @ $50 short, write-off -5, SELL 5 @ $40 short, BUY 5 @ $45 closes the new lot.

    The cover realises (40 - 45) x 5 = -$25.00 -- the new short lot -- and
    the written-off short lot realises nothing.
    """
    fills = [
        _fill(side=OrderSide.SELL, qty=5, price=50.0, ts_ms=1000),
        _fill(side=OrderSide.SELL, qty=5, price=40.0, ts_ms=3000),
        _fill(side=OrderSide.BUY, qty=5, price=45.0, ts_ms=4000),
    ]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="-5", ts_ms=2000)])

    assert _close(result.realized_pnl, -25.0), f"expected -25.0; got {result.realized_pnl}"
    assert len(result.written_off_lots) == 1
    assert _close(result.written_off_lots[0].cost, 50.0)


def test_a_write_off_takes_the_oldest_lots_whatever_was_bought_since() -> None:
    """BUY 10 @ $10, BUY 10 @ $12, write-off 10, SELL 10 @ $11.

    The write-off takes what was held then -- the $10 lot, the oldest -- so
    the sale closes the $12 lot: (11 - 12) x 10 = -$10.00.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=10, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=10, price=12.0, ts_ms=2000),
        _fill(side=OrderSide.SELL, qty=10, price=11.0, ts_ms=4000),
    ]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="10", ts_ms=3000)])

    assert _close(result.realized_pnl, -10.0), f"expected -10.0; got {result.realized_pnl}"
    assert _close(result.closed_lots[0].entry_price, 12.0)
    assert _close(result.written_off_lots[0].cost, 10.0)


def test_a_write_off_that_parts_a_lot_splits_it_oldest_side_written_off() -> None:
    """BUY 10 @ $10, BUY 10 @ $12, write-off 15: 10@$10 and 5@$12 set apart, 5@$12 closable.

    A following SELL 5 @ $11 closes the remaining $12 part: (11 - 12) x 5 = -$5.00.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=10, price=10.0, ts_ms=1000),
        _fill(side=OrderSide.BUY, qty=10, price=12.0, ts_ms=2000),
        _fill(side=OrderSide.SELL, qty=5, price=11.0, ts_ms=4000),
    ]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="15", ts_ms=3000)])

    assert _close(result.realized_pnl, -5.0), f"expected -5.0; got {result.realized_pnl}"
    assert _close(result.written_off_lots[0].cost, 10.0)
    assert _close(result.written_off_lots[0].qty, 10.0)
    assert _close(result.written_off_lots[1].cost, 12.0)
    assert _close(result.written_off_lots[1].qty, 5.0)
    assert [lot.written_off for lot in result.open_lots] == [True, True]


def test_a_write_off_of_another_bot_sets_aside_nothing_of_this_one() -> None:
    """BUY 10 @ $10 with BUY 10 @ $20 beside it by another bot, write-off only that one, SELL 10 @ $11.

    The write-off names its own owner's lots, so this bot's sale still closes
    its own $10 lot: +$10.00. The other bot's $20 lot stays its own.
    """
    other = replace(
        _fill(side=OrderSide.BUY, qty=10, price=20.0, ts_ms=1500),
        sid="other-bot",
    )
    fills = [
        _fill(side=OrderSide.BUY, qty=10, price=10.0, ts_ms=1000),
        other,
        _fill(side=OrderSide.SELL, qty=10, price=11.0, ts_ms=4000),
    ]
    result = compute_fifo_pnl(
        fills,
        write_offs=[replace(_write_off(qty="10", ts_ms=3000), sid="other-bot")],
    )

    assert _close(result.realized_pnl, 10.0), f"expected +10.0; got {result.realized_pnl}"
    assert len(result.written_off_lots) == 1, "the write-off set apart its own bot's lot"
    assert _close(result.written_off_lots[0].cost, 20.0)
    assert [lot.written_off for lot in result.open_lots] == [True]


def test_an_account_wide_write_off_takes_the_quantity_after_a_cross_owner_close() -> None:
    """A mixed walk after one bot's sale consumed the written-off bot's older lot.

    Bot A buys 10 @ $10, bot B buys 10 @ $20, B's SELL 10 closes A's older
    lot (the cross-owner close a mixed queue allows), then A's +10 is
    discharged. A's own tagged lots are gone, so the write-off takes its
    quantity from the symbol's oldest same-side lot -- B's -- and A's later
    round trip closes only its own new lot: +$5.00.
    """
    other_buy = replace(_fill(side=OrderSide.BUY, qty=10, price=20.0, ts_ms=1500), sid="other-bot")
    other_sell = replace(_fill(side=OrderSide.SELL, qty=10, price=21.0, ts_ms=2500), sid="other-bot")
    fills = [
        _fill(side=OrderSide.BUY, qty=10, price=10.0, ts_ms=1000),
        other_buy,
        other_sell,
        _fill(side=OrderSide.BUY, qty=5, price=30.0, ts_ms=4000),
        _fill(side=OrderSide.SELL, qty=5, price=31.0, ts_ms=5000),
    ]
    result = compute_fifo_pnl(fills, write_offs=[_write_off(qty="10", ts_ms=3000)])

    # B's cross-owner close realized against A's older lot, +$110; A's own
    # round trip realized +$5. The discharge removed its quantity from the
    # closable books (B's remaining lot is set apart), so no later close can
    # take it.
    assert _close(result.realized_pnl, 115.0), f"expected +115.0; got {result.realized_pnl}"
    assert len(result.written_off_lots) == 1, "the discharge took the oldest remaining lot"
    assert _close(result.written_off_lots[0].cost, 20.0)
    assert [lot.written_off for lot in result.open_lots] == [True]
