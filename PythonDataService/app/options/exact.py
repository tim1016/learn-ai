"""Exact conversion of a decimal amount to a whole number of smaller units, or a refusal.

Strikes are whole thousandths of a dollar and money is whole cents. A value
that is not a whole number of its unit is refused, never rounded, and the
answer does not depend on the ambient decimal context.
"""

from __future__ import annotations

from decimal import Decimal

# How large and how fine a number is read at all. The exact ratio below writes out
# every digit an exponent implies, so "1e10000000" would otherwise take seconds.
_MAX_DIGITS_ABOVE_THE_POINT = 12
_MAX_DECIMALS = 40


def whole_units(amount: Decimal, *, units_per_one: int, what: str, unit: str) -> int:
    """``amount`` as a whole number of the ``unit`` of which ``units_per_one`` make one; refuses any other amount.

    ``what`` and ``unit`` word the refusal: "a strike must be a whole number of
    thousandths of a dollar".
    """
    if not amount.is_finite():
        raise ValueError(f"{what} must be a finite number; got {amount}")
    if amount.adjusted() > _MAX_DIGITS_ABOVE_THE_POINT or amount.as_tuple().exponent < -_MAX_DECIMALS:
        raise ValueError(f"{what} must be an everyday number; got {amount}")
    numerator, denominator = amount.as_integer_ratio()
    units, remainder = divmod(numerator * units_per_one, denominator)
    if remainder:
        raise ValueError(f"{what} must be a whole number of {unit}; got {amount}")
    return units
