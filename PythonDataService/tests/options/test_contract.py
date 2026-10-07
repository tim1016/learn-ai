from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract, strike_thousandths
from app.options.expiry import option_expiry_anchor_ms


@pytest.mark.parametrize(
    ("symbol", "root", "expiry_date", "right", "strike"),
    [
        # Alpaca's own examples: "AAPL Jan 19 2024 100 Call" on its "Options Trading"
        # page, "AAPL Jun 20 2025 100 Call" in its option-contract reference.
        ("AAPL240119C00100000", "AAPL", date(2024, 1, 19), "call", "100"),
        ("AAPL250620C00100000", "AAPL", date(2025, 6, 20), "call", "100"),
        ("SPY261218P00452500", "SPY", date(2026, 12, 18), "put", "452.5"),
        ("F270115C00002125", "F", date(2027, 1, 15), "call", "2.125"),
        ("GOOGL260116P01250000", "GOOGL", date(2026, 1, 16), "put", "1250"),
    ],
)
def test_a_contract_and_its_symbol_name_each_other(
    symbol: str, root: str, expiry_date: date, right: str, strike: str
) -> None:
    contract = OptionContract.of(root=root, expiry_date=expiry_date, right=right, strike=Decimal(strike))

    assert contract.occ_symbol == symbol
    assert OptionContract.from_occ_symbol(symbol) == contract
    assert contract.expiry_date == expiry_date
    assert contract.expiry_ms == option_expiry_anchor_ms(expiry_date)
    assert contract.strike == Decimal(strike)


def test_a_strike_a_listing_reports_as_a_float_names_the_same_contract_as_its_decimal() -> None:
    from_float = OptionContract.of(root="SPY", expiry_date=date(2026, 12, 18), right="put", strike=452.5)
    from_decimal = OptionContract.of(root="SPY", expiry_date=date(2026, 12, 18), right="put", strike=Decimal("452.50"))

    assert from_float == from_decimal
    assert from_float.occ_symbol == "SPY261218P00452500"


@pytest.mark.parametrize(
    "strike",
    [
        pytest.param(Decimal("452.5005"), id="finer than a thousandth"),
        pytest.param(0.1 + 0.2, id="float arithmetic residue"),
        pytest.param(Decimal("450.5000000000000000000000000000001"), id="too fine for the decimal context to see"),
        pytest.param(float("nan"), id="not a number"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param("four fifty", id="not numeric"),
        pytest.param(True, id="a boolean"),
    ],
)
def test_a_strike_that_is_not_a_whole_number_of_thousandths_is_refused(strike: object) -> None:
    with pytest.raises(ValueError, match="strike"):
        strike_thousandths(strike)  # type: ignore[arg-type]


@pytest.mark.parametrize("strike", ["1e10000000", "1e-10000000"])
def test_a_number_too_large_or_too_fine_to_be_a_strike_is_refused_before_it_is_written_out(strike: str) -> None:
    """Either would otherwise be expanded digit by digit, for seconds, before being refused or accepted."""
    with pytest.raises(ValueError, match="everyday number"):
        strike_thousandths(strike)


@pytest.mark.parametrize(
    ("root", "strike", "refused_field"),
    [
        pytest.param("AAPL1", "100", "root", id="an adjusted contract's root carries a digit"),
        pytest.param("TOOLONG", "100", "root", id="a root longer than six letters"),
        pytest.param("brk", "100", "root", id="a lower-case root"),
        pytest.param("BRK.B", "100", "root", id="a dotted ticker is not a root"),
        pytest.param("SPY", "0", "strike_thousandths", id="a zero strike"),
        pytest.param("SPY", "-5", "strike_thousandths", id="a negative strike"),
        pytest.param("SPY", "100000", "strike_thousandths", id="a strike the eight digits cannot hold"),
    ],
)
def test_a_contract_the_symbol_cannot_spell_is_refused(root: str, strike: str, refused_field: str) -> None:
    with pytest.raises(ValidationError) as refused:
        OptionContract.of(root=root, expiry_date=date(2026, 12, 18), right="call", strike=Decimal(strike))

    assert [error["loc"] for error in refused.value.errors()] == [(refused_field,)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("strike_thousandths", True, id="True is not a strike of a thousandth"),
        pytest.param("strike_thousandths", "450000", id="text is not a strike"),
        pytest.param("strike_thousandths", 450000.0, id="a float is not a whole number of thousandths"),
        pytest.param("expiry_ms", "1797627600000", id="text is not an instant"),
    ],
)
def test_a_contract_is_not_coerced_into_being(field: str, value: object) -> None:
    fields = {"root": "SPY", "expiry_ms": 1_797_627_600_000, "right": "call", "strike_thousandths": 450_000}

    with pytest.raises(ValidationError) as refused:
        OptionContract(**(fields | {field: value}))

    assert [error["loc"] for error in refused.value.errors()] == [(field,)]


def test_an_expiry_that_is_not_on_the_anchor_is_refused() -> None:
    midnight_utc = 1_797_552_000_000  # 2026-12-18T00:00:00Z

    with pytest.raises(ValidationError, match="is not an option expiry"):
        OptionContract(root="SPY", expiry_ms=midnight_utc, right="call", strike_thousandths=450_000)


def test_an_instant_no_calendar_holds_is_refused_as_out_of_range() -> None:
    """Refused by its size, before its date is asked for: asking would raise from the operating system."""
    with pytest.raises(ValidationError) as refused:
        OptionContract(root="SPY", expiry_ms=10**20, right="call", strike_thousandths=450_000)

    assert [error["type"] for error in refused.value.errors()] == ["less_than_equal"]


def test_an_expiry_year_the_two_digits_cannot_write_is_refused() -> None:
    with pytest.raises(ValidationError, match="two digits"):
        OptionContract.of(root="SPY", expiry_date=date(2100, 1, 15), right="call", strike=450)


_NOT_A_SYMBOL = "is not an option contract symbol"


@pytest.mark.parametrize(
    ("symbol", "reason"),
    [
        pytest.param("AAPL1240119C00100000", _NOT_A_SYMBOL, id="an adjusted root"),
        pytest.param("O:AAPL240119C00100000", _NOT_A_SYMBOL, id="a vendor prefix"),
        pytest.param("AAPL  240119C00100000", _NOT_A_SYMBOL, id="a space-padded root"),
        pytest.param("AAPL240119X00100000", _NOT_A_SYMBOL, id="neither call nor put"),
        pytest.param("AAPL240119C0010000", _NOT_A_SYMBOL, id="seven strike digits"),
        pytest.param("AAPL", _NOT_A_SYMBOL, id="a stock symbol"),
        pytest.param("AAPL24011\u0669C00100000", _NOT_A_SYMBOL, id="an Arabic-Indic digit in the date"),
        pytest.param("AAPL240119C0010000\uff10", _NOT_A_SYMBOL, id="a full-width digit in the strike"),
        pytest.param("AAPL241319C00100000", "241319 is not a date", id="a month that does not exist"),
        pytest.param("AAPL240119C00000000", "strike_thousandths", id="a zero strike"),
    ],
)
def test_text_that_is_not_a_contract_symbol_is_refused(symbol: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        OptionContract.from_occ_symbol(symbol)
