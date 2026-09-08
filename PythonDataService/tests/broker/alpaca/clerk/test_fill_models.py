"""The two synthetic fill models: the sim world's immediate fill, and limit_touch.

limit_touch: eligibility starts after the decision bar; a bar that reaches the
limit fills at the limit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.fill_models import (
    SyntheticFill,
    immediate_fill_price,
    limit_touch_fill,
)
from app.broker.contract.models import BrokerOrderLeg
from app.services.source_bar_ledger import RetainedSourceBar

_T0 = 1_800_000_000_000


def _bar(seq: int, *, start_ms: int, low: str, high: str) -> RetainedSourceBar:
    return RetainedSourceBar(
        seq=seq, account_id="shadow:x", provider="ibkr", symbol="SPY",
        bar_identity=f"ibkr:SPY:{start_ms}:{start_ms + 60_000}", bar_ref=f"bar-{seq}",
        start_ms=start_ms, end_ms=start_ms + 60_000,
        open=Decimal(low), high=Decimal(high), low=Decimal(low), close=Decimal(high), volume=1, fetched_at_ms=start_ms + 60_000,
        session_phase="UNKNOWN",
    )


def _buy(limit: float) -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=limit, extended_hours=True)


def test_the_decision_bar_itself_never_fills() -> None:
    decision = _bar(1, start_ms=_T0, low="99.00", high="101.00")  # would touch a 100.10 buy
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=decision.end_ms, bars=[decision], cancel_at_ms=_T0 + 3_600_000) is None


def test_the_first_later_bar_that_touches_fills_at_the_limit() -> None:
    bars = [
        _bar(1, start_ms=_T0, low="99.00", high="101.00"),
        _bar(2, start_ms=_T0 + 60_000, low="100.50", high="100.70"),  # above the limit: no touch
        _bar(3, start_ms=_T0 + 120_000, low="100.05", high="100.30"),  # low ≤ 100.10: touch
    ]
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=_T0 + 60_000, bars=bars, cancel_at_ms=_T0 + 3_600_000) == SyntheticFill(
        filled_at_ms=_T0 + 180_000, price=Decimal("100.10"), bar_ref="bar-3"
    )


def test_a_sell_touches_on_the_high() -> None:
    sell = BrokerOrderLeg(symbol="SPY", side="sell", quantity=1, order_type="limit", limit_price=99.80, extended_hours=True)
    bars = [_bar(2, start_ms=_T0 + 60_000, low="99.00", high="99.79"), _bar(3, start_ms=_T0 + 120_000, low="99.50", high="99.80")]
    fill = limit_touch_fill(sell, decision_bar_end_ms=_T0 + 60_000, bars=bars, cancel_at_ms=_T0 + 3_600_000)
    assert fill is not None and fill.bar_ref == "bar-3" and fill.price == Decimal("99.80")


def test_a_bar_closing_after_the_cancel_instant_cannot_fill() -> None:
    late = _bar(2, start_ms=_T0 + 60_000, low="90.00", high="110.00")
    assert limit_touch_fill(_buy(100.10), decision_bar_end_ms=_T0 + 60_000, bars=[late], cancel_at_ms=_T0 + 90_000) is None


def test_market_legs_are_refused() -> None:
    with pytest.raises(ValueError, match="limit"):
        limit_touch_fill(BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), decision_bar_end_ms=_T0, bars=[], cancel_at_ms=_T0)


def test_a_bar_closing_exactly_at_the_cancel_instant_still_fills() -> None:
    """The eligibility window is closed at its end: ``end_ms == cancel_at_ms`` is in.

    The predicate is ``bar.end_ms > cancel_at_ms``, so the boundary is
    inclusive — a bar that closed exactly as the vendor would have cancelled
    was live for its whole range.
    """
    boundary = _bar(2, start_ms=_T0 + 60_000, low="100.00", high="100.50")

    fill = limit_touch_fill(
        _buy(100.10),
        decision_bar_end_ms=_T0 + 60_000,
        bars=[boundary],
        cancel_at_ms=boundary.end_ms,
    )

    assert fill == SyntheticFill(filled_at_ms=boundary.end_ms, price=Decimal("100.10"), bar_ref="bar-2")


@pytest.mark.parametrize(
    ("side", "limit", "close", "expected"),
    [
        ("buy", 100.10, "100.10", "100.10"),  # exactly at the limit is marketable
        ("buy", 100.10, "100.05", "100.05"),
        ("buy", 100.10, "100.20", None),
        ("sell", 99.80, "99.80", "99.80"),
        ("sell", 99.80, "99.90", "99.90"),
        ("sell", 99.80, "99.70", None),
    ],
)
def test_immediate_fill_price_is_the_sim_worlds_whole_model(
    side: str, limit: float, close: str, expected: str | None
) -> None:
    """R9: the sim world cannot rest an order, so a limit either transacts at
    the decision bar's close or is cancelled unfilled on the spot."""
    leg = BrokerOrderLeg(
        symbol="SPY", side=side, quantity=1, order_type="limit", limit_price=limit, extended_hours=True
    )

    fill = immediate_fill_price(leg, Decimal(close))

    assert fill == (None if expected is None else Decimal(expected))


def test_immediate_fill_price_always_fills_a_market_leg() -> None:
    market = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)

    assert immediate_fill_price(market, Decimal("123.45")) == Decimal("123.45")
