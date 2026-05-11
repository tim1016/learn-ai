"""IBKR equity-tier commission model used by the QC reconciler.

Reference: QuantConnect docs for the Interactive Brokers brokerage model
(``self.set_brokerage_model(BrokerageName.InteractiveBrokersBrokerage,
AccountType.Margin)``), equity tier:

* Per share: $0.005
* Minimum per order: $1.00
* Maximum per order: 0.5% of trade value

The model is intentionally **not** wired into the backtest engine. Phase 3
runs the engine with ``commission_per_order=0`` and applies this model
inside the reconciler, comparing the computed fee to QC's recorded
``orderFeeAmount``. This keeps the commission policy confined to one
auditable file and avoids touching every ``LoggedTrade`` call site.

Formula:
    fee = clamp(
        max(abs(qty) * per_share, min_per_order),
        upper=abs(qty) * fill_price * max_pct_of_value,
    )

Canonical implementation: this file.
Validated against: tests/research/parity/test_ibkr_commission.py
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_PER_SHARE_DEFAULT = Decimal("0.005")
_MIN_PER_ORDER_DEFAULT = Decimal("1.00")
_MAX_PCT_OF_VALUE_DEFAULT = Decimal("0.005")  # 0.5%
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class IbkrEquityCommissionModel:
    """IBKR equity-tier commission model (per-share with floor and cap)."""

    per_share: Decimal = _PER_SHARE_DEFAULT
    min_per_order: Decimal = _MIN_PER_ORDER_DEFAULT
    max_pct_of_value: Decimal = _MAX_PCT_OF_VALUE_DEFAULT

    def fee(self, *, quantity: int, fill_price: Decimal) -> Decimal:
        """Return the commission for a single fill rounded to cents.

        ``quantity`` may be negative (sell side); the absolute value drives
        both the per-share and trade-value computations.
        """
        shares = Decimal(abs(int(quantity)))
        if shares == 0 or fill_price <= Decimal("0"):
            return Decimal("0.00")
        trade_value = shares * fill_price
        per_share_total = (shares * self.per_share).quantize(_CENT, rounding=ROUND_HALF_UP)
        floor_applied = max(self.min_per_order, per_share_total)
        cap = (trade_value * self.max_pct_of_value).quantize(_CENT, rounding=ROUND_HALF_UP)
        return min(floor_applied, cap)


__all__ = ["IbkrEquityCommissionModel"]
