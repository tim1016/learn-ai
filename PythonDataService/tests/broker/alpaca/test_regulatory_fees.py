"""Unit tests for the canonical Alpaca regulatory fee model (ADR 0059 D6)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.regulatory_fees import (
    FillFees,
    RateNotPinnedError,
    fees_for_fill,
    rates_for,
    settle_session,
)
from app.broker.contract.models import OrderSide

D = Decimal
TRADE_DATE_2026 = date(2026, 9, 8)


@pytest.mark.parametrize(
    ("trade_date", "sec_per_dollar"),
    [
        (date(2024, 5, 21), None),  # before the first pinned SEC row
        (date(2024, 5, 22), D("0.0000278")),  # SEC advisory 2024-2
        (date(2025, 5, 13), D("0.0000278")),
        (date(2025, 5, 14), D("0")),  # SEC advisory 2025-2: $0.00 per million
        (date(2026, 4, 3), D("0")),
        (date(2026, 4, 4), D("0.0000206")),  # SEC advisory 2026-2
    ],
)
def test_rates_for_sec_regime_boundaries(trade_date: date, sec_per_dollar: Decimal | None) -> None:
    assert rates_for(trade_date).sec_per_dollar == sec_per_dollar


@pytest.mark.parametrize(
    ("trade_date", "per_share", "cap"),
    [
        (date(2023, 12, 31), None, None),  # before the first pinned TAF row
        (date(2024, 1, 1), D("0.000166"), D("8.30")),
        (date(2025, 12, 31), D("0.000166"), D("8.30")),
        (date(2026, 1, 1), D("0.000195"), D("9.79")),
        (date(2027, 1, 1), D("0.000232"), D("11.61")),
        (date(2028, 1, 1), D("0.000240"), D("12.05")),
        (date(2029, 1, 1), D("0.000249"), D("12.50")),
    ],
)
def test_rates_for_taf_annual_schedule(trade_date: date, per_share: Decimal | None, cap: Decimal | None) -> None:
    rates = rates_for(trade_date)
    assert rates.taf_per_share == per_share
    assert rates.taf_cap_per_trade == cap


@pytest.mark.parametrize(
    ("trade_date", "cat_per_share"),
    [(date(2026, 8, 31), None), (date(2026, 9, 1), D("0.000003"))],
)
def test_rates_for_cat_pinned_from_publication(trade_date: date, cat_per_share: Decimal | None) -> None:
    assert rates_for(trade_date).cat_per_share == cat_per_share


def test_sell_accrues_all_three_components_unrounded() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00"))

    assert fees == FillFees(sec=D("0.515"), taf=D("0.0195"), cat=D("0.0003"))
    assert fees.unpinned == ()


def test_buy_owes_only_cat() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00"))

    assert fees == FillFees(sec=D("0"), taf=D("0"), cat=D("0.0003"))


@pytest.mark.parametrize(
    ("quantity", "expected_taf"),
    [
        (D("50205"), D("9.789975")),  # 50,205 × 0.000195 is still under the cap
        (D("50206"), D("9.79")),  # one more share and the per-trade cap binds
        (D("60000"), D("9.79")),
    ],
)
def test_taf_cap_binds_above_boundary(quantity: Decimal, expected_taf: Decimal) -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=quantity, fill_price=D("1.00"))

    assert fees.taf == expected_taf


def test_fractional_share_accrues_proportionally() -> None:
    fees = fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00"))

    assert fees.cat == D("0.0000015")


def test_unpinned_component_is_none_not_zero() -> None:
    fees = fees_for_fill(trade_date=date(2025, 6, 2), side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00"))

    assert fees.sec == D("0")  # the $0.00 regime is a pinned zero
    assert fees.taf == D("0.0166")
    assert fees.cat is None
    assert fees.unpinned == ("cat",)


def test_buy_before_sec_pin_still_owes_zero_sec_and_taf() -> None:
    fees = fees_for_fill(trade_date=date(2024, 3, 1), side=OrderSide.BUY, quantity=D("10"), fill_price=D("100.00"))

    assert (fees.sec, fees.taf, fees.cat) == (D("0"), D("0"), None)


def test_sell_before_sec_pin_has_unpinned_sec() -> None:
    fees = fees_for_fill(trade_date=date(2024, 3, 1), side=OrderSide.SELL, quantity=D("10"), fill_price=D("100.00"))

    assert fees.sec is None
    assert fees.taf == D("0.00166")
    assert fees.unpinned == ("sec", "cat")


@pytest.mark.parametrize("quantity", [D("0"), D("-1")])
def test_non_positive_quantity_is_refused(quantity: Decimal) -> None:
    with pytest.raises(ValueError, match="positive"):
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=quantity, fill_price=D("1"))


def test_settle_session_rounds_each_component_up_to_the_cent() -> None:
    fills = [
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("100"), fill_price=D("250.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("100"), fill_price=D("250.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("60000"), fill_price=D("10.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("50205"), fill_price=D("1.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.SELL, quantity=D("50206"), fill_price=D("1.00")),
        fees_for_fill(trade_date=TRADE_DATE_2026, side=OrderSide.BUY, quantity=D("0.5"), fill_price=D("400.00")),
    ]

    settled = settle_session(fills)

    # sec: 0.515 + 12.36 + 1.034223 + 1.0342436 = 14.9434666 → 14.95
    # taf: 0.0195 + 9.79 + 9.789975 + 9.79     = 29.389475  → 29.39
    # cat: 0.0003·2 + 0.18 + 0.150615 + 0.150618 + 0.0000015 = 0.4818345 → 0.49
    assert (settled.sec, settled.taf, settled.cat) == (D("14.95"), D("29.39"), D("0.49"))
    assert settled.total == D("44.83")
    assert settled.fill_count == 6


def test_settle_session_does_not_bump_an_exact_cent() -> None:
    settled = settle_session([FillFees(sec=D("0.50"), taf=D("0"), cat=D("0.010"))])

    assert (settled.sec, settled.taf, settled.cat) == (D("0.50"), D("0.00"), D("0.01"))


def test_settle_session_of_nothing_is_zero() -> None:
    settled = settle_session([])

    assert settled.total == D("0.00")
    assert settled.fill_count == 0


def test_settle_session_refuses_an_unpinned_component() -> None:
    fills = [fees_for_fill(trade_date=date(2025, 6, 2), side=OrderSide.SELL, quantity=D("1"), fill_price=D("1"))]

    with pytest.raises(RateNotPinnedError) as excinfo:
        settle_session(fills)

    assert excinfo.value.components == ("cat",)
