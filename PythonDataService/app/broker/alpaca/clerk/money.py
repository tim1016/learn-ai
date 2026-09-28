"""The single normalization and arithmetic boundary for custody cash admission.

Formula: exact decimal products/sums; required cents = ceil(100*x),
spendable cents = floor(100*x); consent never rounds.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2540, Money contract.
Canonical implementation: this module; historical REAL values enter as str.
Validated against: tests/broker/alpaca/clerk/test_money.py (integer/Fraction oracle).

Supported input magnitude is < 10**309 and scale <= 324 decimal places.
The 1400-digit context preserves products of two such inputs and accumulated
cash across the supported SQLite row population, independently of ambient
Decimal configuration. Inexact arithmetic traps instead of silently changing
an admission answer. This cannot recover precision already lost in old floats.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)

_MONEY_CONTEXT = Context(prec=1400, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
ZERO = Decimal(0)
CENT = Decimal("0.01")


class MoneyInputError(ValueError):
    """An input cannot support a known, exact cash-admission decision."""


@contextmanager
def money_context() -> Iterator[Context]:
    """Use exact supported money arithmetic without changing caller context."""
    with localcontext(_MONEY_CONTEXT) as context:
        yield context


def normalize_money(value: object) -> Decimal:
    """Normalize one recorded input, never a previously multiplied float."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MoneyInputError("Money evidence must be a finite decimal number.")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise MoneyInputError("Money evidence is not a decimal number.") from exc
    if not amount.is_finite():
        raise MoneyInputError("Money evidence is not finite.")
    # Reject unsupported exponents before any context arithmetic. Otherwise
    # a short wire value such as 1e1000000 raises Decimal.Overflow instead of
    # the named input refusal. Insignificant trailing zeros do not add scale.
    if amount.is_zero():
        return ZERO
    parts = amount.as_tuple()
    significant = len(parts.digits)
    while significant and parts.digits[significant - 1] == 0:
        significant -= 1
    scale = parts.exponent + len(parts.digits) - significant
    if amount.adjusted() >= 309 or scale < -324:
        raise MoneyInputError("Money evidence exceeds the supported magnitude or decimal scale.")
    with money_context():
        return amount.normalize()


def notional(quantity: object, price: object) -> Decimal:
    """Multiply normalized factors once, before any cents display rounding."""
    with money_context():
        return normalize_money(quantity) * normalize_money(price)


def cents_required(amount: Decimal) -> int:
    """Round a derived requirement upward only at the actionable boundary."""
    with money_context():
        return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))


def cents_spendable(amount: Decimal) -> int:
    """Round derived available cash downward only at the actionable boundary."""
    with money_context():
        return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))


def consent_cents(value: object) -> int:
    """Reject fractional cents rather than silently changing user consent."""
    with money_context():
        amount = normalize_money(value)
        scaled = amount * 100
        if amount <= ZERO or scaled != scaled.to_integral_value():
            raise MoneyInputError("Choose a positive dollar budget in whole cents.")
        cents = int(scaled)
    # Durable cents are SQLite INTEGER, not a float/json-number consent.
    if cents > 2**63 - 1:
        raise MoneyInputError("Budget exceeds the supported whole-cent amount.")
    return cents


def cash_admits(*, cash: object, claims: object, required: object) -> bool:
    """Compare exact normalized money with no tolerance or display rounding."""
    with money_context():
        cash_value = normalize_money(cash)
        # Claims/requirements can be products with up to 648 fractional places.
        claim = claims if isinstance(claims, Decimal) else normalize_money(claims)
        needed = required if isinstance(required, Decimal) else normalize_money(required)
        if not claim.is_finite() or not needed.is_finite() or claim < ZERO or needed < ZERO:
            raise MoneyInputError("Cash claims and requirements must be finite and nonnegative.")
        return claim + needed <= cash_value
