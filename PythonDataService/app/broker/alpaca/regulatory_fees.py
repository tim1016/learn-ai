"""Alpaca equity regulatory pass-through fees (ADR 0059 D6).

Formula:
    value = quantity × fill_price
    SEC §31 (sells only)      sec = value × r_sec(d)
    FINRA TAF (sells only)    taf = min(quantity × r_taf(d), cap_taf(d))
    FINRA CAT (both sides)    cat = quantity × r_cat(d)   (NMS equity: 1 share = 1 EES)
    Session settlement        each component summed over the ET trade date and
                              rounded UP to the cent; total = sec + taf + cat
Reference:
    Alpaca Securities LLC, "Broker Fee Schedule", §"Pass-Through Regulatory and
    Exchange Fees — Equities"
    (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf, retrieved
    2026-09-07); SEC Fee Rate Advisories 2024-2, 2025-2, 2026-2
    (https://www.sec.gov/rules-regulations/fee-rate-advisories/<year>-2); FINRA
    SR-FINRA-2024-019 fee-adjustment schedule
    (https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule).
    Row-by-row citations: docs/references/alpaca-regulatory-fees.md.
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/test_regulatory_fees.py;
    tests/fixtures/test_alpaca_regulatory_fees_fixture.py (golden FEE-001).

Observed FEE activities are the truth (ADR 0059 D6); this model predicts them
and prices fills for shadow and backtest parity. A component whose rate is not
pinned for the trade date is ``None`` — never zero — so "unknown" can never be
read as "free". Buys owe no SEC or TAF on any date, so those are ``0`` for a
buy regardless of pinning. Only listed (NMS) equities are modelled: one share
is one CAT executed-equivalent share.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Literal, NamedTuple

from app.broker.contract.models import OrderSide

FeeComponent = Literal["sec", "taf", "cat"]

_COMPONENTS: tuple[FeeComponent, ...] = ("sec", "taf", "cat")
_CENT = Decimal("0.01")
_ZERO = Decimal("0")


class TafRate(NamedTuple):
    """FINRA TAF's two coupled numbers: they take effect on the same date."""

    per_share: Decimal
    cap_per_trade: Decimal


# Each table is ascending by effective date; the latest row on or before the
# trade date is in force. A trade date before a table's first row has NO pinned
# rate for that component. Row sources: docs/references/alpaca-regulatory-fees.md.
_SEC_PER_DOLLAR: tuple[tuple[date, Decimal], ...] = (
    (date(2024, 5, 22), Decimal("0.0000278")),  # $27.80 per $1M — SEC advisory 2024-2
    (date(2025, 5, 14), Decimal("0")),  # $0.00 per $1M — SEC advisory 2025-2
    (date(2026, 4, 4), Decimal("0.0000206")),  # $20.60 per $1M — SEC advisory 2026-2
)
_TAF_PER_SHARE_AND_CAP: tuple[tuple[date, TafRate], ...] = (
    (date(2024, 1, 1), TafRate(Decimal("0.000166"), Decimal("8.30"))),
    (date(2026, 1, 1), TafRate(Decimal("0.000195"), Decimal("9.79"))),
    (date(2027, 1, 1), TafRate(Decimal("0.000232"), Decimal("11.61"))),
    (date(2028, 1, 1), TafRate(Decimal("0.000240"), Decimal("12.05"))),
    (date(2029, 1, 1), TafRate(Decimal("0.000249"), Decimal("12.50"))),
)
_CAT_PER_SHARE: tuple[tuple[date, Decimal], ...] = (
    (date(2026, 9, 1), Decimal("0.000003")),  # Alpaca fee schedule, retrieved 2026-09-07
)


class RateNotPinnedError(ValueError):
    """A session holds a fill with no pinned rate for some component."""

    def __init__(self, components: Sequence[FeeComponent]) -> None:
        self.components: tuple[FeeComponent, ...] = tuple(components)
        super().__init__(f"no pinned regulatory rate for: {', '.join(self.components)}")


@dataclass(frozen=True)
class RegulatoryRates:
    """Rates in force on one trade date; ``None`` means not pinned for that date."""

    sec_per_dollar: Decimal | None
    taf_per_share: Decimal | None
    taf_cap_per_trade: Decimal | None
    cat_per_share: Decimal | None


@dataclass(frozen=True)
class FillFees:
    """Unrounded per-fill accruals; a ``None`` component is unpinned, not zero."""

    sec: Decimal | None
    taf: Decimal | None
    cat: Decimal | None

    @property
    def unpinned(self) -> tuple[FeeComponent, ...]:
        return tuple(name for name in _COMPONENTS if getattr(self, name) is None)


@dataclass(frozen=True)
class SessionFees:
    """What Alpaca charges at end of day: each component's accrual rounded up to the cent."""

    sec: Decimal
    taf: Decimal
    cat: Decimal

    @property
    def total(self) -> Decimal:
        return self.sec + self.taf + self.cat


def _in_force[R](table: Sequence[tuple[date, R]], trade_date: date) -> R | None:
    """Value of the latest row effective on or before ``trade_date``, else ``None``."""
    latest: R | None = None
    for effective_from, value in table:
        if effective_from <= trade_date:
            latest = value
    return latest


def rates_for(trade_date: date) -> RegulatoryRates:
    """Resolve the three pass-through rates in force on ``trade_date``."""
    taf = _in_force(_TAF_PER_SHARE_AND_CAP, trade_date)
    return RegulatoryRates(
        sec_per_dollar=_in_force(_SEC_PER_DOLLAR, trade_date),
        taf_per_share=None if taf is None else taf.per_share,
        taf_cap_per_trade=None if taf is None else taf.cap_per_trade,
        cat_per_share=_in_force(_CAT_PER_SHARE, trade_date),
    )


def fees_for_fill(
    *,
    trade_date: date,
    side: OrderSide,
    quantity: Decimal,
    fill_price: Decimal,
) -> FillFees:
    """Accrue one fill's fees at full precision; rounding happens at settlement."""
    if quantity <= 0 or fill_price <= 0:
        raise ValueError(
            f"fill quantity and price must be positive; got quantity={quantity} fill_price={fill_price}"
        )
    rates = rates_for(trade_date)
    cat = None if rates.cat_per_share is None else quantity * rates.cat_per_share
    if side != OrderSide.SELL:
        return FillFees(sec=_ZERO, taf=_ZERO, cat=cat)
    sec = None if rates.sec_per_dollar is None else quantity * fill_price * rates.sec_per_dollar
    taf = None
    if rates.taf_per_share is not None and rates.taf_cap_per_trade is not None:
        taf = min(quantity * rates.taf_per_share, rates.taf_cap_per_trade)
    return FillFees(sec=sec, taf=taf, cat=cat)


def _ceil_cents(amount: Decimal) -> Decimal:
    return amount.quantize(_CENT, rounding=ROUND_CEILING)


def settle_session(fills: Sequence[FillFees]) -> SessionFees:
    """Sum each component over one ET trade date and round it UP to the cent.

    Raises ``RateNotPinnedError`` if any fill has an unpinned component: a
    session containing an unknown cannot settle to a number.
    """
    unpinned = tuple(
        name for name in _COMPONENTS if any(name in fill.unpinned for fill in fills)
    )
    if unpinned:
        raise RateNotPinnedError(unpinned)
    return SessionFees(
        sec=_ceil_cents(sum((fill.sec for fill in fills), _ZERO)),
        taf=_ceil_cents(sum((fill.taf for fill in fills), _ZERO)),
        cat=_ceil_cents(sum((fill.cat for fill in fills), _ZERO)),
    )
