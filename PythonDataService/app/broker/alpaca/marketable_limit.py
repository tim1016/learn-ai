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
    tests/broker/alpaca/test_marketable_limit.py::test_marketable_limit_price,
    tests/broker/alpaca/test_marketable_limit.py::test_every_anchor_across_the_dollar_band_is_a_valid_leg_limit_price

**Why the tick rule exists twice** (CLAUDE.md guiding philosophy #5 permits a
duplicate only for a real reason, with a parity test naming the canonical
file). ``BrokerOrderLeg._limit_price_matches_order_type`` enforces Alpaca's
precision rule on the *final* price, at the contract boundary, where it guards
every leg from every producer. This module needs the same rule *before*
quantising, to pick the tick the anchor rounds to — the band is chosen by the
pre-quantisation ``raw``, which is what makes a sub-dollar close crossing $1
round up to $1.01 rather than to a sub-penny tick the vendor would reject. The
parity test above sweeps the $1 boundary and asserts every anchor this function
returns validates as a ``BrokerOrderLeg.limit_price``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from app.broker.contract.models import OrderSide

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
    from app.broker.alpaca.config import AlpacaSettings

_BPS_PER_UNIT = Decimal(10_000)
_DOLLAR_TICK = Decimal("0.01")
_SUB_DOLLAR_TICK = Decimal("0.0001")


def marketable_limit_price(*, side: OrderSide, close: Decimal, allowance_bps: Decimal) -> Decimal:
    """The limit price a program leg carries outside the regular session.

    Raises ``ValueError`` when the quantised result is not positive — a sell
    allowance at or past 10 000 bps, or a sub-penny close floored to zero.
    ``BrokerOrderLeg`` would reject such a price too, but as a pydantic
    ``ValidationError`` raised from inside ``LegShape.apply``, outside every
    ``except ProgramLegRefused`` its callers hold; ``shape_program_leg`` maps
    this to a typed ``EXTENDED_ANCHOR_UNPRICEABLE`` refusal instead.
    """
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
    price = raw.quantize(tick, rounding=rounding)
    if price <= 0:
        raise ValueError(
            f"a {side.value} anchored at close {close} with {allowance_bps} bps quantises "
            f"to {price}, which is not a submittable limit price"
        )
    return price


@dataclass(frozen=True)
class ExtendedHoursAllowances:
    """The operator's extended-session allowances, in basis points (ADR 0059 D4).

    Neither value has a default in code. ``None`` from :meth:`from_settings`
    means "not configured", which the leg policy and Start admission refuse
    explicitly — never zero.

    Two constructors, both pure, because there are two documents the same six
    numbers can arrive in and *which one* an extended-session leg prices from
    is a decision this type does not make: ``program_leg.resolve_extended_hours_allowances``
    owns the order. This module only converts.
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
    def from_envelope(cls, envelope: LiveEnvelopeValues) -> ExtendedHoursAllowances:
        """The two allowances a sealed or effective live envelope carries.

        Never ``None``: a ``LiveEnvelopeValues`` cannot exist without all six
        values, so unlike the settings constructor there is no "half
        configured" case to answer for.

        The conversion is ``Decimal(str(...))``, exactly as ``from_settings``
        does it, so a sealed envelope and an environment-configured one at the
        same numbers produce the same anchor. This is adapter-level only —
        nothing here re-derives ``LiveEnvelopeValues.sha`` or a record's digest.
        """
        return cls(
            entry_bps=Decimal(str(envelope.xh_entry_bps)),
            exit_bps=Decimal(str(envelope.xh_exit_bps)),
        )
