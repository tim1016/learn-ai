"""Alpaca option regulatory and clearing pass-through fees: the prediction (ADR 0076 decision 13, ADR 0059 D6).

Formula:
    premium value = contracts × premium × multiplier
    SEC §31 (sells only)       sec = premium value × r_sec(d)
    FINRA TAF (sells only)     taf = contracts × r_taf(d)                  (options carry no per-trade cap)
    FINRA CAT (both sides)     cat = contracts × multiplier × r_cat(d)     (one contract counts its multiplier
                                                                            in executed-equivalent shares)
    ORF (both sides)           orf = contracts × r_orf(d)
    OCC clearing (both sides)  occ = contracts × r_occ(d)
    Day settlement             each fee type summed over the account's ET trade date, then rounded UP to
                               the cent; the charge is the sum of the five rounded types
    Entry provision            the entry's own fills settled as a day of their own, in whole cents
Reference:
    Alpaca Securities LLC, "Brokerage Fee Schedule", §"Pass-Through Regulatory and Exchange Fees —
    Options" and §"How fees are charged"
    (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf, revised 2026-10-01, read
    2026-10-06; its revisions of 2026-02-12, 2026-04-09, 2026-07-01 and 2026-09-01 as the Internet
    Archive holds them). SEC §31: the SEC fee rate advisories ``regulatory_fees`` pins, and 17 CFR
    240.31, under which the sale of a listed stock or ETF option is a covered sale. FINRA TAF:
    SR-FINRA-2026-020 (91 FR 60427) for the per-contract ladder and SR-FINRA-2026-021 (SEC Release
    34-106409) for the pause from 2026-10-01 through 2026-12-31. CAT: SEC Release 34-105449
    (SR-FINRA-2026-010), a listed option counts its multiplier in executed-equivalent shares.
    Row-by-row citations are in the golden fixture's attribution.
Canonical implementation: this file, over the date lookup, cent rounding and day settlement of
    ``regulatory_fees``, which the stock model uses too.
Validated against:
    tests/broker/alpaca/test_option_fees.py;
    tests/fixtures/test_alpaca_option_fees_fixture.py (golden FEE-002).

Observed ``FEE`` activities are the truth (ADR 0059 D6); this model predicts them. Every figure it
gives is an estimate until Alpaca posts the day's fees:

- Alpaca dates its schedule by revision and states no effective dates. The ORF, OCC and CAT rows
  below are dated by the revision that first printed them. SEC and TAF are dated by the regulator.
- Alpaca's ORF is one flat figure it calls an average of what it remits to the exchanges.
- A fee type whose rate is not pinned for the trade date is ``None``, never zero, so "unknown" is
  never read as "free". A buy owes no SEC fee and no TAF on any date.
- Only standard stock and ETF options are modelled. Index options, and the commission Alpaca
  charges a customer it classes as a professional options trader, are not.

Every product, sum and conversion to cents is taken in the Clerk's exact money context
(``clerk.money.money_context``), so no figure depends on the caller's decimal context. The one
step that drops digits is the rule's own: each fee type's day total, rounded up to the cent.

Nothing on the money path calls this yet. The fee attribution
(``app/services/alpaca_fee_attribution.py``) settles fees per fee type across an account's whole
day, stock and option fills together, which is where these accruals belong once Alpaca's own fee
activities for an option trade have been seen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, Inexact
from typing import Literal

from app.broker.alpaca.clerk.money import MoneyInputError, contract_multiplier, money_context, normalize_money
from app.broker.alpaca.regulatory_fees import (
    RateNotPinnedError,
    in_force,
    settle_components,
    unpinned_components,
)
from app.broker.alpaca.regulatory_fees import rates_for as stock_rates_for
from app.broker.contract.models import OrderSide

OptionFeeComponent = Literal["sec", "taf", "cat", "orf", "occ"]

_COMPONENTS: tuple[OptionFeeComponent, ...] = ("sec", "taf", "cat", "orf", "occ")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")

# Each table is ascending by effective date; the latest row on or before the trade date is in
# force. A trade date before a table's first row has NO pinned rate for that fee type.
_TAF_PER_CONTRACT: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 1, 1), Decimal("0.00329")),  # SR-FINRA-2026-020, the 2026 column
    (date(2026, 10, 1), Decimal("0")),  # SR-FINRA-2026-021: paused through 2026-12-31
    (date(2027, 1, 1), Decimal("0.00329")),  # SR-FINRA-2026-021, Exhibit 5B: resumes, through 2028
    (date(2029, 1, 1), Decimal("0.00390")),  # SR-FINRA-2026-020
    (date(2030, 1, 1), Decimal("0.00404")),
    (date(2031, 1, 1), Decimal("0.00420")),
)
# Dated by the Alpaca schedule revision that first printed the figure.
_ORF_PER_CONTRACT: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 2, 12), Decimal("0.02375")),
    (date(2026, 4, 9), Decimal("0.02295")),
    (date(2026, 7, 1), Decimal("0.015")),
)
_OCC_PER_CONTRACT: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 2, 12), Decimal("0.025")),  # printed "(no cap)" in that revision
)
_CAT_PER_EQUIVALENT_SHARE: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 7, 1), Decimal("0.000003")),  # the first revision to list CAT under Options
)


class OptionRateNotPinnedError(RateNotPinnedError):
    """A day holds an option fill with no pinned rate for some fee type."""

    rate_kind = "option fee rate"


@dataclass(frozen=True)
class OptionRates:
    """Rates in force on one trade date; ``None`` means not pinned for that date."""

    sec_per_dollar: Decimal | None
    taf_per_contract: Decimal | None
    cat_per_equivalent_share: Decimal | None
    orf_per_contract: Decimal | None
    occ_per_contract: Decimal | None


@dataclass(frozen=True)
class OptionFillFees:
    """Unrounded accruals of one option fill; a ``None`` fee type is unpinned, not zero."""

    sec: Decimal | None
    taf: Decimal | None
    cat: Decimal | None
    orf: Decimal | None
    occ: Decimal | None

    @property
    def unpinned(self) -> tuple[OptionFeeComponent, ...]:
        return unpinned_components(self, _COMPONENTS)


@dataclass(frozen=True)
class OptionSessionFees:
    """What a day's option fills are charged: each fee type's accrual rounded up to the cent."""

    sec: Decimal
    taf: Decimal
    cat: Decimal
    orf: Decimal
    occ: Decimal

    @property
    def total(self) -> Decimal:
        with money_context():
            return self.sec + self.taf + self.cat + self.orf + self.occ


def option_rates_for(trade_date: date) -> OptionRates:
    """Resolve the five pass-through rates in force on ``trade_date``.

    The SEC rate is the one statute's, so it is the stock model's table.
    """
    return OptionRates(
        sec_per_dollar=stock_rates_for(trade_date).sec_per_dollar,
        taf_per_contract=in_force(_TAF_PER_CONTRACT, trade_date),
        cat_per_equivalent_share=in_force(_CAT_PER_EQUIVALENT_SHARE, trade_date),
        orf_per_contract=in_force(_ORF_PER_CONTRACT, trade_date),
        occ_per_contract=in_force(_OCC_PER_CONTRACT, trade_date),
    )


def fees_for_option_fill(
    *,
    trade_date: date,
    side: OrderSide,
    contracts: Decimal,
    premium: Decimal,
    multiplier: int,
) -> OptionFillFees:
    """Accrue one option fill's fees at full precision; rounding happens at settlement.

    ``premium`` is dollars per share and ``multiplier`` the contract's own
    recorded multiplier; it is never assumed. A count or a premium that is
    not a finite number, or is of a size the Clerk's exact money arithmetic
    does not hold, is refused as any other bad fill is.
    """
    refused = ValueError(
        f"an option fill is a whole number of contracts at a price above zero, both finite and of a "
        f"supported size; got contracts={contracts} premium={premium}"
    )
    try:
        # First: an infinity passes every test below, and a NaN cannot be compared at all.
        normalize_money(contracts)
        normalize_money(premium)
    except MoneyInputError as exc:
        raise refused from exc
    if contracts <= 0 or premium <= 0 or contracts != contracts.to_integral_value():
        raise refused
    try:
        shares_per_contract = contract_multiplier(multiplier)
    except MoneyInputError as exc:
        raise ValueError(f"an option fill needs its contract's multiplier; got {multiplier!r}") from exc
    rates = option_rates_for(trade_date)

    def per_contract(rate: Decimal | None) -> Decimal | None:
        return None if rate is None else contracts * rate

    with money_context():
        cat = (
            None
            if rates.cat_per_equivalent_share is None
            else contracts * shares_per_contract * rates.cat_per_equivalent_share
        )
        orf, occ = per_contract(rates.orf_per_contract), per_contract(rates.occ_per_contract)
        if side != OrderSide.SELL:
            return OptionFillFees(sec=_ZERO, taf=_ZERO, cat=cat, orf=orf, occ=occ)
        sec = (
            None
            if rates.sec_per_dollar is None
            else contracts * premium * shares_per_contract * rates.sec_per_dollar
        )
        return OptionFillFees(sec=sec, taf=per_contract(rates.taf_per_contract), cat=cat, orf=orf, occ=occ)


def settle_option_session(fills: Sequence[OptionFillFees]) -> OptionSessionFees:
    """Sum each fee type over one account's ET trade date and round it UP to the cent.

    This is the charge for a day that holds only these fills. Alpaca settles a
    fee type over the account's whole day, stock fills included, so a day that
    also traded stock rounds SEC, TAF and CAT once across both.

    Raises ``OptionRateNotPinnedError`` if any fill has an unpinned fee type: a
    day containing an unknown cannot settle to a number.
    """
    with money_context() as exact:
        # Rounding up to the cent is the one step that drops digits, and it is the rule.
        exact.traps[Inexact] = False
        return OptionSessionFees(**settle_components(fills, _COMPONENTS, not_pinned=OptionRateNotPinnedError))


def option_fee_provision_cents(fills: Sequence[OptionFillFees]) -> int:
    """What an entry sets aside for its own fees, in whole cents: its fills settled as a day of their own.

    Each fee type is rounded up for this entry alone. The day's charge rounds
    once across every fill of the account, so the entry's share of it can
    differ from this by up to a cent per fee type, either way. It is the one
    figure of an entry's money that rounds (ADR 0076 decision 2).
    """
    with money_context():
        return int(settle_option_session(fills).total / _CENT)
