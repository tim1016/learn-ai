"""Marketable-limit anchor for program legs outside the regular session (ADR 0059 D5.3).

Formula:
    buy:  ceil_tick( close × (1 + allowance_bps / 10⁴) )
    sell: floor_tick( close × (1 − allowance_bps / 10⁴) )
    where the tick is 0.01 for a price ≥ 1 and 0.0001 below 1 (Alpaca's
    limit-price precision rule, mirrored by ``BrokerOrderLeg``'s validator).
    Rounding is always in the marketable direction, so the anchor never
    understates the allowance the operator set.
Reference:
    ADR 0059 Decision 5.3; CONTEXT.md "Marketable limit anchor"; Alpaca
    "Orders at Alpaca" § Extended Hours Trading (limit-only) — see
    docs/references/alpaca-extended-hours.md.
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/test_marketable_limit.py::test_marketable_limit_price
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import ValidationError

from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.contract.models import OrderSide

logger = logging.getLogger(__name__)

_BPS_PER_UNIT = Decimal(10_000)
_DOLLAR_TICK = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")


def marketable_limit_price(*, side: OrderSide, close: Decimal, allowance_bps: Decimal) -> Decimal:
    """The limit price a program leg carries outside the regular session."""
    if close <= 0:
        raise ValueError(f"close must be positive; got {close}")
    if allowance_bps < 0:
        raise ValueError(f"allowance_bps must be non-negative; got {allowance_bps}")
    fraction = allowance_bps / _BPS_PER_UNIT
    if side is OrderSide.BUY:
        raw = close * (1 + fraction)
        rounding = ROUND_CEILING
    else:
        raw = close * (1 - fraction)
        rounding = ROUND_FLOOR
    tick = _DOLLAR_TICK if raw >= 1 else _SUB_DOLLAR_TICK
    return raw.quantize(tick, rounding=rounding)


@dataclass(frozen=True)
class ExtendedHoursAllowances:
    """The operator's extended-session allowances, in basis points (ADR 0059 D4).

    Both values come from the environment file; neither has a default in
    code. ``None`` from the constructors means "not configured", which the
    leg policy and Start admission refuse explicitly — never zero.
    """

    entry_bps: Decimal
    exit_bps: Decimal

    @classmethod
    def from_settings(cls, settings: AlpacaSettings) -> ExtendedHoursAllowances | None:
        if settings.live_xh_entry_bps is None or settings.live_xh_exit_bps is None:
            return None
        return cls(
            entry_bps=Decimal(str(settings.live_xh_entry_bps)),
            exit_bps=Decimal(str(settings.live_xh_exit_bps)),
        )

    @classmethod
    def from_environment(cls) -> ExtendedHoursAllowances | None:
        """Read the process settings; absent credentials mean absent allowances.

        A synthetic (``sim:``) authority can be built in a process that has
        no Alpaca credentials at all; that is not an error, it is "no
        allowances", and it is logged so an operator can see why an
        extended-hours leg was refused.
        """
        try:
            settings = get_alpaca_settings()
        except ValidationError as exc:
            logger.info(
                "Extended-hours allowances are unavailable: Alpaca settings did not load",
                extra={"action": "extended_hours_allowances_unavailable", "error": str(exc)},
            )
            return None
        return cls.from_settings(settings)
