"""The exact decimal helpers the option contract and its money share."""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.options.exact import exact_sum

_FORTY_DECIMALS = Decimal("123456789012.1234567890123456789012345678901234567891")


def test_a_sum_keeps_every_digit_however_large_a_multiple_is() -> None:
    """A hundred-digit multiple of a forty-decimal amount has more digits than any fixed precision allows for."""
    multiple = 10**100 + 7

    with localcontext() as three_digits:
        three_digits.prec = 3
        total = exact_sum([(multiple, _FORTY_DECIMALS), (-1, Decimal("0.5"))])

    assert Fraction(total) == multiple * Fraction(_FORTY_DECIMALS) - Fraction(1, 2)


@pytest.mark.parametrize(
    "amount",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("1E+5000000"), Decimal("1E-41"), 0.5],
    ids=["no number", "infinite", "too large to write out", "finer than forty decimals", "a float"],
)
def test_a_sum_refuses_a_term_it_could_not_be_exact_about(amount: object) -> None:
    with pytest.raises(ValueError, match="a term of an exact sum"):
        exact_sum([(1, Decimal(1)), (2, amount)])  # type: ignore[list-item]
