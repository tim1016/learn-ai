"""Custody money for an option contract: quantity is contracts, cash is contracts × premium × multiplier.

Every case is worked by hand in its docstring and compared exactly. Each
option case sits beside its stock twin, which must read as it always has: a
fill with no multiplier is shares.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl, realized_pnl_today
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import MoneyInputError, contract_multiplier, notional
from app.broker.contract.models import OrderSide

_CALL = "AAPL261016C00200000"
_PUT = "AAPL261016P00190000"
_SHARES = "AAPL"


def _fill(
    *,
    side: OrderSide,
    qty: float,
    price: float,
    ts_ms: int,
    symbol: str = _CALL,
    multiplier: int | None = 100,
    sid: str = "bot:a",
) -> FillRecord:
    return FillRecord(
        account_id="acct",
        sid=sid,
        intent_id=f"int_{ts_ms}",
        order_ref=f"learn-ai/{sid}/v1:int_{ts_ms}",
        event_key=f"exec:{sid}:{ts_ms}",
        symbol=symbol,
        side=side,
        quantity=qty,
        fill_price=price,
        filled_at_ms=ts_ms,
        fee=None,
        multiplier=multiplier,
    )


def _stock(*, side: OrderSide, qty: float, price: float, ts_ms: int, symbol: str = _SHARES) -> FillRecord:
    return _fill(side=side, qty=qty, price=price, ts_ms=ts_ms, symbol=symbol, multiplier=None)


# ── Notional ──────────────────────────────────────────────────────────────────


def test_two_contracts_at_a_dollar_fifty_move_three_hundred_dollars() -> None:
    """2 contracts × $1.50 × 100 = $300.00; the same figures as shares are $3.00."""
    assert notional(2, 1.50, multiplier=100) == Decimal(300)
    assert notional(2, 1.50) == Decimal(3)
    assert _fill(side=OrderSide.BUY, qty=2, price=1.50, ts_ms=1).exact_notional == Decimal(300)
    assert _stock(side=OrderSide.BUY, qty=2, price=1.50, ts_ms=1).exact_notional == Decimal(3)


def test_a_half_cent_premium_times_its_multiplier_is_exact() -> None:
    """3 × $1.505 × 100 = $451.50, with no float residue."""
    assert notional(3, 1.505, multiplier=100) == Decimal("451.5")


@pytest.mark.parametrize("bad", [0, -100, True, 100.0, "100", Decimal(100)])
def test_a_multiplier_that_is_not_a_whole_number_of_one_or_more_is_refused(bad: object) -> None:
    with pytest.raises(MoneyInputError):
        contract_multiplier(bad)
    with pytest.raises(MoneyInputError):
        notional(1, 1, multiplier=bad)  # type: ignore[arg-type]


# ── Realized P&L ──────────────────────────────────────────────────────────────


def test_two_contracts_bought_at_1_50_and_sold_at_2_10_gain_120_dollars() -> None:
    """(2.10 − 1.50) × 2 contracts × 100 = $120.00 before fees."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.BUY, qty=2, price=1.50, ts_ms=1_000),
            _fill(side=OrderSide.SELL, qty=2, price=2.10, ts_ms=2_000),
        ]
    )
    assert result.exact_realized_pnl == Decimal(120)
    assert result.open_lots == []
    assert result.exact_open_pnl == Decimal(0)
    (closed,) = result.closed_lots
    # The record keeps contracts and premiums as they traded, and says what they multiply by.
    assert (closed.qty, closed.entry_price, closed.exit_price, closed.multiplier) == (2.0, 1.50, 2.10, 100)


def test_the_same_fills_as_shares_gain_a_dollar_twenty_as_before() -> None:
    """Stock twin: (2.10 − 1.50) × 2 shares = $1.20, with no multiplier anywhere."""
    result = compute_fifo_pnl(
        [
            _stock(side=OrderSide.BUY, qty=2, price=1.50, ts_ms=1_000),
            _stock(side=OrderSide.SELL, qty=2, price=2.10, ts_ms=2_000),
        ]
    )
    assert result.exact_realized_pnl == Decimal("1.2")
    assert result.closed_lots[0].multiplier is None


def test_a_sold_option_bought_back_for_less_keeps_the_difference() -> None:
    """Sold 1 at $3.07, bought back at $1.20: (3.07 − 1.20) × 1 × 100 = $187.00."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.SELL, qty=1, price=3.07, ts_ms=1_000, symbol=_PUT),
            _fill(side=OrderSide.BUY, qty=1, price=1.20, ts_ms=2_000, symbol=_PUT),
        ]
    )
    assert result.exact_realized_pnl == Decimal(187)
    assert result.open_lots == []


def test_a_sold_option_bought_back_for_more_loses_the_difference() -> None:
    """Sold 2 at $0.80, bought back at $1.35: (0.80 − 1.35) × 2 × 100 = −$110.00."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.SELL, qty=2, price=0.80, ts_ms=1_000, symbol=_PUT),
            _fill(side=OrderSide.BUY, qty=2, price=1.35, ts_ms=2_000, symbol=_PUT),
        ]
    )
    assert result.exact_realized_pnl == Decimal(-110)


def test_partial_closes_take_the_oldest_contracts_first() -> None:
    """BUY 3 @ 1.00, BUY 2 @ 1.40, SELL 4 @ 2.00, then SELL 1 @ 1.90.

    First sale: 3 × (2.00 − 1.00) × 100 = $300 and 1 × (2.00 − 1.40) × 100 = $60.
    One contract stays open at $1.40: it cost $140.00, and at a mark of $1.90
    it is up (1.90 − 1.40) × 1 × 100 = $50.00.
    Second sale closes it: (1.90 − 1.40) × 1 × 100 = $50.
    """
    opened = [
        _fill(side=OrderSide.BUY, qty=3, price=1.00, ts_ms=1_000),
        _fill(side=OrderSide.BUY, qty=2, price=1.40, ts_ms=2_000),
        _fill(side=OrderSide.SELL, qty=4, price=2.00, ts_ms=3_000),
    ]
    partly = compute_fifo_pnl(opened, mark_prices={_CALL: 1.90})
    assert [lot.exact_realized_pnl for lot in partly.closed_lots] == [Decimal(300), Decimal(60)]
    assert partly.exact_realized_pnl == Decimal(360)
    (still_open,) = partly.open_lots
    assert (still_open.exact_qty, still_open.exact_cost, still_open.multiplier) == (Decimal(1), Decimal("1.4"), 100)
    assert still_open.exact_cost_basis == Decimal(140)
    assert partly.exact_open_pnl == Decimal(50)

    flat = compute_fifo_pnl([*opened, _fill(side=OrderSide.SELL, qty=1, price=1.90, ts_ms=4_000)])
    assert flat.exact_realized_pnl == Decimal(410)
    assert flat.open_lots == []


def test_a_contract_with_a_multiplier_other_than_100_counts_its_own() -> None:
    """A contract of 10 shares: BUY 5 @ 0.80, SELL 5 @ 1.05 → 0.25 × 5 × 10 = $12.50, not $125.00."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.BUY, qty=5, price=0.80, ts_ms=1_000, multiplier=10),
            _fill(side=OrderSide.SELL, qty=5, price=1.05, ts_ms=2_000, multiplier=10),
        ]
    )
    assert result.exact_realized_pnl == Decimal("12.5")


def test_an_open_sold_contract_gains_as_its_price_falls() -> None:
    """Sold 2 at $1.10, marked at $0.70: (1.10 − 0.70) × 2 × 100 = $80.00 open gain."""
    result = compute_fifo_pnl(
        [_fill(side=OrderSide.SELL, qty=2, price=1.10, ts_ms=1_000, symbol=_PUT)],
        mark_prices={_PUT: 0.70},
    )
    assert result.exact_open_pnl == Decimal(80)
    assert result.open_lots[0].side is OrderSide.SELL


def test_option_lots_match_an_exact_fraction_oracle() -> None:
    """Prices that are not exact in binary: the answer is the exact rational, by the multiplier."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.BUY, qty=7, price=0.1, ts_ms=1_000),
            _fill(side=OrderSide.BUY, qty=3, price=0.2, ts_ms=2_000),
            _fill(side=OrderSide.SELL, qty=9, price=0.3, ts_ms=3_000),
        ],
        mark_prices={_CALL: 0.7},
    )
    tenth = Fraction(1, 10)
    realized = (7 * (3 * tenth - tenth) + 2 * (3 * tenth - 2 * tenth)) * 100
    assert Fraction(result.exact_realized_pnl) == realized == Fraction(160)
    assert Fraction(result.exact_open_pnl) == (7 * tenth - 2 * tenth) * 1 * 100 == Fraction(50)


# ── Positions per contract ────────────────────────────────────────────────────


def test_two_contracts_on_one_underlying_are_two_positions() -> None:
    """A call and a put on AAPL: selling the call closes only the call."""
    result = compute_fifo_pnl(
        [
            _fill(side=OrderSide.BUY, qty=1, price=2.00, ts_ms=1_000, symbol=_CALL),
            _fill(side=OrderSide.BUY, qty=1, price=1.00, ts_ms=2_000, symbol=_PUT),
            _fill(side=OrderSide.SELL, qty=1, price=2.50, ts_ms=3_000, symbol=_CALL),
        ]
    )
    assert result.exact_realized_pnl == Decimal(50)
    assert [(lot.symbol, lot.exact_qty) for lot in result.open_lots] == [(_PUT, Decimal(1))]


def test_a_contract_and_shares_of_its_underlying_are_never_netted() -> None:
    """100 AAPL shares and 1 AAPL call, both bought. Selling the shares leaves the call whole.

    Shares: (191 − 190) × 100 = $100.00. The call stays open: 1 contract at
    $2.00, and at a mark of $2.50 it is up (2.50 − 2.00) × 1 × 100 = $50.00.
    """
    result = compute_fifo_pnl(
        [
            _stock(side=OrderSide.BUY, qty=100, price=190.0, ts_ms=1_000),
            _fill(side=OrderSide.BUY, qty=1, price=2.00, ts_ms=2_000),
            _stock(side=OrderSide.SELL, qty=100, price=191.0, ts_ms=3_000),
        ],
        mark_prices={_CALL: 2.50},
    )
    assert result.exact_realized_pnl == Decimal(100)
    (call,) = result.open_lots
    assert (call.symbol, call.exact_qty, call.multiplier) == (_CALL, Decimal(1), 100)
    assert result.exact_open_pnl == Decimal(50)


def test_two_bots_holding_one_contract_each_keep_their_own_lots() -> None:
    """Bot A buys 1 at $1.00 and sells it at $1.50; bot B buys 2 at $1.20 and holds.

    Each bot's money is read over its own fills, as the budget reads it:
    A gains (1.50 − 1.00) × 1 × 100 = $50.00 and is flat; B holds 2 contracts
    that cost 2 × 1.20 × 100 = $240.00.
    """
    fills = [
        _fill(side=OrderSide.BUY, qty=1, price=1.00, ts_ms=1_000, sid="bot:a"),
        _fill(side=OrderSide.BUY, qty=2, price=1.20, ts_ms=2_000, sid="bot:b"),
        _fill(side=OrderSide.SELL, qty=1, price=1.50, ts_ms=3_000, sid="bot:a"),
    ]
    a = compute_fifo_pnl([fill for fill in fills if fill.sid == "bot:a"])
    b = compute_fifo_pnl([fill for fill in fills if fill.sid == "bot:b"])
    assert (a.exact_realized_pnl, a.open_lots) == (Decimal(50), [])
    assert b.exact_realized_pnl == Decimal(0)
    assert sum(lot.exact_cost_basis for lot in b.open_lots) == Decimal(240)


def test_one_symbol_cannot_hold_contracts_and_shares() -> None:
    """A fill that counts differently from its symbol's open lots is refused, never netted."""
    with pytest.raises(MoneyInputError, match="contracts of 100 shares"):
        compute_fifo_pnl(
            [
                _fill(side=OrderSide.BUY, qty=1, price=2.00, ts_ms=1_000),
                _fill(side=OrderSide.SELL, qty=1, price=2.50, ts_ms=2_000, multiplier=None),
            ]
        )
    with pytest.raises(MoneyInputError, match="contracts of 10 shares"):
        compute_fifo_pnl(
            [
                _fill(side=OrderSide.BUY, qty=1, price=2.00, ts_ms=1_000),
                _fill(side=OrderSide.BUY, qty=1, price=2.50, ts_ms=2_000, multiplier=10),
            ]
        )


# ── Realized today ────────────────────────────────────────────────────────────


def test_a_contract_bought_yesterday_and_sold_today_counts_today_with_its_multiplier() -> None:
    """Bought 2 at $1.50 before the session, sold at $2.10 inside it: $120.00 realized today."""
    fills = [
        _fill(side=OrderSide.BUY, qty=2, price=1.50, ts_ms=500),
        _fill(side=OrderSide.SELL, qty=2, price=2.10, ts_ms=1_500),
    ]
    assert realized_pnl_today(fills, session_open_ms=1_000, session_close_ms=2_000) == 120.0
    assert realized_pnl_today(fills, session_open_ms=2_000, session_close_ms=3_000) == 0.0
