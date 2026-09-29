"""Exact money boundaries from PRD #2540, independent integer/fraction receipts."""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.broker.alpaca.clerk.money import (
    MoneyInputError,
    apportion_units,
    cash_admits,
    cents_required,
    cents_spendable,
    consent_cents,
    display_cents,
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


def test_apportion_units_is_largest_remainder_with_key_ties_and_loses_no_unit() -> None:
    assert apportion_units(10, {"b": Decimal(1), "a": Decimal(1), "c": Decimal(1)}) == {"a": 4, "b": 3, "c": 3}
    assert apportion_units(0, {"a": Decimal(0)}) == {"a": 0}
    weights = {"x": Decimal("0.125"), "y": Decimal("10.01"), "z": Decimal("3")}
    shares = apportion_units(10_000, weights)
    assert sum(shares.values()) == 10_000
    total = sum(Fraction(value) for value in weights.values())
    for key, value in weights.items():
        assert abs(Fraction(shares[key]) - 10_000 * Fraction(value) / total) < 1


@pytest.mark.parametrize(("units", "weights"), [(1, {"a": Decimal(0)}), (-1, {"a": Decimal(1)}), (1, {"a": Decimal(-1)})])
def test_apportion_units_refuses_to_invent_or_lose_units(units: int, weights: dict[str, Decimal]) -> None:
    with pytest.raises(ValueError):
        apportion_units(units, weights)


@pytest.mark.parametrize(("amount", "cents"), [
    ("0.005", 0), ("0.015", 2), ("1.25125", 125), ("764.715", 76472), ("-3.205", -320), ("-0.004", 0),
])
def test_display_cents_rounds_half_even_for_shown_figures(amount: str, cents: int) -> None:
    assert display_cents(Decimal(amount)) == cents


@pytest.mark.parametrize("amount", [0.015, 1, None])
def test_display_cents_refuses_anything_but_an_exact_decimal(amount: object) -> None:
    """#2556: a float view of an exact value can sit across a half cent, so no
    displayed cent is rounded from one; a recorded float is normalized first."""
    with pytest.raises(TypeError, match="exact Decimal"):
        display_cents(amount)  # type: ignore[arg-type]
