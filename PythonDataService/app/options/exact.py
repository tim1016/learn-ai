"""Exact conversion of a decimal amount to a whole number of smaller units, or a refusal.

Formula: units = amount · units_per_one, refused unless it is an integer;
  sum = Σ mᵢ·aᵢ over whole numbers mᵢ and decimals aᵢ, with no digit dropped.
Reference: the General Decimal Arithmetic Specification as Python's ``decimal``
  module implements it: ``Decimal.as_integer_ratio`` is exact, and a context
  that traps ``Inexact`` raises in place of any rounding.
Canonical implementation: this file.
Validated against: tests/options/test_exact.py, and through its callers in
  tests/options/test_contract.py and tests/options/test_requirement.py.

Strikes are whole thousandths of a dollar and money is whole cents. A value
that is not a whole number of its unit is refused, never rounded, and the
answer does not depend on the ambient decimal context.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, Inexact, InvalidOperation, Overflow, localcontext

# How large and how fine a number is read at all. The exact ratio below writes out
# every digit an exponent implies, so "1e10000000" would otherwise take seconds.
_MAX_DIGITS_ABOVE_THE_POINT = 12
_MAX_DECIMALS = 40
# The most digits such a number writes.
_EVERYDAY_DIGITS = _MAX_DIGITS_ABOVE_THE_POINT + 1 + _MAX_DECIMALS


def require_everyday(amount: object, *, what: str) -> Decimal:
    """``amount`` if it is a finite decimal of a readable size; refuses anything else as a ``ValueError``."""
    if not isinstance(amount, Decimal):
        raise ValueError(f"{what} must be an exact decimal; got {amount!r}")
    if not amount.is_finite():
        raise ValueError(f"{what} must be a finite number; got {amount}")
    if amount.adjusted() > _MAX_DIGITS_ABOVE_THE_POINT or amount.as_tuple().exponent < -_MAX_DECIMALS:
        raise ValueError(f"{what} must be an everyday number; got {amount}")
    return amount


def whole_units(amount: Decimal, *, units_per_one: int, what: str, unit: str) -> int:
    """``amount`` as a whole number of the ``unit`` of which ``units_per_one`` make one; refuses any other amount.

    ``what`` and ``unit`` word the refusal: "a strike must be a whole number of
    thousandths of a dollar".
    """
    numerator, denominator = require_everyday(amount, what=what).as_integer_ratio()
    units, remainder = divmod(numerator * units_per_one, denominator)
    if remainder:
        raise ValueError(f"{what} must be a whole number of {unit}; got {amount}")
    return units


def exact_sum(terms: Iterable[tuple[int, Decimal]]) -> Decimal:
    """The sum of whole-number multiples of everyday decimals, exact whatever the ambient decimal context.

    Exact however large a multiple is: the working precision is sized from the
    terms, and anything it could not hold raises in place of rounding.
    """
    counted = [(multiple, require_everyday(amount, what="a term of an exact sum")) for multiple, amount in terms]
    # A whole number of b bits writes no more than b/3 + 1 digits; each term adds at most one carry.
    digits = _EVERYDAY_DIGITS + max((multiple.bit_length() // 3 + 1 for multiple, _ in counted), default=0) + len(counted)
    exact = Context(prec=digits, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[Inexact, InvalidOperation, Overflow])
    with localcontext(exact):
        return sum((multiple * amount for multiple, amount in counted), start=Decimal(0))
