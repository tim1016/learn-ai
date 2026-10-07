"""The option fee prediction: rates by date, sides, the multiplier, unpinned rates, settlement."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.broker.alpaca.option_fees import (
    OptionRateNotPinnedError,
    fees_for_option_fill,
    option_fee_provision_cents,
    option_rates_for,
    settle_option_session,
)
from app.broker.alpaca.regulatory_fees import fees_for_fill, rates_for
from app.broker.contract.models import OrderSide

_TODAY = date(2026, 10, 5)


def _fees(side: OrderSide, *, contracts: str = "2", premium: str = "2.10", multiplier: int = 100, on: date = _TODAY):
    return fees_for_option_fill(
        trade_date=on, side=side, contracts=Decimal(contracts), premium=Decimal(premium), multiplier=multiplier
    )


def test_a_purchase_owes_no_sec_fee_and_no_taf_and_owes_the_per_contract_fees() -> None:
    """Buying 2 contracts: ORF 2 × 0.015, OCC 2 × 0.025, CAT 2 × 100 × 0.000003."""
    fees = _fees(OrderSide.BUY, premium="1.50")
    assert (fees.sec, fees.taf) == (Decimal(0), Decimal(0))
    assert (fees.orf, fees.occ, fees.cat) == (Decimal("0.030"), Decimal("0.050"), Decimal("0.0006"))


def test_a_sale_owes_the_sec_fee_on_its_whole_premium_not_on_its_price() -> None:
    """Selling 2 at $2.10 is $420.00 of premium: 420 × 0.0000206 = $0.008652, not 4.20 × 0.0000206."""
    assert _fees(OrderSide.SELL).sec == Decimal("0.008652")
    # The stock model would charge the same numbers as 2 shares at $2.10.
    as_shares = fees_for_fill(trade_date=_TODAY, side=OrderSide.SELL, quantity=Decimal(2), fill_price=Decimal("2.10"))
    assert as_shares.sec == Decimal("0.00008652")


def test_the_multiplier_is_the_contracts_own_and_never_assumed() -> None:
    """A contract of 10 shares: a tenth of the premium, and a tenth of the CAT shares."""
    standard, small = _fees(OrderSide.SELL), _fees(OrderSide.SELL, multiplier=10)
    assert small.sec == standard.sec / 10
    assert small.cat == standard.cat / 10
    # The per-contract fees do not depend on what a contract delivers.
    assert (small.orf, small.occ, small.taf) == (standard.orf, standard.occ, standard.taf)
    for bad in (0, -100, True, 100.0, None):
        with pytest.raises(ValueError, match="multiplier"):
            _fees(OrderSide.SELL, multiplier=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(("contracts", "premium"), [("0", "1"), ("-1", "1"), ("1.5", "1"), ("1", "0"), ("1", "-0.05")])
def test_a_fill_is_whole_contracts_at_a_price_above_zero(contracts: str, premium: str) -> None:
    with pytest.raises(ValueError, match="whole number of contracts"):
        _fees(OrderSide.BUY, contracts=contracts, premium=premium)


def test_the_taf_is_paused_for_the_last_quarter_of_2026_and_resumes() -> None:
    def taf(on: date) -> Decimal | None:
        return option_rates_for(on).taf_per_contract

    assert taf(date(2026, 9, 30)) == Decimal("0.00329")
    assert taf(date(2026, 10, 1)) == Decimal(0)
    assert taf(date(2026, 12, 31)) == Decimal(0)
    assert taf(date(2027, 1, 1)) == Decimal("0.00329")
    assert taf(date(2028, 12, 31)) == Decimal("0.00329")
    assert taf(date(2029, 1, 1)) == Decimal("0.00390")
    assert taf(date(2031, 6, 1)) == Decimal("0.00420")


def test_the_orf_follows_alpacas_schedule_revisions() -> None:
    def orf(on: date) -> Decimal | None:
        return option_rates_for(on).orf_per_contract

    assert orf(date(2026, 2, 11)) is None
    assert orf(date(2026, 2, 12)) == Decimal("0.02375")
    assert orf(date(2026, 4, 9)) == Decimal("0.02295")
    assert orf(date(2026, 6, 30)) == Decimal("0.02295")
    assert orf(date(2026, 7, 1)) == Decimal("0.015")


def test_the_sec_rate_is_the_stock_models_one_table() -> None:
    for on in (date(2024, 3, 1), date(2025, 6, 2), date(2026, 4, 3), date(2026, 4, 4), _TODAY):
        assert option_rates_for(on).sec_per_dollar == rates_for(on).sec_per_dollar


def test_a_rate_that_is_not_pinned_is_unknown_and_a_day_holding_one_does_not_settle() -> None:
    """2026-01-15: Alpaca's ORF, OCC and CAT figures have no date this early."""
    fees = _fees(OrderSide.SELL, on=date(2026, 1, 15))
    assert (fees.orf, fees.occ, fees.cat) == (None, None, None)
    assert fees.unpinned == ("cat", "orf", "occ")
    assert fees.taf == Decimal("0.00658")
    with pytest.raises(OptionRateNotPinnedError) as refused:
        settle_option_session([fees])
    assert refused.value.components == ("cat", "orf", "occ")
    with pytest.raises(OptionRateNotPinnedError):
        option_fee_provision_cents([fees])


def test_each_fee_type_is_summed_over_the_day_and_then_rounded_up_to_the_cent() -> None:
    """Three sales of 1 contract at $2.20. Each owes $0.004532 of SEC fee: $0.013596 on the day, charged $0.02.

    Rounded sale by sale it would be three cents.
    """
    sale = _fees(OrderSide.SELL, contracts="1", premium="2.20")
    settled = settle_option_session([sale, sale, sale])
    assert settled.sec == Decimal("0.02")
    assert (settled.taf, settled.cat, settled.orf, settled.occ) == (
        Decimal("0.00"), Decimal("0.01"), Decimal("0.05"), Decimal("0.08"),
    )
    assert settled.total == Decimal("0.16")


def test_a_day_with_no_option_fill_owes_nothing() -> None:
    assert settle_option_session([]).total == Decimal(0)
    assert option_fee_provision_cents([]) == 0


def test_an_entrys_provision_is_its_own_fills_settled_alone_in_whole_cents() -> None:
    """Buy 2 at $1.50: CAT $0.0006 → 1¢, ORF 3¢, OCC 5¢: 9 cents."""
    assert option_fee_provision_cents([_fees(OrderSide.BUY, premium="1.50")]) == 9
