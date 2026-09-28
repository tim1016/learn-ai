"""Exact money boundaries from PRD #2540, independent integer/fraction receipts."""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.broker.alpaca.clerk.money import (
    MoneyInputError,
    cash_admits,
    cents_required,
    cents_spendable,
    consent_cents,
    money_context,
    normalize_money,
    notional,
)


def test_fractional_share_cost_preserves_every_digit_until_actionable_boundary() -> None:
    expected = Fraction(125, 1000) * Fraction(1001, 100)
    amount = notional(0.125, 10.01)
    assert Fraction(amount) == expected
    assert cents_required(amount) == 126
    assert cents_spendable(amount) == 125


def test_binary_float_inputs_are_normalized_before_multiplication() -> None:
    assert notional(0.1, 0.2) == Decimal("0.02")
    assert normalize_money(0.1) == Decimal("0.1")


def test_even_a_subcent_shortage_refuses_without_epsilon() -> None:
    assert not cash_admits(cash=100, claims=0, required="100.0000000001")
    assert cash_admits(cash="1.25125", claims=0, required=notional("0.125", "10.01"))


@pytest.mark.parametrize("amount", ["0.001", "12.345", True, "NaN", "Infinity", -1, 0])
def test_consent_requires_positive_whole_cents(amount: object) -> None:
    with pytest.raises(MoneyInputError):
        consent_cents(amount)


def test_consent_is_an_exact_cent_integer() -> None:
    assert consent_cents("100.00") == 10000
    assert consent_cents("0.01") == 1


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), True, "1e-325", "1e309"])
def test_unknown_or_unsupported_recorded_money_is_never_zero(amount: object) -> None:
    with pytest.raises(MoneyInputError):
        normalize_money(amount)


def test_supported_magnitude_and_scale_survive_small_ambient_context() -> None:
    left = "999999999999999999.999999999999999999"
    right = "0.000000000000000001"
    with localcontext() as ambient:
        ambient.prec = 6
        product = notional(left, right)
        with money_context():
            total = sum((product for _ in range(10_000)), Decimal(0))
    assert Fraction(total) == Fraction(left) * Fraction(right) * 10_000


def test_negative_claims_cannot_manufacture_available_cash() -> None:
    with pytest.raises(MoneyInputError):
        cash_admits(cash=100, claims=-1, required=101)


def test_extreme_finite_float_inputs_are_normalized_without_precision_loss() -> None:
    smallest = float.fromhex("0x0.0000000000001p-1022")
    largest = float.fromhex("0x1.fffffffffffffp+1023")
    with money_context():
        actual = notional(smallest, smallest) + notional(largest, largest)
    assert Fraction(actual) == Fraction(str(smallest)) ** 2 + Fraction(str(largest)) ** 2


@pytest.mark.parametrize("value", ["1e1000000", "1e-1000000", "1." + "1" * 1500])
def test_extreme_wire_exponents_are_named_refusals(value: str) -> None:
    with pytest.raises(MoneyInputError, match="supported"):
        normalize_money(value)


def test_dollars_renders_signed_cents_with_two_decimals() -> None:
    from app.broker.alpaca.clerk.money import dollars

    assert dollars(0) == "0.00"
    assert dollars(5) == "0.05"
    assert dollars(125) == "1.25"
    assert dollars(-125) == "-1.25"
