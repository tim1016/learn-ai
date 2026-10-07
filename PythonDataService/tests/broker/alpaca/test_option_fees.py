"""The option fee prediction: rates by date, sides, the multiplier, unpinned rates, settlement."""

from __future__ import annotations

from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence
from app.broker.alpaca.clerk.sqlite.option_admission import work_out_option_entry
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.broker.alpaca.option_fees import (
    OptionRateNotPinnedError,
    entry_fee_provision_cents,
    fees_for_option_fill,
    option_fee_provision_cents,
    option_rates_for,
    settle_option_session,
)
from app.broker.alpaca.regulatory_fees import RateNotPinnedError, fees_for_fill, rates_for
from app.broker.contract.models import OrderSide
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.requirement import EntrySize, LegQuote
from app.options.structure import ResolvedLeg, ResolvedStructure
from app.schemas.action_plan import ActionPlan

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


@pytest.mark.parametrize(
    ("contracts", "premium"),
    [
        ("Infinity", "1"),
        ("-Infinity", "1"),
        ("1", "Infinity"),
        ("NaN", "1"),
        ("1", "NaN"),
        ("sNaN", "1"),
        ("1E+999999", "1"),  # finite, and past anything the arithmetic can hold
        ("1", "1E-400"),
    ],
)
def test_a_fill_that_is_no_finite_number_of_a_supported_size_is_refused_as_any_other_bad_fill_is(
    contracts: str, premium: str
) -> None:
    """An infinity is above zero and "whole", so it used to be priced: an infinite fee. A NaN cannot be compared.

    Each is refused as a bad fill, and none gets out as an arithmetic error.
    """
    for side in (OrderSide.BUY, OrderSide.SELL):
        with pytest.raises(ValueError, match="whole number of contracts"):
            _fees(side, contracts=contracts, premium=premium)


def test_a_fee_is_the_same_whatever_decimal_context_the_caller_works_in() -> None:
    """137 contracts sold at $2.10, twice in a day, worked out by a caller whose context keeps two digits.

    ORF is 137 × 0.015 = $2.055 a sale; two digits would make it 2.1. The day:
    SEC 2 × 0.592662 → $1.19, CAT 2 × 0.0411 → $0.09, ORF $4.11, OCC 2 × 3.425 = $6.85, the TAF
    paused: $12.24, which two digits would make 12.
    """

    def priced() -> tuple[object, ...]:
        sale = _fees(OrderSide.SELL, contracts="137", premium="2.10")
        day = settle_option_session([sale, sale])
        return sale, day, day.total, option_fee_provision_cents([sale, sale])

    with localcontext(Context(prec=2)):
        in_two_digits = priced()

    assert in_two_digits == priced()
    sale, day, total, cents = in_two_digits
    assert (sale.sec, sale.cat, sale.orf, sale.occ) == (
        Decimal("0.592662"), Decimal("0.0411"), Decimal("2.055"), Decimal("3.425"),
    )
    assert (day.sec, day.taf, day.cat, day.orf, day.occ) == (
        Decimal("1.19"), Decimal("0.00"), Decimal("0.09"), Decimal("4.11"), Decimal("6.85"),
    )
    assert (total, cents) == (Decimal("12.24"), 1224)


def test_a_premium_with_more_digits_than_the_callers_context_keeps_still_settles_to_the_right_cent() -> None:
    """One contract sold at $500 and 1e-40 of a dollar, in the default context of 28 digits.

    At $500 exactly the SEC fee is 500 × 100 × 0.0000206 = $1.03 to the cent. The 1e-40 puts it a hair
    over, and a fee type rounds up: $1.04. Dropping the premium's digits past the 28th charges $1.03.
    With CAT 1¢, ORF 2¢ and OCC 3¢ the entry sets aside $1.10.
    """
    sale = _fees(OrderSide.SELL, contracts="1", premium="500.0000000000000000000000000000000000000001")
    assert sale.sec > Decimal("1.03")
    assert settle_option_session([sale]).sec == Decimal("1.04")
    assert option_fee_provision_cents([sale]) == 110


def test_the_taf_is_paused_for_the_last_quarter_of_2026_and_resumes() -> None:
    def taf(on: date) -> Decimal | None:
        return option_rates_for(on).taf_per_contract

    assert taf(date(2026, 9, 30)) == Decimal("0.00329")
    assert taf(date(2026, 10, 1)) == Decimal(0)
    assert taf(date(2026, 12, 31)) == Decimal(0)
    assert taf(date(2027, 1, 1)) == Decimal("0.00329")
    assert taf(date(2028, 12, 31)) == Decimal("0.00329")
    assert taf(date(2029, 1, 1)) == Decimal("0.00390")
    # SR-FINRA-2026-020 (91 FR 60427), the 2030 column.
    assert taf(date(2029, 12, 31)) == Decimal("0.00390")
    assert taf(date(2030, 1, 1)) == Decimal("0.00404")
    assert taf(date(2030, 12, 31)) == Decimal("0.00404")
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


def test_a_sale_before_the_taf_ladder_starts_owes_an_unknown_taf_not_a_free_one() -> None:
    """The per-contract TAF ladder here starts on 2026-01-01. The day before, a sale's TAF is unknown.

    Its SEC fee is a pinned zero that day (advisory 2025-2), so only the TAF
    and Alpaca's three undated figures are unknown.
    """
    last_day_without = _fees(OrderSide.SELL, on=date(2025, 12, 31))
    assert last_day_without.taf is None
    assert last_day_without.sec == Decimal(0)
    assert last_day_without.unpinned == ("taf", "cat", "orf", "occ")
    assert _fees(OrderSide.SELL, on=date(2026, 1, 1)).taf == Decimal("0.00658")
    # A purchase owes no TAF on any date, pinned or not.
    assert _fees(OrderSide.BUY, on=date(2025, 12, 31)).taf == Decimal(0)


def test_a_sale_before_the_first_pinned_sec_rate_owes_an_unknown_sec_fee_not_a_free_one() -> None:
    """The SEC table starts on 2024-05-22 (advisory 2024-2, $27.80 per million). The day before, the fee is unknown.

    On the day: 2 × 2.10 × 100 = $420.00 of premium × 0.0000278 = $0.011676.
    """
    day_before = _fees(OrderSide.SELL, on=date(2024, 5, 21))
    assert day_before.sec is None
    assert "sec" in day_before.unpinned
    assert _fees(OrderSide.SELL, on=date(2024, 5, 22)).sec == Decimal("0.011676")
    # A purchase owes no SEC fee on any date, pinned or not.
    assert _fees(OrderSide.BUY, on=date(2024, 5, 21)).sec == Decimal(0)
    with pytest.raises(OptionRateNotPinnedError) as refused:
        settle_option_session([day_before])
    assert refused.value.components == ("sec", "taf", "cat", "orf", "occ")


def test_an_unpinned_option_rate_is_refused_wherever_an_unpinned_stock_rate_is() -> None:
    """One kind of error: a reader that takes an unpinned stock rate as an unknown takes an option's the same way."""
    with pytest.raises(RateNotPinnedError) as refused:
        settle_option_session([_fees(OrderSide.BUY, on=date(2026, 1, 15))])
    assert refused.value.components == ("cat", "orf", "occ")


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


# ---- An entry's provision, from the structure it enters -------------------------------------------------

_FRIDAY = date(2026, 10, 9)
_TUESDAY = date(2026, 10, 6)


def _structure(*legs: tuple[str, str, int, int]) -> ResolvedStructure:
    """Calls expiring Friday, as (leg id, bought or sold, strike, ratio)."""
    return ResolvedStructure(
        legs=tuple(
            ResolvedLeg(
                leg_id=leg_id,
                contract=OptionContract.of(root="SPY", expiry_date=_FRIDAY, right="call", strike=strike),
                position=position,
                ratio=ratio,
            )
            for leg_id, position, strike, ratio in legs
        )
    )


_SPREAD = _structure(("bought", "long", 585, 1), ("sold", "short", 590, 1))
_SPREADS_MARKETS = {
    "bought": LegQuote(bid=Decimal("4.08"), ask=Decimal("4.12")),
    "sold": LegQuote(bid=Decimal("2.18"), ask=Decimal("2.22")),
}


def test_a_spreads_provision_is_each_leg_filled_at_the_price_it_is_entered_at() -> None:
    """One 585 call bought at its ask and one 590 sold at its bid of 2.18, on 2026-10-06. The SEC fee on the
    $218.00 sold is 0.45 of a cent, which rounds up to 1. CAT on 200 shares is 0.06 of a cent: 1. ORF is
    2 x 1.5 = 3 and OCC 2 x 2.5 = 5. The TAF is paused. Ten cents."""
    one = entry_fee_provision_cents(_SPREAD, _SPREADS_MARKETS, EntrySize(multiplier=100, units=1), trade_date=_TUESDAY)
    three = entry_fee_provision_cents(_SPREAD, _SPREADS_MARKETS, EntrySize(multiplier=100, units=3), trade_date=_TUESDAY)

    assert one == 10
    # Six contracts: SEC on the $654.00 sold is 1.35 cents, 2; CAT on 600 shares 0.18, 1; ORF 9; OCC 15.
    assert three == 27


def test_the_sold_leg_is_charged_on_its_bid_and_not_on_its_ask() -> None:
    """Sold at a bid of 4.85 the SEC fee is 0.9991 of a cent and rounds up to 1. At the ask of 4.89 it would be
    1.0073 and round up to 2."""
    sold_first = _structure(("sold", "short", 585, 1), ("bought", "long", 590, 1))
    markets = {
        "sold": LegQuote(bid=Decimal("4.85"), ask=Decimal("4.89")),
        "bought": LegQuote(bid=Decimal("1.89"), ask=Decimal("1.93")),
    }

    assert entry_fee_provision_cents(sold_first, markets, EntrySize(multiplier=100, units=1), trade_date=_TUESDAY) == 10


def test_a_provision_for_a_day_with_no_pinned_rate_is_refused_and_is_never_nothing() -> None:
    with pytest.raises(OptionRateNotPinnedError):
        entry_fee_provision_cents(_SPREAD, _SPREADS_MARKETS, EntrySize(multiplier=100, units=1), trade_date=date(2019, 1, 2))


@pytest.mark.parametrize(("units", "ask"), [(1, "1.94"), (2, "0.07"), (5, "12.40")])
def test_the_account_clerk_sets_aside_the_same_provision_for_the_one_bought_option_it_takes(units: int, ask: str) -> None:
    """The Clerk works its one-leg entry's fees out in its own lines. They are this rule, for one bought leg."""
    call = OptionContract.of(root="SPY", expiry_date=_FRIDAY, right="call", strike=585)
    leg = {
        "leg_id": "long_call", "instrument": {"kind": "option", "underlying": "SPY"}, "position": "long",
        "qty_ratio": 1, "right": "call", "strike": {"selector": "atm"}, "expiry": {"selector": "nearest_weekly"},
    }
    plan = ActionPlan.model_validate({"on_enter": [leg], "on_exit": [{"kind": "close_leg", "entry_leg_id": "long_call"}]})
    # Tuesday 2026-10-06 at noon in New York.
    noon = 1_791_302_400_000
    bid = Decimal(ask) - Decimal("0.02")
    quote = HeldQuote(QuoteSide(bid, noon - 300), QuoteSide(Decimal(ask), noon - 300), QuoteDataType.LIVE, 4)
    evidence = OptionEntryEvidence(
        picked={"long_call": call}, multipliers={"long_call": 100}, quotes={"long_call": quote}, generation=4,
        rechecked_at_ms=noon - 100, leg_gap_limit_percent=Decimal(200), cost_limit_percent=Decimal(100),
    )

    admitted = work_out_option_entry(plan, evidence=evidence, units=units, now_ms=noon)

    assert isinstance(admitted, OptionEntryFacts)
    bought = _structure(("long_call", "long", 585, 1))
    assert admitted.money.fee_provision_cents == entry_fee_provision_cents(
        bought, {"long_call": LegQuote(bid=bid, ask=Decimal(ask))}, EntrySize(multiplier=100, units=units), trade_date=_TUESDAY
    )

