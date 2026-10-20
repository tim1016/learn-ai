"""The fee a setup's policy charges for one fill (#3030).

Formula: not charged   fee = 0
    per contract  fee = r × contracts, on every fill, a buy or a sale
    estimated     fee = Σ over the five fee types of ceil_cent(accrual of this fill), the accruals being the
                  canonical option fee model's for the fill's side, contracts, price and multiplier on its New York date
  with r the policy's dollars per contract and ceil_cent a rounding up to the whole cent.
Reference: the owner's fee policy in #3030 (not charged, per contract, estimated); for the estimate, Alpaca's
  option pass-through fee schedule as ``app/broker/alpaca/option_fees.py`` cites it.
Canonical implementation: ``app/broker/alpaca/option_fees.py`` for every rate and accrual; this file chooses a policy
  and rounds each fill. No rate is written here.
Validated against: tests/options_backtest/test_fees.py, which compares a day of fills with the model's own day
  settlement.

An accepted divergence. The model settles SEC, TAF, CAT and ORF over a whole day: each is added up over the day's fills
and the sum is rounded up to the cent once (OCC it rounds per fill, as this file does). A replay needs a fee at each
fill, so each fee type of each fill is rounded up on its own. A fill's fee is therefore never less than its share of
the day's settlement, and over a day of n fills each of those four fee types is at most n - 1 cents more than the
model's settlement (so at most 4(n - 1) cents). The estimate leans high by cents a day, and never low.

An unknown fee is never a zero. The model has no pinned rate for some fee types on early dates; a fill on such a date
raises ``FeeRateNotPublished`` naming the date and the types. Whether a date is priced, and so the first date that is,
is the model's to say: ``estimated_fees_available`` asks it, and no date is written here.

Importing this module imports ``app.broker.alpaca``, whose ``__init__`` builds the application's settings
(``app.config``), so the environment must hold the vendor key it requires (``POLYGON_API_KEY``), as the data plane's
does. It builds no broker client and opens no connection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal, Inexact, localcontext
from typing import assert_never, get_args

from app.broker.alpaca.option_fees import OptionFeeComponent, OptionFillFees, fees_for_option_fill
from app.broker.alpaca.regulatory_fees import ceil_cents
from app.broker.contract.models import OrderSide
from app.options_backtest import fills
from app.options_backtest.replay import FeeFor
from app.options_backtest.results import Fill
from app.options_backtest.setup import FeePolicy
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

_COMPONENTS: tuple[OptionFeeComponent, ...] = get_args(OptionFeeComponent)
_ZERO = Decimal(0)
# The model refuses a premium of zero. Only SEC depends on the premium (it is a share of the sale's value), so a fill
# at nothing is asked of the model at any price above zero and its SEC accrual is then set to the nothing it is.
_STAND_IN_PREMIUM = Decimal(1)
_SAMPLE_CONTRACTS = Decimal(1)
_SAMPLE_MULTIPLIER = 100


class FeeRateNotPublished(ValueError):
    """The estimated fee of a fill was asked on a date for which the fee model has no pinned rate of some fee type."""

    def __init__(self, trading_date: date, components: tuple[str, ...]) -> None:
        self.trading_date = trading_date
        self.components = components
        super().__init__(
            f"estimated fees cannot be priced on {trading_date}: the option fee model has no published rate on that date "
            f"for {', '.join(components)}. Charge a fee per contract or none for that range, or start the run later."
        )

    def __reduce__(self) -> tuple[type[FeeRateNotPublished], tuple[date, tuple[str, ...]]]:
        # Without this an exception that crosses a process boundary is rebuilt from its message alone and fails.
        return (type(self), (self.trading_date, self.components))


def unpublished_fee_types(trading_date: date) -> tuple[str, ...]:
    """The fee types the model has no pinned rate for on ``trading_date``, for a buy or a sale; empty when it prices both.

    Asked of the model with a sample fill on each side, so the answer is the one a real fill would get.
    """
    at = et_midnight_ms(trading_date)
    missing: set[str] = set()
    for side in (OrderSide.BUY, OrderSide.SELL):
        sample = fees_for_option_fill(
            trade_date_ms=at,
            side=side,
            contracts=_SAMPLE_CONTRACTS,
            premium=_STAND_IN_PREMIUM,
            multiplier=_SAMPLE_MULTIPLIER,
        )
        missing.update(sample.unpinned)
    return tuple(name for name in _COMPONENTS if name in missing)


def estimated_fees_available(trading_date: date) -> bool:
    """Whether the model prices both a buy and a sale on ``trading_date``."""
    return not unpublished_fee_types(trading_date)


def _estimated_fee(fill: Fill) -> Decimal:
    side = OrderSide.BUY if fill.side == "buy" else OrderSide.SELL
    at_nothing = fill.filled == 0
    accruals: OptionFillFees = fees_for_option_fill(
        trade_date_ms=fill.bar_close_ms,
        side=side,
        contracts=Decimal(fill.contracts),
        premium=_STAND_IN_PREMIUM if at_nothing else fill.filled,
        multiplier=fill.multiplier,
    )
    if at_nothing and accruals.sec is not None:
        accruals = replace(accruals, sec=_ZERO)
    if accruals.unpinned:
        raise FeeRateNotPublished(et_date_at_ms(fill.bar_close_ms), accruals.unpinned)
    with localcontext(fills.EXACT) as rounding:
        # Rounding up to the cent is the one step here that drops digits.
        rounding.traps[Inexact] = False
        return sum((ceil_cents(getattr(accruals, name)) for name in _COMPONENTS), start=_ZERO)


def _not_charged(_fill: Fill) -> Decimal:
    return _ZERO


def _per_contract(dollars: Decimal) -> Callable[[Fill], Decimal]:
    def fee(fill: Fill) -> Decimal:
        with localcontext(fills.EXACT):
            return dollars * fill.contracts

    return fee


def fee_for_policy(policy: FeePolicy) -> FeeFor:
    """The replay's ``fee_for`` for a setup's fee policy: one fill in, dollars out."""
    match policy.kind:
        case "not_charged":
            return _not_charged
        case "per_contract":
            assert policy.per_contract_usd is not None  # the policy's own validator guarantees it
            return _per_contract(policy.per_contract_usd)
        case "estimated":
            return _estimated_fee
        case _:
            assert_never(policy.kind)
